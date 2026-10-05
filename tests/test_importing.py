import base64
import io
import socket
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import httpx
from bs4 import BeautifulSoup
from PIL import Image

from publisher import importing


def image_bytes(color='blue'):
    buffer = io.BytesIO()
    Image.new('RGB', (24, 16), color).save(buffer, 'PNG')
    return buffer.getvalue()


def zip_bytes(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


class ImportingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.assets = self.root / 'assets'
        self.image = image_bytes()

    def tearDown(self):
        self.tmp.cleanup()

    def images(self, result):
        return [tag['src'] for tag in BeautifulSoup(result['body'], 'html.parser').find_all('img')]

    def test_zip_relative_and_reference_images_resolve_from_article_directory(self):
        article = '''---
title: "图文文章"
author: 编辑
---
## 小标题
![第一张](../images/a%20b.png)

![再次出现][same]

[same]: ../images/a%20b.png
'''.encode('utf-8')
        raw = zip_bytes({'docs/article.md': article, 'images/a b.png': self.image})
        result = importing.import_document(raw, '图文包.zip', self.assets)
        self.assertTrue(result['ready'])
        self.assertEqual(result['title'], '图文文章')
        self.assertEqual(result['format'], 'html')
        self.assertEqual(result['image_count'], 2)
        self.assertEqual(result['local_image_count'], 1)
        self.assertEqual(len(list(self.assets.glob('*.jpg'))), 1)
        self.assertEqual(self.images(result)[0], self.images(result)[1])
        self.assertEqual(result['cover'], self.images(result)[0])
        self.assertEqual(Path(result['source_file']).read_bytes(), article)
        self.assertEqual(Path(result['source_package']).read_bytes(), raw)
        self.assertIn('<h2>', result['body'])

    def test_same_content_from_distinct_sources_is_stored_once(self):
        body = '![甲](https://example.com/a.png)\n\n![乙](https://example.com/b.png)\n\n![甲重复](https://example.com/a.png)'
        with patch.object(importing, 'download_image', return_value=self.image) as download:
            result = importing.materialize(body, 'markdown', self.assets)
        self.assertTrue(result['ready'])
        self.assertEqual(download.call_count, 2)
        self.assertEqual(result['image_count'], 3)
        self.assertEqual(result['local_image_count'], 1)
        self.assertEqual(len(set(self.images(result))), 1)
        self.assertEqual(len(list(self.assets.glob('*.jpg'))), 1)

    def test_full_html_uses_article_and_lazy_image_source(self):
        raw = '''<html><head><title>文章</title></head><body>
<nav>站点菜单</nav><article><h1>正文标题</h1><p><strong>重点</strong>内容</p>
<img src="loading.gif" data-lazy-src="//example.com/photo.png" alt="配图">
<script>unsafe()</script><aside>广告</aside></article><footer>页脚</footer></body></html>'''.encode('utf-8')
        with patch.object(importing, 'download_image', return_value=self.image) as download:
            result = importing.import_document(raw, '网页.html', self.assets)
        download.assert_called_once_with('https://example.com/photo.png', '')
        self.assertEqual(result['title'], '正文标题')
        self.assertTrue(result['ready'])
        self.assertIn('<strong>重点</strong>', result['body'])
        for unwanted in ['站点菜单', '广告', '页脚', 'unsafe', 'data-lazy-src', 'loading.gif']:
            self.assertNotIn(unwanted, result['body'])

    def test_embedded_image_is_decoded_and_reencoded_locally(self):
        src = 'data:image/png;base64,' + base64.b64encode(self.image).decode('ascii')
        result = importing.materialize(f'<p>内容</p><img src="{src}">', 'html', self.assets)
        self.assertTrue(result['ready'])
        self.assertNotIn('base64', result['body'])
        path = self.assets / Path(self.images(result)[0]).name
        with Image.open(path) as image:
            self.assertEqual(image.format, 'JPEG')
            self.assertEqual(image.size, (24, 16))

    def test_large_embedded_image_does_not_count_as_long_article_text(self):
        pixels = Image.effect_noise((350, 350), 64).convert('RGB')
        buffer = io.BytesIO()
        pixels.save(buffer, 'PNG')
        src = 'data:image/png;base64,' + base64.b64encode(buffer.getvalue()).decode('ascii')
        self.assertGreater(len(src), 50000)
        result = importing.materialize(f'<p>正文</p><img src="{src}">', 'html', self.assets)
        self.assertTrue(result['ready'])
        self.assertLess(len(result['body']), 50000)
        with patch.object(importing, 'download_image') as download, self.assertRaisesRegex(ValueError, '正文过长'):
            importing.materialize('<p>' + '字' * 50001 + '</p>', 'html', self.assets)
        download.assert_not_called()

    def test_missing_images_are_retained_with_actionable_failure(self):
        with patch.object(importing, 'download_image', side_effect=ValueError('下载失败')):
            result = importing.materialize('![外链](https://example.com/missing.png)\n\n![相对](photos/x.png)', 'markdown', self.assets)
        self.assertFalse(result['ready'])
        self.assertEqual(result['image_count'], 2)
        self.assertEqual(result['local_image_count'], 0)
        self.assertEqual(len(result['image_warnings']), 2)
        self.assertEqual(self.images(result), ['https://example.com/missing.png', 'photos/x.png'])
        self.assertIn('ZIP', result['image_warnings'][1])

    def test_existing_local_images_are_reused_and_missing_local_images_fail(self):
        self.assets.mkdir()
        name = 'a' * 32 + '.jpg'
        (self.assets / name).write_bytes(self.image)
        missing = 'b' * 32 + '.jpg'
        result = importing.materialize(f'<p>正文</p><img src="/assets/{name}"><img src="/assets/{missing}">', 'html', self.assets)
        self.assertFalse(result['ready'])
        self.assertEqual(result['local_image_count'], 1)
        self.assertEqual(len(list(self.assets.iterdir())), 1)
        self.assertIn('不存在', result['image_warnings'][0])

    def test_explicit_cover_is_localized_and_does_not_replace_body_images(self):
        raw = zip_bytes({'article.md': '---\ncover: images/cover.png\n---\n# 文章\n\n![正文](images/body.png)',
                         'images/cover.png': image_bytes('red'), 'images/body.png': self.image})
        result = importing.import_document(raw, '包.zip', self.assets)
        self.assertTrue(result['ready'])
        self.assertNotEqual(result['cover'], self.images(result)[0])
        self.assertEqual(result['image_count'], 1)
        self.assertEqual(result['local_image_count'], 1)
        self.assertEqual(len(list(self.assets.glob('*.jpg'))), 2)

    def test_broken_explicit_cover_blocks_readiness(self):
        raw = b'---\ncover: missing.png\n---\n# Article\n\nBody'
        result = importing.import_document(raw, 'article.md', self.assets)
        self.assertFalse(result['ready'])
        self.assertEqual(result['cover'], 'missing.png')
        self.assertTrue(result['image_warnings'][0].startswith('封面'))

    def test_source_backup_preserves_bom_and_newlines_exactly(self):
        raw = b'\xef\xbb\xbf# Original\r\n\r\nBody\r\n'
        result = importing.import_document(raw, 'original.md', self.assets)
        self.assertEqual(Path(result['source_file']).read_bytes(), raw)
        self.assertEqual(result['title'], 'Original')

    def test_zip_cannot_escape_root_in_entry_or_image_reference(self):
        raw = zip_bytes({'../outside.png': self.image, 'article.md': '# Title\nBody'})
        with self.assertRaisesRegex(ValueError, '非法路径'):
            importing.import_document(raw, '包.zip', self.assets)
        raw = zip_bytes({'docs/article.md': '# Title\n![非法](../../outside.png)', 'images/body.png': self.image})
        result = importing.import_document(raw, '包.zip', self.assets)
        self.assertFalse(result['ready'])
        self.assertIn('不在导入包内', result['image_warnings'][0])

    def test_invalid_utf8_in_plain_or_zip_article_is_user_readable(self):
        for name, raw in [('article.md', b'\xff\xfe'), ('包.zip', zip_bytes({'article.md': b'\xff\xfe'}))]:
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'UTF-8'):
                importing.import_document(raw, name, self.assets)

    def test_zip_requires_one_article_and_rejects_case_collisions(self):
        with self.assertRaisesRegex(ValueError, '一篇'):
            importing.import_document(zip_bytes({'a.md': '# A', 'b.md': '# B'}), '包.zip', self.assets)
        with self.assertRaisesRegex(ValueError, '重复文件'):
            importing.import_document(zip_bytes({'article.md': '# A', 'images/A.png': self.image, 'images/a.png': self.image}), '包.zip', self.assets)
        with self.assertRaisesRegex(ValueError, '有效的 ZIP'):
            importing.import_document(b'not a zip', '包.zip', self.assets)

    def test_size_and_image_count_limits_fail_before_processing(self):
        with patch.object(importing, 'MAX_IMPORT', 3), self.assertRaisesRegex(ValueError, '60 MB'):
            importing.import_document(b'# Article', 'article.md', self.assets)
        with patch.object(importing, 'MAX_EXPANDED', 3), self.assertRaisesRegex(ValueError, '100 MB'):
            importing.import_document(zip_bytes({'article.md': '# Article'}), '包.zip', self.assets)
        body = '\n'.join(f'![{n}](https://example.com/{n}.png)' for n in range(31))
        with patch.object(importing, 'download_image') as download, self.assertRaisesRegex(ValueError, '30 张'):
            importing.materialize(body, 'markdown', self.assets)
        download.assert_not_called()

    def test_unsafe_markup_is_removed_without_damaging_text(self):
        result = importing.materialize('<p onclick="evil()">正常<strong>重点</strong></p><script>evil()</script><a href="javascript:evil()">链接</a>', 'html', self.assets)
        self.assertTrue(result['ready'])
        self.assertIn('正常<strong>重点</strong>', result['body'])
        self.assertNotIn('evil', result['body'])


