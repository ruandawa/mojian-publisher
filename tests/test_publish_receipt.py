"""Publication control and receipt fixtures, isolated from the real account.

Every network request is blocked. These verify adapter compatibility and
failure boundaries; they are not evidence of real WeChat publication.
"""
import unittest
from unittest.mock import Mock

from playwright.async_api import async_playwright

from publisher.browser import BrowserDriver, NeedsUser


class PublicationReceiptTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(channel='msedge', headless=True)
        self.context = await self.browser.new_context()
        await self.context.route('**/*', lambda route: route.abort())
        self.page = await self.context.new_page()
        self.driver = BrowserDriver(Mock())
        self.driver.page = self.page

    async def asyncTearDown(self):
        await self.browser.close()
        await self.playwright.stop()

    async def test_button_and_link_confirmation_controls_each_submit_once(self):
        for control in (
            '<button onclick="window.submits++">确认发表</button>',
            '<a href="#" onclick="window.submits++;return false">确认发表</a>',
        ):
            with self.subTest(control=control):
                await self.page.set_content('<div role="dialog">' + control + '</div>')
                await self.page.evaluate('window.submits=0')
                self.assertTrue(await self.driver._publish_dialog_step(self.page.get_by_role('dialog')))
                self.assertEqual(await self.page.evaluate('window.submits'), 1)

    async def test_ambiguous_button_and_link_confirmations_are_not_clicked(self):
        await self.page.set_content('''<div role="dialog">
            <button onclick="window.submits++">发表</button>
            <a href="#" onclick="window.submits++;return false">确认发表</a>
            </div>''')
        await self.page.evaluate('window.submits=0')
        self.assertFalse(await self.driver._publish_dialog_step(self.page.get_by_role('dialog')))
        self.assertEqual(await self.page.evaluate('window.submits'), 0)

    async def test_hidden_and_disabled_confirmation_controls_are_not_clicked(self):
        await self.page.set_content('''<div role="dialog">
            <button disabled onclick="window.submits++">确认发表</button>
            <a href="#" hidden onclick="window.submits++;return false">发表</a>
            </div>''')
        await self.page.evaluate('window.submits=0')
        self.assertFalse(await self.driver._publish_dialog_step(self.page.get_by_role('dialog')))
        self.assertEqual(await self.page.evaluate('window.submits'), 0)

    async def test_identity_and_declaration_challenges_stop_before_confirm_click(self):
        for challenge in ('扫码', '扫描二维码', '管理员验证', '验证码', '声明原创', '同意用户协议'):
            with self.subTest(challenge=challenge):
                await self.page.set_content('''<div role="dialog"><p>''' + challenge + '''</p>
                    <a href="#" onclick="window.submits++;return false">确定</a></div>''')
                await self.page.evaluate('window.submits=0')
                with self.assertRaisesRegex(NeedsUser, '本人验证或声明'):
                    await self.driver._publish_dialog_step(self.page.get_by_role('dialog'))
                self.assertEqual(await self.page.evaluate('window.submits'), 0)

    async def test_unresolved_notification_scope_stops_before_confirm_click(self):
        await self.page.set_content('''<div role="dialog"><p>群发通知</p>
            <a href="#" onclick="window.submits++;return false">确认发表</a></div>''')
        await self.page.evaluate('window.submits=0')
        with self.assertRaisesRegex(NeedsUser, '通知范围'):
            await self.driver._publish_dialog_step(self.page.get_by_role('dialog'))
        self.assertEqual(await self.page.evaluate('window.submits'), 0)

    async def test_standard_permalink_forms_are_recognized(self):
        for link in (
            'https://mp.weixin.qq.com/s/fixture-article-id',
            'https://mp.weixin.qq.com/s?__biz=fixture&mid=123&idx=1&sn=example',
        ):
            with self.subTest(link=link):
                await self.page.set_content('<a href="' + link + '">查看文章</a>')
                self.assertEqual(await self.driver._published_url(), link)

    async def test_unrelated_hidden_empty_and_lookalike_urls_are_ignored(self):
        valid = 'https://mp.weixin.qq.com/s?__biz=fixture&mid=123'
        await self.page.set_content('''
            <a href="https://mp.weixin.qq.com.evil.invalid/s/fake">仿冒域名</a>
            <a href="https://example.invalid/?next=https://mp.weixin.qq.com/s/fake">跳转</a>
            <a href="https://mp.weixin.qq.com/settings">设置</a>
            <a href="https://mp.weixin.qq.com/s">空链接</a>
            <a href="https://mp.weixin.qq.com/s/">空短链</a>
            <a href="http://mp.weixin.qq.com/s/insecure">非 HTTPS</a>
            <a href="https://mp.weixin.qq.com/s/hidden" hidden>隐藏链接</a>
            <a href="''' + valid + '''">查看文章</a>''')
        self.assertEqual(await self.driver._published_url(), valid)

    async def test_absent_article_link_does_not_fabricate_one(self):
        await self.page.set_content('<div>发表成功</div>')
        self.assertEqual(await self.driver._published_url(), '')

    async def test_explicit_success_title_is_recognized_inside_longer_result(self):
        await self.page.set_content('''<div class="weui-desktop-dialog__content">
            <h2 class="weui-desktop-msg__title">发表成功！</h2>
            <p>你可以在发表记录中查看文章。</p></div>''')
        self.assertTrue(await self.driver._publish_signal())

    async def test_hidden_receipt_instructions_and_pending_state_are_not_success(self):
        for markup in (
            '<h2 class="weui-desktop-msg__title" hidden>发表成功</h2>',
            '<div role="alert">发表成功后可以查看文章</div>',
            '<h2 class="weui-desktop-msg__title">等待发表</h2>',
            '<h2 class="weui-desktop-msg__title">发表失败</h2>',
        ):
            with self.subTest(markup=markup):
                await self.page.set_content(markup)
                self.assertFalse(await self.driver._publish_signal())


if __name__ == '__main__':
    unittest.main()
