from __future__ import annotations

import html
import io
import re
import uuid
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlsplit

import bleach
from bs4 import BeautifulSoup
from markdown_it import MarkdownIt
from PIL import Image, ImageDraw, ImageFont, ImageOps

THEMES = {
    'jade': {'name': '青绿手记', 'accent': '#24765d', 'soft': '#edf5ef', 'ink': '#22382e'},
    'ink': {'name': '深蓝观察', 'accent': '#345a82', 'soft': '#edf2f9', 'ink': '#23364b'},
    'warm': {'name': '暖橘随笔', 'accent': '#a36136', 'soft': '#faf1e7', 'ink': '#493b31'},
}
TAGS = {'p','br','h1','h2','h3','h4','strong','b','em','i','u','s','blockquote','ul','ol','li','hr','a','img','pre','code','table','thead','tbody','tr','th','td','section','span','div','sup','sub'}
BODY_FONT = 'font-family:Microsoft YaHei,PingFang SC,sans-serif;font-size:16px;letter-spacing:0.3px;color:#34443c;word-break:break-word;'


def validate_body(body, fmt):
    if not body.strip():
        raise ValueError('请先填写正文。')
    if len(body) > 50000:
        raise ValueError('正文过长，请控制在 5 万字符以内。')
    if fmt not in {'markdown', 'html'}:
        raise ValueError('仅支持 Markdown、纯文本或 HTML。')


def render(body: str, fmt='markdown', theme='jade') -> str:
    t = THEMES.get(theme, THEMES['jade'])
    source = MarkdownIt('commonmark', {'html': False, 'breaks': True}).enable('table').render(body) if fmt == 'markdown' else body
    soup = BeautifulSoup(source, 'html.parser')
    for bad in soup(['script','style','iframe','object','embed','form','input','button','svg','math']):
        bad.decompose()
    clean = bleach.clean(str(soup), tags=TAGS, attributes={'a':['href','title'], 'img':['src','alt']}, protocols=['http','https'], strip=True)
    soup = BeautifulSoup(clean, 'html.parser')
    styles = {
        'p': BODY_FONT + 'margin:0 0 20px;line-height:1.9;',
        'h1': BODY_FONT + f'font-size:24px;font-weight:700;line-height:1.5;margin:28px 0 18px;color:{t["ink"]};',
        'h2': BODY_FONT + f'font-size:20px;font-weight:700;line-height:1.6;margin:30px 0 18px;padding-left:12px;border-left:4px solid {t["accent"]};color:{t["ink"]};',
        'h3': BODY_FONT + f'font-size:18px;font-weight:700;line-height:1.7;margin:24px 0 14px;color:{t["accent"]};',
        'h4': BODY_FONT + f'font-size:16px;font-weight:700;line-height:1.7;margin:22px 0 12px;color:{t["ink"]};',
        'blockquote': f'margin:22px 0;padding:18px 20px;border-left:3px solid {t["accent"]};background:{t["soft"]};color:#607067;',
        'ul': 'padding-left:24px;margin:16px 0 22px;', 'ol': 'padding-left:24px;margin:16px 0 22px;',
        'li': BODY_FONT + 'margin:8px 0;line-height:1.9;', 'strong': f'font-weight:700;color:{t["accent"]};',
        'a': f'color:{t["accent"]};text-decoration:underline;word-break:break-all;',
        'img': 'display:block;max-width:100%;height:auto;margin:22px auto;border-radius:5px;',
        'pre': 'white-space:pre-wrap;word-break:break-word;background:#f3f5f4;padding:16px;font-size:13px;border-radius:5px;',
        'code': 'font-family:Consolas,monospace;font-size:14px;background:#f3f5f4;padding:2px 4px;',
        'hr': 'border:none;border-top:1px solid #dbe3de;margin:30px 0;',
        'table': 'border-collapse:collapse;width:100%;margin:20px 0;font-size:14px;',
        'th': f'border:1px solid #dbe3de;padding:10px;background:{t["soft"]};',
        'td': 'border:1px solid #dbe3de;padding:10px;',
    }
    for tag in soup.find_all(True):
        if tag.name in styles:
            tag['style'] = styles[tag.name]
        if tag.name == 'img':
            src = tag.get('src', '')
            parsed = urlsplit(src)
            if not re.fullmatch(r'/assets/[0-9a-f]{32}\.jpg', src) and not (parsed.scheme == 'https' and parsed.hostname in {'mmbiz.qpic.cn','mmbiz.qlogo.cn'}):
                # Never fetch third-party images or leak user IP via HTML previews.
                tag.replace_with(soup.new_tag('p'))
                continue
    # A meaningful image description is useful below a standalone body image.
    # Never turn a table icon or an inline illustration into an extra block.
    for image in list(soup.find_all('img')):
        caption_text = str(image.get('alt', '')).strip()
        if not caption_text or caption_text.lower() in {'image','photo','图片','图','img'}:
            continue
        if image.find_parent(['table','li','a','h1','h2','h3','h4']):
            continue
        block = image.parent if image.parent.name == 'p' else image
        if block.name == 'p' and (block.get_text(strip=True) or len(block.find_all('img')) != 1):
            continue
        existing_caption = block.find_next_sibling()
        if existing_caption and existing_caption.name == 'p' and existing_caption.get_text(strip=True) == caption_text:
            caption = existing_caption
        else:
            caption = soup.new_tag('p')
            caption.string = caption_text
            block.insert_after(caption)
        caption['style'] = BODY_FONT + 'font-size:12px;color:#7b8982;text-align:center;line-height:1.6;margin:-10px 0 24px;'
    return f'<section style="{BODY_FONT}line-height:1.9;">{soup}</section>'


