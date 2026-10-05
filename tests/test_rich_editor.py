"""Offline DOM regression tests. Fresh browser context; every request is blocked.

These exercise our editing code, not WeChat's editor or account.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from playwright.async_api import async_playwright

from publisher.browser import BrowserDriver, NeedsUser
from publisher.rendering import wechat_compatible_html


class RichEditorTests(unittest.IsolatedAsyncioTestCase):
    def test_wechat_compatible_html_keeps_unsupported_block_text(self):
        content = '''<section><blockquote><p>引言</p></blockquote>
            <pre>路由器\n  ▼\n完成</pre>
            <table><tr><th>指标</th><th>值</th></tr><tr><td>延迟</td><td>120ms</td></tr></table></section>'''
        compatible = wechat_compatible_html(content)
        self.assertNotIn('<section', compatible)
        self.assertNotIn('<blockquote', compatible)
        self.assertNotIn('<pre', compatible)
        self.assertNotIn('<table', compatible)
        self.assertIn('引言', compatible)
        self.assertIn('路由器', compatible)
        self.assertIn('120ms', compatible)

    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(channel='msedge', headless=True)
        self.context = await self.browser.new_context()
        await self.context.route('**/*', lambda route: route.abort())
        self.page = await self.context.new_page()
        await self.page.set_content('''<input id="title" value="标题保持不变">
            <div id="ueditor_0" contenteditable="true" style="min-height:100px">旧正文</div>''')
        self.driver = BrowserDriver(Mock())
        self.driver.page = self.page
        self.editor = self.page.locator('#ueditor_0')

    async def asyncTearDown(self):
        await self.browser.close()
        await self.playwright.stop()

    async def test_rich_body_keeps_all_text_images_and_formatting(self):
        content = '<h2 style="color:rgb(36,118,93)">小标题</h2><p>第一段<strong>重点</strong></p><p>第二段</p><img src="https://example.invalid/pic.jpg">'
        await self.driver._insert_html(self.editor, content)
        self.assertEqual(await self.page.locator('#title').input_value(),'标题保持不变')
        self.assertEqual(await self.editor.locator('h2').inner_text(),'小标题')
        self.assertEqual(await self.editor.locator('strong').inner_text(),'重点')
        self.assertEqual(await self.editor.locator('img').count(),1)
        self.assertIn('第二段',await self.editor.inner_text())

    async def test_truncated_tail_cannot_pass_first_eighty_characters(self):
        await self.editor.evaluate('''el => el.addEventListener('input', () => {
            el.textContent = el.textContent.slice(0, 90);
        })''')
        with self.assertRaisesRegex(NeedsUser,'完整内容不一致'):
            await self.driver._insert_html(self.editor,'<p>'+'正文'*100+'不能丢失的结尾</p>')

    async def test_editor_punctuation_normalization_does_not_false_fail(self):
        # WeChat's editor normalizes punctuation during its ProseMirror input
        # transaction. The ordered content must still be complete, while a
        # presentation-only punctuation rewrite should not pause the job.
        await self.editor.evaluate('''el => el.addEventListener('input', () => {
            el.innerHTML = el.innerHTML.replace('，', ',').replace('“', '"').replace('”', '"');
        })''')
        await self.driver._insert_html(self.editor, '<p>第一段，包含“重点”内容。</p>')
        self.assertIn('第一段', await self.editor.inner_text())

    async def test_image_loss_stops_save_instead_of_plaintext_fallback(self):
        await self.editor.evaluate('''el => el.addEventListener('input', () => {
            el.querySelectorAll('img').forEach(img => img.remove());
        })''')
        with self.assertRaisesRegex(NeedsUser,'图片回读'):
            await self.driver._insert_html(self.editor,'<p><strong>重点</strong></p><img src="https://example.invalid/a.jpg">')
        self.assertEqual(await self.editor.locator('strong').count(),1)

    async def test_image_url_rewrite_and_lazy_attributes_do_not_false_fail(self):
        # WeChat commonly moves the canonical URL to data-src and appends
        # format/cache parameters to src while handling a paste.  Identity is
        # the host/path, so this must remain a successful readback.
        await self.editor.evaluate('''el => el.addEventListener('input', () => {
            el.querySelectorAll('img').forEach(img => {
                const original = img.getAttribute('src');
                img.setAttribute('data-src', original + '?wx_fmt=jpeg&cache=123');
                img.setAttribute('src', original + '?wx_lazy=1');
            });
        })''')
        await self.driver._insert_html(
            self.editor,
            '<p>带图正文</p><img src="https://mmbiz.qpic.cn/example/cover/0">',
        )
        self.assertEqual(await self.editor.locator('img').count(), 1)

    async def test_image_replacement_with_same_count_stops_save(self):
        await self.editor.evaluate('''el => el.addEventListener('input', () => {
            el.querySelectorAll('img').forEach(img => {
                img.setAttribute('src', 'https://mmbiz.qpic.cn/example/other/0');
            });
        })''')
        with self.assertRaisesRegex(NeedsUser, '图片回读'):
            await self.driver._insert_html(
                self.editor,
                '<p>带图正文</p><img src="https://mmbiz.qpic.cn/example/cover/0">',
            )

    async def test_prosemirror_empty_caret_separator_is_not_an_article_image(self):
        await self.editor.evaluate('''el => el.addEventListener('input', () => {
            if(el.querySelector('.ProseMirror-separator')) return;
            const separator = document.createElement('img');
            separator.className = 'ProseMirror-separator';
            el.append(separator);
        })''')
        await self.driver._insert_html(self.editor,'<p>正文</p><img src="https://mmbiz.qpic.cn/real/0">')
        self.assertEqual(await self.editor.locator('img').count(),2)

    async def test_unknown_empty_image_is_not_silently_ignored(self):
        await self.editor.evaluate('''el => el.addEventListener('input', () => {
            el.append(document.createElement('img'));
        })''')
        with self.assertRaisesRegex(NeedsUser,'图片回读'):
            await self.driver._insert_html(self.editor,'<p>正文</p><img src="https://mmbiz.qpic.cn/real/0">')

    async def test_losing_a_duplicate_image_is_detected(self):
        await self.editor.evaluate('''el => el.addEventListener('input', () => {
            el.querySelector('img')?.remove();
        })''')
        with self.assertRaisesRegex(NeedsUser,'图片回读'):
            await self.driver._insert_html(self.editor,'<p>重复图不能漏</p>'+'<img src="https://mmbiz.qpic.cn/real/0">'*2)

    async def test_code_operator_and_decimal_loss_are_detected(self):
        await self.editor.evaluate('''el => el.addEventListener('input', () => {
            el.innerHTML = el.innerHTML.replace('1.2 + x', '12 x');
        })''')
        with self.assertRaisesRegex(NeedsUser,'完整内容不一致'):
            await self.driver._insert_html(self.editor,'<p>value = 1.2 + x;</p>')

    async def test_rejected_insertion_does_not_type_over_existing_body(self):
        await self.page.evaluate('document.execCommand = () => false')
        with self.assertRaisesRegex(NeedsUser,'拒绝写入'):
            await self.driver._insert_html(self.editor,'<p>新正文</p>')
        self.assertEqual(await self.editor.inner_text(),'旧正文')

    async def test_multiple_body_editors_are_not_guessed(self):
        await self.page.set_content('<div class="ProseMirror" contenteditable="true">甲</div><div class="ProseMirror" contenteditable="true">乙</div>')
        with self.assertRaisesRegex(NeedsUser,'ProseMirror'):
            await self.driver._editor(wait=False)

    async def test_body_label_is_distinct_from_an_editable_search_control(self):
        await self.page.set_content('''<div role="textbox" contenteditable="true" aria-label="搜索">工具栏</div>
            <div class="editor-body" contenteditable="true" data-placeholder="请输入正文内容"
                 style="width:700px;height:500px">旧正文</div>''')
        editor = await self.driver._editor()
        self.assertEqual(await editor.get_attribute('class'),'editor-body')

    async def test_shared_rich_editor_class_with_child_placeholders(self):
        await self.page.set_content('''
            <div id="t" class="ProseMirror" contenteditable="true"><p data-placeholder="请在这里输入标题">标题</p></div>
            <div id="a" class="ProseMirror" contenteditable="true"><p data-placeholder="请输入作者">作者</p></div>
            <div id="d" class="ProseMirror" contenteditable="true"><p data-placeholder="请输入摘要">摘要</p></div>
            <div id="b" class="ProseMirror" contenteditable="true"><p data-placeholder="从这里开始写正文">旧正文</p></div>''')
        editor = await self.driver._editor(wait=False)
        self.assertEqual(await editor.get_attribute('id'),'b')
        await self.driver._insert_html(editor,'<p>真正的正文</p>')
        for selector, value in [('#t','标题'),('#a','作者'),('#d','摘要')]:
            self.assertEqual(await self.page.locator(selector).inner_text(),value)
        self.assertEqual(len(self.driver.editor_diagnostics),4)
        selected = [item for item in self.driver.editor_diagnostics if item.get('selected')]
        self.assertEqual(selected[0]['id'],'b')

    async def test_nested_editable_paragraph_is_not_a_second_body(self):
        await self.page.set_content('''<div class="ProseMirror" contenteditable="true"
            data-placeholder="正文"><p class="ProseMirror" contenteditable="true">正文段落</p></div>''')
        editor = await self.driver._editor(wait=False)
        self.assertEqual(await editor.evaluate('el=>el.tagName'),'DIV')

    async def test_larger_body_does_not_break_a_real_ambiguity(self):
        await self.page.set_content('''
            <div class="ProseMirror" contenteditable="true" data-placeholder="正文" style="width:900px;height:400px">甲</div>
            <div class="ProseMirror" contenteditable="true" data-placeholder="正文" style="width:400px;height:50px">乙</div>''')
        with self.assertRaisesRegex(NeedsUser,'无法唯一确认正文'):
            await self.driver._editor(wait=False)
        self.assertEqual(await self.page.locator('.ProseMirror').all_inner_texts(),['甲','乙'])

    async def test_title_in_parent_page_does_not_hide_iframe_body(self):
        await self.page.set_content('''
            <div class="ProseMirror" contenteditable="true"><p data-placeholder="请输入标题">保留标题</p></div>
            <iframe srcdoc="<body contenteditable='true'>旧正文</body>"></iframe>''')
        editor = await self.driver._editor()
        await self.driver._insert_html(editor,'<p>frame 正文</p>')
        self.assertEqual(await editor.inner_text(),'frame 正文')
        self.assertEqual(await self.page.locator('.ProseMirror').inner_text(),'保留标题')

    async def test_false_editable_and_aria_labelled_metadata_are_excluded(self):
        await self.page.set_content('''
            <span id="author-label">作者</span>
            <div class="ProseMirror" contenteditable="true" aria-labelledby="author-label">作者名</div>
            <div contenteditable="false"><div id="body" contenteditable="true">正文</div></div>
            <iframe srcdoc="<body contenteditable='false'>只读预览</body>"></iframe>''')
        editor = await self.driver._editor(wait=False)
        self.assertEqual(await editor.get_attribute('id'),'body')

    async def test_body_word_in_article_text_is_not_editor_identity(self):
        await self.page.set_content('''
            <div class="ProseMirror" contenteditable="true">这是提到正文的文章内容</div>
            <div class="ProseMirror" contenteditable="true">另一篇</div>''')
        with self.assertRaises(NeedsUser):
            await self.driver._editor(wait=False)

    async def test_current_image_label_uploads_without_old_toolbar_id(self):
        await self.page.set_content('''<div id="ueditor_0" contenteditable="true">已经填入的正文</div>
            <button onclick="document.querySelector('#menu').hidden=false">图片</button>
            <div id="menu" hidden><button onclick="document.querySelector('#file').click()">本地上传</button></div>
            <input type="file" id="file" hidden onchange="document.querySelector('#ueditor_0').insertAdjacentHTML('beforeend', '&lt;img src=https://mmbiz.qpic.cn/local-fixture&gt;')">''')
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'image.jpg'
            path.write_bytes(b'local fixture only')
            result = await self.driver._upload(self.page.locator('#ueditor_0'),path)
        self.assertEqual(result,'https://mmbiz.qpic.cn/local-fixture')
        self.assertIn('已经填入的正文',await self.page.locator('#ueditor_0').inner_text())

    async def test_missing_local_image_does_not_touch_article(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(NeedsUser,'文件不存在'):
                await self.driver._upload(self.editor,Path(temp)/'missing.jpg')
        self.assertEqual(await self.editor.inner_text(),'旧正文')

    async def test_iframe_body_editor_is_supported(self):
        await self.page.set_content('''<iframe id="wechat-editor" srcdoc="<body contenteditable='true'>旧 iframe 正文</body>"></iframe>''')
        iframe_editor = await self.driver._editor()
        await self.driver._insert_html(iframe_editor,'<p>新的 iframe 正文</p>')
        self.assertEqual(await iframe_editor.inner_text(),'新的 iframe 正文')

    async def test_contenteditable_without_true_attribute_is_supported(self):
        await self.page.set_content('<div id="body" contenteditable>旧正文</div>')
        editor = await self.driver._editor()
        await self.driver._insert_html(editor,'<p>新正文</p>')
        self.assertEqual(await editor.inner_text(),'新正文')

    async def test_single_generic_editable_root_is_used_after_metadata_exclusion(self):
        await self.page.set_content('<div contenteditable>旧正文</div>')
        editor = await self.driver._editor(wait=False)
        await self.driver._insert_html(editor,'<p>新正文</p>')
        self.assertEqual(await editor.inner_text(),'新正文')


if __name__ == '__main__':
    unittest.main()
