"""One immutable publication plan shared by preview, queue and browser."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from bs4 import BeautifulSoup

from .rendering import image_warnings, publication_html


def preview_plan(body, fmt, theme, assets):
    markup = publication_html(body, fmt, theme)
    images = BeautifulSoup(markup, 'html.parser').find_all('img')
    warnings = image_warnings(body, fmt)
    warnings += sum(tag['src'].startswith('/assets/') and not (assets / tag['src'].rsplit('/',1)[-1]).is_file()
                    for tag in images)
    return {'html': markup, 'image_count': len(images), 'image_warnings': warnings, 'ready': not warnings}


def prepare_snapshot(article, assets: Path):
    plan = preview_plan(article['body'], article['format'], article['theme'], assets)
    if not plan['ready']:
        raise ValueError('正文图片尚未准备好或本地图片已丢失，请在编辑页整理图片后再发送。')
    images = BeautifulSoup(plan['html'], 'html.parser').find_all('img')
    refs = list(dict.fromkeys([article.get('cover','')] + [tag['src'] for tag in images]))
    manifest = []
    for ref in filter(None, refs):
        if not re.fullmatch(r'/assets/[0-9a-f]{32}\.jpg', ref):
            raise ValueError('所有图片需要先保存到本地，请在编辑页整理并下载图片。')
        path = assets / ref.rsplit('/',1)[-1]
        if not path.is_file():
            raise ValueError('文章对应的本地图片不存在，请重新导入或上传。')
        manifest.append({'src': ref, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    return {'html': plan['html'], 'html_sha256': hashlib.sha256(plan['html'].encode()).hexdigest(),
            'images': manifest, 'body_image_count': len(images), 'layout_version': 1}


def verify_snapshot(plan, assets):
    if hashlib.sha256(plan.get('html','').encode()).hexdigest() != plan.get('html_sha256'):
        raise ValueError('任务保存的排版内容校验失败，请重新创建任务。')
    for image in plan.get('images',[]):
        ref = image.get('src','')
        if not re.fullmatch(r'/assets/[0-9a-f]{32}\.jpg', ref):
            raise ValueError('任务图片清单无效，请重新创建任务。')
        path = assets / ref.rsplit('/',1)[-1]
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != image.get('sha256'):
            raise ValueError('排期后图片已变化或丢失，已停止发送。请重新准备文章并创建任务。')
    return plan['html']