def wechat_compatible_html(markup: str, theme='jade') -> str:
    """Compile semantic HTML into styled paragraphs accepted by WeChat.

    This is also the preview fragment.  Cards use ordinary paragraphs with
    inline styles so losing an unsupported container never loses its content.
    In particular, table cells keep their links, emphasis and images instead
    of being flattened to text.
    """
    soup = BeautifulSoup(markup, 'html.parser')
    t = THEMES.get(theme, THEMES['jade'])

    def append_style(tag, value):
        tag['style'] = str(tag.get('style', '')).rstrip(';') + ';' + value

    def inline_header(header):
        result = deepcopy(header)
        for block in list(result.find_all(['p','div','h1','h2','h3','h4','section','ul','ol','li'])):
            block.unwrap()
        return result

    def cell_blocks(cell):
        """Move cell children into valid blocks without nesting p inside p."""
        result, inline = [], []

        def flush():
            if inline:
                paragraph = soup.new_tag('p')
                paragraph['style'] = BODY_FONT + 'margin:0;line-height:1.8;'
                for child in inline:
                    paragraph.append(child)
                result.append(paragraph)
                inline.clear()

        for child in list(cell.contents):
            if getattr(child, 'name', None) in {'p','h1','h2','h3','h4','ul','ol','div','blockquote','pre','table'}:
                flush()
                result.append(child.extract())
            elif getattr(child, 'name', None) is None and not str(child).strip() and not inline:
                continue
            else:
                inline.append(child.extract())
        flush()
        if not result:
            paragraph = soup.new_tag('p')
            paragraph['style'] = BODY_FONT + 'margin:0;line-height:1.8;'
            result.append(paragraph)
        return result

    # Transform innermost tables first; nested table cards remain intact when
    # their parent cell is moved. A row becomes a vertical card on a phone.
    for table in reversed(list(soup.find_all('table'))):
        rows = [row for row in table.find_all('tr') if row.find_parent('table') is table]
        if not rows:
            table.unwrap()
            continue
        header_cells = rows[0].find_all(['th', 'td'], recursive=False)
        has_headers = any(cell.name == 'th' for cell in header_cells)
        headers = [deepcopy(cell) for cell in header_cells] if has_headers else []
        data_rows = rows[1:] if has_headers and len(rows) > 1 else rows
        container = soup.new_tag('div')
        data_cells = [row.find_all(['th', 'td'], recursive=False) for row in data_rows]
        # Metric-by-model matrices read better as one card per model on a
        # phone. Transpose only a rectangular table with clear metric labels;
        # editorial images in that label column would otherwise multiply.
        transpose = (len(headers) >= 3 and data_rows is not rows
                     and re.fullmatch(r'(指标|参数|功能|规格|特性)(\s*/\s*模型)?', headers[0].get_text(strip=True))
                     and all(len(cells) == len(headers) and cells[0].get_text(strip=True)
                             and not cells[0].find('img') for cells in data_cells))
        if transpose:
            legend = soup.new_tag('p')
            legend['style'] = BODY_FONT + 'font-size:12px;color:#7b8982;line-height:1.6;margin:0 0 8px;'
            for child in list(inline_header(headers[0]).contents):
                legend.append(child.extract())
            container.append(legend)
            for column in range(1, len(headers)):
                card = soup.new_tag('div')
                card['style'] = 'margin:18px 0;'
                heading = soup.new_tag('p')
                heading['style'] = BODY_FONT + f'font-weight:700;color:{t["ink"]};background:{t["soft"]};border-left:3px solid {t["accent"]};padding:16px 16px 8px;margin:0;line-height:1.6;'
                for child in list(inline_header(headers[column]).contents):
                    heading.append(child.extract())
                card.append(heading)
                for cells in data_cells:
                    label = soup.new_tag('p')
                    label['style'] = BODY_FONT + f'font-size:13px;font-weight:700;color:{t["accent"]};background:{t["soft"]};border-left:3px solid {t["accent"]};padding:8px 16px 2px;margin:0;line-height:1.6;'
                    for child in list(inline_header(cells[0]).contents):
                        label.append(child.extract())
                    card.append(label)
                    for block in cell_blocks(cells[column]):
                        append_style(block, f'background:{t["soft"]};border-left:3px solid {t["accent"]};padding:4px 16px 12px;margin:0;')
                        card.append(block)
                container.append(card)
            table.replace_with(container)
            continue
        key_value = (len(headers) == 2 and len(data_rows) > 0 and data_rows is not rows
                     and headers[0].get_text(strip=True) in {'项目','参数','指标','特性','功能','配置','字段','规格'})
        if key_value:
            legend = soup.new_tag('p')
            legend['style'] = BODY_FONT + 'font-size:12px;color:#7b8982;line-height:1.6;margin:0 0 8px;'
            for index, header in enumerate(headers):
                if index:
                    legend.append(soup.new_string(' / '))
                for child in list(inline_header(header).contents):
                    legend.append(child.extract())
            container.append(legend)
        for row_index, row in enumerate(data_rows):
            cells = row.find_all(['th','td'], recursive=False)
            if not cells:
                continue
            card = soup.new_tag('div')
            card['style'] = 'margin:18px 0;'
            if key_value and len(cells) == 2:
                for index, cell in enumerate(cells):
                    blocks = cell_blocks(cell)
                    for block in blocks:
                        append_style(block, f'background:{t["soft"]};border-left:3px solid {t["accent"]};padding:4px 16px 14px;margin:0;')
                        if index == 0:
                            append_style(block, f'font-size:14px;font-weight:700;color:{t["accent"]};padding:12px 16px 2px;')
                        card.append(block)
                container.append(card)
                continue
            for cell_index, cell in enumerate(cells):
                field = soup.new_tag('div')
                label = None
                if headers and data_rows is not rows:
                    label = soup.new_tag('p')
                    label['style'] = BODY_FONT + f'font-size:13px;font-weight:700;color:{t["accent"]};margin:0;padding:12px 16px 2px;background:{t["soft"]};border-left:3px solid {t["accent"]};line-height:1.6;'
                    if cell_index < len(headers):
                        header = inline_header(headers[cell_index])
                        # An image in a column heading occurs only once. Field
                        # names repeat, but editorial images do not multiply.
                        if row_index:
                            for image in header.find_all('img'):
                                image.replace_with(soup.new_string(image.get('alt', '')))
                        for child in list(header.contents):
                            label.append(child.extract())
                    else:
                        label.string = f'第 {cell_index + 1} 项'
                    field.append(label)
                blocks = cell_blocks(cell)
                for block_index, block in enumerate(blocks):
                    append_style(block, f'background:{t["soft"]};border-left:3px solid {t["accent"]};padding:4px 16px 12px;margin:0;')
                    if not label and block_index == 0:
                        append_style(block, 'padding-top:12px;')
                    field.append(block)
                card.append(field)
            container.append(card)
        table.replace_with(container)

    # A plain fragment is safer for the editor's root ProseMirror node.
    for section in list(soup.find_all('section')):
        section.unwrap()

    # Each quote paragraph carries its own background and border, even if the
    # editor unwraps the surrounding div. Lists and inline rich nodes survive.
    for quote in reversed(list(soup.find_all('blockquote'))):
        container = soup.new_tag('div')
        container['style'] = 'margin:22px 0;'
        blocks = cell_blocks(quote)
        for index, block in enumerate(blocks):
            append_style(block, f'border-left:3px solid {t["accent"]};background:{t["soft"]};padding:10px 18px;margin:0;')
            if index == 0:
                append_style(block, 'padding-top:16px;')
            if index == len(blocks) - 1:
                append_style(block, 'padding-bottom:16px;')
            container.append(block)
        quote.replace_with(container)

    # Keep code line breaks and leading indentation in an ordinary paragraph.
    # NBSP prevents HTML whitespace collapse at line starts after paste.
    for pre in list(soup.find_all('pre')):
        paragraph = soup.new_tag('p')
        paragraph['style'] = 'font-family:Consolas,Menlo,monospace;font-size:13px;line-height:1.7;white-space:pre-wrap;word-break:break-word;background:#f3f5f4;color:#34443c;padding:16px;margin:20px 0;border-radius:5px;' + str(pre.get('style', ''))
        lines = pre.get_text().split('\n')
        for index, line in enumerate(lines):
            if index:
                paragraph.append(soup.new_tag('br'))
            prefix = re.match(r'^[ \t]*', line).group()
            prefix = prefix.expandtabs(4).replace(' ', '\u00a0')
            paragraph.append(soup.new_string(prefix + line.lstrip(' \t')))
        pre.replace_with(paragraph)

    # A leading text node before the first block makes Chromium's
    # ``execCommand('insertHTML')`` report ``false`` even though it inserted
    # the fragment.  Trim only the fragment boundary; text inside blocks is
    # left untouched.
    return str(soup).strip()


