"""Local article preparation. Remote images are materialized before publishing."""
from __future__ import annotations

import base64
import hashlib
import io
import ipaddress
import re
import socket
import tempfile
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urljoin, urlsplit

import bleach
import httpx
from bs4 import BeautifulSoup
from markdown_it import MarkdownIt

from .rendering import TAGS, save_image, validate_body

MAX_IMAGE = 10 * 1024 * 1024
MAX_IMPORT = 60 * 1024 * 1024
MAX_EXPANDED = 100 * 1024 * 1024
MAX_IMAGES = 30
LOCAL_IMAGE = re.compile(r'^/assets/([0-9a-f]{32}\.jpg)$')


def _public_url(url):
    parts = urlsplit(url)
    if parts.scheme not in {'http', 'https'} or not parts.hostname or parts.username or parts.password:
        raise ValueError('仅支持无登录凭据的 HTTP(S) 公开图片地址。')
    host = parts.hostname.rstrip('.').lower()
    if host == 'localhost' or host.endswith(('.local', '.internal')):
        raise ValueError('不能下载本机或内网地址。')
    try:
        addresses = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == 'https' else 80), type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError('图片域名无法解析。') from exc
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError('不能下载本机或内网地址。')
    return url


def download_image(url, referer=''):
    """Bounded download; validate every redirect before making its request."""
    headers = {'User-Agent': 'Mozilla/5.0 MojianPublisher/1.2', 'Accept': 'image/*'}
    if referer and urlsplit(referer).scheme in {'https', 'http'}:
        headers['Referer'] = referer
    with httpx.Client(timeout=httpx.Timeout(15, connect=8), follow_redirects=False) as client:
        current = url
        for _ in range(5):
            _public_url(current)
            with client.stream('GET', current, headers=headers) as response:
                if response.is_redirect:
                    location = response.headers.get('location')
                    if not location:
                        raise ValueError('图片重定向没有目标地址。')
                    current = urljoin(current, location)
                    continue
                response.raise_for_status()
                size = response.headers.get('content-length', '')
                if size.isdigit() and int(size) > MAX_IMAGE:
                    raise ValueError('图片超过 10 MB。')
                raw = bytearray()
                for chunk in response.iter_bytes(65536):
                    raw.extend(chunk)
                    if len(raw) > MAX_IMAGE:
                        raise ValueError('图片超过 10 MB。')
                return bytes(raw)
    raise ValueError('图片重定向次数过多。')


def _creation_source_metadata(meta):
    source = meta.get('creation_source', 'unspecified').strip().casefold()
    if source not in {'unspecified','ai','non_ai'}:
        raise ValueError('creation_source 仅支持 unspecified、ai 或 non_ai。')
    if 'ai_generated' not in meta:
        return source
    flag = meta['ai_generated'].strip().casefold()
    if flag not in {'true','false'}:
        raise ValueError('ai_generated 请明确填写 true 或 false。')
    declared = 'ai' if flag == 'true' else 'non_ai'
    if source != 'unspecified' and source != declared:
        raise ValueError('creation_source 与 ai_generated 的创作来源声明不一致。')
    return declared


def parse_frontmatter(text, fallback_title, suffix):
    meta = {}
    text = text.lstrip('\ufeff')
    match = re.match(r'^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)', text)
    if match:
        for line in match[1].splitlines():
            item = re.match(r'^([A-Za-z][\w-]*):\s*(.*)$', line)
            if item:
                value = item[2].strip()
                if len(value) > 1 and value[0] == value[-1] and value[0] in {'"', "'"}:
                    value = value[1:-1]
                meta[item[1]] = value
        text = text[match.end():]
    fmt = 'html' if suffix in {'.html', '.htm'} or meta.get('format') == 'html' else 'markdown'
    if fmt == 'html':
        soup = BeautifulSoup(text, 'html.parser')
        title = meta.get('title') or (soup.find('h1') or soup.find('title'))
        title = title.get_text(' ', strip=True) if hasattr(title, 'get_text') else title
        # Remove page chrome only for a full web document; fragments stay intact.
        if soup.find('html') or soup.find('body'):
            articles = soup.find_all('article')
            root = articles[0] if len(articles) == 1 else soup.find('main') or soup.find('body') or soup
            for node in root.find_all(['nav', 'header', 'footer', 'aside']):
                node.decompose()
            text = str(root)
    else:
        heading = re.search(r'^#\s+(.+)$', text, re.M)
        title = meta.get('title') or (heading[1] if heading else '')
    return {'title': str(title or fallback_title).strip()[:64], 'author': meta.get('author', '')[:8],
            'digest': meta.get('digest', '')[:120], 'body': text.strip(), 'format': fmt,
            'source': 'import', 'source_url': meta.get('source_url', ''), 'cover': meta.get('cover', ''),
            'creation_source': _creation_source_metadata(meta)}


