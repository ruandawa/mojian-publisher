"""Publication reads may not follow image redirects outside WeChat's CDN."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from publisher.browser import BrowserDriver
from publisher.store import Store
from publisher.preparation import prepare_snapshot


class ReceiptMediaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.driver = BrowserDriver(Store(Path(self.temp.name)))
        self.context = Mock(request=Mock(get=AsyncMock()))

    def tearDown(self):
        self.temp.cleanup()

    async def test_untrusted_or_credentialed_image_cannot_make_request(self):
        for url in ('http://mmbiz.qpic.cn/image', 'https://127.0.0.1/image',
                    'https://mmbiz.qpic.cn.evil.test/image', 'https://user@mmbiz.qpic.cn/image',
                    'https://mmbiz.qpic.cn:8080/image'):
            with self.assertRaises(ValueError):
                await self.driver._public_image_bytes(self.context,url)
        self.context.request.get.assert_not_awaited()

    async def test_redirect_to_localhost_is_not_followed(self):
        response = Mock(status=302,headers={'location':'http://127.0.0.1/private'},dispose=AsyncMock())
        self.context.request.get.return_value = response
        with self.assertRaises(ValueError):
            await self.driver._public_image_bytes(self.context,'https://mmbiz.qpic.cn/image')
        self.context.request.get.assert_awaited_once()
        response.dispose.assert_awaited_once()

    async def test_oversized_response_is_released_without_body_read(self):
        response = Mock(status=200,ok=True,
                        headers={'content-type':'image/jpeg','content-length':str(11*1024*1024)},
                        body=AsyncMock(),dispose=AsyncMock())
        self.context.request.get.return_value = response
        with self.assertRaises(ValueError):
            await self.driver._public_image_bytes(self.context,'https://mmbiz.qpic.cn/image')
        response.body.assert_not_awaited()
        response.dispose.assert_awaited_once()

    async def test_valid_image_bytes_are_read_and_response_released(self):
        response = Mock(status=200,ok=True,headers={'content-type':'image/jpeg'},
                        body=AsyncMock(return_value=b'image-fixture'),dispose=AsyncMock())
        self.context.request.get.return_value = response
        self.assertEqual(await self.driver._public_image_bytes(self.context,'https://mmbiz.qpic.cn/image'),b'image-fixture')
        self.assertEqual(self.context.request.get.await_args.kwargs['max_redirects'],0)
        response.dispose.assert_awaited_once()

    async def test_frozen_image_changed_during_download_cannot_confirm(self):
        name='a'*32+'.jpg'
        path=self.driver.store.root/'assets'/name
        path.write_bytes(b'original-frozen-bytes')
        article=self.driver.store.save_article({'title':'图文','body':f'![图片](/assets/{name})'})
        prepared=prepare_snapshot(article,self.driver.store.root/'assets')
        job=self.driver.store.enqueue(article['id'],'publish','2099-01-01T00:00:00+00:00',prepared=prepared)
        receipt={'job_id':job['id'],'images':['mmbiz.qpic.cn/mmbiz_jpg/original/640']}
        async def download(context,url):
            path.write_bytes(b'changed-after-validation')
            return b'public-bytes'
        self.driver._public_image_bytes=AsyncMock(side_effect=download)
        body='<img data-src="https://mmbiz.qpic.cn/mmbiz_jpg/processed/640?watermark=1">'
        with patch('publisher.receipts.image_content_matches',return_value=True) as compare:
            self.assertFalse(await self.driver._public_images_match(self.context,body,receipt))
            compare.assert_not_called()


if __name__ == '__main__':
    unittest.main()
