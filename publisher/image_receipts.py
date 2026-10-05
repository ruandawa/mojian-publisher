"""Compare a published picture with the immutable local publication plan.

This is conservative visual evidence, not a cryptographic identity test of the
platform's re-encoded JPEG. The caller must enforce article image order/count,
fetch only approved public CDN URLs and authorize ``allow_watermark`` only for
an official image URL explicitly carrying ``watermark=1``. No network access
or publication action takes place here. Unrecognized transformations fail.
"""
from __future__ import annotations

import hashlib
import io
import re
from pathlib import Path

from bs4 import BeautifulSoup
from PIL import Image, ImageChops, ImageFilter, ImageOps


_ASSET = re.compile(r'/assets/[0-9a-f]{32}\.jpg')
_SHA = re.compile(r'[0-9a-f]{64}')
_MAX_BYTES = 20 * 1024 * 1024
_MAX_PIXELS = 6_000_000  # Local preparation already limits both sides to 2400.


def frozen_body_images(snapshot: dict, assets_dir: Path) -> list[dict]:
    """Return ``src``, frozen ``sha256`` and absolute ``Path`` in body order.

    Repeated uses of the same picture remain repeated; a cover-only picture is
    not added to the body. All manifest entries, including the cover, are
    checked for immutable bytes and confinement to the supplied asset folder.
    Invalid or missing preparation raises ValueError, never a partial list.
    """
    if not isinstance(snapshot, dict):
        raise ValueError('任务缺少冻结的图文排版。')
    plan = snapshot.get('_prepared')
    if not isinstance(plan, dict) or not isinstance(plan.get('html'), str):
        raise ValueError('任务缺少冻结的图文排版，无法核对公开文章图片。')
    markup = plan['html']
    if hashlib.sha256(markup.encode()).hexdigest() != plan.get('html_sha256'):
        raise ValueError('任务保存的排版内容校验失败，无法核对公开文章图片。')
    manifest = plan.get('images')
    if not isinstance(manifest, list):
        raise ValueError('任务图片清单无效。')
    assets = Path(assets_dir).resolve()
    by_src = {}
    for item in manifest:
        if not isinstance(item, dict):
            raise ValueError('任务图片清单无效。')
        src, sha = item.get('src'), item.get('sha256')
        if (not isinstance(src, str) or not _ASSET.fullmatch(src)
                or not isinstance(sha, str) or not _SHA.fullmatch(sha)
                or src in by_src):
            raise ValueError('任务图片清单无效或包含重复条目。')
        path = (assets / src.rsplit('/', 1)[-1]).resolve()
        try:
            # Windows packaged-app filesystem virtualization can resolve a
            # child through LocalCache while its directory retains the normal
            # AppData spelling. Require actual directory identity, not just a
            # textual match; a symlink into a different directory still fails.
            confined = path.parent == assets or path.parent.samefile(assets)
            valid = (confined and path.is_file()
                     and hashlib.sha256(path.read_bytes()).hexdigest() == sha)
        except OSError:
            valid = False
        if not valid:
            raise ValueError('排期后本地图片已变化或丢失，无法核对公开文章。')
        by_src[src] = {'src': src, 'sha256': sha, 'path': path}
    refs = []
    for image in BeautifulSoup(markup, 'html.parser').find_all('img'):
        src = image.get('src')
        if src not in by_src:
            raise ValueError('正文图片未包含在冻结的本地图片清单中。')
        refs.append(dict(by_src[src]))
    count = plan.get('body_image_count')
    if type(count) is not int or count != len(refs):
        raise ValueError('冻结正文图片数量不一致。')
    return refs


def _decode(raw: bytes) -> Image.Image:
    if not isinstance(raw, (bytes, bytearray)) or not raw or len(raw) > _MAX_BYTES:
        raise ValueError('Invalid image bytes')
    with Image.open(io.BytesIO(raw)) as image:
        width, height = image.size
        if width < 1 or height < 1 or width * height > _MAX_PIXELS:
            raise ValueError('Image dimensions exceed comparison budget')
        if getattr(image, 'n_frames', 1) != 1:
            raise ValueError('Animated images cannot be compared as a still')
        image.load()
        return ImageOps.exif_transpose(image).convert('RGB')


