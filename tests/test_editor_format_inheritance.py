"""Offline native editing regressions; no account profile or network requests."""
import unittest
from unittest.mock import Mock

from playwright.async_api import async_playwright

from publisher.browser import BrowserDriver, NeedsUser
from publisher.rendering import publication_html


class EditorFormatInheritanceTests(unittest.IsolatedAsyncioTestCase):
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

    async def _replace_from_bold_caret(self, old_html, incoming=None):
        await self.page.set_content(
            '<div id="body" contenteditable="true" '
            'style="width:600px;min-height:300px;font-weight:400">'
            + old_html + '</div>')
        editor = self.page.locator('#body')
        await editor.evaluate('''el => {
            el.focus();
            const text = el.querySelector('strong, b').firstChild;
            const range = document.createRange();
            range.setStart(text, Math.min(1, text.length));
            range.collapse(true);
            const selection = window.getSelection();
            selection.removeAllRanges(); selection.addRange(range);
        }''')
        if incoming is None:
            incoming = ('<p>新正文普通字<strong>保留重点</strong>恢复普通字</p>'
                        '<p style="border-left:3px solid #24765d;padding:16px;'
                        'background:#edf2e8">'
                        '引言常规<strong>强调</strong>尾段普通</p>')
        await self.driver._insert_html(editor, incoming)
        return editor

    async def test_full_replacement_does_not_inherit_prior_bold_caret(self):
        cases = (
            '<p>旧普通<strong>旧重点</strong>旧普通结尾</p>',
            '<p><strong>旧整段加粗</strong></p>',
            '<p><strong>旧首部加粗</strong>旧普通结尾</p>',
            '<p>旧普通</p><p><strong>旧尾部加粗</strong></p>',
            '<h2 style="font-weight:700">旧标题<strong>旧重点</strong></h2>',
            '<div><p><strong style="font-weight:700;color:#24765d">旧重点</strong></p></div>',
        )
        for old_html in cases:
            with self.subTest(old_html=old_html):
                editor = await self._replace_from_bold_caret(old_html)
                self.assertEqual(await editor.locator('strong, b').all_text_contents(),
                                 ['保留重点', '强调'], await editor.inner_html())
                weights = await editor.evaluate('''el => {
                    const weights = [];
                    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
                    while(walker.nextNode()) {
                        const node = walker.currentNode;
                        if(node.textContent.trim() && !node.parentElement.closest('strong, b'))
                            weights.push(getComputedStyle(node.parentElement).fontWeight);
                    }
                    return weights;
                }''')
                self.assertTrue(all(int(weight) < 600 for weight in weights),
                                (weights, await editor.inner_html()))

    async def test_repeated_formatted_quote_replacement_keeps_inline_emphasis(self):
        # Same formatting path used for an imported article. A quote can have
        # bold headings and bold phrases without making its normal body bold.
        content = publication_html(
            '> ### 引言标题\n>\n'
            '> 普通介绍 **强调片段** 普通结尾。\n>\n'
            '> - 普通清单 **产品名**、**型号名**；\n'
            '> - 普通售后说明。')
        editor = await self._replace_from_bold_caret(
            '<p><strong>旧稿重点</strong></p>', content)
        self.assertEqual(await editor.locator('strong, b, span[style*="font-weight:700"]').all_text_contents(),
                         ['强调片段', '产品名', '型号名'], await editor.inner_html())
        for _ in range(3):
            await editor.evaluate('''el => {
                el.focus();
                const node = el.querySelector('strong, b, span[style*="font-weight:700"]').firstChild;
                const range = document.createRange();
                range.setStart(node, 1); range.collapse(true);
                const selection = window.getSelection();
                selection.removeAllRanges(); selection.addRange(range);
            }''')
            await self.driver._insert_html(editor, content)
            self.assertEqual(await editor.locator('strong, b, span[style*="font-weight:700"]').all_text_contents(),
                             ['强调片段', '产品名', '型号名'],
                             await editor.inner_html())
            weights = await editor.evaluate('''el => {
                const result = [];
                const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
                while(walker.nextNode()) {
                    const node = walker.currentNode;
                    if(node.textContent.trim() && !node.parentElement.closest('strong, b, span[style*="font-weight:700"], h1, h2, h3, h4'))
                        result.push(getComputedStyle(node.parentElement).fontWeight);
                }
                return result;
            }''')
            self.assertTrue(all(int(weight) < 600 for weight in weights),
                            (weights, await editor.inner_html()))

    async def test_native_insertion_rejection_restores_original_nodes(self):
        await self.page.set_content('<div id="body" contenteditable="true"><h2>旧标题</h2>'
                                    '<p>旧正文<strong>旧重点</strong></p></div>')
        editor = self.page.locator('#body')
        original = await editor.inner_html()
        await editor.evaluate('''el => {
            window.originalNodes = Array.from(el.childNodes);
            document.execCommand = () => false;
        }''')
        with self.assertRaisesRegex(NeedsUser, '拒绝写入'):
            await self.driver._insert_html(editor, '<p>新正文</p>')
        self.assertEqual(await editor.inner_html(), original)
        self.assertTrue(await editor.evaluate(
            '(el) => Array.from(el.childNodes).every((node, i) => node === window.originalNodes[i])'))

    async def test_replacement_retains_heading_quote_and_three_image_semantics(self):
        images = ['https://mmbiz.qpic.cn/editor/img%d/0' % number for number in range(3)]
        content = publication_html('''<h2>新标题</h2><p>普通正文<strong>重点</strong></p>
            <blockquote><p>引言普通<strong>引言重点</strong>普通尾段</p></blockquote>
            ''' + ''.join('<p><img src="%s"></p>' % image for image in images), 'html')
        editor = await self._replace_from_bold_caret(
            '<h2 style="font-weight:700"><strong>旧标题</strong></h2>', content)
        self.assertEqual(await editor.locator('h2').all_text_contents(), ['新标题'])
        self.assertEqual(await editor.locator('img').evaluate_all('(els) => els.map(el => el.src)'), images)
        quote = editor.locator('p').filter(has_text='引言普通')
        self.assertEqual(await quote.count(), 1)
        self.assertIn('border-left', await quote.get_attribute('style'))
        self.assertLess(int(await quote.evaluate('(el) => getComputedStyle(el).fontWeight')), 600)


if __name__ == '__main__':
    unittest.main()
