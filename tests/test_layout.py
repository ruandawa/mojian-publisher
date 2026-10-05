"""Local formatting contracts shared by preview and publication."""
import unittest

from bs4 import BeautifulSoup

from publisher.rendering import export_document, publication_html, wechat_compatible_html


IMAGE = '/assets/' + 'a' * 32 + '.jpg'


class PublicationLayoutTests(unittest.TestCase):
    def test_table_cards_keep_rich_cells_and_order(self):
        markup = f'''<table><thead><tr><th>模型</th><th>说明</th></tr></thead><tbody>
        <tr><td><strong>甲</strong></td><td><a href="https://example.com/a">原文</a><br><img src="{IMAGE}" alt="图示"></td></tr>
        <tr><td>乙</td><td><em>第二项</em></td></tr></tbody></table>'''
        converted = publication_html(markup, 'html')
        soup = BeautifulSoup(converted, 'html.parser')
        self.assertIsNone(soup.find('table'))
        self.assertEqual(soup.find('strong').get_text(), '甲')
        self.assertEqual(soup.find('a')['href'], 'https://example.com/a')
        self.assertEqual([img['src'] for img in soup.find_all('img')], [IMAGE])
        self.assertEqual(soup.find('em').get_text(), '第二项')
        self.assertEqual(soup.get_text().count('模型'), 2)
        self.assertLess(soup.get_text().index('甲'), soup.get_text().index('乙'))
        self.assertIn('border-left:3px', converted)
        self.assertIn('background:#edf5ef', converted)

    def test_nested_blocks_in_table_are_valid_and_images_are_not_flattened(self):
        converted = publication_html(f'''<table><tr><th>项目</th><th>详情</th></tr>
        <tr><td><p>字段</p></td><td><p>正文<strong>重点</strong></p>
        <ul><li>列表项</li></ul><p><img src="{IMAGE}"></p></td></tr></table>''', 'html')
        soup = BeautifulSoup(converted, 'html.parser')
        self.assertEqual(len(soup.find_all('img')), 1)
        self.assertEqual(soup.find('strong').get_text(), '重点')
        self.assertEqual(soup.find('li').get_text(), '列表项')
        self.assertTrue(all(paragraph.find('p') is None for paragraph in soup.find_all('p')))

    def test_key_value_table_uses_first_cell_as_card_heading(self):
        converted = publication_html('| 项目 | 内容 |\n|---|---|\n| 模型 | **甲** |\n| 规格 | 128K |')
        soup = BeautifulSoup(converted, 'html.parser')
        self.assertEqual(soup.get_text().count('项目'), 1)
        self.assertEqual(soup.get_text().count('内容'), 1)
        self.assertEqual(soup.find('strong').get_text(), '甲')
        self.assertIn('模型', soup.get_text())
        self.assertIn('128K', soup.get_text())
        self.assertEqual(wechat_compatible_html(converted), converted)

    def test_metric_by_model_table_becomes_one_card_per_model(self):
        converted = publication_html(f'''<table><tr><th>指标 / 模型</th><th><strong>模型甲</strong></th><th>模型乙</th></tr>
        <tr><td>上下文</td><td>128K</td><td>64K</td></tr>
        <tr><td>示例</td><td><a href="https://example.com/a">甲链接</a><img src="{IMAGE}"></td><td><em>乙说明</em></td></tr></table>''', 'html')
        soup = BeautifulSoup(converted, 'html.parser')
        self.assertEqual(soup.get_text().count('模型甲'), 1)
        self.assertEqual(soup.get_text().count('模型乙'), 1)
        self.assertEqual(soup.get_text().count('上下文'), 2)
        self.assertLess(soup.get_text().index('128K'), soup.get_text().index('模型乙'))
        self.assertGreater(soup.get_text().index('64K'), soup.get_text().index('模型乙'))
        self.assertEqual(soup.find('a')['href'], 'https://example.com/a')
        self.assertEqual(soup.find('em').get_text(), '乙说明')
        self.assertEqual(len(soup.find_all('img')), 1)
        self.assertEqual(wechat_compatible_html(converted), converted)

    def test_nested_table_preserves_both_values(self):
        converted = publication_html('''<table><tr><td>外层</td><td>
        <table><tr><td><strong>内层</strong></td></tr></table></td></tr></table>''', 'html')
        soup = BeautifulSoup(converted, 'html.parser')
        self.assertIn('外层', soup.get_text())
        self.assertEqual(soup.find('strong').get_text(), '内层')
        self.assertIsNone(soup.find('table'))

    def test_heading_image_in_header_is_not_duplicated_by_cards(self):
        converted = publication_html(f'''<table><tr><th><img src="{IMAGE}" alt="字段图"></th><th>值</th></tr>
        <tr><td>甲</td><td>1</td></tr><tr><td>乙</td><td>2</td></tr></table>''', 'html')
        soup = BeautifulSoup(converted, 'html.parser')
        self.assertEqual(len(soup.find_all('img')), 1)
        self.assertIn('字段图', soup.get_text())

    def test_standalone_image_has_caption_but_inline_image_does_not(self):
        converted = publication_html(f'![基准测试对比图]({IMAGE})\n\n文字![图标]({IMAGE})结尾。')
        soup = BeautifulSoup(converted, 'html.parser')
        self.assertEqual(len(soup.find_all('img')), 2)
        self.assertIn('基准测试对比图', soup.get_text())
        self.assertNotIn('图标', soup.get_text())
        caption = next(paragraph for paragraph in soup.find_all('p') if paragraph.get_text() == '基准测试对比图')
        self.assertIn('font-size:12px', caption['style'])
        self.assertEqual(wechat_compatible_html(converted), converted)

    def test_existing_image_caption_is_formatted_without_duplication(self):
        converted = publication_html(f'<p><img src="{IMAGE}" alt="基准图"></p><p>基准图</p>', 'html')
        soup = BeautifulSoup(converted, 'html.parser')
        self.assertEqual(soup.get_text().count('基准图'), 1)
        self.assertIn('font-size:12px', soup.find_all('p')[-1]['style'])

    def test_fourth_level_heading_in_quote_and_long_code_do_not_lose_structure(self):
        converted = publication_html('> #### 提醒\n>\n> 保持结构。\n\n```text\n' + '宽字符' * 80 + '\n    缩进\n```')
        soup = BeautifulSoup(converted, 'html.parser')
        self.assertIn('font-size:16px', soup.find('h4')['style'])
        self.assertIn('border-left:3px', soup.find('h4')['style'])
        code = next(paragraph for paragraph in soup.find_all('p') if '宽字符' in paragraph.get_text())
        self.assertIn('word-break:break-word', code['style'])
        self.assertIn('\u00a0' * 4 + '缩进', code.get_text())

    def test_quote_keeps_accent_background_and_inline_content(self):
        converted = publication_html('> **重点**与[出处](https://example.com)\n>\n> 第二段。', theme='warm')
        soup = BeautifulSoup(converted, 'html.parser')
        self.assertIsNone(soup.find('blockquote'))
        self.assertEqual(soup.find('strong').get_text(), '重点')
        self.assertEqual(soup.find('a')['href'], 'https://example.com')
        paragraphs = soup.find_all('p')
        self.assertEqual(len(paragraphs), 2)
        for paragraph in paragraphs:
            self.assertIn('border-left:3px solid #a36136', paragraph['style'])
            self.assertIn('background:#faf1e7', paragraph['style'])

    def test_code_keeps_indentation_blank_lines_and_operators(self):
        code = 'if x <= 1.5:\n    a = "甲"\n\n    print(a)\n'
        converted = publication_html('```python\n' + code + '```')
        soup = BeautifulSoup(converted, 'html.parser')
        self.assertIsNone(soup.find('pre'))
        paragraph = soup.find('p')
        self.assertIn('font-family:Consolas', paragraph['style'])
        self.assertIn('white-space:pre-wrap', paragraph['style'])
        self.assertEqual(len(paragraph.find_all('br')), 4)
        readback = ''.join('\n' if getattr(node, 'name', None) == 'br' else str(node) for node in paragraph.contents)
        self.assertEqual(readback.replace('\u00a0', ' '), code)

    def test_headings_and_lists_keep_mobile_typography_without_section(self):
        converted = publication_html('## 小标题\n\n第一段\n\n- 第一项\n- 第二项\n\n**结尾**')
        soup = BeautifulSoup(converted, 'html.parser')
        self.assertIsNone(soup.find('section'))
        self.assertIn('font-size:20px', soup.find('h2')['style'])
        self.assertIn('margin:0 0 20px', soup.find('p')['style'])
        self.assertEqual([li.get_text() for li in soup.find_all('li')], ['第一项', '第二项'])
        self.assertIn('font-size:16px', soup.find('li')['style'])

    def test_export_uses_exact_publication_fragment_and_conversion_is_idempotent(self):
        body = '> 引言\n\n| 项目 | 结果 |\n|---|---|\n| **甲** | 完成 |\n\n```text\n  ok\n```'
        fragment = publication_html(body, theme='ink')
        self.assertEqual(wechat_compatible_html(fragment, 'ink'), fragment)
        exported = export_document({'title':'测试', 'body':body, 'format':'markdown', 'theme':'ink'})
        self.assertIn(fragment, exported)


if __name__ == '__main__':
    unittest.main()
