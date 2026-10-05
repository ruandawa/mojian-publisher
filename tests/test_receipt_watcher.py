"""Receipt watching observes a pending submission without submitting it again."""
import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from fastapi.testclient import TestClient

from publisher.app import create_app, poll_publish_receipt
from publisher.browser import VerificationRecoveryRequired
from publisher.store import Conflict, Store, now


ARTICLE = {'title': '等待管理员验证的文章', 'body': '完整正文', 'format': 'markdown',
           'theme': 'jade', 'source': 'manual', 'creation_source': 'non_ai'}


def pending_submission(store):
    article = store.save_article(ARTICLE)
    job = store.enqueue(article['id'], 'publish', now())
    store.update_job(job['id'], status='needs_review', stage='publishing',
                     step='verification', message='等待管理员扫码验证。')
    return store.job(job['id'])


def linked_browser(job):
    browser = Mock()
    browser.lock = asyncio.Lock()
    browser.active_job_id = None
    browser.context = Mock()
    browser.page_job_id = job['id']
    browser.page.is_closed.return_value = False
    browser.valid_baselines = set()
    browser._load_receipt = Mock(return_value=None)
    browser._receipt_connection_attempted = False
    browser.receipt_trackable = Mock(side_effect=lambda candidate: (
        candidate['id'] == browser.page_job_id or candidate['id'] in browser.valid_baselines))
    browser.interacting = True
    browser.verify = AsyncMock(return_value=False)
    browser.execute = AsyncMock()
    browser.open = AsyncMock()
    browser.page.goto = AsyncMock()
    return browser


class ReceiptWatcherTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name))
        self.store.save_settings({'automation_enabled': False})
        self.job = pending_submission(self.store)
        self.browser = linked_browser(self.job)

    def tearDown(self):
        self.temp.cleanup()

    def assert_no_submission(self):
        self.browser.execute.assert_not_awaited()
        self.browser.open.assert_not_awaited()
        self.browser.page.goto.assert_not_awaited()

    async def test_missing_receipt_stays_pending_without_logs_or_settings_changes(self):
        events = self.store.events()
        for _ in range(3):
            self.assertFalse(await poll_publish_receipt(self.store, self.browser))
        self.assertEqual(self.browser.verify.await_count, 3)
        self.assertEqual(self.store.job(self.job['id'])['status'], 'needs_review')
        self.assertEqual(self.store.events(), events)
        self.assertFalse(self.store.settings()['automation_enabled'])
        self.assert_no_submission()

    async def test_changed_page_value_errors_stay_pending_silently(self):
        self.store.save_settings({'automation_enabled': True})
        events = self.store.events()
        self.browser.verify.side_effect = ValueError('当前页面不属于这篇文章。')
        for _ in range(3):
            self.assertFalse(await poll_publish_receipt(self.store, self.browser))
        self.assertEqual(self.store.job(self.job['id'])['status'], 'needs_review')
        self.assertEqual(self.store.events(), events)
        self.assertTrue(self.store.settings()['automation_enabled'])
        self.assert_no_submission()

    async def test_success_with_queue_off_and_panel_open_is_checked_only_once(self):
        async def verified(job):
            self.store.update_job(job['id'], status='published', step='done',
                                  article_url='https://mp.weixin.qq.com/s/verified',
                                  message='已收到微信发表成功回执。')
            return True
        self.browser.verify.side_effect = verified
        self.assertTrue(await poll_publish_receipt(self.store, self.browser))
        for _ in range(3):
            self.assertFalse(await poll_publish_receipt(self.store, self.browser))
        self.browser.verify.assert_awaited_once()
        self.assertFalse(self.store.settings()['automation_enabled'])
        self.assertEqual(self.store.job(self.job['id'])['article_url'],
                         'https://mp.weixin.qq.com/s/verified')
        self.assert_no_submission()

    async def test_unlinked_closed_and_other_task_pages_are_not_inspected(self):
        for page_job_id, closed in ((None, False), ('another-task', False),
                                   (self.job['id'], True)):
            with self.subTest(page_job_id=page_job_id, closed=closed):
                self.browser.page_job_id = page_job_id
                self.browser.page.is_closed.return_value = closed
                self.assertFalse(await poll_publish_receipt(self.store, self.browser))
        self.browser.page = None
        self.assertFalse(await poll_publish_receipt(self.store, self.browser))
        self.browser.verify.assert_not_awaited()

    async def test_busy_browser_and_running_job_prevent_receipt_inspection(self):
        await self.browser.lock.acquire()
        try:
            self.assertFalse(await poll_publish_receipt(self.store, self.browser))
        finally:
            self.browser.lock.release()
        self.browser.active_job_id = 'executing-task'
        self.assertFalse(await poll_publish_receipt(self.store, self.browser))
        self.browser.active_job_id = None
        article = self.store.save_article({**ARTICLE, 'title': '另一篇正在执行'})
        other = self.store.enqueue(article['id'], 'draft', now())
        self.store.update_job(other['id'], status='running')
        self.assertFalse(await poll_publish_receipt(self.store, self.browser))
        self.browser.verify.assert_not_awaited()
        self.assert_no_submission()

    async def test_only_explicit_publication_verification_is_watched(self):
        for status, stage, step in (
            ('needs_review', 'publishing', 'publishing'),
            ('needs_review', 'draft_saved', 'verification'),
            ('waiting_user', 'publishing', 'verification'),
            ('published', 'publishing', 'done'),
            ('queued', '', 'verification'),
        ):
            with self.subTest(status=status, stage=stage, step=step):
                self.store.update_job(self.job['id'], status=status, stage=stage, step=step)
                self.assertFalse(await poll_publish_receipt(self.store, self.browser))
        self.browser.verify.assert_not_awaited()
        self.assert_no_submission()

    async def test_post_submit_receipt_and_platform_review_continue_with_queue_off(self):
        for status, step in (('needs_review','receipt'), ('review_pending','review')):
            with self.subTest(status=status, step=step):
                self.store.update_job(self.job['id'], status=status, step=step)
                self.assertFalse(await poll_publish_receipt(self.store, self.browser))
        self.assertEqual(self.browser.verify.await_count, 2)
        self.assertFalse(self.store.settings()['automation_enabled'])
        self.assert_no_submission()

    async def test_restart_without_verified_receipt_never_opens_and_old_jobs_need_explicit_baseline(self):
        self.store.update_job(self.job['id'], status='review_pending', step='review')
        self.browser.page_job_id = None
        self.browser.context = None
        self.assertFalse(await poll_publish_receipt(self.store, self.browser))
        self.browser.verify.assert_not_awaited()
        self.browser.context = Mock()
        self.assertFalse(await poll_publish_receipt(self.store, self.browser))
        self.browser.verify.assert_not_awaited()
        # The driver, not the scheduler, validates the persisted receipt's
        # schema, full baseline and job/draft identity before returning True.
        self.browser.valid_baselines.add(self.job['id'])
        self.assertFalse(await poll_publish_receipt(self.store, self.browser))
        self.browser.verify.assert_awaited_once()
        self.assert_no_submission()

    async def test_restart_valid_baseline_restores_session_once_then_only_verifies(self):
        self.store.update_job(self.job['id'], status='review_pending', step='review')
        self.browser.page_job_id = None
        self.browser.context = None
        self.browser._load_receipt.return_value = {'schema':1, 'job_id':self.job['id']}
        self.browser.valid_baselines.add(self.job['id'])
        events = self.store.events()
        async def opened():
            self.assertTrue(self.browser.lock.locked())
            self.browser.context = Mock()
        self.browser.open.side_effect = opened
        for _ in range(3):
            self.assertFalse(await poll_publish_receipt(self.store, self.browser))
        self.browser.open.assert_awaited_once_with()
        self.assertEqual(self.browser.verify.await_count, 3)
        self.browser.execute.assert_not_awaited()
        self.browser.page.goto.assert_not_awaited()
        self.assertEqual(self.store.events(), events)
        self.assertFalse(self.store.settings()['automation_enabled'])
        self.assertEqual(self.store.job(self.job['id'])['status'], 'review_pending')

    async def test_existing_session_with_valid_baseline_never_reopens(self):
        self.store.update_job(self.job['id'], status='review_pending', step='review')
        self.browser._load_receipt.return_value = {'schema':1}
        await poll_publish_receipt(self.store, self.browser)
        self.browser.open.assert_not_awaited()
        self.browser.verify.assert_awaited_once()

    async def test_failed_restart_attempt_is_not_repeated_and_does_not_start_jobs(self):
        self.store.update_job(self.job['id'], status='review_pending', step='review')
        self.browser.context = None
        self.browser._load_receipt.return_value = {'schema':1}
        self.browser.open.side_effect = ValueError('无法恢复后台连接。')
        for _ in range(3):
            self.assertFalse(await poll_publish_receipt(self.store, self.browser))
        self.browser.open.assert_awaited_once()
        self.browser.verify.assert_not_awaited()
        self.browser.execute.assert_not_awaited()
        self.assertFalse(self.store.settings()['automation_enabled'])
        self.assertEqual(self.store.job(self.job['id'])['status'], 'review_pending')

    async def test_restart_returning_no_session_is_attempted_only_once(self):
        self.store.update_job(self.job['id'], status='review_pending', step='review')
        self.browser.context = None
        self.browser._load_receipt.return_value = {'schema':1}
        for _ in range(3):
            self.assertFalse(await poll_publish_receipt(self.store, self.browser))
        self.browser.open.assert_awaited_once()
        self.browser.verify.assert_not_awaited()
        self.browser.execute.assert_not_awaited()

    async def test_queued_only_or_invalid_receipt_never_restores_session(self):
        self.browser.context = None
        self.browser._load_receipt.return_value = {'schema':1}
        self.store.update_job(self.job['id'], status='queued', stage='', step='account')
        self.assertFalse(await poll_publish_receipt(self.store, self.browser))
        self.browser._load_receipt.assert_not_called()
        self.store.update_job(self.job['id'], status='review_pending', stage='publishing', step='review')
        for invalid in (None, False, {}, [], Mock()):
            self.browser._load_receipt.return_value = invalid
            self.assertFalse(await poll_publish_receipt(self.store, self.browser))
        self.assertFalse(self.browser._receipt_connection_attempted)
        self.browser.open.assert_not_awaited()
        self.browser.verify.assert_not_awaited()

    async def test_busy_restart_with_valid_baseline_never_opens(self):
        self.browser.context = None
        self.browser._load_receipt.return_value = {'schema':1}
        await self.browser.lock.acquire()
        try:
            self.assertFalse(await poll_publish_receipt(self.store, self.browser))
        finally:
            self.browser.lock.release()
        self.browser.active_job_id = 'running-job'
        self.assertFalse(await poll_publish_receipt(self.store, self.browser))
        self.browser.active_job_id = None
        self.store.update_job(self.job['id'], status='running')
        self.assertFalse(await poll_publish_receipt(self.store, self.browser))
        self.browser.open.assert_not_awaited()
        self.browser._load_receipt.assert_not_called()
        self.assertFalse(self.browser._receipt_connection_attempted)

    async def test_independent_history_receipts_can_be_observed_for_multiple_jobs(self):
        self.store.update_job(self.job['id'], status='review_pending', step='review')
        article = self.store.save_article({**ARTICLE, 'title':'另一篇已提交的文章'})
        other = self.store.enqueue(article['id'], 'publish', now())
        self.store.update_job(other['id'], status='review_pending', stage='publishing', step='review')
        self.browser.page_job_id = 'unrelated-editor'
        self.browser.valid_baselines.update({self.job['id'], other['id']})
        async def reject(current):
            self.store.update_job(current['id'], status='failed', stage='platform_rejected',
                                  step='done', message='微信审核未通过，当前文章不会自动重发。')
            return False
        self.browser.verify.side_effect = reject
        self.assertFalse(await poll_publish_receipt(self.store, self.browser))
        self.assertEqual(self.browser.verify.await_count, 2)
        for _ in range(3):
            self.assertFalse(await poll_publish_receipt(self.store, self.browser))
        self.assertEqual(self.browser.verify.await_count, 2)
        self.assertEqual(self.store.job(self.job['id'])['stage'], 'platform_rejected')
        self.assert_no_submission()