def materialize(body, fmt, assets, *, base_dir=None, package_root=None, base_url='', cover=''):
    """Parse once, localize inline/reference images and return sanitized HTML."""
    # Embedded image bytes are not article text. Apply the text limit to a
    # sanitized skeleton before download, then to the finalized local HTML.
    if not body.strip():
        raise ValueError('请先填写正文。')
    validate_body('正文', fmt)
    if len(body.encode('utf-8')) > MAX_IMPORT:
        raise ValueError('导入内容不能超过 60 MB。')
    source = MarkdownIt('commonmark', {'html': False, 'breaks': True}).enable('table').render(body) if fmt == 'markdown' else body
    soup = BeautifulSoup(source, 'html.parser')
    for node in soup.find_all(['script','style','iframe','object','embed','form','input','button','svg','math']):
        node.decompose()
    for tag in soup.find_all('img'):
        # Lazy-loaded HTML exports often keep the real source in data-src.
        source = (tag.get('data-src') or tag.get('data-original') or
                  tag.get('data-lazy-src') or tag.get('data-url') or tag.get('src') or '')
        if source.startswith('//'):
            source = urljoin(base_url or 'https://example.invalid', source)
        tag['src'] = source.strip()
    images = soup.find_all('img')
    body_sources = {tag['src'] for tag in images}
    if len(images) > MAX_IMAGES:
        raise ValueError('单篇文章最多支持 30 张正文图片。')
    skeleton = BeautifulSoup(str(soup), 'html.parser')
    for tag in skeleton.find_all('img'):
        tag.attrs = {'src': '/assets/' + '0' * 32 + '.jpg', 'alt': tag.get('alt', '')}
    checked = bleach.clean(str(skeleton), tags=TAGS, attributes={'a':['href','title'], 'img':['src','alt']},
                           protocols=['http','https'], strip=True)
    validate_body(checked, 'html')
    cover_tag = None
    if cover:
        cover_tag = soup.new_tag('img', src=cover.strip())
        if cover_tag['src'].startswith('//'):
            cover_tag['src'] = urljoin(base_url or 'https://example.invalid', cover_tag['src'])
    all_images = images + ([cover_tag] if cover_tag is not None else [])
    sources = list(dict.fromkeys(tag['src'] for tag in all_images))
    assets.mkdir(parents=True, exist_ok=True)
    cached_images = {}
    image_lock = threading.Lock()

    def resolve(source):
        try:
            match = LOCAL_IMAGE.fullmatch(source)
            if match:
                if not (assets / match[1]).is_file():
                    raise ValueError('本地资源已不存在。')
                return source, ''
            if source.startswith('data:image/'):
                if ';base64,' not in source or len(source) > MAX_IMAGE * 2:
                    raise ValueError('嵌入图片格式无效或过大。')
                raw = base64.b64decode(source.split(',', 1)[1], validate=True)
            else:
                parts = urlsplit(source)
                if parts.scheme in {'https','http'}:
                    raw = download_image(source, base_url)
                elif parts.scheme:
                    raise ValueError('图片地址格式不支持。')
                elif base_dir is not None:
                    candidate = (Path(base_dir) / unquote(parts.path).replace('\\', '/')).resolve()
                    allowed_root = Path(package_root or base_dir).resolve()
                    if not candidate.is_relative_to(allowed_root) or not candidate.is_file():
                        raise ValueError('图片文件不在导入包内。')
                    if candidate.stat().st_size > MAX_IMAGE:
                        raise ValueError('图片超过 10 MB。')
                    raw = candidate.read_bytes()
                elif base_url:
                    raw = download_image(urljoin(base_url, source), base_url)
                else:
                    raise ValueError('相对图片需要把文章和图片一起打包成 ZIP 导入。')
            content_hash = hashlib.sha256(raw).digest()
            with image_lock:
                if content_hash not in cached_images:
                    cached_images[content_hash] = save_image(raw, assets)
                return cached_images[content_hash], ''
        except (OSError, ValueError, httpx.HTTPError, httpx.InvalidURL) as exc:
            # Keep the failed reference visible to the local preflight.
            return source, str(exc)

    with ThreadPoolExecutor(max_workers=4) as pool:
        resolved = dict(zip(sources, pool.map(resolve, sources)))
    warnings = []
    for index, tag in enumerate(all_images, 1):
        new_src, error = resolved[tag['src']]
        tag['src'] = new_src
        if error:
            label = '封面' if tag is cover_tag else f'图片 {index}'
            warnings.append(f'{label}：{error}')
    for fig in soup.find_all(['figure', 'figcaption']):
        fig.name = 'div' if fig.name == 'figure' else 'p'
    clean = bleach.clean(str(soup), tags=TAGS, attributes={'a':['href','title'], 'img':['src','alt']},
                         protocols=['http','https'], strip=True)
    validate_body(clean, 'html')
    result = {'body': clean.strip(), 'format': 'html', 'image_warnings': warnings,
            'image_count': len(images),
            'local_image_count': len({resolved[src][0] for src in body_sources
                                      if not resolved[src][1] and LOCAL_IMAGE.fullmatch(resolved[src][0])}),
            'ready': not warnings}
    if cover_tag is not None:
        result['cover'] = cover_tag['src']
    return result


