import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from publisher.browser import BrowserDriver, NeedsUser
from publisher.login import SessionIdentity, classify_login
from publisher.store import Store


HOME = 'https://mp.weixin.qq.com/cgi-bin/home?token=fixture-token'


class LoginEvidenceTests(unittest.TestCase):
    def test_url_token_alone_is_not_verified_login(self):
        result = classify_login(HOME, {}, 'fixture-account')
        self.assertFalse(result['logged_in'])
        self.assertEqual(result['state'], 'unverified')
        self.assertEqual(result['account_name'], '')

    def test_visible_account_name_confirms_login(self):
        result = classify_login(HOME, {'names':[' fixture-account ', 'fixture-account']}, 'fixture-account')
        self.assertTrue(result['logged_in'])
        self.assertTrue(result['account_matches'])
        self.assertEqual(result['account_name'], 'fixture-account')

    def test_mismatch_uses_observed_name_not_configured_name(self):
        result = classify_login(HOME, {'names':['另一个公众号']}, 'fixture-account')
        self.assertTrue(result['logged_in'])
        self.assertFalse(result['account_matches'])
        self.assertEqual(result['state'], 'mismatch')
        self.assertEqual(result['account_name'], '另一个公众号')

    def test_expiry_and_login_form_override_old_name(self):
        for evidence in ({'expired':True}, {'login_form':True}):
            result = classify_login(HOME, {'names':['fixture-account'], **evidence}, 'fixture-account', ever_logged=True)
            self.assertFalse(result['logged_in'])
            self.assertEqual(result['state'], 'expired')

    def test_closed_ambiguous_and_unknown_are_not_success(self):
        cases = [('', {}, False, 'closed'),
                 (HOME, {'names':['fixture-account','another']}, True, 'unverified'),
                 (HOME, {'error':True}, True, 'unknown'),
                 ('https://example.com/?token=x', {'names':['fixture-account']}, True, 'unknown')]
        for url, probe, opened, expected in cases:
            with self.subTest(expected=expected):
                result = classify_login(url, probe, 'fixture-account', opened=opened)
                self.assertEqual(result['state'], expected)
                self.assertFalse(result['logged_in'])

    def test_first_scan_and_expired_session_are_distinct(self):
        self.assertEqual(classify_login('https://mp.weixin.qq.com/', {}, ever_logged=False)['state'], 'waiting_scan')
        self.assertEqual(classify_login('https://mp.weixin.qq.com/', {}, ever_logged=True)['state'], 'expired')


class AccountGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_task_rechecks_visible_identity_and_rejects_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            browser = BrowserDriver(Store(Path(directory)))
            browser.status = AsyncMock(return_value=classify_login(HOME, {'names':['wrong-account']}, 'fixture-account'))
            with self.assertRaises(NeedsUser):
                await browser._account()
            browser.status.assert_awaited_once_with(refresh=True)
            browser.status = AsyncMock(return_value=classify_login(HOME, {'names':['fixture-account']}, 'fixture-account'))
            await browser._account()


class SessionIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tick = 100
        self.identity = SessionIdentity(clock=lambda:self.tick, max_age=600)
        self.identity.resolve(HOME, {'names':['fixture-account']}, 'fixture-account')

    def test_editor_status_preserves_verified_identity_with_evidence_time(self):
        result = self.identity.resolve(HOME.replace('/home','/appmsg'), {}, 'fixture-account')
        self.assertEqual(result['state'], 'connected')
        self.assertEqual(result['identity_source'], 'session')
        self.assertEqual(result['account_name'], 'fixture-account')
        self.assertIn('verified_at', result)
        self.assertNotIn('fixture-token', str(result))

    def test_task_fresh_check_never_reuses_cached_identity(self):
        result = self.identity.resolve(HOME, {}, 'fixture-account', fresh=True)
        self.assertFalse(result['logged_in'])
        self.assertEqual(result['state'], 'unverified')

    def test_different_session_expiry_and_window_close_clear_identity(self):
        for url, probe, opened in [(HOME+'2',{},True),(HOME,{'expired':True},True),('',{},False)]:
            with self.subTest(url=url,probe=probe):
                identity=SessionIdentity()
                identity.resolve(HOME, {'names':['fixture-account']}, 'fixture-account')
                self.assertFalse(identity.resolve(url,probe,'fixture-account',opened=opened)['logged_in'])
                self.assertFalse(identity.resolve(HOME,{},'fixture-account')['logged_in'])

    def test_ambiguous_page_and_old_evidence_do_not_inherit_success(self):
        ambiguous=self.identity.resolve(HOME,{'names':['fixture-account','another']},'fixture-account')
        self.assertFalse(ambiguous['logged_in'])
        self.tick+=601
        self.assertFalse(self.identity.resolve(HOME,{},'fixture-account')['logged_in'])

    def test_config_change_does_not_change_observed_identity(self):
        result=self.identity.resolve(HOME,{},'other-account')
        self.assertEqual(result['state'],'mismatch')
        self.assertEqual(result['account_name'],'fixture-account')
        self.assertFalse(result['account_matches'])


if __name__ == '__main__':
    unittest.main()
