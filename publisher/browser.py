"""Background browser adapter. No private endpoints or CAPTCHA solving.

WeChat may change its editor. Ambiguous results pause; mutation checkpoints are
persisted BEFORE a click. Live account acceptance is still required.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import re
import secrets
import time
import traceback
import unicodedata
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

from .editor_dom import EDITOR_PROBE, candidate_summary, choose_body
from .rendering import render, wechat_compatible_html
from .preparation import prepare_snapshot, verify_snapshot
from .login import ACCOUNT_PROBE, SessionIdentity
from .store import now
from .receipts import PublicationReceiptMixin


class NeedsUser(Exception):
    pass


class CreationSourceRequired(NeedsUser):
    pass


class VerificationRecoveryRequired(ValueError):
    """The user must confirm no phone approval before recreating a challenge."""
    pass


# WeChat rewrites image markup while handling a paste.  In particular, it may
# move the canonical URL to ``data-src``, add a cache/format query string, or
# use a lazy-loading placeholder in ``src``.  Comparing the raw ``src``
# attribute therefore reports a false loss even when the same image is still
# in the editor.  Keep the attribute list deliberately small and public: these
# are the URL attributes used by the WeChat editor, not a broad scrape of page
# data.
_IMAGE_URL_ATTRIBUTES = (
    'data-src', 'src', 'data-original', 'data-lazy-src', 'data-url',
)


def _image_url_key(value):
    """Return a stable image identity, ignoring transport-only URL details."""
    if not isinstance(value, str):
        return ''
    value = unquote(value.strip())
    if not value or value.startswith(('data:', 'blob:')):
        return ''
    try:
        parts = urlsplit(value)
    except ValueError:
        return ''
    host = (parts.hostname or '').lower()
    path = (parts.path or '').rstrip('/')
    # The query contains wx_fmt, cache-busters and lazy-loading flags that are
    # routinely changed by the platform.  Host/path still distinguishes the
    # uploaded image and is stable across those rewrites.
    if host:
        return f'{host}{path}'
    # Keep local/relative paths useful in offline tests and before upload.
    return path or value.split('?', 1)[0].split('#', 1)[0].rstrip('/')


def _image_aliases(tag):
    aliases = set()
    for attribute in _IMAGE_URL_ATTRIBUTES:
        value = tag.get(attribute)
        key = _image_url_key(value)
        if key:
            aliases.add(key)
    return aliases


def _image_identities_match(expected_tags, actual_tags):
    """Match image elements by stable aliases, preserving duplicate counts.

    A bipartite match is used instead of a set comparison so two occurrences
    of the same image remain two occurrences.  An image with no usable URL is
    only matched to another URL-less image; a replaced/lost image thus still
    fails closed when its original identity is known.
    """
    if len(expected_tags) != len(actual_tags):
        return False
    expected = [_image_aliases(tag) for tag in expected_tags]
    actual = [_image_aliases(tag) for tag in actual_tags]
    edges = []
    for aliases in expected:
        row = []
        for index, other in enumerate(actual):
            if aliases and other and aliases.intersection(other):
                row.append(index)
            elif not aliases and not other:
                row.append(index)
        edges.append(row)

    matched = {}

    def visit(index, seen):
        for candidate in edges[index]:
            if candidate in seen:
                continue
            seen.add(candidate)
            previous = matched.get(candidate)
            if previous is None or visit(previous, seen):
                matched[candidate] = index
                return True
        return False

    return all(visit(index, set()) for index in range(len(expected)))


def _image_identity_summary(tags):
    return {
        'count': len(tags),
        'known': [sorted(_image_aliases(tag)) for tag in tags],
        'unknown': sum(not _image_aliases(tag) for tag in tags),
        'unidentified': [{'class': tag.get('class', []), 'alt': tag.get('alt', ''),
                          'contenteditable': tag.get('contenteditable', '')}
                         for tag in tags if not _image_aliases(tag)],
    }


def _article_image_tags(soup):
    # ProseMirror adds an empty helper image after an inline image so its caret
    # can move past it. Only named editor decorations are excluded; a real
    # image whose URL disappeared must still fail the readback.
    decorations = {'ProseMirror-separator', 'ProseMirror-widget'}
    return [tag for tag in soup.find_all('img')
            if _image_aliases(tag) or not decorations.intersection(tag.get('class', []))]


class BrowserDriver(PublicationReceiptMixin):
    _receipt_image_key = staticmethod(_image_url_key)
    @staticmethod
    def _receipt_image_keys(markup):
        keys = []
        for tag in _article_image_tags(BeautifulSoup(markup, 'html.parser')):
            key = next((_image_url_key(tag.get(attribute)) for attribute in _IMAGE_URL_ATTRIBUTES
                        if _image_url_key(tag.get(attribute))), '')
            # Missing real images remain present as unknown identities; only
            # named, URL-less editor decorations are excluded above.
            keys.append(key)
        return keys
    def __init__(self, store):
        self.store = store
        self.playwright = None
        self.context = None
        self.page = None
        self.lock = asyncio.Lock()
        self.last_message = '微信执行器待命'
        self.active_job_id = None
        self.page_job_id = None
        self.ever_logged = False
        self.status_lock = asyncio.Lock()
        self.last_status = None
        self.identity = SessionIdentity()
        self.resume_job_id = None
        self.resume_page = None
        self.editor_diagnostics = []
        # Details from the last body readback are kept separately from the
        # editor candidate probe.  They are written to evidence only when a
        # mutation pauses, so a mismatch can be diagnosed without logging the
        # article itself.
        self.readback_diagnostics = {}
        self.cover_diagnostics = {}
        self.window_visible = False
        self.headless = False
        self.needs_user = False
        self.interaction_until = 0
        self.human_frame = None

    def _browser_mode(self):
        return 'background'

    def _launch_args(self, force_visible=False):
        # No window flags are needed for a true headless context.  Keep the
        # browser surface free of automation UI while retaining the normal
        # WeChat DOM and persistent profile.
        return ['--disable-session-crashed-bubble']

    async def _close_context(self):
        if self.context:
            await asyncio.wait_for(self.context.close(), timeout=6)
        self.context = None
        self.page = None
        self.page_job_id = None
        self.resume_job_id = self.resume_page = None
        self.human_frame = None

    async def _launch_context(self, force_visible=False):
        await self._close_context()
        if not self.playwright:
            self.playwright = await async_playwright().start()
        for channel in ('msedge', 'chrome'):
            try:
                self.context = await self.playwright.chromium.launch_persistent_context(
                    str(self.store.root / 'wechat-profile'), channel=channel,
                    headless=True, viewport={'width':1280,'height':860},
                    locale='zh-CN', chromium_sandbox=True,
                    args=self._launch_args(force_visible),
                )
                break
            except Exception:
                self.context = None
        if not self.context:
            raise ValueError('无法启动微信执行器。请确认已安装 Microsoft Edge 或 Chrome，且没有另一份墨笺占用登录数据。')
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        self.page.set_default_timeout(12000)
        await self.page.goto('https://mp.weixin.qq.com/', wait_until='domcontentloaded')
        self.headless = True
        self.window_visible = False
        self.last_message = '微信执行器已启动。扫码和本人验证可在软件内的连接面板完成。'
        return await self.status()

    async def open(self, force_visible=False):
        if self.page and not self.page.is_closed():
            return await self.status()
        return await self._launch_context()

    async def show(self):
        result = await self.open()
        self.interaction_until = time.monotonic() + 30
        return result

    async def hide(self):
        self.release_interaction()
        return await self.status()

    @property
    def interacting(self):
        return time.monotonic() < self.interaction_until

    def release_interaction(self):
        self.interaction_until = 0
        self.human_frame = None

    def _human_page(self):
        if not self.interacting or not self.page or self.page.is_closed():
            raise ValueError('连接面板已失效，请重新打开。')
        if urlsplit(self.page.url).hostname != 'mp.weixin.qq.com':
            raise ValueError('当前页面不在微信公众平台，连接面板已停止操作。')
        return self.page

    async def interaction_frame(self):
        """On-demand local preview. Nothing is saved to disk or sent remotely."""
        page = self._human_page()
        self.interaction_until = time.monotonic() + 30
        viewport = page.viewport_size or {'width':1280, 'height':860}
        frame_id = secrets.token_urlsafe(18)
        raw = await page.screenshot(type='png', scale='css', timeout=8000)
        self.human_frame = (frame_id, page, page.url, time.monotonic(), viewport)
        return {'frame_id':frame_id, 'image':'data:image/png;base64,' + base64.b64encode(raw).decode(),
                **viewport, 'captured_at':now(),
                'verification_visible':await self._verification_dialog() is not None}

    async def interaction_action(self, values):
        # These actions originate from the human's clicks/keystrokes in the
        # workbench panel. No general navigation, scripts or private API relay.
        page = self._human_page()
        frame = self.human_frame
        if (not frame or values['frame_id'] != frame[0] or page is not frame[1]
                or page.url != frame[2] or time.monotonic() - frame[3] > 15):
            raise ValueError('微信页面已变化，请等待面板刷新后再操作。')
        self.human_frame = None  # An action cannot be replayed with this frame.
        self.interaction_until = time.monotonic() + 30
        kind = values['kind']
        if kind == 'click':
            await page.mouse.click(values['x'] * (frame[4]['width'] - 1), values['y'] * (frame[4]['height'] - 1))
        elif kind == 'scroll':
            await page.mouse.wheel(0, values['delta'])
        elif kind == 'text':
            await page.keyboard.insert_text(values['text'])
        elif kind == 'key':
            if values['key'] not in {'Enter','Tab','Backspace','Escape','ArrowUp','ArrowDown','PageUp','PageDown'}:
                raise ValueError('不支持此按键。')
            await page.keyboard.press(values['key'])
        else:
            raise ValueError('无效的面板操作。')

    async def close(self):
        try:
            if self.context:
                await asyncio.wait_for(self.context.close(), timeout=6)
        except Exception:
            logging.exception('WeChat window cleanup did not finish normally')
        finally:
            self.context = None
            try:
                if self.playwright:
                    await asyncio.wait_for(self.playwright.stop(), timeout=6)
            except Exception:
                logging.exception('Browser driver cleanup did not finish normally')
            self.playwright = None
        self.page = None
        self.last_status = None
        self.identity.clear()
        self.window_visible = False
        self.headless = False
        self.needs_user = False
        self.active_job_id = self.page_job_id = None
        self.resume_job_id = self.resume_page = None
        self.release_interaction()

    async def status(self, refresh=False):
        configured = self.store.settings()['account_name'].strip()
        runtime = {'browser_mode':'background', 'window_visible':False,
                   'needs_user':self.needs_user, 'active_job_id':self.active_job_id,
                   'interacting':self.interacting, 'last_message':self.last_message}
        if not self.page or self.page.is_closed():
            result = self.identity.resolve('',{},configured,opened=False)
            result.update(state='closed',label='待连接',message='任务会自动启动后台连接；首次使用请在软件内扫码。')
            return {**result, **runtime, 'window_state':'closed', 'checked_at':now()}
        if not refresh and self.lock.locked() and self.last_status:
            return {**self.last_status, **runtime, 'window_state':'background'}
        async with self.status_lock:
            page = self.page
            check = None
            try:
                parts = urlsplit(page.url)
                token = parse_qs(parts.query).get('token',[''])[0]
                if refresh and self.context and parts.hostname=='mp.weixin.qq.com' and token:
                    check = await self.context.new_page()
                    await check.goto('https://mp.weixin.qq.com/cgi-bin/home?' + urlencode({'t':'home/index','lang':'zh_CN','token':token}), wait_until='domcontentloaded',timeout=20000)
                    page = check
                probe = await asyncio.wait_for(page.evaluate(ACCOUNT_PROBE), timeout=5)
                if refresh:
                    for _ in range(12):
                        if probe.get('names') or probe.get('login_form') or probe.get('expired'):
                            break
                        await asyncio.sleep(.4)
                        probe = await asyncio.wait_for(page.evaluate(ACCOUNT_PROBE), timeout=5)
                result = self.identity.resolve(page.url,probe,configured,self.ever_logged,fresh=refresh)
                self.ever_logged = self.ever_logged or result['logged_in']
            except Exception:
                result = self.identity.resolve(self.page.url,{'error':True},configured,self.ever_logged,fresh=refresh)
            finally:
                if check:
                    with contextlib.suppress(Exception):
                        await check.close()
            result['message'] = result['message'].replace('微信窗口','软件内的连接面板')
            result['checked_at'] = now()
            result.update(**runtime, window_state='background')
            self.last_status = result
            return result

    async def _visible(self, selector):
        for item in await self.page.locator(selector).all():
            if await item.is_visible():
                return item
        return None

    async def _click_text(self, text, scope=None):
        root = scope or self.page
        options = root.get_by_role('button', name=text, exact=True).or_(root.get_by_role('link', name=text, exact=True))
        # Popup entries appear after an asynchronous editor transaction. A
        # snapshot immediately after the opener click can legitimately be empty.
        for attempt in range(20):
            found = [item for item in await options.all()
                     if await item.is_visible() and await item.is_enabled()]
            if not found:
                found = [item for item in await root.get_by_text(text, exact=True).all()
                         if await item.is_visible() and await item.is_enabled()]
            if len(found) == 1:
                await found[0].click()
                return
            if len(found) > 1:
                break
            await asyncio.sleep(.2)
        raise NeedsUser(f'微信页面的“{text}”入口无法唯一识别（可见入口 {len(found)} 个），请查看处理面板。')

    async def _account(self):
        result = await self.status(refresh=True)
        if not result['logged_in'] or result['account_matches'] is not True:
            raise NeedsUser(result['message'])

    async def _field(self, label, selector, wait=True):
        """Resolve a unique editable control without assuming its HTML tag."""
        for attempt in range(20 if wait else 1):
            candidates = [item for item in await self.page.locator(selector).all()
                          if await item.is_visible() and await item.evaluate('(el)=>el.isContentEditable || ((el.tagName==="INPUT" || el.tagName==="TEXTAREA") && !el.disabled && !el.readOnly)')]
            if len(candidates) > 1:
                raise NeedsUser(f'找到多个{label}输入区，请在微信确认当前只编辑一篇图文。')
            if candidates:
                return candidates[0]
            if wait:
                await asyncio.sleep(.4)
        raise NeedsUser(f'未找到可编辑的{label}输入区，已停止后续操作。')

    async def _title(self, wait=True):
        return await self._field('标题', '#title, input[placeholder="请在这里输入标题"], textarea[placeholder="请在这里输入标题"], [contenteditable][data-placeholder="请在这里输入标题"], [contenteditable][placeholder="请在这里输入标题"]', wait)

    async def _field_value(self, item):
        return await item.evaluate('(el)=>(el.isContentEditable ? el.innerText : el.value)||""')

    async def _editor_controls(self, article):
        title = await self._title()
        editor = await self._editor()
        return title, editor

    async def _fill_metadata(self, article):
        for key, label, selector in (
            ('author', '作者', '#author, input[placeholder="请输入作者"]'),
            ('digest', '摘要', '#js_description'),
        ):
            if article[key]:
                field = await self._field(label, selector)
                await self._fill_field(field, article[key], label)

    async def _current_editor(self, job):
        if not self.page or self.page.is_closed():
            raise NeedsUser('请打开微信中本任务的编辑页，再点“继续填写当前页”。')
        parts = urlsplit(self.page.url)
        if parts.hostname!='mp.weixin.qq.com' or parts.path!='/cgi-bin/appmsg':
            raise NeedsUser('当前不是微信图文编辑页。请先打开需要继续的那篇草稿。')
        title = await self._title(wait=False)
        value = (await self._field_value(title)).strip()
        if value and value!=job['snapshot']['title']:
            raise NeedsUser('当前微信编辑页的标题与任务不一致，请切换到这篇文章后再继续。')
        return await self._editor_controls(job['snapshot'])

    async def prepare_resume_editor(self, job):
        if self.lock.locked():
            raise ValueError('微信正在执行任务，请稍候。')
        if job['status']!='needs_review' or job['stage'] not in {'editing','resume_editor'}:
            raise ValueError('此任务不能继续填写当前页，请先核对微信实际结果。')
        async with self.lock:
            try:
                await self._account()
                await self._current_editor(job)
            except NeedsUser as exc:
                raise ValueError(str(exc)) from exc
            self.resume_job_id, self.resume_page = job['id'], self.page
            self.store.update_job(job['id'],status='queued',stage='resume_editor',
                message='已确认继续填写当前微信编辑页，等待执行；不会新建另一篇图文。')

    async def _editor(self, wait=True):
        self.editor_diagnostics = []

        async def collect(scope, scope_name, depth=0):
            candidates = []
            for item in await scope.locator('[contenteditable], [role="textbox"], body').all():
                if not await item.is_visible():
                    continue
                info = await item.evaluate(EDITOR_PROBE)
                if info['tag'] == 'body' and not info['editable']:
                    continue
                info['scope'] = scope_name
                candidates.append((item, info))
            # Collect the iframe body even when the parent has a title editor.
            if depth < 4:
                for index, iframe in enumerate(await scope.locator('iframe').all()):
                    if not await iframe.is_visible():
                        continue
                    handle = await iframe.element_handle()
                    frame = await handle.content_frame() if handle else None
                    if frame:
                        candidates.extend(await collect(frame, f'{scope_name}/iframe[{index}]', depth+1))
            return candidates

        attempts = 25 if wait else 1
        for attempt in range(attempts):
            candidates = await collect(self.page, 'page')
            self.editor_diagnostics = [info for _, info in candidates]
            selected = choose_body(self.editor_diagnostics)
            if selected is not None:
                self.editor_diagnostics[selected]['selected'] = True
                return candidates[selected][0]
            if attempt < attempts - 1:
                await asyncio.sleep(.4)
        eligible = [info for info in self.editor_diagnostics if not info['excluded']]
        reason = '检测到多个可编辑控件，无法唯一确认正文' if len(eligible) > 1 else '未识别到明确的正文输入区'
        details = candidate_summary(self.editor_diagnostics)
        raise NeedsUser(f'{reason}，标题和正文尚未填写。编辑器适配需要检查；候选信息：{details or "无可见候选"}。')

    async def _fill_field(self, field, value, label):
        if not value:
            return
        await field.fill(value)
        if (await self._field_value(field)).strip() == value:
            return
        await field.click()
        await field.press('Control+A')
        await self.page.keyboard.type(value, delay=1)
        if (await self._field_value(field)).strip() != value:
            raise NeedsUser(f'微信{label}未写入，已停止后续操作。')

    async def _insert_html(self, editor, content):
        self.readback_diagnostics = {}
        await editor.click()
        ok = await editor.evaluate('''(el, html) => {
            el.focus();
            const selection = window.getSelection();
            const range = document.createRange();
            // Chromium preserves the old first block's editing context when
            // insertHTML replaces a selection. An old heading can therefore
            // wrap the entire new fragment and make ordinary paragraphs bold.
            // Start at the empty body root, in the same synchronous operation,
            // so the browser cannot inherit that old block or inline marks.
            // Retain the original nodes until native insertion is accepted.
            const previous = Array.from(el.childNodes);
            el.replaceChildren();
            range.selectNodeContents(el);
            selection.removeAllRanges();
            selection.addRange(range);
            let ok;
            try {
                ok = document.execCommand('insertHTML', false, html);
            } catch (error) {
                el.replaceChildren(...previous);
                throw error;
            }
            if (!ok) {
                el.replaceChildren(...previous);
                return false;
            }
            el.dispatchEvent(new InputEvent('input', {bubbles:true,inputType:'insertFromPaste'}));
            return ok;
        }''', content)
        await asyncio.sleep(.2)
        if not ok:
            raise NeedsUser('微信编辑器拒绝写入排版正文，已停止保存，未改用纯文本覆盖。')
        expected = BeautifulSoup(content, 'html.parser')
        normalize = lambda text: re.sub(r'[\s\u200b\ufeff]+', '', text)
        # WeChat's ProseMirror normalizes punctuation, smart quotes, list
        # markers and code-block decoration while it accepts pasted HTML.
        # Accept only typographic equivalents, never drop punctuation,
        # arithmetic signs, decimal points or code letter case.
        def semantic(text):
            text = unicodedata.normalize('NFKC', text)
            return normalize(text.translate(str.maketrans({'“':'"','”':'"','‘':"'",'’':"'"})))

        expected_image_tags = expected.find_all('img')
        expected_text = normalize(expected.get_text())
        expected_semantic = semantic(expected.get_text())
        # The WeChat editor rewrites ProseMirror nodes asynchronously after
        # insertHTML. Give that rewrite a short bounded window to settle;
        # never continue unless the complete text and image sets match.
        for attempt in range(10):
            visible_text = normalize(await editor.inner_text())
            # WeChat may flatten headings, tables and lists while keeping the
            # same text. Accept textContent only as an exact second view;
            # truncation still fails closed because neither can match.
            dom_text = normalize((await editor.text_content()) or '')
            actual = BeautifulSoup(await editor.inner_html(), 'html.parser')
            actual_image_tags = _article_image_tags(actual)
            visible_semantic = semantic(visible_text)
            dom_semantic = semantic(dom_text)
            text_ok = (expected_text == visible_text or expected_text == dom_text
                       or expected_semantic == visible_semantic
                       or expected_semantic == dom_semantic)
            images_ok = _image_identities_match(expected_image_tags, actual_image_tags)
            if text_ok and images_ok:
                return
            if attempt < 9:
                await asyncio.sleep(.35)
        self.readback_diagnostics = {
            'expected_len': len(expected_text),
            'visible_len': len(visible_text),
            'dom_len': len(dom_text),
            'expected_semantic_len': len(expected_semantic),
            'visible_semantic_len': len(visible_semantic),
            'dom_semantic_len': len(dom_semantic),
            # Hashes make it possible to prove which view differed without
            # putting article text into the local evidence file.
            'expected_semantic_sha256': hashlib.sha256(expected_semantic.encode('utf-8')).hexdigest(),
            'visible_semantic_sha256': hashlib.sha256(visible_semantic.encode('utf-8')).hexdigest(),
            'dom_semantic_sha256': hashlib.sha256(dom_semantic.encode('utf-8')).hexdigest(),
            'expected_images': _image_identity_summary(expected_image_tags),
            'actual_images': _image_identity_summary(actual_image_tags),
        }
        if not text_ok:
            raise NeedsUser('正文回读与文章完整内容不一致，已停止保存。')
        raise NeedsUser('正文图片回读与文章不一致，已停止保存。')

    async def _upload(self, editor, path):
        if not path.is_file():
            raise NeedsUser('任务对应的本地图片文件不存在，正文已保留，已停止保存。')
        before = set(await editor.locator('img').evaluate_all('(els)=>els.map(e=>e.getAttribute("data-src")||e.src)'))
        await editor.click()
        await editor.press('Control+End')
        button = await self._visible('#js_editor_insertimage')
        if button:
            await button.click()
        else:
            # The current toolbar shown by the user labels this entry 图片.
            # Only click a unique exact match, never a guessed coordinate.
            await self._click_text('图片')
        upload = self.page.get_by_text('本地上传', exact=True)
        visible = []
        for _ in range(10):
            visible = [item for item in await upload.all() if await item.is_visible()]
            if visible:
                break
            await asyncio.sleep(.2)
        if len(visible) != 1:
            raise NeedsUser('图片菜单中未找到唯一的“本地上传”入口，正文已保留，已停止保存。')
        async with self.page.expect_file_chooser(timeout=12000) as info:
            await visible[0].click()
        await (await info.value).set_files(str(path))
        for _ in range(35):
            for src in await editor.locator('img').evaluate_all('(els)=>els.map(e=>e.getAttribute("data-src")||e.src)'):
                if src not in before and urlsplit(src).hostname in {'mmbiz.qpic.cn','mmbiz.qlogo.cn'}:
                    return src
            await asyncio.sleep(0.7)
        raise NeedsUser('图片上传未得到微信图片地址，请检查浏览器。')

    async def _cover_urls(self):
        return await self.page.locator('#js_cover_area, .js_cover_preview_new').evaluate_all(r'''els =>
            els.filter(el=>el.getClientRects().length && getComputedStyle(el).visibility!=='hidden')
            .flatMap(el=>[el,...el.querySelectorAll('*')]).flatMap(n =>
                ['data-src','src','data-original','data-url'].map(a=>n.getAttribute(a)||'')
                .concat([...getComputedStyle(n).backgroundImage.matchAll(/url\(["']?([^"')]+)["']?\)/g)].map(m=>m[1])))''')

    async def _cover(self, cover_url):
        expected = _image_url_key(cover_url)
        self.cover_diagnostics = {'expected':expected}
        before = {_image_url_key(url) for url in await self._cover_urls() if _image_url_key(url)}
        self.cover_diagnostics['before'] = sorted(before)
        if expected in before:
            return  # The same editor was resumed after a later pre-save pause.
        previews = [item for item in await self.page.locator('.js_cover_preview_new').all() if await item.is_visible()]
        change = None
        if len(previews) == 1:
            await previews[0].hover()  # Reveal the existing cover's change control.
            changes = [item for item in await previews[0].locator('.js_chooseCover').all() if await item.is_visible()]
            if len(changes) == 1:
                change = changes[0]
        # The editor renders desktop/mobile cover controls at the same time.
        # Hidden copies (and nested wrappers of one control) are not another
        # article. Resolve the visible leaf rather than clicking the whole set.
        entries = [item for item in await self.page.locator('.js_cover_btn_area').all()
                   if await item.is_visible()]
        leaves = [item for item in entries if not await item.evaluate('''el =>
            [...el.querySelectorAll('.js_cover_btn_area')].some(n =>
                n.getClientRects().length && getComputedStyle(n).visibility !== 'hidden')''')]
        self.cover_diagnostics.update(entry_count=len(entries),leaf_count=len(leaves))
        if change is None and len(leaves) != 1:
            raise NeedsUser(f'封面入口无法唯一识别（可见入口 {len(leaves)} 个），已停止保存。')
        await (change or leaves[0]).click()
        await self._click_text('从正文选择')
        dialog = self.page.locator('.weui-desktop-dialog_img-picker')
        await dialog.wait_for(state='visible')
        selected = None
        self.cover_diagnostics['picker'] = await dialog.locator('.appmsg_content_img').evaluate_all('''els=>els.map(el=>({
            tag:el.tagName,classes:el.className,parent_tag:el.parentElement?.tagName,
            parent_classes:el.parentElement?.className,grandparent_classes:el.parentElement?.parentElement?.className,
            selected:el.getAttribute('aria-selected'),parent_selected:el.parentElement?.getAttribute('aria-selected')
        }))''')
        for item in await dialog.locator('.appmsg_content_img').all():
            if not await item.is_visible():
                continue
            images = await item.evaluate('''el => [el, ...el.querySelectorAll('img')].flatMap(n =>
                ['data-src','src','data-original','data-url'].map(a=>n.getAttribute(a)||'')
                .concat([...getComputedStyle(n).backgroundImage.matchAll(/url\(["']?([^"')]+)["']?\)/g)].map(m=>m[1])))''')
            if expected in {_image_url_key(image) for image in images}:
                selected = item
                break
        if not selected:
            raise NeedsUser('微信封面选择器中未找到本篇封面，请手动选择后保存草稿。')
        # A thumbnail can be covered by the picker's selection mask. Click its
        # one-image wrapper, allowing normal event bubbling and hit testing.
        parent = selected.locator('..')
        control = parent if await parent.locator('.appmsg_content_img').count() == 1 else selected
        already_selected = await control.evaluate('''el=>
            el.getAttribute('aria-selected')==='true' ||
            el.querySelector('input[type=checkbox]:checked')!==null ||
            /(?:^|\s)(?:selected|is-selected)(?:\s|$)/.test(el.className||'')''')
        if not already_selected:
            await control.click()
        await self._click_text('下一步', dialog)
        await asyncio.sleep(0.5)
        await self._click_text('确认')
        for _ in range(30):
            urls = await self._cover_urls()
            actual = {_image_url_key(url) for url in urls if urlsplit(url).hostname in {'mmbiz.qpic.cn','mmbiz.qlogo.cn'}}
            # Cropping may generate a new asset URL. It must be new after the
            # uniquely identified source and the crop confirmation, never an
            # unrelated pre-existing cover or a loading placeholder.
            if expected in actual or actual.difference(before):
                self.cover_diagnostics['actual'] = sorted(actual)
                return
            await asyncio.sleep(.5)
        self.cover_diagnostics['actual'] = sorted({_image_url_key(url) for url in await self._cover_urls() if _image_url_key(url)})
        raise NeedsUser('微信封面回读未确认所选图片，已停止保存。')

    async def _save_signal(self):
        return await self.page.locator('#js_save_success, .weui-desktop-toast, .weui-desktop-tips').evaluate_all('''els => els.filter(e=>e.getClientRects().length).map(e=>e.innerText.trim()).filter(s=>/已保存|保存成功/.test(s)).join('|')''')

    async def _save(self, job):
        before = await self._save_signal()
        self.store.update_job(job['id'], stage='saving', step='saving', message='正在保存微信草稿，等待微信确认。')
        await self._click_text('保存为草稿')
        observed_empty = not before
        for _ in range(30):
            signal = await self._save_signal()
            if not signal:
                observed_empty = True
            if signal and (observed_empty or signal != before):
                parts = urlsplit(self.page.url)
                query = parse_qs(parts.query)
                remote_id = query.get('appmsgid', query.get('AppMsgId',['']))[0]
                # Store no transient login token in exported job metadata.
                remote = 'https://mp.weixin.qq.com/cgi-bin/appmsg?' + urlencode({'t':'media/appmsg_edit_v2','action':'edit','type':77,'appmsgid':remote_id,'lang':'zh_CN'}) if remote_id else ''
                self.store.update_job(job['id'], stage='draft_saved', step='saved', remote_url=remote, message='微信页面已显示新的草稿保存成功提示。')
                return
            await asyncio.sleep(0.5)
        raise NeedsUser('已点击保存，但未观察到新的成功回执。请核对微信草稿箱；工具不会重复保存。')

    async def _publish_signal(self):
        texts = await self.page.locator('.weui-desktop-msg__title, .weui-desktop-msg_title, .weui-desktop-toast, .weui-desktop-dialog__bd, .weui-desktop-dialog__content, [role="alert"]').evaluate_all('els=>els.filter(e=>e.getClientRects().length).map(e=>e.innerText.trim())')
        return any(re.fullmatch(r'(发表成功|发布成功|已成功发表|已成功发布)[！!。]?', text) for text in texts)

    async def _published_url(self):
        links = await self.page.locator('a[href]').evaluate_all('''els => els.filter(e => e.getClientRects().length).map(e => e.href).filter(Boolean)''')
        for link in links:
            try:
                parts = urlsplit(link)
            except ValueError:
                continue
            # WeChat uses both the short /s/<id> form and /s?__biz=...
            # article links. A substring check can also pick up a redirect
            # or lookalike domain, so retain only the actual article host.
            article_path = (parts.path == '/s' and bool(parts.query)) or (
                parts.path.startswith('/s/') and bool(parts.path[3:]))
            if parts.scheme == 'https' and parts.hostname == 'mp.weixin.qq.com' and article_path:
                return link
        return ''

    async def _record_publish_receipt(self, job, *, supplemental=False):
        article_url = await self._published_url()
        evidence = ''
        with contextlib.suppress(Exception):
            name = job['id'] + '-published.png'
            await self.page.screenshot(path=str(self.store.root / 'evidence' / name), timeout=8000)
            evidence = '/evidence/' + name
        self.store.update_job(job['id'], status='published', step='done', article_url=article_url,
            evidence=evidence, message=('补充核对：' if supplemental else '') + '微信页面已显示发表成功回执。'
                + (f'文章链接：{article_url}' if article_url else ''))

    async def _dialog_control(self, dialog, name):
        controls = dialog.get_by_role('button', name=name, exact=True).or_(
            dialog.get_by_role('link', name=name, exact=True))
        visible = [item for item in await controls.all() if await item.is_visible() and await item.is_enabled()]
        if not visible:
            visible = [item for item in await dialog.get_by_text(name, exact=True).all()
                       if await item.is_visible() and await item.is_enabled()]
        return visible[0] if len(visible) == 1 else None

    async def _verification_dialog(self):
        # WeChat's administrator challenge still uses its older .dialog
        # markup, without an ARIA role. Source/settings use the newer dialog.
        dialogs = self.page.locator(
            '.weui-desktop-dialog:visible, [role="dialog"]:visible, .dialog_wrp .dialog:visible')
        matches = [item for item in await dialogs.all()
                   if re.search(r'微信验证|管理员验证|验证码|扫码后|扫描.*二维码|二维码.*验证', await item.inner_text())]
        return matches[0] if len(matches) == 1 else None

    def _mark_verification(self, job):
        current = self.store.job(job['id'])
        receipt = self._load_receipt(current)
        if receipt:
            receipt['verification_pending'] = True
            self._write_receipt(current, receipt)
        self.store.update_job(job['id'], step='verification',
            message='微信要求管理员扫码验证，请在软件内扫码并在手机确认；验证后自动核对发表回执。')

    async def _source_dialog_kind(self, dialog):
        text = await dialog.inner_text()
        if '创作来源' not in text:
            return ''
        if (await self._dialog_control(dialog, '无需声明并发表') is not None
                and await self._dialog_control(dialog, '去声明') is not None):
            return 'source_notice'
        # This is the actual radio form, not an originality or protocol dialog.
        if '声明类型' in text and '内容由AI生成' in text and '无需声明' in text:
            return 'source_form'
        return ''

    async def _publish_dialog_kind(self, dialog):
        source = await self._source_dialog_kind(dialog)
        if source:
            return source
        text = await dialog.inner_text()
        if ('未开启群发通知' in text
                and await self._dialog_control(dialog, '继续发表') is not None):
            return 'notification_notice'
        return 'confirmation'

    async def _open_creation_source(self):
        labels = self.page.get_by_text('创作来源', exact=True)
        visible = [item for item in await labels.all() if await item.is_visible()]
        if len(visible) != 1:
            raise NeedsUser('未找到唯一的创作来源设置入口，请在软件内处理面板核对。')
        # The public editor renders a label plus a value in the same row.
        # Click the unique row value, without touching neighboring originality.
        row = visible[0].locator('..')
        for _ in range(3):
            values = row.get_by_text(re.compile(r'^(未添加|无需声明|内容由AI生成)$'))
            options = [item for item in await values.all() if await item.is_visible() and await item.is_enabled()]
            if len(options) == 1:
                await options[0].click()
                return
            row = row.locator('..')
        raise NeedsUser('创作来源设置入口结构变化，请在软件内处理面板核对。')

    async def _publish_dialog_step(self, dialog, creation_source='unspecified'):
        if await self._publish_dialog_kind(dialog) == 'notification_notice':
            text = await dialog.inner_text()
            if re.search(r'扫码|扫描|管理员验证|验证码|声明原创|同意.*协议', text):
                raise NeedsUser('微信要求本人验证或声明，请在软件内处理面板完成。')
            await (await self._dialog_control(dialog, '继续发表')).click()
            return 'final_submit'
        source_kind = await self._source_dialog_kind(dialog)
        if source_kind:
            if creation_source not in {'ai', 'non_ai'}:
                raise CreationSourceRequired('请在软件内确认本篇创作来源：文字或配图是否由 AI 生成，确认后将在同一篇草稿继续发表。')
            if source_kind == 'source_notice':
                name = '去声明' if creation_source == 'ai' else '无需声明并发表'
                control = await self._dialog_control(dialog, name)
                await control.click()
                return 'source_open' if creation_source == 'ai' else 'source_continue'
            name = '内容由AI生成' if creation_source == 'ai' else '无需声明'
            # Platform radios may visually hide the native input under an
            # icon. Click the unique associated label, then read checked state.
            labels = [item for item in await dialog.locator('label:visible').all()
                      if (await item.inner_text()).strip() == name]
            if len(labels) > 1:
                raise NeedsUser('微信创作来源存在多个同名选项，已停止发表。')
            radio = (labels[0].locator('input[type="radio"]') if labels else
                     dialog.get_by_role('radio', name=name, exact=True))
            if await radio.count() != 1 or not await radio.is_enabled():
                raise NeedsUser('微信创作来源选项未就绪或不唯一，已停止发表。')
            if labels:
                await labels[0].click()
            elif await radio.is_visible():
                await radio.check()
            else:
                raise NeedsUser('微信创作来源选项尚未就绪，已停止发表。')
            if not await radio.is_checked():
                raise NeedsUser('微信创作来源选择未确认，已停止发表。')
            confirm = None
            for _ in range(10):
                candidate = await self._dialog_control(dialog, '确认')
                if candidate and 'disabled' not in (await candidate.get_attribute('class') or ''):
                    confirm = candidate
                    break
                await asyncio.sleep(.1)
            if confirm is None:
                raise NeedsUser('微信创作来源确认按钮尚未就绪，已停止发表。')
            await confirm.click()
            return 'source_saved'
        text = await dialog.inner_text()
        if re.search(r'扫码|扫描|管理员验证|验证码|声明原创|同意.*协议', text):
            raise NeedsUser('微信要求本人验证或声明。请在软件内的处理面板完成，然后核对发表结果。')
        await self._set_publish_notification(dialog)
        names = re.compile(r'^(发表|发布|确认发表|确认发布|继续发表|确定)$')
        candidates = dialog.get_by_role('button', name=names).or_(dialog.get_by_role('link', name=names))
        visible = [item for item in await candidates.all() if await item.is_visible() and await item.is_enabled()]
        if len(visible) == 1:
            await visible[0].click()
            return 'submit'
        return False

    async def _set_publish_notification(self, dialog):
        """Publish without sending a subscriber notification.

        Restrict the custom switch to the observed notification row; grouped
        notification and scheduling switches in this dialog are separate.
        """
        text = await dialog.inner_text()
        notify = dialog.get_by_label('群发通知', exact=True)
        if await notify.count() == 1:
            await notify.uncheck()
            if await notify.is_checked():
                raise NeedsUser('群发通知关闭状态未确认，已停止发表。')
            return
        if '群发通知' not in text:
            return
        rows = dialog.locator('.mass_send__notify:visible').filter(
            has=self.page.get_by_text('群发通知', exact=True))
        if await rows.count() != 1:
            raise NeedsUser('微信发表设置的群发通知开关不唯一，请在软件内处理面板核对通知范围。')
        checkbox = rows.locator('input.weui-desktop-switch__input[type="checkbox"]')
        boxes = rows.locator('.weui-desktop-switch__box:visible')
        if await checkbox.count() != 1 or await boxes.count() != 1 or not await checkbox.is_enabled():
            raise NeedsUser('微信群发通知开关结构变化，已停止发表。')
        if await checkbox.is_checked():
            await boxes.click()
        for _ in range(10):
            if not await checkbox.is_checked():
                return
            await asyncio.sleep(.1)
        raise NeedsUser('群发通知关闭状态未确认，已停止发表。')

    async def confirm_creation_source(self, job, value):
        if value not in {'ai', 'non_ai'}:
            raise ValueError('请选择真实的创作来源。')
        if self.lock.locked() or self.interacting:
            raise ValueError('请先关闭微信处理面板，再确认来源。')
        async with self.lock:
            if (job['action'] != 'publish' or not job['remote_url']
                    or not ((job['stage'] == 'source_required' and job['status'] in {'waiting_user','needs_review'})
                            or (job['stage'] == 'publishing' and job['status'] == 'needs_review'))):
                raise ValueError('当前任务无法安全恢复创作来源，请先核对微信发表结果。')
            if self.page_job_id not in {None, job['id']}:
                raise ValueError('当前页面属于其他任务，未修改来源。')
            # A recorded source_required checkpoint guarantees the final
            # submission was not reached. It can reopen its saved draft after
            # restart. Legacy uncertain publishing states cannot do this.
            reopen = self.page_job_id is None
            if reopen and job['stage'] != 'source_required':
                raise ValueError('本次微信会话无法核对旧发表任务，请先核对发表结果。')
            if not self.page or self.page.is_closed():
                if not reopen:
                    raise ValueError('微信会话已关闭，请重新连接公众号。')
                await self.open()
            try:
                await self._account()
                if reopen:
                    token = parse_qs(urlsplit(self.page.url).query).get('token',[''])[0]
                    await self.page.goto(job['remote_url'] + '&' + urlencode({'token':token}), wait_until='domcontentloaded')
                title = await self._title(wait=False)
            except NeedsUser as exc:
                raise ValueError(str(exc)) from exc
            if (await self._field_value(title)).strip() != job['snapshot']['title']:
                raise ValueError('当前草稿与任务不一致，未修改来源。')
            saved_id = parse_qs(urlsplit(job['remote_url']).query).get('appmsgid', [''])[0]
            current_id = parse_qs(urlsplit(self.page.url).query).get('appmsgid', [''])[0]
            if not saved_id or current_id != saved_id:
                raise ValueError('当前微信草稿编号与任务不一致，未修改来源。')
            dialogs = self.page.locator('.weui-desktop-dialog:visible, [role="dialog"]:visible')
            if reopen and not await dialogs.count():
                # This persisted checkpoint is before submission. Open its
                # source setting directly; WeChat may remember dismissal of
                # the notice and otherwise jump straight to final settings.
                await self._open_creation_source()
                for _ in range(20):
                    if await dialogs.count():
                        break
                    await asyncio.sleep(.2)
            if await dialogs.count() != 1 or not await self._source_dialog_kind(dialogs.first):
                raise ValueError('微信当前不是发表前的创作来源步骤，未重复提交任务。')
            self.page_job_id = job['id']
            self.store.confirm_creation_source(job['id'], value)
            self.needs_user = False

    async def _publish(self, job):
        # Stop after a final click until an explicit successful platform receipt.
        await self._account()
        await self._prepare_publication_receipt(job)
        self.store.update_job(job['id'], stage='publishing', step='publishing', message='正在提交发表，等待微信确认。')
        publish = await self._visible('button:has-text("发表"), a:has-text("发表"), button:has-text("发布")')
        if not publish or (await publish.inner_text()).strip() not in {'发表','发布'}:
            raise NeedsUser('当前页面没有明确的发表入口，请在微信草稿箱核对。')
        await publish.click()
        await asyncio.sleep(1)
        # Source selection is a prerequisite, distinct from the final submit.
        # A step is never clicked twice, even if the platform is slow to change.
        handled = set()
        last_step = ''
        submitted = False
        source = job['snapshot'].get('creation_source', 'unspecified')
        declared_labels = await self.page.locator('.js_claim_source_selected:visible').all_text_contents()
        has_ai_declaration = any(text.strip() == '内容由AI生成' for text in declared_labels)
        source_declared = ((source == 'ai' and has_ai_declaration)
                           or (source == 'non_ai' and not has_ai_declaration))
        for _ in range(40):
            if await self._publish_signal():
                if not self._load_receipt(self.store.job(job['id'])):
                    raise NeedsUser('缺少本次提交的关联记录，尚不能确认正式发表。请核对微信发表记录。')
                else:
                    if await self._observe_history_receipt(self.store.job(job['id']), force=True):
                        return
            if await self._verification_dialog() is not None:
                self._mark_verification(job)
                raise NeedsUser('微信要求管理员扫码验证。请使用管理本公众号的微信，在软件内扫码并在手机完成验证。')
            dialogs = self.page.locator('.weui-desktop-dialog:visible, [role="dialog"]:visible')
            if await dialogs.count() == 1:
                dialog_text = await dialogs.first.inner_text()
                if re.search(r'微信验证|管理员验证|验证码|扫码后|扫描.*二维码|二维码.*验证', dialog_text):
                    self._mark_verification(job)
                    raise NeedsUser('微信要求管理员扫码验证。请使用管理本公众号的微信，在软件内扫码并在手机完成验证。')
                kind = await self._publish_dialog_kind(dialogs.first)
                if kind not in handled and (not submitted or kind == 'notification_notice'):
                    try:
                        if kind in {'confirmation','notification_notice'} and source not in {'ai','non_ai'}:
                            cancel = await self._dialog_control(dialogs.first, '取消')
                            if cancel is None:
                                raise NeedsUser('需要确认创作来源，当前发表设置无法安全关闭。')
                            await cancel.click()
                            await self._open_creation_source()
                            raise CreationSourceRequired('请在软件内确认本篇创作来源，确认后将在同一篇草稿继续发表。')
                        if kind in {'confirmation','notification_notice'} and not source_declared:
                            # The platform remembers dismissal of its source
                            # notice. Never rely on it to label an AI article.
                            cancel = await self._dialog_control(dialogs.first, '取消')
                            if cancel is None:
                                raise NeedsUser('未确认创作来源且无法关闭发表设置，已停止提交。')
                            await cancel.click()
                            await self._open_creation_source()
                            last_step = 'source_form_opened'
                            await asyncio.sleep(.5)
                            continue
                        step = await self._publish_dialog_step(dialogs.first, source)
                    except CreationSourceRequired:
                        self.store.update_job(job['id'], stage='source_required', step='creation_source',
                                              message='请确认本篇文章的创作来源。')
                        raise
                    except NeedsUser:
                        if kind in {'source_notice','source_form'} and not submitted:
                            self.store.update_job(job['id'], stage='source_required', step='creation_source',
                                message='创作来源尚未完成，未提交发表；修复或确认后可在原草稿继续。')
                        raise
                    if step:
                        handled.add(kind)
                        last_step = step
                        submitted = step in {'submit','final_submit'}
                        if step in {'source_saved','source_continue'}:
                            source_declared = True
                        self.store.update_job(job['id'], step='receipt' if submitted else 'publishing',
                            message='已提交发表，正在等待微信成功回执。' if submitted else '正在按已确认的来源处理微信发表设置。')
            elif not submitted and not await dialogs.count() and last_step == 'source_open':
                await self._open_creation_source()
                last_step = 'source_form_opened'
            elif not submitted and not await dialogs.count() and last_step == 'source_saved':
                # Source form confirmation returns to the editor. The initial
                # publish entry is allowed once more only after that transition.
                await self._click_text('发表')
                last_step = 'source_publish_opened'
            elif submitted and not await dialogs.count():
                if await self._observe_history_receipt(self.store.job(job['id'])):
                    return
            await asyncio.sleep(0.5)
        raise NeedsUser('未收到明确的发表成功回执，可能需要扫码或等待审核。请核对微信后台后处理，不会自动重发。')

    async def execute(self, job):
        async with self.lock:
            self.active_job_id = job['id']
            self.needs_user = False
            self.editor_diagnostics = []
            self.cover_diagnostics = {}
            operation = '核对公众号登录'
            try:
                operation = '检查本地文章与图片'
                article = job['snapshot']
                try:
                    plan = article.get('_prepared') or prepare_snapshot(article,self.store.root/'assets')
                    prepared_html = verify_snapshot(plan,self.store.root/'assets')
                except ValueError as exc:
                    raise NeedsUser(str(exc)) from exc
                self.store.update_job(job['id'],step='account',message='正在后台连接微信并核对公众号。')
                operation = '核对公众号登录'
                if not self.page or self.page.is_closed():
                    # Queue work starts its own background execution surface.
                    # Login/verification exceptions are surfaced in the
                    # workbench and can request a visible window explicitly.
                    await self.open(force_visible=False)
                await self._account()
                self.page_job_id = job['id']
                article = job['snapshot']
                if job['stage'] == 'draft_saved':
                    if job['remote_url']:
                        token = parse_qs(urlsplit(self.page.url).query).get('token',[''])[0]
                        await self.page.goto(job['remote_url'] + '&' + urlencode({'token':token}), wait_until='domcontentloaded')
                    else:
                        title = await self._title(wait=False)
                        if (await self._field_value(title)).strip() != article['title']:
                            raise NeedsUser('请在微信打开此前保存的同一篇草稿，再继续任务。')
                else:
                    operation = '打开并识别微信编辑器'
                    self.store.update_job(job['id'],step='editor',message='正在打开并识别微信编辑器。')
                    if job['stage']=='resume_editor':
                        if self.resume_job_id!=job['id'] or self.resume_page is not self.page:
                            raise NeedsUser('浏览器会话已变化，请确认当前微信编辑页后，再点“继续填写当前页”。')
                        title, editor = await self._current_editor(job)
                    else:
                        token = parse_qs(urlsplit(self.page.url).query)['token'][0]
                        target = 'https://mp.weixin.qq.com/cgi-bin/appmsg?' + urlencode({'t':'media/appmsg_edit_v2','action':'edit','isNew':1,'type':77,'token':token,'lang':'zh_CN'})
                        await self.page.goto(target, wait_until='domcontentloaded')
                        title, editor = await self._editor_controls(article)
                    # Resolve the core editor before writing. Optional metadata
                    # must not prevent the title and body from being displayed.
                    # Editor may autosave, so checkpoint before the first fill.
                    self.store.update_job(job['id'], stage='editing', step='body', message='正在填写微信图文编辑器。')
                    operation = '填写文章标题'
                    self.store.update_job(job['id'],message='正在把文章标题填入微信。')
                    await self._fill_field(title, article['title'], '标题')
                    operation = '写入排版正文'
                    self.store.update_job(job['id'],message='标题已填写，正在写入排版正文。')
                    soup = BeautifulSoup(prepared_html, 'html.parser')
                    text_first = BeautifulSoup(str(soup), 'html.parser')
                    for img in text_first.find_all('img'):
                        img.decompose()
                    await self._insert_html(editor, wechat_compatible_html(str(text_first)))
                    files = list(dict.fromkeys([article['cover']] + [img['src'] for img in soup.find_all('img') if img.get('src','').startswith('/assets/')]))
                    files = [src for src in files if src]
                    uploads = {}
                    for index, src in enumerate(files, 1):
                        operation = f'上传图片 {index}/{len(files)}'
                        self.store.update_job(job['id'],step='images',message=f'正文已写入并回读核对，正在{operation}。')
                        uploads[src] = await self._upload(editor, self.store.root / 'assets' / src.rsplit('/',1)[-1])
                    # A second full-body insertion is only needed after an
                    # upload changes image URLs. Re-inserting an identical
                    # text-only body makes some WeChat editor builds rewrite
                    # the DOM a second time and causes a false readback
                    # mismatch, even though the first verified insertion is
                    # already complete.
                    if files:
                        for img in soup.find_all('img'):
                            if img.get('src') in uploads:
                                img['src'] = uploads[img['src']]
                        cover_url = uploads.get(article['cover'])
                        body_html = wechat_compatible_html(str(soup))
                        with_cover = '<p><img src="' + cover_url + '" style="width:100%;height:auto"></p>' + body_html if cover_url else body_html
                        operation = '写入并核对带图片的完整正文'
                        self.store.update_job(job['id'],message='正在写入并核对完整正文和图片。')
                        await self._insert_html(editor, with_cover)
                        if cover_url:
                            operation = '设置文章封面'
                            self.store.update_job(job['id'],step='images',message='正文已填写，正在设置微信封面。')
                            await self._cover(cover_url)
                            # Cover is selected; retain only the user's actual body.
                            operation = '核对最终正文'
                            await self._insert_html(editor, body_html)
                    if not (await editor.inner_text()).strip():
                        raise NeedsUser('微信编辑器正文为空，已停止保存。')
                    operation = '填写作者与摘要'
                    self.store.update_job(job['id'],message='正文已填写，正在填写作者与摘要。')
                    await self._fill_metadata(article)
                    operation = '保存微信草稿'
                    await self._save(job)
                if job['action'] == 'publish':
                    operation = '发表文章'
                    await self._publish(self.store.job(job['id']))
                else:
                    self.store.update_job(job['id'], status='drafted', step='done', message='已保存到微信草稿箱，尚未发表。')
            except Exception as exc:
                current = self.store.job(job['id'])
                self.needs_user = True
                evidence = ''
                # Record actionable code locations without logging login URLs,
                # tokens, article contents or Playwright's raw call arguments.
                frames = [{'file':frame.filename.replace('\\', '/').rsplit('/', 1)[-1],
                           'line':frame.lineno, 'function':frame.name}
                          for frame in traceback.extract_tb(exc.__traceback__)]
                with contextlib.suppress(Exception):
                    (self.store.root / 'evidence' / (job['id'] + '.json')).write_text(
                        json.dumps({'operation':operation, 'exception':type(exc).__name__,
                                    'stage':current['stage'], 'frames':frames,
                                    'editor_candidates':self.editor_diagnostics,
                                    'readback':self.readback_diagnostics,
                                    'cover_controls':self.cover_diagnostics,
                                    'page_path':urlsplit(self.page.url).path if self.page else '',
                                    'time':now()}, ensure_ascii=False, indent=2), encoding='utf-8')
                if self.page and not self.page.is_closed():
                    try:
                        name = job['id'] + '.png'
                        await self.page.screenshot(path=str(self.store.root / 'evidence' / name))
                        evidence = '/evidence/' + name
                    except Exception:
                        pass
                safe = current['stage'] in {'','draft_saved','source_required'}
                message = (f'在“{operation}”时暂停：{exc}' if isinstance(exc, NeedsUser)
                           else f'在“{operation}”时失败（{type(exc).__name__}）。已保留现场截图和诊断记录，当前任务不会自动重复提交。')
                message = message.replace('微信窗口','软件内的连接面板').replace('请检查浏览器','请查看处理面板')
                self.last_message = message
                self.store.update_job(job['id'], status='waiting_user' if safe else 'needs_review', message=message, evidence=evidence)
            else:
                self.needs_user = False
                self.last_message = '任务已完成，微信后台继续保持后台运行。'
            finally:
                self.active_job_id = None

    async def verify(self, job):
        if self.lock.locked():
            raise ValueError('当前任务仍在执行，请稍候。')
        async with self.lock:
            return await self._verify_receipt_locked(job)

    async def _verify_receipt_locked(self, job):
        if not self.page or self.page.is_closed():
            raise ValueError('请先打开微信窗口。')
        if not self.receipt_trackable(job):
            raise ValueError('本次浏览器会话不能关联到该任务，请到微信后台核对后手动记录结果。')
        if job['status'] not in {'needs_review','review_pending'} or job['stage'] != 'publishing':
            raise ValueError('请先在微信后台核对，并使用“记录已核对结果”。')
        title = await self._visible('#title') if self.page_job_id == job['id'] else None
        if title and (await self._field_value(title)).strip() != job['snapshot']['title']:
            raise ValueError('当前浏览器文章与任务不一致。')
        if self.page_job_id == job['id'] and await self._publish_signal():
            if self._load_receipt(job):
                await self._observe_history_receipt(job, force=True)
                return self.store.job(job['id'])['status'] == 'published'
            raise ValueError('缺少本次提交的关联记录，页面提示尚不能证明正式发表。请核对微信发表记录。')
        await self._observe_history_receipt(job)
        return self.store.job(job['id'])['status'] == 'published'

    async def refresh_verification(self, job, *, previous_not_confirmed=False):
        if self.lock.locked():
            raise ValueError('微信正在处理任务，请稍后刷新验证码。')
        async with self.lock:
            current = self.store.job(job['id'])
            if (current['action'],current['status'],current['stage'],current['step']) != (
                    'publish','needs_review','publishing','verification'):
                raise ValueError('当前任务不在等待管理员扫码，未重新提交。')
            receipt = self._load_receipt(current)
            if not receipt or self.page_job_id not in {None,job['id']}:
                raise ValueError('当前验证页面不能关联到原任务，未重新提交。')
            recovering = self.page_job_id is None or not self.page or self.page.is_closed()
            if recovering and not receipt.get('verification_pending'):
                raise ValueError('没有已确认的管理员验证记录，不能恢复验证码。')
            if not self.page or self.page.is_closed():
                await self.open()
            await self._account()
            if await self._observe_history_receipt(current,force=True):
                return
            receipt = self._load_receipt(current)
            history, rows = await self._history_view()
            try:
                from .receipts import text_key
                if receipt.get('record_id') or any(
                        row['id'] not in receipt['baseline_ids'] and text_key(row['title'])==receipt['title']
                        for row in rows):
                    raise ValueError('发表记录已有相关结果，已停止刷新验证码，请核对记录。')
            finally:
                await history.close()
            verification = await self._verification_dialog()
            recovering = recovering or verification is None
            if recovering and not receipt.get('verification_pending'):
                raise ValueError('没有已确认的管理员验证记录，不能恢复验证码。')
            if recovering and not previous_not_confirmed:
                raise VerificationRecoveryRequired('验证会话已中断。只有确认上次未在手机完成发表，才可恢复二维码；已确认时请先核对发表记录。')
            remote_id = parse_qs(urlsplit(current['remote_url']).query).get('appmsgid',[''])[0]
            if recovering:
                current_id = parse_qs(urlsplit(self.page.url).query).get('appmsgid',[''])[0]
                if current_id and current_id != remote_id:
                    raise ValueError('当前页面是其他草稿，请先返回公众号首页再恢复验证码。')
                token = parse_qs(urlsplit(self.page.url).query).get('token',[''])[0]
                if not token or not remote_id:
                    raise ValueError('登录或原草稿已失效，不能恢复验证码。')
                await self.page.goto(current['remote_url']+'&'+urlencode({'token':token}),wait_until='domcontentloaded')
                title, editor = await self._editor_controls(current['snapshot'])
                from .receipts import body_hash
                if body_hash(await editor.inner_html()) != receipt['body_sha256']:
                    raise ValueError('已保存的草稿正文发生变化，未恢复验证码。')
                if sorted(self._receipt_image_keys(await editor.inner_html())) != sorted(receipt['images']):
                    raise ValueError('已保存的草稿图片发生变化，未恢复验证码。')
            title = await self._title(wait=False)
            if (await self._field_value(title)).strip()!=current['snapshot']['title']:
                raise ValueError('当前文章与原任务不同，未刷新验证码。')
            if not remote_id or parse_qs(urlsplit(self.page.url).query).get('appmsgid',[''])[0]!=remote_id:
                raise ValueError('草稿编号变化，未刷新验证码。')
            source=current['snapshot'].get('creation_source')
            labels=await self.page.locator('.js_claim_source_selected:visible').all_text_contents()
            if source not in {'ai','non_ai'} or (source=='ai')!=any(x.strip()=='内容由AI生成' for x in labels):
                raise ValueError('原文章创作来源变化，已停止刷新验证码。')
            if recovering:
                self.page_job_id=job['id']
                await self._click_text('发表')
            else:
                if verification is None:
                    raise ValueError('当前不是微信管理员验证页面，请先核对发表结果。')
                dedicated = await self._dialog_control(verification,'刷新二维码')
                if dedicated is not None:
                    await dedicated.click()
                    return
                closes = verification.locator('.weui-desktop-dialog__close-btn:visible')
                if await closes.count()!=1:
                    raise ValueError('微信验证页面没有可安全刷新的入口。')
                await closes.click()
            dialogs = self.page.locator('.weui-desktop-dialog:visible, [role="dialog"]:visible')
            handled=set()
            for _ in range(30):
                if await self._verification_dialog() is not None:
                    self._mark_verification(current)
                    self.store.log('已更新原任务的管理员验证二维码，请扫码并在手机确认。',job['id'])
                    return
                if await dialogs.count()==1:
                    dialog=dialogs.first
                    kind=await self._publish_dialog_kind(dialog)
                    if kind not in {'confirmation','notification_notice'} or kind in handled:
                        if kind in handled:
                            await asyncio.sleep(.3)
                            continue
                        raise ValueError('微信验证步骤变化，请查看处理面板，未重复发送。')
                    labels=await self.page.locator('.js_claim_source_selected:visible').all_text_contents()
                    is_ai=any(x.strip()=='内容由AI生成' for x in labels)
                    source=current['snapshot'].get('creation_source')
                    if source not in {'ai','non_ai'} or (source=='ai')!=is_ai:
                        raise ValueError('原文章创作来源未确认，已停止刷新验证码。')
                    step=await self._publish_dialog_step(dialog,source)
                    if not step:
                        raise ValueError('未找到明确的原文章验证入口。')
                    handled.add(kind)
                await asyncio.sleep(.4)
            raise ValueError('微信尚未显示新的验证码，请核对当前处理面板。')