class ReceiptWatcherSchedulerTests(unittest.TestCase):
    def test_scheduler_receives_existing_result_without_starting_queued_jobs(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            store.save_settings({'automation_enabled': False})
            job = pending_submission(store)
            article = store.save_article({**ARTICLE, 'title': '暂未执行的另一篇'})
            queued = store.enqueue(article['id'], 'draft', now())
            app = create_app(store)
            browser = app.state.browser
            browser.page = Mock()
            browser.page.is_closed.return_value = False
            browser.page_job_id = job['id']
            browser.context = Mock()
            browser.receipt_trackable = lambda current: current['id'] == job['id']
            browser.interaction_until = time.monotonic() + 30
            browser.execute = AsyncMock()
            browser.close = AsyncMock()
            async def verified(current):
                store.update_job(current['id'], status='published', step='done',
                                 message='微信发表成功。')
                return True
            browser.verify = AsyncMock(side_effect=verified)
            with TestClient(app, base_url='http://127.0.0.1'):
                for _ in range(50):
                    if store.job(job['id'])['status'] == 'published':
                        break
                    time.sleep(.01)
                self.assertEqual(store.job(job['id'])['status'], 'published')
                self.assertEqual(store.job(queued['id'])['status'], 'queued')
                browser.verify.assert_awaited_once()
                browser.execute.assert_not_awaited()
                self.assertFalse(store.settings()['automation_enabled'])


class ReceiptPendingStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name))
        self.job = pending_submission(self.store)
        self.store.update_job(self.job['id'], status='review_pending', step='review',
                              message='已提交，微信审核中。')

    def tearDown(self):
        self.temp.cleanup()

    def test_pending_review_survives_restart_and_cannot_resume_cancel_or_duplicate(self):
        original = self.store.job(self.job['id'])
        self.store.recover()
        self.assertEqual(self.store.job(self.job['id']), original)
        for action in (self.store.resume, self.store.cancel, self.store.claim_now):
            with self.subTest(action=action.__name__), self.assertRaises(Conflict):
                action(self.job['id'])
        with self.assertRaises(Conflict):
            self.store.enqueue(self.job['article_id'], 'publish', now())
        with self.assertRaises(Conflict):
            self.store.confirm_creation_source(self.job['id'], 'non_ai')

    def test_review_pending_blocks_changed_revision_of_same_article_but_not_other_articles(self):
        article = self.store.article(self.job['article_id'])
        self.store.save_article({**article, 'body':'修改后的完整正文'}, article['id'])
        with self.assertRaises(Conflict):
            self.store.enqueue(article['id'], 'publish', now())
        other_article = self.store.save_article({**ARTICLE, 'title':'另一篇可以执行'})
        other = self.store.enqueue(other_article['id'], 'draft', now())
        self.assertEqual(self.store.claim_due()['id'], other['id'])
        self.assertEqual(self.store.job(self.job['id'])['status'], 'review_pending')

    def test_unresolved_submission_blocks_due_jobs_and_source_reset(self):
        self.store.update_job(self.job['id'], status='needs_review', step='receipt')
        other_article = self.store.save_article({**ARTICLE, 'title':'下一篇暂不发送'})
        other = self.store.enqueue(other_article['id'], 'publish', now())
        self.assertIsNone(self.store.claim_due())
        with self.assertRaises(Conflict):
            self.store.claim_now(other['id'])
        with self.assertRaises(Conflict):
            self.store.confirm_creation_source(self.job['id'], 'non_ai')
        self.assertEqual(self.store.job(other['id'])['status'], 'queued')

    def test_rejected_version_never_requeues_without_explicit_article_revision(self):
        self.store.update_job(self.job['id'], status='failed', stage='platform_rejected', step='done')
        self.store.recover()
        self.assertEqual(self.store.job(self.job['id'])['stage'], 'platform_rejected')
        with self.assertRaises(Conflict):
            self.store.enqueue(self.job['article_id'], 'publish', now())
        with self.assertRaises(Conflict):
            self.store.resume(self.job['id'])
        article = self.store.article(self.job['article_id'])
        revised = self.store.save_article({**article, 'body':'用户修改后的一篇正文'}, article['id'])
        new = self.store.enqueue(revised['id'], 'publish', now())
        self.assertEqual(new['status'], 'queued')
        self.assertEqual(self.store.job(self.job['id'])['status'], 'failed')


class ReceiptRoutesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name))
        self.job = pending_submission(self.store)
        self.app = create_app(self.store)
        self.client = TestClient(self.app, base_url='http://127.0.0.1')
        self.headers = {'X-Mojian-Token':self.client.get('/api/bootstrap').json()['token']}

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def test_verify_response_reports_platform_review_without_claiming_publication(self):
        async def review(job):
            self.store.update_job(job['id'], status='review_pending', step='review',
                                  message='已提交，微信审核中。')
            return False
        self.app.state.browser.verify = AsyncMock(side_effect=review)
        response = self.client.post('/api/jobs/' + self.job['id'] + '/verify', headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'ok':False,'status':'review_pending','message':'已提交，微信审核中。'})
        self.assertEqual(self.store.job(self.job['id'])['article_url'], '')

    def test_receipt_screenshots_open_but_private_baseline_is_not_served(self):
        for suffix in ('', '-published', '-review_pending', '-failed', '-public-mobile'):
            name = self.job['id'] + suffix + '.png'
            (self.store.root/'evidence'/name).write_bytes(b'fixture-png')
            response = self.client.get('/api/evidence/'+name, headers=self.headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, b'fixture-png')
        for suffix in ('-receipt.json', '-secret.png'):
            name = self.job['id'] + suffix
            (self.store.root/'evidence'/name).write_bytes(b'private-fixture')
            self.assertEqual(self.client.get('/api/evidence/'+name,headers=self.headers).status_code,404)

    def test_refresh_verification_delegates_only_to_guarded_driver_method(self):
        self.app.state.browser.refresh_verification = AsyncMock()
        response = self.client.post('/api/jobs/' + self.job['id'] + '/refresh-verification', headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {'ok':True})
        self.app.state.browser.refresh_verification.assert_awaited_once_with(self.store.job(self.job['id']))
        before = self.store.job(self.job['id'])
        self.app.state.browser.refresh_verification.side_effect = ValueError('验证二维码已不存在。')
        response = self.client.post('/api/jobs/' + self.job['id'] + '/refresh-verification', headers=self.headers)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['error'], '验证二维码已不存在。')
        self.assertEqual(self.store.job(self.job['id']), before)

    def test_refresh_verification_requires_the_local_session_token(self):
        self.app.state.browser.refresh_verification = AsyncMock()
        response = self.client.post('/api/jobs/' + self.job['id'] + '/refresh-verification')
        self.assertEqual(response.status_code, 403)
        self.app.state.browser.refresh_verification.assert_not_awaited()

    def test_qr_recovery_without_confirmation_returns_required_code_and_leaves_job_unchanged(self):
        before = self.store.job(self.job['id'])
        self.app.state.browser.refresh_verification = AsyncMock(
            side_effect=VerificationRecoveryRequired('请确认上次未在手机完成发表。'))
        response = self.client.post('/api/jobs/' + self.job['id'] + '/refresh-verification',
                                    headers=self.headers)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['code'], 'verification_recovery_required')
        self.app.state.browser.refresh_verification.assert_awaited_once_with(before)
        self.assertEqual(self.store.job(self.job['id']), before)

    def test_qr_recovery_explicit_true_only_is_forwarded_to_driver(self):
        self.app.state.browser.refresh_verification = AsyncMock()
        route = '/api/jobs/' + self.job['id'] + '/refresh-verification'
        for payload in ({}, {'previous_not_confirmed':False}):
            with self.subTest(payload=payload):
                self.app.state.browser.refresh_verification.reset_mock()
                response = self.client.post(route, headers=self.headers, json=payload)
                self.assertEqual(response.status_code, 200, response.text)
                self.app.state.browser.refresh_verification.assert_awaited_once_with(self.store.job(self.job['id']))
        self.app.state.browser.refresh_verification.reset_mock()
        response = self.client.post(route, headers=self.headers, json={'previous_not_confirmed':True})
        self.assertEqual(response.status_code, 200, response.text)
        self.app.state.browser.refresh_verification.assert_awaited_once_with(
            self.store.job(self.job['id']), previous_not_confirmed=True)

    def test_qr_recovery_rejects_coerced_confirmation_and_unauthenticated_true(self):
        self.app.state.browser.refresh_verification = AsyncMock()
        route = '/api/jobs/' + self.job['id'] + '/refresh-verification'
        for value in ('true', 'yes', 1, None):
            with self.subTest(value=value):
                response = self.client.post(route, headers=self.headers,
                                            json={'previous_not_confirmed':value})
                self.assertEqual(response.status_code, 422)
        response = self.client.post(route, json={'previous_not_confirmed':True})
        self.assertEqual(response.status_code, 403)
        self.app.state.browser.refresh_verification.assert_not_awaited()


if __name__ == '__main__':
    unittest.main(verbosity=2)
