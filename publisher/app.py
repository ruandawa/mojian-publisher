from __future__ import annotations

import asyncio
import contextlib
import os
import re
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from . import ai
from .browser import BrowserDriver, VerificationRecoveryRequired
from .rendering import THEMES, export_document, image_warnings, local_images, make_cover, render, save_image, validate_body
from .importing import MAX_IMPORT, import_document, materialize
from .preparation import prepare_snapshot, preview_plan
from .store import CST, ROOT, Conflict, Store
from .version import VERSION


class ArticleInput(BaseModel):
    title: str = Field(min_length=1, max_length=64)
    author: str = Field(default='', max_length=8)
    digest: str = Field(default='', max_length=120)
    body: str = Field(min_length=1, max_length=50000)
    format: str = 'markdown'
    theme: str = 'jade'
    cover: str = ''
    source: str = 'manual'
    creation_source: Literal['unspecified','ai','non_ai'] = 'unspecified'
    revision: int | None = None


class PreviewInput(BaseModel):
    body: str = Field(default='', max_length=50000)
    format: str = 'markdown'
    theme: str = 'jade'
    cover: str = Field(default='',max_length=2000)


class SettingsInput(BaseModel):
    account_name: str = Field(default='', max_length=120)
    author: str = Field(default='', max_length=8)
    theme: str = 'jade'
    ai_base_url: str = Field(default='https://api.deepseek.com', max_length=500)
    ai_model: str = Field(default='deepseek-chat', max_length=120)
    ai_instructions: str = Field(default='自然、具体，不编造事实。', max_length=3000)
    ai_api_key: str | None = Field(default='', max_length=2000)
    automation_enabled: bool = False
    default_action: str = 'draft'
    browser_mode: str = 'background'


class QueueInput(BaseModel):
    article_id: str
    action: str = 'draft'
    scheduled_at: str = ''
    run_now: bool = False


class GenerateInput(BaseModel):
    topic: str = Field(min_length=1, max_length=500)
    notes: str = Field(default='', max_length=30000)
    length: int = Field(default=1200, ge=300, le=4000)


class ResolveInput(BaseModel):
    status: str
    confirmed: bool = False
    article_url: str = ''


class CreationSourceInput(BaseModel):
    creation_source: Literal['ai','non_ai']


class ExitInput(BaseModel):
    confirmed: bool = False


class WindowStateInput(BaseModel):
    dirty: bool = False


class VerificationRefreshInput(BaseModel):
    previous_not_confirmed: bool = Field(default=False, strict=True)


class PanelActionInput(BaseModel):
    frame_id: str = Field(min_length=1, max_length=100)
    kind: Literal['click','scroll','key','text']
    x: float = Field(default=0, ge=0, le=1, allow_inf_nan=False)
    y: float = Field(default=0, ge=0, le=1, allow_inf_nan=False)
    delta: int = Field(default=0, ge=-700, le=700)
    key: str = Field(default='', max_length=20)
    text: str = Field(default='', max_length=1000)


async def poll_publish_receipt(store, browser):
    """Restore a proven submission's session once, then only observe receipts."""
    if browser.lock.locked() or browser.active_job_id:
        return False
    jobs = store.jobs()
    if any(job['status'] == 'running' for job in jobs):
        return False
    eligible = [job for job in jobs if job['action'] == 'publish'
                and job['stage'] == 'publishing' and (
                    job['status'] == 'needs_review' and job['step'] in {'verification','receipt'}
                    or job['status'] == 'review_pending' and job['step'] == 'review')]
    if not eligible:
        return False
    if browser.context is None or not browser.page or browser.page.is_closed():
        if getattr(browser, '_receipt_connection_attempted', False) is True:
            return False
        # Only the driver's complete schema/snapshot/account/draft validation
        # can authorize restoring its persistent session. Queued jobs or a
        # merely related page are never sufficient to open a browser here.
        baselined = []
        for job in eligible:
            receipt = browser._load_receipt(job)
            if isinstance(receipt, dict) and receipt:
                baselined.append(job)
        if not baselined:
            return False
        async with browser.lock:
            if browser.active_job_id or any(job['status'] == 'running' for job in store.jobs()):
                return False
            if browser.context is None or not browser.page or browser.page.is_closed():
                if getattr(browser, '_receipt_connection_attempted', False) is True:
                    return False
                browser._receipt_connection_attempted = True
                try:
                    # open() uses the existing headless persistent profile. It
                    # does not execute a job, submit a send or restore a QR.
                    await browser.open()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    return False
        if browser.context is None or not browser.page or browser.page.is_closed():
            return False
    eligible.sort(key=lambda job: job['id'] != browser.page_job_id)
    confirmed = False
    for job in eligible:
        if browser.lock.locked() or browser.active_job_id:
            break
        try:
            if not browser.receipt_trackable(job):
                continue
            confirmed = bool(await browser.verify(job)) or confirmed
        except ValueError:
            # Page changes and unknown historical records do not authorize a
            # second submission or alter the scheduler's mutation settings.
            continue
    return confirmed