class DownloadBoundaryTests(unittest.TestCase):
    def public_addresses(self, host, port, **kwargs):
        address = '127.0.0.1' if host == 'localhost' else '10.0.0.7' if host == 'internal.example' else '8.8.8.8'
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, port))]

    def client_patch(self, handler):
        real_client = httpx.Client
        return patch.object(importing.httpx, 'Client', side_effect=lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))

    def test_redirect_to_private_host_is_blocked_before_request(self):
        requests = []
        def handler(request):
            requests.append(str(request.url))
            return httpx.Response(302, headers={'location': 'http://internal.example/photo.png'})
        with patch.object(importing.socket, 'getaddrinfo', side_effect=self.public_addresses), self.client_patch(handler):
            with self.assertRaisesRegex(ValueError, '内网'):
                importing.download_image('https://example.com/photo.png')
        self.assertEqual(requests, ['https://example.com/photo.png'])

    def test_private_or_credentialed_initial_url_never_requests(self):
        with patch.object(importing.socket, 'getaddrinfo', side_effect=self.public_addresses), self.client_patch(lambda request: self.fail('不应联网')):
            for url in ['http://localhost/x', 'http://internal.example/x', 'https://name:secret@example.com/x', 'file:///x']:
                with self.subTest(url=url), self.assertRaises(ValueError):
                    importing.download_image(url)

    def test_declared_and_streamed_size_limits_both_apply(self):
        for response in [httpx.Response(200, headers={'content-length': '20'}, content=b'abc'), httpx.Response(200, content=b'x' * 20)]:
            with self.subTest(headers=response.headers), patch.object(importing, 'MAX_IMAGE', 10), patch.object(importing.socket, 'getaddrinfo', side_effect=self.public_addresses), self.client_patch(lambda request: response):
                with self.assertRaisesRegex(ValueError, '10 MB'):
                    importing.download_image('https://example.com/photo.png')

    def test_download_passes_source_referer_and_accepts_relative_redirect(self):
        seen = []
        def handler(request):
            seen.append(request)
            if request.url.path == '/old.png':
                return httpx.Response(302, headers={'location': '/new.png'})
            return httpx.Response(200, content=b'image data')
        with patch.object(importing.socket, 'getaddrinfo', side_effect=self.public_addresses), self.client_patch(handler):
            result = importing.download_image('https://example.com/old.png', 'https://example.com/article')
        self.assertEqual(result, b'image data')
        self.assertEqual([request.url.path for request in seen], ['/old.png', '/new.png'])
        self.assertTrue(all(request.headers['referer'] == 'https://example.com/article' for request in seen))


if __name__ == '__main__':
    unittest.main()
