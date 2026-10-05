import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

from publisher.app import create_app
from publisher.browser import BrowserDriver
from publisher.store import Store


class FakePage:
    url = 'https://mp.weixin.qq.com/'

    def is_closed(self):
        return False

    def set_default_timeout(self, _timeout):
        pass

    async def goto(self, *_args, **_kwargs):
        pass

    async def bring_to_front(self):
        pass


class FakeContext:
    def __init__(self):
        self.pages = [FakePage()]
        self.closed = False

    async def new_page(self):
        page = FakePage()
        self.pages.append(page)
        return page

    async def close(self):
        self.closed = True


class FakeChromium:
    def __init__(self):
        self.calls = []
        self.contexts = []

    async def launch_persistent_context(self, *args, **kwargs):
        kwargs['profile_path'] = args[0] if args else ''
        self.calls.append(kwargs)
        context = FakeContext()
        self.contexts.append(context)
        return context


class FakePlaywright:
    def __init__(self):
        self.chromium = FakeChromium()

    async def start(self):
        return self

    async def stop(self):
        pass


class BackgroundBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_background_open_and_in_app_panel_stay_headless(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            driver = BrowserDriver(store)
            fake = FakePlaywright()
            driver.status = AsyncMock(return_value={'open': True, 'state': 'waiting_scan'})
            with patch('publisher.browser.async_playwright', return_value=fake):
                await driver.open()
                self.assertTrue(fake.chromium.calls[0]['headless'])
                self.assertFalse(driver.window_visible)
                await driver.show()
                self.assertEqual(len(fake.chromium.calls), 1)
                self.assertTrue(fake.chromium.calls[0]['headless'])
                self.assertFalse(driver.window_visible)
                self.assertTrue(driver.interacting)

    async def test_legacy_visible_setting_is_normalized_to_background(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            store.save_settings({'browser_mode': 'visible'})
            driver = BrowserDriver(store)
            fake = FakePlaywright()
            driver.status = AsyncMock(return_value={'open': True})
            with patch('publisher.browser.async_playwright', return_value=fake):
                await driver.open()
            self.assertTrue(fake.chromium.calls[0]['headless'])
            self.assertFalse(driver.window_visible)
            self.assertEqual(store.settings()['browser_mode'], 'background')


    async def test_in_app_frame_accepts_one_scoped_action(self):
        with tempfile.TemporaryDirectory() as directory:
            driver = BrowserDriver(Store(Path(directory)))
            page = Mock()
            page.url = 'https://mp.weixin.qq.com/cgi-bin/home'
            page.is_closed.return_value = False
            page.viewport_size = {'width':1280, 'height':860}
            page.screenshot = AsyncMock(return_value=b'png-fixture')
            page.mouse = Mock()
            page.mouse.click = AsyncMock()
            page.keyboard = Mock()
            page.keyboard.insert_text = AsyncMock()
            page.keyboard.press = AsyncMock()
            driver.page = page
            driver._verification_dialog = AsyncMock(return_value=None)
            driver.interaction_until = time.monotonic() + 30
            frame = await driver.interaction_frame()
            self.assertTrue(frame['image'].startswith('data:image/png;base64,'))
            await driver.interaction_action({'frame_id':frame['frame_id'],'kind':'click','x':.5,'y':.25,'delta':0,'key':'','text':''})
            page.mouse.click.assert_awaited_once()
            with self.assertRaisesRegex(ValueError,'页面已变化'):
                await driver.interaction_action({'frame_id':frame['frame_id'],'kind':'click','x':.5,'y':.25,'delta':0,'key':'','text':''})


class BackgroundApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.app = create_app(self.store)

    def tearDown(self):
        self.tmp.cleanup()

    def test_settings_expose_background_mode_and_window_controls(self):
        with TestClient(self.app, base_url='http://127.0.0.1') as client:
            headers = {'X-Mojian-Token': client.get('/api/bootstrap').json()['token']}
            state = client.get('/api/state', headers=headers).json()
            self.assertEqual(state['settings']['browser_mode'], 'background')
            self.assertEqual(state['browser']['browser_mode'], 'background')
            self.assertEqual(state['browser']['window_state'], 'closed')
            response = client.put('/api/settings', headers=headers, json={
                'account_name': 'fixture-account', 'author': '', 'theme': 'jade',
                'ai_base_url': 'https://api.deepseek.com', 'ai_model': 'deepseek-chat',
                'ai_instructions': '直接', 'ai_api_key': '',
                'automation_enabled': False, 'default_action': 'draft',
                'browser_mode': 'background',
            })
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['browser_mode'], 'background')

    def test_show_and_hide_routes_do_not_open_a_browser_implicitly_in_test(self):
        async def show():
            return {'open': True, 'window_state': 'background', 'message': 'shown'}

        async def hide():
            return {'open': True, 'window_state': 'background', 'message': 'hidden'}

        with TestClient(self.app, base_url='http://127.0.0.1') as client:
            headers = {'X-Mojian-Token': client.get('/api/bootstrap').json()['token']}
            self.app.state.browser.show = show
            self.app.state.browser.hide = hide
            self.assertEqual(client.post('/api/browser/show', headers=headers).json()['window_state'], 'background')
            self.assertEqual(client.post('/api/browser/hide', headers=headers).json()['ok'], True)

    def test_closing_panel_auto_resumes_login_waiting_job(self):
        article = self.store.save_article({'title':'登录后自动继续','author':'','digest':'','body':'正文','format':'markdown','theme':'jade','cover':'','source':'manual'})
        job = self.store.enqueue(article['id'], 'draft', '2000-01-01T00:00:00+00:00')
        self.store.update_job(job['id'], status='waiting_user', stage='', step='account', message='等待扫码')
        self.app.state.browser.interaction_until = time.monotonic() + 30
        async def logged_in(refresh=False):
            return {'open':True, 'state':'logged_in', 'logged_in':True,
                    'account_matches':True, 'window_state':'background',
                    'message':'已连接'}
        self.app.state.browser.status = logged_in
        self.app.state.browser.execute = AsyncMock()
        with TestClient(self.app, base_url='http://127.0.0.1') as client:
            headers = {'X-Mojian-Token': client.get('/api/bootstrap').json()['token']}
            response = client.post('/api/browser/hide', headers=headers)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(response.json()['ok'])
            for _ in range(20):
                if self.app.state.browser.execute.await_count:
                    break
                time.sleep(.01)
            self.app.state.browser.execute.assert_awaited_once()
            self.assertEqual(self.store.job(job['id'])['status'], 'running')


if __name__ == '__main__':
    unittest.main(verbosity=2)