def create_app(store=None):
    store = store or Store()
    token = secrets.token_urlsafe(32)
    browser = BrowserDriver(store)
    browser._receipt_connection_attempted = False
    generation_lock = asyncio.Lock()
    direct_tasks = set()

    def run_now(job_id, *, preserve_schedule=False):
        if browser.interacting:
            raise Conflict('请先关闭微信处理面板，再执行任务。')
        if browser.lock.locked():
            raise Conflict('微信正在处理任务，请等待当前步骤完成。')
        job = store.claim_now(job_id, preserve_schedule=preserve_schedule)
        task = asyncio.create_task(browser.execute(job))
        direct_tasks.add(task)
        task.add_done_callback(direct_tasks.discard)

    async def scheduler():
        while True:
            try:
                # Receiving the result of an existing submission is independent
                # of the switch that starts new scheduled mutations. The panel
                # can stay open for the administrator to complete verification.
                await poll_publish_receipt(store, browser)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Transient page/network failures leave the pending submission
                # unchanged; never turn them into a retry or stop the queue.
                pass
            try:
                if store.settings()['automation_enabled'] and not browser.interacting and not browser.lock.locked():
                    jobs = store.jobs()
                    if not any(j['status'] in {'waiting_user','needs_review','running'} for j in jobs):
                        job = store.claim_due()
                        if job:
                            await browser.execute(job)
            except asyncio.CancelledError:
                raise
            except Exception:
                store.save_settings({'automation_enabled':False})
                store.log('执行器发生异常，已暂停自动化。请检查任务日志。', level='error')
            await asyncio.sleep(2)

    @asynccontextmanager
    async def lifespan(app):
        store.recover()
        worker = asyncio.create_task(scheduler())
        try:
            yield
        finally:
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker
            for task in list(direct_tasks):
                task.cancel()
            if direct_tasks:
                await asyncio.gather(*direct_tasks, return_exceptions=True)
            await browser.close()
            store.recover()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store, app.state.browser, app.state.csrf = store, browser, token
    app.state.generation_lock = generation_lock
    app.state.window_dirty = False

    @app.middleware('http')
    async def local_only(request, call_next):
        host = request.headers.get('host', '').split(':')[0]
        if host not in {'127.0.0.1','localhost'}:
            return JSONResponse({'error':'仅允许本机访问'}, status_code=403)
        origin = request.headers.get('origin')
        if origin and origin != str(request.base_url).rstrip('/'):
            return JSONResponse({'error':'拒绝跨站请求'}, status_code=403)
        if request.headers.get('sec-fetch-site') == 'cross-site':
            return JSONResponse({'error':'拒绝跨站请求'}, status_code=403)
        if request.url.path.startswith('/api/') and request.url.path not in {'/api/health','/api/bootstrap'}:
            if not secrets.compare_digest(request.headers.get('x-mojian-token',''), token):
                return JSONResponse({'error':'会话已更新，请刷新工作台。'}, status_code=403)
        length = request.headers.get('content-length','0')
        limit = MAX_IMPORT + 1024 * 1024 if request.url.path == '/api/import' else 12 * 1024 * 1024
        if not length.isdigit() or int(length) > limit:
            return JSONResponse({'error':'上传内容过大'}, status_code=413)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob: https://mmbiz.qpic.cn https://mmbiz.qlogo.cn; connect-src 'self'; frame-src 'self' blob:; base-uri 'none'; form-action 'self'"
        return response

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        content = {'error':str(exc)}
        if isinstance(exc, VerificationRecoveryRequired):
            content['code'] = 'verification_recovery_required'
        return JSONResponse(content, status_code=409 if isinstance(exc, Conflict) else 400)

    @app.exception_handler(KeyError)
    async def not_found(request, exc):
        return JSONResponse({'error':str(exc.args[0])}, status_code=404)

    @app.get('/api/health')
    async def health():
        return {'app':'mojian-publisher','version':VERSION,'ok':True,'pid':os.getpid(),
                'desktop_window':getattr(app.state,'desktop_window',False)}

    @app.post('/api/system/show')
    async def show_window():
        callback = getattr(app.state, 'show_window', None)
        if not callback:
            raise ValueError('程序窗口尚未就绪，请稍后再次打开。')
        asyncio.get_running_loop().call_later(0.1, callback)
        return {'ok':True}

    @app.post('/api/system/exit')
    async def exit_app(values: ExitInput | None = None):
        callback = getattr(app.state,'request_exit',None)
        if not callback:
            raise ValueError('当前为开发预览，请从启动它的终端停止服务。')
        asyncio.get_running_loop().call_later(0.5,lambda:callback(confirmed=bool(values and values.confirmed)))
        return {'ok':True}

    @app.post('/api/system/window-state')
    async def window_state(values: WindowStateInput):
        app.state.window_dirty = values.dirty
        return {'ok':True}

    @app.get('/api/bootstrap')
    async def bootstrap():
        return {'token':token, 'version':VERSION}

    @app.get('/api/state')
    async def state():
        return {'version':VERSION,'articles':store.articles(), 'jobs':store.jobs(), 'settings':store.settings(), 'events':store.events(),
                'browser':await browser.status(), 'themes':THEMES, 'data_dir':str(store.root), 'time':datetime.now(CST).isoformat()}

    def validate_article(item):
        values = item.model_dump()
        values['title'] = values['title'].strip()
        if not values['title']:
            raise ValueError('请填写文章标题。')
        validate_body(values['body'], values['format'])
        if values['theme'] not in THEMES:
            raise ValueError('无效排版主题')
        if values['cover'] and not (re.fullmatch(r'/assets/[0-9a-f]{32}\.jpg',values['cover']) and (store.root / 'assets' / values['cover'].rsplit('/',1)[-1]).is_file()):
            raise ValueError('封面不存在，请重新上传。')
        return values

    @app.post('/api/articles')
    async def create_article(item:ArticleInput):
        return store.save_article(validate_article(item))

    @app.put('/api/articles/{article_id}')
    async def update_article(article_id:str, item:ArticleInput):
        values = validate_article(item)
        # Older clients omit this field. Editing their title/body must not
        # silently erase an already confirmed per-article declaration.
        if 'creation_source' not in item.model_fields_set:
            values.pop('creation_source', None)
        return store.save_article(values, article_id)

    @app.post('/api/preview')
    async def preview(item:PreviewInput):
        if item.format not in {'markdown','html'} or item.theme not in THEMES:
            raise ValueError('无效排版选项')
        return preview_plan(item.body,item.format,item.theme,store.root / 'assets')

    @app.post('/api/prepare')
    async def prepare_article(item:PreviewInput):
        if item.theme not in THEMES:
            raise ValueError('无效排版选项')
        prepared = await asyncio.to_thread(materialize,item.body,item.format,store.root / 'assets',cover=item.cover)
        prepared['html'] = preview_plan(prepared['body'],'html',item.theme,store.root/'assets')['html']
        return prepared

    @app.post('/api/upload')
    async def upload(file:UploadFile=File(...)):
        raw = await file.read(10 * 1024 * 1024 + 1)
        return {'url':save_image(raw, store.root / 'assets')}

    @app.post('/api/import')
    async def import_article(file:UploadFile=File(...)):
        raw = await file.read(MAX_IMPORT + 1)
        return await asyncio.to_thread(import_document,raw,file.filename or '导入文章.md',store.root / 'assets',store.settings()['theme'])

    @app.post('/api/cover')
    async def cover(item:ArticleInput):
        values = validate_article(item)
        return {'url':make_cover(values['title'], values['theme'], store.root / 'assets')}

    @app.put('/api/settings')
    async def settings(item:SettingsInput):
        values = item.model_dump()
        values['ai_base_url'] = ai.validate_base_url(values['ai_base_url'])
        if (values['theme'] not in THEMES or values['default_action'] not in {'draft','publish'}
                or values['browser_mode'] != 'background'):
            raise ValueError('无效设置项')
        if values['automation_enabled'] and not values['account_name'].strip():
            raise ValueError('启动自动化前，请填写公众号名称。')
        return store.save_settings(values)

    @app.post('/api/automation/{action}')
    async def automation(action:str):
        if action not in {'start','pause'}:
            raise ValueError('无效操作')
        if action == 'start' and not store.settings()['account_name'].strip():
            raise ValueError('请先在账号与设置中填写公众号名称，再开启自动化。')
        store.save_settings({'automation_enabled':action=='start'})
        store.log('自动化已开启。' if action=='start' else '已暂停后续任务；正在执行的任务会完成当前流程。')
        return {'ok':True}

    @app.post('/api/generate')
    async def generate(item:GenerateInput):
        if generation_lock.locked():
            raise Conflict('已有写作任务正在生成，请稍候。')
        async with generation_lock:
            values = await ai.generate(store.settings(secret=True), item.topic, item.notes, item.length)
            s = store.settings()
            article = ArticleInput(**{**values, 'author':s['author'], 'theme':s['theme'],
                                      'source':'ai', 'creation_source':'ai'})
            data = validate_article(article)
            data['cover'] = make_cover(data['title'],data['theme'],store.root/'assets')
            result = store.save_article(data)
            store.log('AI 写作完成：' + result['title'])
            return result

    @app.post('/api/jobs')
    async def enqueue(item:QueueInput):
        if item.run_now:
            if browser.interacting:
                raise Conflict('请先关闭微信处理面板，再执行任务。')
            if item.scheduled_at:
                raise ValueError('立即发送不需要排期时间。')
            if browser.lock.locked() or any(j['status'] in {'running','waiting_user','needs_review'} for j in store.jobs()):
                raise Conflict('请先处理已有任务的提示，再发送这篇文章。')
        if item.action not in {'draft','publish'}:
            raise ValueError('请选择保存草稿或发表文章。')
        article = store.article(item.article_id)
        # Old imports and pasted articles use the same local preparation as
        # new file imports. Downloads finish before a browser is touched.
        if image_warnings(article['body'],article['format']) or 'mmbiz.qpic.cn' in article['body'] or 'mmbiz.qlogo.cn' in article['body']:
            prepared = await asyncio.to_thread(materialize,article['body'],article['format'],store.root / 'assets')
            if not prepared['ready']:
                raise ValueError('图片准备未完成：' + '；'.join(prepared['image_warnings']))
            article.update(body=prepared['body'],format=prepared['format'])
            article = store.save_article(article,article['id'])
        if not article['cover']:
            images = local_images(article['body'],article['format'])
            article['cover'] = images[0] if images else make_cover(article['title'],article['theme'],store.root/'assets')
            article = store.save_article(article,article['id'])
        prepared_plan = prepare_snapshot(article,store.root/'assets')
        if item.scheduled_at:
            try:
                scheduled = datetime.fromisoformat(item.scheduled_at)
                if not scheduled.tzinfo:
                    scheduled = scheduled.replace(tzinfo=CST)
                if scheduled.timestamp() < datetime.now(timezone.utc).timestamp()-60:
                    raise ValueError('排期时间已过去，请重新选择。')
            except (ValueError, OverflowError) as exc:
                raise ValueError('请选择有效的未来时间（北京时间）。') from exc
        else:
            scheduled = datetime.now(timezone.utc)
        job = store.enqueue(article['id'],item.action,scheduled.astimezone(timezone.utc).isoformat(timespec='seconds'),prepared=prepared_plan)
        if item.run_now:
            run_now(job['id'])
        return store.job(job['id'])

    @app.post('/api/jobs/{job_id}/creation-source')
    async def confirm_job_creation_source(job_id:str, item:CreationSourceInput):
        if browser.interacting:
            raise Conflict('请先关闭微信处理面板，再确认创作来源。')
        if browser.lock.locked():
            raise Conflict('微信正在处理任务，请等待当前步骤完成。')
        job = store.job(job_id)
        eligible = (job['action'] == 'publish' and (
            (job['stage'] == 'source_required' and job['status'] in {'needs_review','waiting_user'})
            or (job['stage'] == 'publishing' and job['status'] == 'needs_review'
                and job['step'] not in {'verification','receipt','review'})))
        if not eligible:
            raise Conflict('当前任务不在等待创作来源确认，不能重复恢复或重新发表。')
        if any(other['id'] != job_id and other['status'] in {'running','waiting_user','needs_review'}
               for other in store.jobs()):
            raise Conflict('另一篇任务仍在处理或等待核对，请先处理它。')
        # The browser must recheck the real prerequisite dialog, account and
        # draft title before the store can restore this same job. This route
        # cannot infer a declaration or mark the article as published.
        await browser.confirm_creation_source(job, item.creation_source)
        run_now(job_id, preserve_schedule=True)
        return {'ok':True}

    @app.post('/api/jobs/{job_id}/refresh-verification')
    async def refresh_job_verification(job_id:str, item:VerificationRefreshInput | None = None):
        # The driver refreshes only this task's still-visible administrator
        # challenge and owns the same lock as receipt probes and panel input.
        job = store.job(job_id)
        if item is not None and item.previous_not_confirmed:
            await browser.refresh_verification(job, previous_not_confirmed=True)
        else:
            await browser.refresh_verification(job)
        return {'ok':True}

    @app.post('/api/jobs/{job_id}/{action}')
    async def job_action(job_id:str,action:str):
        if action in {'resume','continue-editing','run'} and browser.interacting:
            raise Conflict('请先关闭微信处理面板，再继续任务。')
        if action=='cancel':
            store.cancel(job_id)
        elif action=='resume':
            store.resume(job_id)
            run_now(job_id)
        elif action=='continue-editing':
            await browser.prepare_resume_editor(store.job(job_id))
            run_now(job_id)
        elif action=='run':
            run_now(job_id)
        elif action=='verify':
            verified = await browser.verify(store.job(job_id))
            current = store.job(job_id)
            return {'ok':verified, 'status':current['status'],
                    'message':current['message'] or (
                        '微信回执已确认。' if verified else '尚未发现明确成功回执，请核对微信后台。')}
        else:
            raise ValueError('无效操作')
        return {'ok':True}

    @app.put('/api/jobs/{job_id}/resolve')
    async def resolve(job_id:str,item:ResolveInput):
        current = store.job(job_id)
        if current['status'] not in {'needs_review','waiting_user'} or not item.confirmed or item.status not in {'published','drafted','cancelled'}:
            raise ValueError('请先核对微信后台，再明确选择真实结果。')
        if item.status=='published':
            parsed = urlsplit(item.article_url)
            if parsed.scheme!='https' or parsed.hostname!='mp.weixin.qq.com' or not parsed.path.startswith('/s'):
                raise ValueError('记录发表成功时，请粘贴微信文章的 https://mp.weixin.qq.com/s 链接。')
        store.update_job(job_id,status=item.status,stage='user_confirmed',article_url=item.article_url,
                         message='结果由用户在微信后台核对后记录；非工具自动回执。')
        return {'ok':True}

    @app.post('/api/browser/open')
    async def open_browser():
        if browser.lock.locked() or any(j['status']=='running' for j in store.jobs()):
            raise Conflict('浏览器正在执行任务，请稍候。')
        async with browser.lock:
            return await browser.show()

    @app.post('/api/browser/show')
    async def show_browser():
        return await open_browser()

    @app.post('/api/browser/hide')
    async def hide_browser():
        if browser.lock.locked():
            raise Conflict('微信正在处理面板操作，请稍后关闭。')
        was_interacting = browser.interacting
        waiting = None
        async with browser.lock:
            try:
                status = await browser.status(refresh=True)
                # Only a login checkpoint can resume automatically; an editor
                # failure is also waiting_user but needs separate handling.
                blockers = [j for j in store.jobs() if j['status'] in {'running','waiting_user','needs_review'}]
                if (was_interacting and status.get('logged_in') and status.get('account_matches') is True
                        and len(blockers) == 1):
                    candidate = blockers[0]
                    if (candidate['status'] == 'waiting_user' and candidate['step'] == 'account'
                            and candidate['stage'] in {'', 'draft_saved'}):
                        waiting = candidate
            finally:
                browser.release_interaction()
        if waiting:
            store.resume(waiting['id'])
            run_now(waiting['id'])
            store.log('已检测到公众号登录，面板关闭后自动继续任务。', waiting['id'])
        return {'ok':True, **status, 'interacting':False, 'resumed':bool(waiting)}

    @app.get('/api/browser/frame')
    async def browser_frame():
        if browser.lock.locked():
            raise Conflict('微信正在执行任务，处理面板暂不可操作。')
        async with browser.lock:
            try:
                return await browser.interaction_frame()
            except ValueError:
                raise
            except Exception:
                raise ValueError('暂时无法显示微信页面，请稍后刷新面板。') from None

    @app.post('/api/browser/interact')
    async def browser_interact(item:PanelActionInput):
        if browser.lock.locked():
            raise Conflict('微信正在执行任务，处理面板暂不可操作。')
        async with browser.lock:
            try:
                await browser.interaction_action(item.model_dump())
                return {'ok':True}
            except ValueError:
                raise
            except Exception:
                raise ValueError('本次操作未能确认，请刷新面板查看结果，不要连续重复点击。') from None

    @app.get('/api/browser/status')
    async def browser_status(refresh:bool=False):
        if not refresh:
            return await browser.status()
        if browser.lock.locked():
            raise Conflict('微信正在执行任务，请等待任务结束后再刷新登录状态。')
        async with browser.lock:
            return await browser.status(refresh=True)

    @app.get('/api/articles/{article_id}/export')
    async def export(article_id:str):
        article = store.article(article_id)
        document = export_document(article)
        # Bundle local images into HTML so the exported file is portable.
        import base64
        def inline(match):
            path = store.root/'assets'/match.group(1)
            return 'src="data:image/jpeg;base64,' + base64.b64encode(path.read_bytes()).decode() + '"' if path.is_file() else match.group(0)
        document = re.sub(r'src="/assets/([0-9a-f]{32}\.jpg)"',inline,document)
        return HTMLResponse(document,headers={'Content-Disposition':'attachment; filename="article.html"'})

    @app.get('/assets/{name}')
    async def asset(name:str):
        if not re.fullmatch(r'[0-9a-f]{32}\.jpg',name) or not (store.root/'assets'/name).is_file():
            return Response(status_code=404)
        return FileResponse(store.root/'assets'/name)

    @app.get('/api/evidence/{name}')
    async def evidence(name:str):
        if not re.fullmatch(r'[0-9a-f]{32}(?:-(?:published|review_pending|failed|public-mobile))?\.png',name) or not (store.root/'evidence'/name).is_file():
            return Response(status_code=404)
        return FileResponse(store.root/'evidence'/name)

    @app.get('/')
    async def index():
        return FileResponse(ROOT/'web'/'index.html')

    @app.get('/static/{name}')
    async def static(name:str):
        if name not in {'app.js','control.js','style.css','icon.svg'}:
            return Response(status_code=404)
        return FileResponse(ROOT/'web'/name)

    return app
