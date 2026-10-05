"""Administrator verification is a human checkpoint, never a second send."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from playwright.async_api import async_playwright
from publisher.browser import BrowserDriver, NeedsUser
from publisher.store import Store, now


class AdminVerificationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name))
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.launch(channel='msedge', headless=True)
        self.page = await self.browser.new_page()
        await self.page.route('**/*', lambda route: route.abort())
        self.driver = BrowserDriver(self.store)
        self.driver._prepare_publication_receipt = AsyncMock()
        self.driver.page = self.page
        self.driver._account = AsyncMock()
        article = self.store.save_article({'title':'扫码验证测试','body':'本地正文','creation_source':'non_ai'})
        self.job = self.store.enqueue(article['id'],'publish',now())
        self.driver.page_job_id = self.job['id']

    async def asyncTearDown(self):
        await self.browser.close()
        await self.pw.stop()
        self.temp.cleanup()

    async def test_validation_after_publish_confirmation_stops_without_second_send(self):
        await self.page.set_content('''<input id="title" value="扫码验证测试">
            <button onclick="window.entry++;document.querySelector('#dialog').innerHTML=
                '<h2>发表</h2><button onclick=showValidation()>发表</button>'">发表</button>
            <div id="dialog" role="dialog"></div><script>
            window.entry=0;window.submit=0;window.auth=0;
            function showValidation(){window.submit++;document.querySelector('#dialog').innerHTML=
                '<h2>微信验证</h2><p>扫码后，请联系管理员进行验证</p><button onclick="window.auth++">刷新二维码</button>';}
            </script>''')
        with self.assertRaisesRegex(NeedsUser,'管理员扫码'):
            await self.driver._publish(self.job)
        self.assertEqual(await self.page.evaluate('[window.entry,window.submit,window.auth]'),[1,1,0])
        actual = self.store.job(self.job['id'])
        self.assertEqual((actual['stage'],actual['step']),('publishing','verification'))
        self.assertNotEqual(actual['status'],'published')
        self.assertEqual(actual['article_url'],'')

    async def test_negative_receipt_probe_holds_lock_and_does_not_change_result(self):
        await self.page.set_content('<input id="title" value="扫码验证测试"><h2>微信验证</h2>')
        self.store.update_job(self.job['id'],status='needs_review',stage='publishing',step='verification')
        async def pending_receipt():
            self.assertTrue(self.driver.lock.locked())
            return False
        self.driver._publish_signal = pending_receipt
        before = self.store.job(self.job['id'])
        self.assertFalse(await self.driver.verify(before))
        self.assertFalse(self.driver.lock.locked())
        self.assertEqual(self.store.job(self.job['id']),before)
        self.driver._account.assert_not_awaited()

    async def test_classic_qr_dialog_is_detected_above_underlying_settings(self):
        await self.page.set_content('''<button onclick="show()">发表</button>
          <div id="settings" role="dialog"></div><div class="dialog_wrp"></div>
          <script>window.clicks=0;window.auth=0;
          function show(){document.querySelector('#settings').innerHTML='<h2>发表</h2><button onclick="qr()">发表</button>'}
          function qr(){window.clicks++;document.querySelector('.dialog_wrp').innerHTML=
          '<div class="dialog"><div class="dialog_hd"><h3>微信验证</h3></div><p>扫码后，请联系管理员进行验证</p><button onclick="window.auth++">扫码</button></div>';}
          </script>''')
        with self.assertRaisesRegex(NeedsUser,'管理员扫码'):
            await self.driver._publish(self.job)
        self.assertEqual(await self.page.evaluate('[window.clicks,window.auth]'),[1,0])
        self.assertEqual(self.store.job(self.job['id'])['step'],'verification')

    async def test_busy_verification_cannot_interleave_with_panel_input(self):
        self.driver._publish_signal = AsyncMock(return_value=False)
        async with self.driver.lock:
            with self.assertRaises(ValueError):
                await self.driver.verify(self.job)
        self.driver._publish_signal.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
