"""Software verification-panel behavior with a fake API and a fresh browser.

No live account, persistent browser profile, user database, or publication call.
"""
import asyncio
import copy
from pathlib import Path
import unittest
from urllib.parse import urlsplit

from playwright.async_api import async_playwright, expect


ROOT = Path(__file__).resolve().parents[1]
ORIGIN = 'http://127.0.0.1:18732'
STAMP = '2026-10-05T00:00:00Z'


class VerificationUITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.jobs = []
        self.articles = []
        self.posts = []
        self.unexpected = []
        self.errors = []
        self.verify_release = None
        self.publish_on_verify = False
        self.refresh_release = None
        self.refresh_error = False
        self.refresh_recovery_required = False
        self.refresh_payloads = []
        self.frame_requests = 0
        self.verification_visible = None
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(channel='msedge', headless=True)
        self.context = await self.browser.new_context(viewport={'width': 1450, 'height': 1000})
        await self.context.route('**/*', self.serve)
        self.page = await self.context.new_page()
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))

    async def asyncTearDown(self):
        await self.context.close()
        await self.browser.close()
        await self.playwright.stop()
        self.assertEqual(self.errors, [])
        self.assertEqual(self.unexpected, [])
        self.assertTrue(all(path in ['/api/browser/show', '/api/browser/hide',
                                     '/api/jobs/verification/verify',
                                     '/api/jobs/verification/refresh-verification', '/api/preview']
                            or path.startswith('/api/articles/')
                            for path in self.posts), self.posts)

    def verification_job(self, **changes):
        job = {'id': 'verification', 'article_id': 'article',
               'snapshot': {'title': '扫码隔离文章'}, 'action': 'publish',
               'status': 'needs_review', 'stage': 'publishing', 'step': 'verification',
               'scheduled_at': STAMP, 'updated_at': STAMP,
               'message': '请用公众号管理员微信扫码，并在手机上确认发表。'}
        job.update(changes)
        return job

    async def serve(self, route):
        request = route.request
        target = urlsplit(request.url)
        if target.scheme != 'http' or target.netloc != '127.0.0.1:18732':
            self.unexpected.append(request.url)
            await route.abort()
            return
        path = target.path
        if request.method == 'POST':
            self.posts.append(path)
        if path == '/':
            await route.fulfill(path=ROOT / 'web/index.html', content_type='text/html; charset=utf-8')
        elif path.startswith('/static/'):
            filename = path.removeprefix('/static/')
            mime = {'app.js': 'application/javascript', 'control.js': 'application/javascript',
                    'style.css': 'text/css', 'icon.svg': 'image/svg+xml'}.get(filename)
            if mime:
                await route.fulfill(path=ROOT / 'web' / filename, content_type=mime)
            else:
                self.unexpected.append(request.url)
                await route.abort()
        elif path == '/api/bootstrap':
            await route.fulfill(json={'token': 'isolated-verification-test'})
        elif path == '/api/state':
            await route.fulfill(json={'version': 'test', 'articles': copy.deepcopy(self.articles),
                'jobs': copy.deepcopy(self.jobs), 'events': [],
                'settings': {'automation_enabled': False},
                'browser': {'state': 'logged_in', 'logged_in': True, 'account_name': '测试账号',
                            'message': '账号已连接。'}})
        elif path == '/api/preview':
            await route.fulfill(json={'html': '<p>测试正文</p>', 'ready': True,
                                     'image_count': 0, 'image_warnings': []})
        elif path == '/api/browser/show':
            await route.fulfill(json={'message': '软件内验证画面。'})
        elif path == '/api/browser/frame':
            self.frame_requests += 1
            # A local placeholder frame, not an authentic QR code or live page.
            frame = {'frame_id': 'isolated-frame', 'width': 1, 'height': 1,
                'image': 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aNj8AAAAASUVORK5CYII='}
            if self.verification_visible is not None:
                frame['verification_visible'] = self.verification_visible
            await route.fulfill(json=frame)
        elif path == '/api/browser/hide':
            await route.fulfill(json={'resumed': False})
        elif path == '/api/jobs/verification/verify':
            if self.verify_release:
                await self.verify_release.wait()
            if self.publish_on_verify:
                self.jobs[0].update(status='published', stage='published', step='done',
                                    message='已收到微信发表成功回执。')
            await route.fulfill(json={'message': '已核对当前发表结果。'})
        elif path == '/api/jobs/verification/refresh-verification':
            payload = request.post_data_json if request.post_data else None
            self.refresh_payloads.append(payload)
            if self.refresh_release:
                await self.refresh_release.wait()
            if self.refresh_recovery_required and not (payload or {}).get('previous_not_confirmed'):
                await route.fulfill(status=400, json={'error':'验证会话已中断，请确认上次未在手机完成发表。',
                    'code':'verification_recovery_required'})
            elif self.refresh_error:
                await route.fulfill(status=409, json={'error': '验证码尚未刷新，请重新核对结果。'})
            else:
                await route.fulfill(json={'message': '验证码已刷新，请用管理员微信扫码。'})
        elif path == '/api/articles/article' and request.method == 'PUT':
            article = dict(self.articles[0], **request.post_data_json)
            self.articles[0] = article
            await route.fulfill(json=article)
        else:
            self.unexpected.append(request.url)
            await route.abort()

    async def load(self):
        await self.page.goto(ORIGIN + '/#queue')
        await expect(self.page.locator('#app-version')).to_have_text('vtest')

    async def refresh(self):
        await self.page.evaluate('refresh()')

    async def test_auto_open_once_manual_reopen_and_read_only_verification(self):
        self.jobs = [self.verification_job()]
        await self.load()
        panel = self.page.locator('#wechat-dialog')
        await expect(panel).to_be_visible()
        await expect(self.page.locator('#wechat-title')).to_have_text('管理员扫码验证')
        await expect(self.page.locator('#wechat-message')).to_contain_text('手机上确认发表')
        await expect(self.page.locator('#wechat-verify')).to_be_visible()
        await expect(self.page.locator('#current-task-state')).to_have_text('等待扫码验证')
        await expect(self.page.locator('#job-card-verification .badge')).to_have_text('等待扫码验证')
        await expect(self.page.locator('#job-card-verification .task-steps .attention')).to_contain_text('管理员扫码验证')
        await self.page.locator('#wechat-done').click()
        await expect(panel).not_to_be_visible()
        await self.refresh()
        await expect(panel).not_to_be_visible()
        self.assertEqual(self.posts.count('/api/browser/show'), 1)
        card = self.page.locator('#job-card-verification')
        await expect(card.get_by_role('button', name='扫码验证', exact=True)).to_be_visible()
        await expect(card.get_by_role('button', name='核对发表结果', exact=True)).to_be_visible()
        self.assertEqual(await card.locator('[data-action="run"], [data-action="resume"], [data-action="resolve"]').count(), 0)
        await card.get_by_role('button', name='扫码验证', exact=True).click()
        await expect(panel).to_be_visible()
        self.verify_release = asyncio.Event()
        self.publish_on_verify = True
        await self.page.locator('#wechat-verify').click()
        await expect(self.page.locator('#wechat-verify')).to_be_disabled()
        await self.page.locator('#wechat-verify').dispatch_event('click')
        self.verify_release.set()
        await expect(self.page.locator('#wechat-message')).to_contain_text('已收到微信发表成功回执')
        await expect(self.page.locator('#wechat-verify')).to_be_hidden()
        await expect(self.page.locator('#job-card-verification .badge')).to_have_text('微信已发表')
        self.assertEqual(self.posts.count('/api/jobs/verification/verify'), 1)
        self.assertEqual(self.posts.count('/api/browser/show'), 2)

    async def test_generic_review_source_and_non_publish_do_not_auto_open(self):
        self.jobs = [self.verification_job(id='review', step='publishing'),
            self.verification_job(id='source', status='waiting_user', stage='source_required', step='creation_source'),
            self.verification_job(id='draft', action='draft'),
            self.verification_job(id='wrong-stage', stage='editing')]
        await self.load()
        await self.refresh()
        await expect(self.page.locator('#wechat-dialog')).not_to_be_visible()
        await expect(self.page.locator('#task-dialog')).not_to_be_visible()
        self.assertEqual(self.posts.count('/api/browser/show'), 0)
        self.assertEqual(await self.page.locator('#jobs-list [data-action="scan-verification"]').count(), 0)
        await expect(self.page.locator('#job-card-review .badge')).to_have_text('结果待核对')
        await expect(self.page.locator('#job-card-source [data-action="creation-source-panel"]')).to_be_visible()

    async def test_unsaved_editor_defers_auto_panel_until_saved(self):
        self.articles = [{'id': 'article', 'title': '原标题', 'body': '保留编辑正文',
                          'format': 'markdown', 'revision': 1}]
        await self.load()
        await self.page.locator('.sidebar [data-nav="workspace"]').click()
        await self.page.locator('#article-title').fill('正在编辑的新标题')
        self.jobs = [self.verification_job()]
        await self.refresh()
        await expect(self.page.locator('#wechat-dialog')).not_to_be_visible()
        await expect(self.page.locator('#article-title')).to_have_value('正在编辑的新标题')
        await expect(self.page.locator('#article-body')).to_have_value('保留编辑正文')
        self.assertEqual(self.posts.count('/api/browser/show'), 0)
        await self.page.locator('#save-btn').click()
        await expect(self.page.locator('#wechat-dialog')).to_be_visible()
        self.assertEqual(self.articles[0]['title'], '正在编辑的新标题')
        self.assertEqual(self.articles[0]['body'], '保留编辑正文')

    async def test_existing_modal_defers_auto_panel(self):
        await self.load()
        await self.page.evaluate("document.getElementById('article-preview-dialog').showModal()")
        self.jobs = [self.verification_job()]
        await self.refresh()
        await expect(self.page.locator('#article-preview-dialog')).to_be_visible()
        await expect(self.page.locator('#wechat-dialog')).not_to_be_visible()
        self.assertEqual(self.posts.count('/api/browser/show'), 0)
        await self.page.locator('#article-preview-dialog .dialog-close').click()
        await self.refresh()
        await expect(self.page.locator('#wechat-dialog')).to_be_visible()
        self.assertEqual(self.posts.count('/api/browser/show'), 1)

    async def test_qr_refresh_single_click_lock_frame_reload_and_error_toast(self):
        self.jobs = [self.verification_job()]
        await self.load()
        await expect(self.page.locator('#wechat-frame')).to_be_visible()
        refresh = self.page.locator('#wechat-refresh-verification')
        await expect(refresh).to_have_text('二维码过期，刷新验证码')
        self.refresh_release = asyncio.Event()
        initial_frames = self.frame_requests
        await refresh.click()
        await expect(refresh).to_be_disabled()
        await expect(self.page.locator('#wechat-verify')).to_be_disabled()
        await expect(self.page.locator('#wechat-done')).to_be_disabled()
        await refresh.dispatch_event('click')
        await self.page.locator('#wechat-verify').dispatch_event('click')
        self.refresh_release.set()
        await expect(refresh).to_be_enabled()
        await expect(self.page.locator('#toast')).to_have_text('验证码已刷新，请用管理员微信扫码。')
        self.assertEqual(self.posts.count('/api/jobs/verification/refresh-verification'), 1)
        self.assertEqual(self.posts.count('/api/jobs/verification/verify'), 0)
        self.assertGreater(self.frame_requests, initial_frames)

        self.refresh_error = True
        await refresh.click()
        await expect(self.page.locator('#toast')).to_have_text('验证码尚未刷新，请重新核对结果。')
        await expect(refresh).to_be_enabled()
        await expect(self.page.locator('#wechat-done')).to_be_enabled()
        await expect(self.page.locator('#wechat-frame')).to_be_visible()
        self.assertEqual(self.posts.count('/api/jobs/verification/refresh-verification'), 2)

    async def test_qr_refresh_is_hidden_and_guarded_for_other_results(self):
        self.jobs = [self.verification_job()]
        await self.load()
        await expect(self.page.locator('#wechat-frame')).to_be_visible()
        for changes in (
            {'status': 'review_pending', 'stage': 'publishing', 'step': 'review'},
            {'status': 'failed', 'stage': 'platform_rejected', 'step': 'done'},
            {'status': 'published', 'stage': 'published', 'step': 'done'},
            {'status': 'needs_review', 'stage': 'editing', 'step': 'body'},
        ):
            with self.subTest(changes=changes):
                self.jobs[0].update(changes)
                await self.refresh()
                await expect(self.page.locator('#wechat-refresh-verification')).to_be_hidden()
                await self.page.locator('#wechat-refresh-verification').dispatch_event('click')
                await expect(self.page.locator('#toast')).to_have_text('当前任务不处于管理员扫码验证状态，请先核对发表结果。')
        self.assertEqual(self.posts.count('/api/jobs/verification/refresh-verification'), 0)

    async def test_qr_recovery_cancel_does_not_send_confirmation_or_retry(self):
        self.jobs = [self.verification_job()]
        self.refresh_recovery_required = True
        await self.load()
        await expect(self.page.locator('#wechat-frame')).to_be_visible()
        messages = []
        async def dismiss(dialog):
            messages.append(dialog.message)
            await dialog.dismiss()
        self.page.once('dialog', dismiss)
        await self.page.locator('#wechat-refresh-verification').click()
        await expect(self.page.locator('#toast')).to_contain_text('未恢复二维码')
        await expect(self.page.locator('#wechat-refresh-verification')).to_be_enabled()
        await expect(self.page.locator('#wechat-verify')).to_be_enabled()
        self.assertEqual(self.refresh_payloads, [None])
        self.assertEqual(self.posts.count('/api/jobs/verification/refresh-verification'), 1)
        self.assertEqual(len(messages), 1)
        self.assertIn('上次没有在手机完成发表', messages[0])
        self.assertIn('取消', messages[0])
        self.assertIn('核对发表结果', messages[0])

    async def test_missing_qr_frame_shows_action_without_automatic_recovery(self):
        self.jobs = [self.verification_job()]
        self.verification_visible = False
        await self.load()
        await expect(self.page.locator('#wechat-message')).to_contain_text('二维码已过期或验证窗口已关闭')
        await self.refresh()
        await expect(self.page.locator('#wechat-message')).to_contain_text('若已在手机确认，请先核对结果')
        self.assertEqual(self.posts.count('/api/jobs/verification/refresh-verification'), 0)
        self.verification_visible = True
        await self.page.evaluate('refreshWechatFrame()')
        await expect(self.page.locator('#wechat-message')).to_contain_text('请用公众号管理员微信扫码')

    async def test_qr_recovery_accept_sends_true_only_after_confirmation(self):
        self.jobs = [self.verification_job()]
        self.refresh_recovery_required = True
        await self.load()
        await expect(self.page.locator('#wechat-frame')).to_be_visible()
        requests_at_confirmation = []
        async def accept(dialog):
            requests_at_confirmation.append(copy.deepcopy(self.refresh_payloads))
            await dialog.accept()
        self.page.once('dialog', accept)
        await self.page.locator('#wechat-refresh-verification').click()
        await expect(self.page.locator('#toast')).to_have_text('验证码已刷新，请用管理员微信扫码。')
        await expect(self.page.locator('#wechat-refresh-verification')).to_be_enabled()
        self.assertEqual(requests_at_confirmation, [[None]])
        self.assertEqual(self.refresh_payloads, [None, {'previous_not_confirmed':True}])
        self.assertEqual(self.posts.count('/api/jobs/verification/refresh-verification'), 2)

    async def test_review_pending_survives_reload_without_success_or_resubmission(self):
        self.jobs = [self.verification_job(status='review_pending', step='review',
                                          message='已提交，微信审核中。')]
        await self.load()
        await expect(self.page.locator('#wechat-dialog')).not_to_be_visible()
        card = self.page.locator('#job-card-verification')
        await expect(card.locator('.badge')).to_have_text('已提交，微信审核中')
        await expect(card.locator('.task-steps .current')).to_contain_text('微信审核')
        await expect(self.page.locator('#published-total')).to_have_text('0篇')
        await expect(self.page.locator('#pending-total')).to_have_text('1篇')
        self.assertEqual(await card.locator('button').count(), 1)
        await expect(card.get_by_role('button', name='核对结果', exact=True)).to_be_visible()
        await self.page.locator('[data-filter="done"]').click()
        self.assertEqual(await self.page.locator('#jobs-list .job-card').count(), 0)
        await self.page.locator('[data-filter="active"]').click()
        await expect(card).to_be_visible()
        await self.page.reload()
        await expect(card.locator('.badge')).to_have_text('已提交，微信审核中')
        await expect(self.page.locator('#wechat-dialog')).not_to_be_visible()
        self.publish_on_verify = True
        await card.get_by_role('button', name='核对结果', exact=True).click()
        await expect(card.locator('.badge')).to_have_text('微信已发表')
        await expect(self.page.locator('#published-total')).to_have_text('1篇')
        self.assertEqual(self.posts.count('/api/jobs/verification/verify'), 1)
        self.assertEqual(self.posts.count('/api/browser/show'), 0)

    async def test_platform_rejection_shows_evidence_without_publish_controls(self):
        self.jobs = [self.verification_job(status='failed', stage='platform_rejected', step='done',
                                          message='微信审核未通过。', evidence='/evidence/rejected.png')]
        await self.load()
        card = self.page.locator('#job-card-verification')
        await expect(card.locator('.badge')).to_have_text('微信审核未通过')
        await expect(card.locator('.task-steps .attention')).to_contain_text('微信审核')
        await expect(card.locator('.task-steps li:last-child')).to_have_class('pending')
        self.assertEqual(await card.locator('button').count(), 1)
        await expect(card.get_by_role('button', name='现场截图', exact=True)).to_be_visible()
        await expect(self.page.locator('#published-total')).to_have_text('0篇')
        await self.page.locator('[data-filter="attention"]').click()
        await expect(card).to_be_visible()
        self.assertEqual(self.posts.count('/api/browser/show'), 0)

    async def test_published_article_link_excludes_preview_and_untrusted_urls(self):
        valid = [
            'https://mp.weixin.qq.com/s/Actual_public-id',
            'https://mp.weixin.qq.com/s?__biz=public&mid=123&idx=1&sn=hash&scene=1',
        ]
        invalid = [
            'https://mp.weixin.qq.com/s/public?tempkey=private-preview',
            'https://mp.weixin.qq.com/s?__biz=x&mid=1&idx=1&sn=x&TempKey=preview',
            'https://mp.weixin.qq.com/s?__biz=x&mid=1&idx=1',
            'https://mp.weixin.qq.com/s?scene=1',
            'https://mp.weixin.qq.com/cgi-bin/appmsg?token=secret',
            'https://mp.weixin.qq.com.evil.example/s/public',
            'https://user:password@mp.weixin.qq.com/s/public',
            'http://mp.weixin.qq.com/s/public',
            'javascript:alert(1)',
        ]
        self.jobs = [self.verification_job(id='url-'+str(index), status='published',
            stage='published', step='done', article_url=url)
            for index, url in enumerate(valid + invalid)]
        # An existing URL while waiting for review must not be advertised as published.
        self.jobs.append(self.verification_job(id='not-published', status='review_pending',
                                              step='review', article_url=valid[0]))
        await self.load()
        links = self.page.locator('#jobs-list a', has_text='查看文章')
        self.assertEqual(await links.count(), len(valid))
        self.assertEqual(await links.nth(0).get_attribute('href'), valid[0])
        self.assertEqual(await links.nth(1).get_attribute('href'), valid[1].removesuffix('&scene=1'))
        self.assertEqual(await self.page.locator('#job-card-not-published a').count(), 0)
        self.assertEqual(self.posts.count('/api/browser/show'), 0)


if __name__ == '__main__':
    unittest.main()
