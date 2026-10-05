"""Offline cover DOM variants; no WeChat session or network access."""
import unittest
from unittest.mock import Mock

from playwright.async_api import async_playwright

from publisher.browser import BrowserDriver, NeedsUser


class CoverTests(unittest.IsolatedAsyncioTestCase):
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

    async def fixture(self, extra='', wrong=False):
        final = 'other' if wrong else 'cover'
        await self.page.set_content(f'''
            <div class="js_cover_btn_area" style="display:none">hidden</div>
            <div class="js_cover_btn_area"><button class="js_cover_btn_area"
                 onclick="document.querySelector('#choose').hidden=false">选择封面</button></div>
            {extra}
            <button id="choose" hidden onclick="document.querySelector('.weui-desktop-dialog_img-picker').hidden=false">从正文选择</button>
            <div class="weui-desktop-dialog_img-picker" hidden>
                <div class="appmsg_content_img" style="width:80px;height:80px" onclick="this.dataset.selected='true'">
                    <img data-src="https://mmbiz.qpic.cn/article/cover/0?cache=new"></div>
                <button onclick="this.parentElement.hidden=true;document.querySelector('#confirm').hidden=false">下一步</button>
            </div>
            <button id="confirm" hidden>确认</button>
            <div id="js_cover_area"></div>
            <script>document.querySelector('#confirm').onclick = function() {{
                this.hidden=true;
                const img=document.createElement('img');
                img.setAttribute('data-src','https://mmbiz.qpic.cn/article/{final}/0?wx_fmt=jpeg');
                document.querySelector('#js_cover_area').append(img);
            }};</script>''')

    async def test_hidden_and_nested_controls_lazy_image_and_query_rewrite(self):
        await self.fixture()
        await self.driver._cover('https://mmbiz.qpic.cn/article/cover/0?cache=old')
        self.assertEqual(self.driver.cover_diagnostics['entry_count'],2)
        self.assertEqual(self.driver.cover_diagnostics['leaf_count'],1)
        self.assertEqual(await self.page.locator('#js_cover_area img').count(),1)

    async def test_two_separate_cover_controls_stop_before_any_click(self):
        await self.fixture('<button class="js_cover_btn_area">另一个封面</button>')
        with self.assertRaisesRegex(NeedsUser, '无法唯一识别'):
            await self.driver._cover('https://mmbiz.qpic.cn/article/cover/0')
        self.assertFalse(await self.page.locator('#choose').is_visible())

    async def test_unrelated_picker_image_stops(self):
        await self.fixture()
        with self.assertRaisesRegex(NeedsUser, '未找到本篇封面'):
            await self.driver._cover('https://mmbiz.qpic.cn/article/unrelated/0')
        self.assertFalse(await self.page.locator('#confirm').is_visible())

    async def test_popup_entry_appears_after_opener_returns(self):
        await self.page.set_content('''<button id="delayed" hidden
            onclick="window.clicked=true">从正文选择</button>
            <script>setTimeout(()=>document.querySelector('#delayed').hidden=false,350)</script>''')
        await self.driver._click_text('从正文选择')
        self.assertTrue(await self.page.evaluate('window.clicked'))

    async def test_current_editor_cover_can_be_read_from_new_preview_class(self):
        await self.page.set_content('''<div class="js_cover_preview_new"
            style="background-image:url('https://mmbiz.qpic.cn/article/cover/0?wx_fmt=jpeg')"></div>''')
        await self.driver._cover('https://mmbiz.qpic.cn/article/cover/0?cache=old')
        self.assertNotIn('entry_count',self.driver.cover_diagnostics)

    async def test_selected_image_wrapper_receives_click_over_thumbnail_mask(self):
        await self.fixture()
        await self.page.locator('.appmsg_content_img').evaluate('''el=>{
            const wrapper=document.createElement('div');
            wrapper.style='position:relative;width:80px;height:80px';
            el.replaceWith(wrapper);wrapper.append(el);
            const mask=document.createElement('span');
            mask.style='position:absolute;inset:0;background:#ccc';wrapper.append(mask);
            wrapper.onclick=()=>wrapper.dataset.selected='true';
        }''')
        await self.driver._cover('https://mmbiz.qpic.cn/article/cover/0')
        self.assertEqual(await self.page.locator('.appmsg_content_img').locator('..').get_attribute('data-selected'),'true')