def _stats(image: Image.Image, mask: Image.Image | None = None):
    histogram = image.histogram(mask)
    count = sum(histogram)
    if not count:
        return None
    return histogram, count, sum(value * amount for value, amount in enumerate(histogram)) / count


def _fraction(stats, threshold):
    histogram, count, _ = stats
    return sum(histogram[threshold + 1:]) / count


def _quantile(stats, percentile):
    histogram, count, _ = stats
    total = 0
    for value, amount in enumerate(histogram):
        total += amount
        if total >= count * percentile:
            return value
    return 255


def image_content_matches(original_bytes: bytes, public_bytes: bytes, *, allow_watermark=False) -> bool:
    """Allow proportional JPEG re-encoding and a tightly bounded watermark.

    Pixels are compared in RGB after one-pixel smoothing, with whole-image,
    non-watermark high-percentile, spatial-block, sliding-detail and edge checks.
    Only the bottom-right 32% by 12% (3.84% of the picture), plus at most four
    pixels (one percent of height) of JPEG/blur boundary, receives separate
    limits. Even there changed area and magnitude are bounded. Uniform
    replacements, crops, swapped pictures and large edits fail. Downscaling below one quarter
    or to an unreadable thumbnail is deliberately not accepted. Thresholds may
    reject unusually severe platform transforms; failure only postpones receipt.
    """
    try:
        original, public = _decode(original_bytes), _decode(public_bytes)
        ow, oh = original.size
        width, height = public.size
        # At most a one-pixel rounding error in a proportional resize.
        if abs(width * oh - height * ow) > max(ow, oh):
            return False
        scale = min(width / ow, height / oh)
        if scale < .25 or max(width, height) < min(128, max(ow, oh)):
            return False
        original = original.resize(public.size, Image.Resampling.LANCZOS)
        original = original.filter(ImageFilter.GaussianBlur(1))
        public = public.filter(ImageFilter.GaussianBlur(1))
        red, green, blue = ImageChops.difference(original, public).split()
        difference = ImageChops.lighter(ImageChops.lighter(red, green), blue)
        whole = _stats(difference)
        if whole[2] > 4 or _fraction(whole, 35) > .012 or _fraction(whole, 80) > .001:
            return False
        mask = Image.new('L', public.size, 255)
        if allow_watermark:
            left = int(width * .68)
            top = max(0, int(height * .88) - min(4, int(height * .01)))
            # This region is bounded rather than discarded from the comparison.
            roi = _stats(difference.crop((left, top, width, height)))
            if (roi[2] > 16 or _fraction(roi, 8) > .30
                    or _fraction(roi, 35) > .22 or _fraction(roi, 80) > .015):
                return False
            mask.paste(0, (left, top, width, height))
        outside = _stats(difference, mask)
        if (outside[2] > 4 or _quantile(outside, .95) > 10
                or _quantile(outside, .99) > 24 or _fraction(outside, 35) > .003
                or _fraction(outside, 65) > .0005):
            return False
        # Sliding 7x7 neighborhoods detect a changed small digit even when it
        # falls on a block boundary; unmasked watermark pixels never contribute.
        detail = ImageChops.multiply(difference, mask).filter(ImageFilter.BoxBlur(3))
        if detail.getextrema()[1] > 18:
            return False
        # A small changed word/subject must not hide in the global average.
        for top in range(0, height, 32):
            for left in range(0, width, 32):
                box = (left, top, min(left + 32, width), min(top + 32, height))
                tile = _stats(difference.crop(box), mask.crop(box))
                if tile and (tile[2] > 9 or _fraction(tile, 35) > .12):
                    return False
        edge_a = original.convert('L').filter(ImageFilter.FIND_EDGES)
        edge_b = public.convert('L').filter(ImageFilter.FIND_EDGES)
        edges = _stats(ImageChops.difference(edge_a, edge_b), mask)
        return edges[2] <= 3 and _fraction(edges, 25) <= .02
    except (OSError, ValueError, TypeError, Image.DecompressionBombError):
        return False