def import_document(raw, filename, assets, theme='jade'):
    if len(raw) > MAX_IMPORT:
        raise ValueError('导入文件不能超过 60 MB。')
    filename = filename or '导入文章.md'
    suffix = Path(filename).suffix.lower()
    if suffix not in {'.zip', '.md', '.markdown', '.txt', '.html', '.htm'}:
        raise ValueError('请选择 MD、TXT、HTML 或 ZIP 图文包。')
    with tempfile.TemporaryDirectory(prefix='mojian-import-') as temp:
        base_dir = None
        package_root = None
        if suffix == '.zip':
            base_dir = Path(temp)
            package_root = base_dir
            try:
                with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                    entries = archive.infolist()
                    if len(entries) > 200 or sum(x.file_size for x in entries) > MAX_EXPANDED:
                        raise ValueError('导入包过大，请控制在 200 个文件、解压后 100 MB 内。')
                    seen = set()
                    for entry in entries:
                        parts = PurePosixPath(entry.filename.replace('\\','/'))
                        if parts.is_absolute() or '..' in parts.parts or ':' in entry.filename:
                            raise ValueError('导入包包含非法路径。')
                        target = base_dir.joinpath(*parts.parts).resolve()
                        if not target.is_relative_to(base_dir.resolve()):
                            raise ValueError('导入包包含非法路径。')
                        identity = str(target).casefold()
                        if identity in seen:
                            raise ValueError('导入包包含重复文件路径。')
                        seen.add(identity)
                        if entry.is_dir():
                            target.mkdir(parents=True, exist_ok=True)
                        else:
                            target.parent.mkdir(parents=True, exist_ok=True)
                            target.write_bytes(archive.read(entry))
                candidates = sorted(p for p in base_dir.rglob('*') if p.suffix.lower() in {'.md','.markdown','.html','.htm'} and p.is_file())
                if len(candidates) != 1:
                    raise ValueError('图文包内请保留一篇 MD 或 HTML 文章，另附图片目录。')
                document = candidates[0]
                original_text = document.read_bytes()
                text = original_text.decode('utf-8-sig')
                suffix = document.suffix.lower()
                filename = document.name
                base_dir = document.parent
            except UnicodeDecodeError as exc:
                raise ValueError('请使用 UTF-8 编码的文章。') from exc
            except zipfile.BadZipFile as exc:
                raise ValueError('导入文件不是有效的 ZIP。') from exc
            except RuntimeError as exc:
                raise ValueError('导入包无法读取，请使用未加密的 ZIP。') from exc
        else:
            try:
                original_text = raw
                text = raw.decode('utf-8-sig')
            except UnicodeDecodeError as exc:
                raise ValueError('请使用 UTF-8 编码的文章。') from exc
        parsed = parse_frontmatter(text, Path(filename).stem, suffix)
        # Preserve the source for later reformatting; only prepared HTML is edited.
        original_dir = assets.parent / 'imports'
        original_dir.mkdir(parents=True, exist_ok=True)
        source_file = original_dir / (hashlib.sha256(raw).hexdigest()[:24] + suffix)
        source_file.write_bytes(original_text)
        if package_root is not None:
            # Keep relative image files with their source so reformatting never
            # depends on the temporary extraction directory after import.
            source_package = original_dir / (hashlib.sha256(raw).hexdigest()[:24] + '.zip')
            source_package.write_bytes(raw)
        prepared = materialize(parsed['body'], parsed['format'], assets, base_dir=base_dir,
                               package_root=package_root, base_url=parsed['source_url'], cover=parsed['cover'])
        parsed.update(prepared, theme=theme)
        local = BeautifulSoup(parsed['body'], 'html.parser').find_all('img')
        if not parsed['cover']:
            parsed['cover'] = next((tag['src'] for tag in local if LOCAL_IMAGE.fullmatch(tag['src'])), '')
        parsed['source_file'] = str(source_file)
        if package_root is not None:
            parsed['source_package'] = str(source_package)
        return parsed