def publication_html(body: str, fmt='markdown', theme='jade') -> str:
    """The shared formatted fragment for local preview and publication."""
    return wechat_compatible_html(render(body, fmt, theme), theme)


def local_images(body, fmt):
    soup = BeautifulSoup(render(body, fmt), 'html.parser')
    return list(dict.fromkeys(i['src'] for i in soup.find_all('img') if i.get('src','').startswith('/assets/')))


def image_warnings(body, fmt):
    source = MarkdownIt().render(body) if fmt == 'markdown' else body
    soup = BeautifulSoup(source, 'html.parser')
    def supported(src):
        parsed = urlsplit(src)
        return bool(re.fullmatch(r'/assets/[0-9a-f]{32}\.jpg',src)) or (parsed.scheme == 'https' and parsed.hostname in {'mmbiz.qpic.cn','mmbiz.qlogo.cn'})
    return sum(not supported(tag.get('src','')) for tag in soup.find_all('img'))


def save_image(raw: bytes, root: Path) -> str:
    if len(raw) > 10 * 1024 * 1024:
        raise ValueError('图片大小不能超过 10 MB。')
    try:
        with Image.open(io.BytesIO(raw)) as source:
            if source.width * source.height > 24000000:
                raise ValueError('图片分辨率过大，请先缩小。')
            image = ImageOps.exif_transpose(source).convert('RGB')
            image.thumbnail((2400, 2400))
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValueError('请选择有效的 JPG、PNG 或 WebP 图片。') from exc
    name = uuid.uuid4().hex + '.jpg'
    image.save(root / name, 'JPEG', quality=92)
    return '/assets/' + name


