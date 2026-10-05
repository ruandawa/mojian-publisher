"""Observe public publication pages without repeating a submission."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import unicodedata
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .store import now
from .image_receipts import frozen_body_images, image_content_matches


def text_key(value):
    value = unicodedata.normalize('NFKC', value or '')
    value = value.translate(str.maketrans({'“':'"','”':'"','‘':"'",'’':"'"}))
    return re.sub(r'[\s\u200b\ufeff]+', '', value)


def body_hash(markup):
    return hashlib.sha256(text_key(BeautifulSoup(markup, 'html.parser').get_text()).encode()).hexdigest()


def snapshot_hash(job):
    return hashlib.sha256(json.dumps(job['snapshot'], sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def public_article_url(value, record_id=''):
    try:
        parts = urlsplit(value)
        query = parse_qs(parts.query)
    except ValueError:
        return ''
    if parts.hostname != 'mp.weixin.qq.com' or parts.scheme not in {'http','https'}:
        return ''
    if parts.username or parts.password or parts.netloc not in {'mp.weixin.qq.com','mp.weixin.qq.com:443'}:
        return ''
    # A signed preview is readable only for a session and is not publication.
    if 'tempkey' in query or 'token' in query:
        return ''
    # Published history can expose WeChat's 22-character public short link.
    # Its record association comes from select_record; content still requires
    # an independent anonymous title/body/image check before completion.
    if re.fullmatch(r'/s/[A-Za-z0-9_-]{22}', parts.path) and not parts.query:
        return urlunsplit(('https', 'mp.weixin.qq.com', parts.path, '', ''))
    if parts.path != '/s' or not all(query.get(k) for k in ('__biz','mid','idx','sn')):
        return ''
    if record_id and query['mid'] != [record_id]:
        return ''
    return urlunsplit(('https', 'mp.weixin.qq.com', '/s', parts.query, ''))


def select_record(records, receipt):
    candidates = []
    for row in records:
        if not re.fullmatch(r'\d+', row.get('id','')):
            continue
        if receipt.get('record_id'):
            if row['id'] != receipt['record_id']:
                continue
        elif row['id'] in receipt['baseline_ids']:
            continue
        if text_key(row['title']) != receipt['title']:
            continue
        try:
            sent = int(row['sent_at'])
            started = datetime.fromisoformat(receipt['started_at']).timestamp()
        except (ValueError, KeyError, TypeError):
            continue
        if not started-10 <= sent <= time.time()+60:
            continue
        candidates.append(row)
    return candidates[0] if len(candidates) == 1 else None


class PublicationReceiptMixin:
    def _receipt_path(self, job):
        return self.store.root / 'evidence' / (job['id']+'-receipt.json')

    def _load_receipt(self, job):
        try:
            item = json.loads(self._receipt_path(job).read_text(encoding='utf-8'))
            valid = (item.get('schema') == 1 and item['job_id'] == job['id']
                     and item['snapshot_sha256'] == snapshot_hash(job)
                     and item['title'] == text_key(job['snapshot']['title'])
                     and item['remote_url'] == job['remote_url']
                     and item['account_name'] == self.store.settings()['account_name'].strip()
                     and isinstance(item['baseline_ids'], list)
                     and all(isinstance(x,str) and x.isdigit() for x in item['baseline_ids'])
                     and isinstance(item['images'], list)
                     and re.fullmatch(r'[a-f0-9]{64}',item['body_sha256'])
                     and datetime.fromisoformat(item['started_at']).tzinfo is not None)
            return item if valid else None
        except (OSError, ValueError, TypeError, KeyError):
            return None

    def _write_receipt(self, job, item):
        path = self._receipt_path(job)
        staging = path.with_suffix('.tmp')
        staging.write_text(json.dumps(item,ensure_ascii=False,indent=2),encoding='utf-8')
        staging.replace(path)

    def receipt_trackable(self, job):
        return bool(self.page and not self.page.is_closed() and (
            self.page_job_id == job['id'] or (self.context and self._load_receipt(job))))

    async def _history_view(self):
        if not self.context or not self.page or self.page.is_closed():
            raise ValueError('微信连接已关闭，请在软件内重新连接后核对结果。')
        token = parse_qs(urlsplit(self.page.url).query).get('token',[''])[0]
        if not token:
            raise ValueError('公众号登录已失效，请重新连接后核对结果。')
        page = await self.context.new_page()
        try:
            # This is the ordinary UI URL observed on the 发表记录 navigation
            # link. No private JSON endpoint or credential extraction is used.
            await page.goto('https://mp.weixin.qq.com/cgi-bin/appmsgpublish?'+urlencode(
                {'sub':'list','begin':0,'count':10,'lang':'zh_CN','token':token}),
                wait_until='domcontentloaded',timeout=20000)
            await page.get_by_text('发表记录',exact=True).first.wait_for(timeout=12000)
            # Wait for records or the empty-state message, not just the sidebar.
            for _ in range(40):
                if await page.locator('.publish_hover_content:visible').count():
                    break
                text = await page.locator('body').inner_text()
                if re.search(r'共发表了\s*0\s*次|暂无发表记录|暂未发表|还没有发表记录',text):
                    break
                await asyncio.sleep(.25)
            else:
                raise ValueError('微信发表记录尚未加载完成，未推断发表结果。')
            rows = await page.locator('.publish_hover_content:visible').evaluate_all('''els=>els.map(e=>{
              const a=e.querySelector('a.weui-desktop-mass-appmsg__title');
              const stat=e.querySelector('.weui-desktop-mass__status_text');
              const info=e.querySelector('a.appmsg-underline[href]');
              const u=info?new URL(info.getAttribute('href'),location.origin):null;
              return {title:a?.innerText||'',href:a?.href||'',state:stat?.innerText.trim()||'',
                id:u?.searchParams.get('appmsg_id')||'',sent_at:u?.searchParams.get('send_time')||''};
            })''')
            # Refuse a new baseline if the page layout contains unidentifiable
            # records: an incomplete baseline cannot prove which send is new.
            if any(not row['id'].isdigit() or not row['title'] for row in rows):
                raise ValueError('微信发表记录结构变化，无法唯一核对文章，已停止自动判断。')
            return page, rows
        except Exception:
            await page.close()
            raise

    async def _prepare_publication_receipt(self, job):
        fresh = self.store.job(job['id'])
        if self._load_receipt(fresh):
            return
        title, editor = await self._editor_controls(fresh['snapshot'])
        if text_key(await self._field_value(title)) != text_key(fresh['snapshot']['title']):
            raise ValueError('当前草稿标题不一致，未提交发表。')
        remote_id = parse_qs(urlsplit(fresh['remote_url']).query).get('appmsgid',[''])[0]
        current_id = parse_qs(urlsplit(self.page.url).query).get('appmsgid',[''])[0]
        if not remote_id or remote_id != current_id:
            raise ValueError('当前草稿编号不一致，未提交发表。')
        markup = await editor.inner_html()
        history, rows = await self._history_view()
        try:
            item = {'schema':1,'job_id':job['id'],'snapshot_sha256':snapshot_hash(fresh),
                    'account_name':self.store.settings()['account_name'].strip(),
                    'title':text_key(fresh['snapshot']['title']),'remote_url':fresh['remote_url'],
                    'baseline_ids':[row['id'] for row in rows], 'started_at':now(),
                    'body_sha256':body_hash(markup),'images':self._receipt_image_keys(markup),
                    'record_id':'','last_result':''}
            self._write_receipt(fresh,item)
        finally:
            await history.close()

    async def _load_public_images(self, page, *, timeout_seconds=20):
        """Expose lazy images and require decoded pixels before evidence capture."""
        deadline = time.monotonic() + timeout_seconds
        try:
            for image in await page.locator('#js_content img').all():
                # Use the same decoration exclusion as the receipt comparison.
                # An actual image with a missing URL is never silently skipped.
                keys = self._receipt_image_keys(await image.evaluate('e=>e.outerHTML'))
                if not keys:
                    continue
                if not keys[0]:
                    return False
                remaining = int((deadline - time.monotonic()) * 1000)
                if remaining <= 0:
                    return False
                await image.scroll_into_view_if_needed(timeout=min(3000, remaining))
                remaining = int((deadline - time.monotonic()) * 1000)
                if remaining <= 0:
                    return False
                image_deadline = min(deadline, time.monotonic() + 5)
                while True:
                    loaded = await image.evaluate('''image=>({complete:image.complete,
                        width:image.naturalWidth,height:image.naturalHeight,
                        source:image.currentSrc||image.src||''})''')
                    # WeChat's lazy SVG placeholder is also complete and has
                    # positive dimensions. Only the decoded network resource
                    # named by this article image proves the picture loaded.
                    if (loaded['complete'] and loaded['width'] > 0 and loaded['height'] > 0
                            and urlsplit(loaded['source']).scheme in {'http', 'https'}
                            and self._receipt_image_key(loaded['source']) == keys[0]):
                        break
                    remaining = image_deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                    await asyncio.sleep(min(.1, remaining))
            return True
        except Exception:
            # This only postpones the public receipt. It never resubmits a send.
            return False
        finally:
            try:
                await page.evaluate('window.scrollTo(0,0)')
                await page.evaluate('()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))')
            except Exception:
                pass

    async def _read_public_article(self, url, receipt):
        # Confirm the ordinary public link from a context with no login cookies.
        anonymous = await self.context.browser.new_context(viewport={'width':390,'height':844})
        try:
            page = await anonymous.new_page()
            await page.goto(url,wait_until='domcontentloaded',timeout=20000)
            if not public_article_url(page.url,receipt.get('record_id','')):
                return False
            await page.locator('#js_content').wait_for(state='visible',timeout=10000)
            await page.locator('#activity-name').wait_for(state='visible',timeout=10000)
            title = await page.locator('#activity-name').inner_text()
            body = await page.locator('#js_content').inner_html()
            match = text_key(title) == receipt['title'] and body_hash(body) == receipt['body_sha256']
            if match:
                match = await self._public_images_match(anonymous, body, receipt)
            if match:
                match = await self._load_public_images(page)
            if match:
                await page.screenshot(path=str(self.store.root/'evidence'/(receipt['job_id']+'-public-mobile.png')),full_page=True)
            return match
        except Exception:
            return False
        finally:
            await anonymous.close()

    async def _public_image_bytes(self, context, url):
        """Read only public WeChat CDN images, with bounded trusted redirects."""
        current = url
        for _ in range(4):
            parts = urlsplit(current)
            if (parts.scheme != 'https' or parts.hostname not in {'mmbiz.qpic.cn','mmbiz.qlogo.cn'}
                    or parts.username or parts.password or parts.port not in {None,443}):
                raise ValueError('公开图片不在微信图片服务器。')
            response = await context.request.get(current, timeout=10000, max_redirects=0)
            try:
                if response.status in {301,302,303,307,308}:
                    location = response.headers.get('location','')
                    if not location:
                        raise ValueError('公开图片重定向没有地址。')
                    current = urljoin(current,location)
                    continue
                if not response.ok or not response.headers.get('content-type','').lower().startswith('image/'):
                    raise ValueError('公开图片尚不可访问。')
                size = response.headers.get('content-length','')
                if size.isdigit() and int(size) > 10*1024*1024:
                    raise ValueError('公开图片过大。')
                raw = await response.body()
                if len(raw) > 10*1024*1024:
                    raise ValueError('公开图片过大。')
                return raw
            finally:
                await response.dispose()
        raise ValueError('公开图片重定向次数过多。')

    async def _public_images_match(self, context, body, receipt):
        actual = self._receipt_image_keys(body)
        expected = receipt['images']
        # Preserve position and duplicate count. Matching stable URLs need no
        # extra network request; all images must still decode before capture.
        if actual == expected:
            return '' not in actual
        if len(actual) != len(expected) or '' in actual or '' in expected:
            return False
        try:
            job = self.store.job(receipt['job_id'])
            local = frozen_body_images(job['snapshot'], self.store.root/'assets')
            if len(local) != len(actual):
                return False
            urls = []
            for tag in BeautifulSoup(body,'html.parser').find_all('img'):
                keys = self._receipt_image_keys(str(tag))
                if not keys:
                    continue
                value = next((tag.get(name) for name in ('data-src','src','data-original','data-lazy-src','data-url')
                              if self._receipt_image_key(tag.get(name)) == keys[0]), '')
                urls.append(value)
            async def compare():
                for index, (before, after) in enumerate(zip(expected,actual)):
                    if before == after:
                        continue
                    parts = urlsplit(urls[index])
                    # Only the observed official watermark rewrite permits a
                    # changed resource identity; unknown rewrites stay pending.
                    if parse_qs(parts.query).get('watermark') != ['1']:
                        return False
                    raw = await self._public_image_bytes(context,urls[index])
                    original = local[index]['path'].read_bytes()
                    if hashlib.sha256(original).hexdigest() != local[index]['sha256']:
                        return False
                    if not await asyncio.to_thread(image_content_matches,original,raw,allow_watermark=True):
                        return False
                return True
            return await asyncio.wait_for(compare(),timeout=30)
        except Exception:
            return False

    async def _observe_history_receipt(self, job, *, force=False):
        receipt = self._load_receipt(job)
        if not receipt or not self.context:
            return False
        checked = getattr(self,'receipt_checked',{})
        if not force and time.monotonic()-checked.get(job['id'],0)<10:
            return False
        checked[job['id']] = time.monotonic()
        self.receipt_checked = checked
        await self._account()
        history, rows = await self._history_view()
        try:
            row = select_record(rows,receipt)
            if not row:
                return False
            receipt['record_id'] = row['id']
            receipt['record_sent_at'] = row['sent_at']
            receipt['record_state'] = row['state']
            url = public_article_url(row['href'],row['id'])
            if row['state'] in {'发表失败','发布失败','审核不通过','审核未通过'}:
                status, stage, step = 'failed','platform_rejected','done'
                message = '微信审核未通过：发表记录显示“'+row['state']+'”。内容仍保留在本机，工具不会自动重发。'
            elif url and row['state'] not in {'审核中','待审核'} and await self._read_public_article(url,receipt):
                status, stage, step = 'published','published','done'
                message = '已核对微信发表记录及公开文章：正文完整、配图对应且已加载，文章可访问。'
            else:
                status, stage, step = 'review_pending','publishing','review'
                if row['state'] in {'审核中','待审核'}:
                    message = '已提交，微信审核中；软件会继续核对发表记录，不会重复发送。'
                elif row['state'] in {'已发表','发表成功','发布成功'}:
                    message = '微信显示已发表，正在核对公开正文与配图；软件不会重复发送。'
                else:
                    message = '已提交，正在等待微信公开文章回执；软件不会重复发送。'
            receipt['last_result'] = status
            self._write_receipt(job,receipt)
            current = self.store.job(job['id'])
            if current['status'] != status or current['message'] != message:
                name = job['id']+'-'+status+'.png'
                await history.screenshot(path=str(self.store.root/'evidence'/name),timeout=8000)
                self.store.update_job(job['id'],status=status,stage=stage,step=step,
                    message=message,evidence='/evidence/'+name,
                    article_url=url if status=='published' else '')
            self.needs_user = False
            return True
        finally:
            await history.close()
