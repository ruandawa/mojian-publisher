import json
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock
from PIL import Image

from publisher.browser import BrowserDriver, NeedsUser
from publisher.store import Store, now
from publisher.rendering import save_image


ARTICLE = {'title':'编辑器适配测试','author':'','digest':'','body':'测试正文','format':'markdown','theme':'jade','cover':'','source':'manual'}


class EditorRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.tmp.name))
        article=self.store.save_article(ARTICLE)
        self.job=self.store.enqueue(article['id'],'draft',now())
        self.browser=BrowserDriver(self.store)
        self.browser.page=Mock()
        self.browser.page.is_closed.return_value=False
        self.browser.page.url='https://mp.weixin.qq.com/cgi-bin/appmsg?token=fixture'
        self.browser.page.goto=AsyncMock()
        self.browser.page.screenshot=AsyncMock()
        self.browser._account=AsyncMock()

    def tearDown(self):
        self.tmp.cleanup()

    async def test_missing_editor_is_retryable_before_any_fill(self):
        self.store.claim_due()
        self.browser._editor_controls=AsyncMock(side_effect=NeedsUser('未找到标题输入区'))
        await self.browser.execute(self.store.job(self.job['id']))
        result=self.store.job(self.job['id'])
        self.assertEqual(result['stage'],'')
        self.assertEqual(result['status'],'waiting_user')
        self.assertIn('未找到标题输入区',result['message'])

    async def test_resume_requires_the_current_article_and_leaves_state_on_mismatch(self):
        self.store.update_job(self.job['id'],status='needs_review',stage='editing')
        self.browser._current_editor=AsyncMock(side_effect=NeedsUser('标题与任务不一致'))
        with self.assertRaisesRegex(ValueError,'标题与任务不一致'):
            await self.browser.prepare_resume_editor(self.store.job(self.job['id']))
        self.assertEqual(self.store.job(self.job['id'])['status'],'needs_review')
        self.browser.page.goto.assert_not_awaited()

    async def test_resume_binds_to_existing_page_without_navigating_or_filling(self):
        self.store.update_job(self.job['id'],status='needs_review',stage='editing')
        self.browser._current_editor=AsyncMock(return_value=(Mock(),Mock()))
        await self.browser.prepare_resume_editor(self.store.job(self.job['id']))
        self.assertEqual(self.store.job(self.job['id'])['stage'],'resume_editor')
        self.assertIs(self.browser.resume_page,self.browser.page)
        self.browser.page.goto.assert_not_awaited()

    async def test_restart_cannot_turn_resume_into_a_new_article(self):
        self.store.update_job(self.job['id'],status='running',stage='resume_editor')
        await self.browser.execute(self.store.job(self.job['id']))
        self.assertEqual(self.store.job(self.job['id'])['status'],'needs_review')
        self.browser.page.goto.assert_not_awaited()

    async def test_resume_rejects_uncertain_save_or_publish(self):
        for stage in ['saving','publishing','draft_saved']:
            self.store.update_job(self.job['id'],status='needs_review',stage=stage)
            with self.assertRaises(ValueError):
                await self.browser.prepare_resume_editor(self.store.job(self.job['id']))
        self.browser._account.assert_not_awaited()

    async def test_upload_failure_keeps_article_body_and_identifies_step(self):
        raw = io.BytesIO()
        Image.new('RGB',(32,24),'green').save(raw,'PNG')
        cover = save_image(raw.getvalue(),self.store.root/'assets')
        article = self.store.save_article({**ARTICLE, 'cover':cover})
        job = self.store.enqueue(article['id'],'draft',now())
        editor = Mock()
        self.browser._editor_controls = AsyncMock(return_value=(Mock(),editor))
        self.browser._fill_field = AsyncMock()
        self.browser._insert_html = AsyncMock()
        self.browser._upload = AsyncMock(side_effect=TimeoutError('sensitive token=fixture'))
        self.browser._save = AsyncMock()
        await self.browser.execute(job)
        self.browser._insert_html.assert_awaited_once()
        content = self.browser._insert_html.await_args.args[1]
        self.assertIn('测试正文',content)
        self.assertIn('style=',content)
        self.browser._save.assert_not_awaited()
        result = self.store.job(job['id'])
        self.assertEqual(result['status'],'needs_review')
        self.assertIn('上传图片 1/1',result['message'])
        self.assertIn('TimeoutError',result['message'])
        diagnostic = (self.store.root/'evidence'/(job['id']+'.json')).read_text(encoding='utf-8')
        self.assertNotIn('fixture',diagnostic)
        self.assertTrue(json.loads(diagnostic)['frames'])

    async def test_metadata_failure_happens_after_body_but_before_save(self):
        editor = Mock(inner_text=AsyncMock(return_value='测试正文'))
        self.browser._editor_controls = AsyncMock(return_value=(Mock(),editor))
        self.browser._fill_field = AsyncMock()
        calls = []
        async def insert(*args):
            calls.append('body')
        async def metadata(*args):
            self.assertIn('body',calls)
            raise NeedsUser('摘要未找到')
        self.browser._insert_html = AsyncMock(side_effect=insert)
        self.browser._fill_metadata = AsyncMock(side_effect=metadata)
        self.browser._save = AsyncMock()
        await self.browser.execute(self.job)
        self.browser._save.assert_not_awaited()
        result = self.store.job(self.job['id'])
        self.assertIn('填写作者与摘要',result['message'])
        self.assertEqual(result['stage'],'editing')

    async def test_text_only_body_is_not_inserted_twice_after_readback(self):
        editor = Mock(inner_text=AsyncMock(return_value='测试正文'))
        self.browser._editor_controls = AsyncMock(return_value=(Mock(),editor))
        self.browser._fill_field = AsyncMock()
        self.browser._insert_html = AsyncMock()
        self.browser._fill_metadata = AsyncMock()
        self.browser._save = AsyncMock()
        await self.browser.execute(self.job)
        self.browser._insert_html.assert_awaited_once()
        self.browser._save.assert_awaited_once()
        result = self.store.job(self.job['id'])
        self.assertEqual(result['status'],'drafted')

    async def test_core_controls_do_not_require_metadata(self):
        self.browser._title = AsyncMock(return_value='title')
        self.browser._editor = AsyncMock(return_value='body')
        self.browser._field = AsyncMock(side_effect=AssertionError('metadata probed too soon'))
        result = await self.browser._editor_controls({**ARTICLE,'digest':'有摘要','author':'作者'})
        self.assertEqual(result,('title','body'))
        self.browser._field.assert_not_awaited()


if __name__=='__main__':
    unittest.main()