def make_cover(title: str, theme: str, root: Path) -> str:
    t = THEMES.get(theme, THEMES['jade'])
    canvas = Image.new('RGB', (1200, 510), t['soft'])
    draw = ImageDraw.Draw(canvas)
    draw.rectangle((0,0,15,510), fill=t['accent'])
    draw.ellipse((900,-160,1370,310), fill=t['accent'])
    draw.ellipse((990,20,1190,220), outline=t['soft'], width=2)
    font_paths = [Path('C:/Windows/Fonts/msyh.ttc'), Path('C:/Windows/Fonts/simhei.ttf'), Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')]
    fp = next((str(p) for p in font_paths if p.exists()), None)
    size = 52 if len(title) <= 36 else 40
    font = ImageFont.truetype(fp, size) if fp else ImageFont.load_default()
    small = ImageFont.truetype(fp, 20) if fp else ImageFont.load_default()
    draw.text((70,60), 'M O J I A N   /   墨 笺', fill=t['accent'], font=small)
    lines, line = [], ''
    for char in title:
        if draw.textlength(line + char, font=font) > 820:
            lines.append(line)
            line = char
        else:
            line += char
    if line:
        lines.append(line)
    for i, line in enumerate(lines[:4]):
        draw.text((70,150+i*(size+22)), line, fill=t['ink'], font=font)
    draw.line((70,453,830,453), fill=t['accent'], width=1)
    name = uuid.uuid4().hex + '.jpg'
    canvas.save(root / name, 'JPEG', quality=93)
    return '/assets/' + name


def export_document(article):
    return '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>' + html.escape(article['title']) + '</title><body style="max-width:680px;margin:40px auto;padding:0 24px"><h1>' + html.escape(article['title']) + '</h1>' + publication_html(article['body'],article['format'],article['theme']) + '</body></html>'
