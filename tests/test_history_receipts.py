"""Isolated fixtures for history association, rejection, and QR recovery."""
import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from playwright.async_api import async_playwright
from publisher.browser import BrowserDriver
from publisher.receipts import body_hash, public_article_url, select_record, snapshot_hash, text_key
from publisher.store import Store, now

REMOTE='https://mp.weixin.qq.com/cgi-bin/appmsg?appmsgid=100&action=edit'
PUBLIC='https://mp.weixin.qq.com/s?__biz=fixture&mid=200&idx=1&sn=fixture'


class HistoryReceiptTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.temp.name))
        self.store.save_settings({'account_name':'fixture-account'})
        article=self.store.save_article({'title':'流程测试','body':'正文','creation_source':'ai'})
        self.job=self.store.enqueue(article['id'],'publish',now())
        self.store.update_job(self.job['id'],status='needs_review',stage='publishing',step='verification',remote_url=REMOTE)
        self.job=self.store.job(self.job['id'])
        self.pw=await async_playwright().start()
        self.browser=await self.pw.chromium.launch(channel='msedge',headless=True)
        self.context=await self.browser.new_context()
        await self.context.route('**/*',lambda route:route.abort())
        self.page=await self.context.new_page()
        wrapped=Mock(wraps=self.page);wrapped.url=REMOTE+'&token=fixture'
        self.driver=BrowserDriver(self.store)
        self.driver.page=wrapped;self.driver.context=self.context
        self.driver.page_job_id=self.job['id']
        self.driver._account=AsyncMock()
        self.receipt={'schema':1,'job_id':self.job['id'],'snapshot_sha256':snapshot_hash(self.job),
            'account_name':'fixture-account','title':text_key(article['title']),'remote_url':REMOTE,
            'baseline_ids':['99'],'started_at':now(),'body_sha256':body_hash('<p>正文</p>'),
            'images':[],'record_id':'','last_result':''}
        self.driver._write_receipt(self.job,self.receipt)
        async def empty_history(): return await self.history([])
        self.driver._history_view=AsyncMock(side_effect=empty_history)

    async def asyncTearDown(self):
        await self.browser.close();await self.pw.stop();self.temp.cleanup()

    def row(self,**extra):
        return {'id':'200','title':'流程测试','state':'审核中','href':PUBLIC,'sent_at':str(int(time.time())),**extra}

    async def history(self,rows):
        page=await self.context.new_page()
        await page.set_content('<h1>发表记录</h1>')
        return page,rows

    async def test_record_selection_rejects_old_ambiguous_wrong_title_and_stale_time(self):
        self.assertIsNone(select_record([self.row(id='99')],self.receipt))
        self.assertIsNone(select_record([self.row(),self.row(id='201')],self.receipt))
        self.assertIsNone(select_record([self.row(title='其他文章')],self.receipt))
        self.assertIsNone(select_record([self.row(sent_at=str(int(time.time())-60))],self.receipt))
        self.assertEqual(select_record([self.row()],self.receipt)['id'],'200')

    async def test_snapshot_account_or_remote_changes_invalidate_recovery(self):
        self.assertTrue(self.driver._load_receipt(self.job))
        for key,value in [('title','变更'),('remote_url',REMOTE+'x'),('account_name','other')]:
            altered={**self.receipt,key:value};self.driver._write_receipt(self.job,altered)
            self.assertIsNone(self.driver._load_receipt(self.job))
        self.driver._write_receipt(self.job,self.receipt)
        self.driver.page_job_id=None
        self.assertTrue(self.driver.receipt_trackable(self.job))

    async def test_preview_and_lookalike_urls_are_never_publication(self):
        for url in (PUBLIC+'&tempkey=secret',PUBLIC+'&token=secret',PUBLIC.replace('qq.com','qq.com.evil.test'),PUBLIC.replace('mid=200','mid=201')):
            self.assertEqual(public_article_url(url,'200'),'')
        self.assertEqual(public_article_url(PUBLIC.replace('https:','http:'),'200'),PUBLIC)

    async def test_public_history_short_link_requires_valid_path_and_content_check(self):
        short='https://mp.weixin.qq.com/s/abcdefghijklmnopqrstuv'
        self.assertEqual(public_article_url(short+'#rd','200'),short)
        for bad in (short+'?tempkey=secret',short+'?token=secret',short+'?untrusted=1',
                    short+'/',short[:-1],short.replace('qq.com','qq.com.evil.test'),
                    short.replace('mp.weixin.qq.com','user@mp.weixin.qq.com'),
                    short.replace('qq.com','qq.com:8080')):
            self.assertEqual(public_article_url(bad,'200'),'')
        async def view(): return await self.history([self.row(state='已发表',href=short)])
        self.driver._history_view=AsyncMock(side_effect=view)
        self.driver._read_public_article=AsyncMock(return_value=False)
        await self.driver._observe_history_receipt(self.job,force=True)
        self.assertEqual(self.store.job(self.job['id'])['status'],'review_pending')
        self.driver._read_public_article.return_value=True
        await self.driver._observe_history_receipt(self.store.job(self.job['id']),force=True)
        self.assertEqual(self.store.job(self.job['id'])['status'],'published')
        self.assertEqual(self.store.job(self.job['id'])['article_url'],short)

    async def test_review_is_persisted_once_without_success_or_navigation(self):
        async def view(): return await self.history([self.row()])
        self.driver._history_view=AsyncMock(side_effect=view)
        self.driver._read_public_article=AsyncMock()
        self.assertTrue(await self.driver._observe_history_receipt(self.job,force=True))
        current=self.store.job(self.job['id'])
        self.assertEqual((current['status'],current['step'],current['article_url']),('review_pending','review',''))
        stamp=current['updated_at']
        await self.driver._observe_history_receipt(current,force=True)
        self.assertEqual(self.store.job(self.job['id'])['updated_at'],stamp)
        self.driver._read_public_article.assert_not_awaited()
        self.assertEqual(self.page.url,'about:blank')

    async def test_platform_rejection_is_terminal_and_never_public_success(self):
        async def view(): return await self.history([self.row(state='发表失败')])
        self.driver._history_view=AsyncMock(side_effect=view)
        await self.driver._observe_history_receipt(self.job,force=True)
        current=self.store.job(self.job['id'])
        self.assertEqual((current['status'],current['stage'],current['article_url']),('failed','platform_rejected',''))

    async def test_success_requires_matching_accessible_public_content(self):
        async def view(): return await self.history([self.row(state='已发表')])
        self.driver._history_view=AsyncMock(side_effect=view)
        self.driver._read_public_article=AsyncMock(return_value=False)
        await self.driver._observe_history_receipt(self.job,force=True)
        self.assertEqual(self.store.job(self.job['id'])['status'],'review_pending')
        self.driver._read_public_article.return_value=True
        await self.driver._observe_history_receipt(self.store.job(self.job['id']),force=True)
        self.assertEqual(self.store.job(self.job['id'])['status'],'published')
        self.assertEqual(self.store.job(self.job['id'])['article_url'],PUBLIC)

    async def test_qr_refresh_does_nothing_after_record_exists(self):
        self.driver._observe_history_receipt=AsyncMock(return_value=True)
        self.driver._title=AsyncMock()
        await self.driver.refresh_verification(self.job)
        self.driver._title.assert_not_awaited()

    async def test_qr_refresh_same_draft_once_keeps_human_challenge(self):
        self.driver._observe_history_receipt=AsyncMock(return_value=False)
        await self.page.set_content('''<input id="title" value="流程测试"><span class="js_claim_source_selected">内容由AI生成</span>
          <div role="dialog" id="dialog"><h2>微信验证</h2><button class="weui-desktop-dialog__close-btn" onclick="settings()">关闭</button></div>
          <script>window.submits=0;window.auth=0;
          function settings(){document.querySelector('#dialog').innerHTML='<h2>发表</h2><button onclick="notice()">发表</button>'}
          function notice(){window.submits++;document.querySelector('#dialog').innerHTML='<h2>发表</h2><p>未开启群发通知</p><button onclick="qr()">继续发表</button>'}
          function qr(){window.submits++;document.querySelector('#dialog').innerHTML='<h2>微信验证</h2><button onclick="window.auth++">扫码验证</button>'}
          </script>''')
        await self.driver.refresh_verification(self.job)
        self.assertEqual(await self.page.evaluate('[window.submits,window.auth]'),[2,0])
        self.assertEqual(self.store.job(self.job['id'])['step'],'verification')

    async def test_wrong_draft_prevents_qr_refresh(self):
        self.driver._observe_history_receipt=AsyncMock(return_value=False)
        await self.page.set_content('<input id="title" value="别的文章"><div role="dialog">微信验证</div>')
        with self.assertRaises(ValueError):
            await self.driver.refresh_verification(self.job)

    async def test_qr_recovery_after_restart_rechecks_saved_body_and_stops_at_classic_challenge(self):
        self.receipt['verification_pending']=True
        self.driver._write_receipt(self.job,self.receipt)
        self.driver.page_job_id=None
        self.driver._observe_history_receipt=AsyncMock(return_value=False)
        self.driver.page.goto=AsyncMock()
        await self.page.set_content('''<input id="title" value="流程测试"><div id="body"><p>正文</p></div>
          <span class="js_claim_source_selected">内容由AI生成</span><button onclick="settings()">发表</button>
          <div role="dialog" id="dialog"></div><div class="dialog_wrp"></div>
          <script>window.sends=0;window.auth=0;
          function settings(){document.querySelector('#dialog').innerHTML='<h2>发表</h2><button onclick="qr()">发表</button>'}
          function qr(){window.sends++;document.querySelector('#dialog').innerHTML='';
            document.querySelector('.dialog_wrp').innerHTML='<div class="dialog"><h3>微信验证</h3><p>扫码后，请联系管理员进行验证</p><button onclick="window.auth++">扫码</button></div>'}
          </script>''')
        self.driver._editor_controls=AsyncMock(return_value=(self.page.locator('#title'),self.page.locator('#body')))
        await self.driver.refresh_verification(self.job,previous_not_confirmed=True)
        self.driver.page.goto.assert_awaited_once()
        self.assertEqual(await self.page.evaluate('[window.sends,window.auth]'),[1,0])
        self.assertEqual(self.driver.page_job_id,self.job['id'])
        self.assertTrue(self.driver._load_receipt(self.job)['verification_pending'])

    async def test_restart_without_proven_verification_cannot_navigate_or_publish(self):
        self.driver.page_job_id=None
        self.driver.page.goto=AsyncMock()
        with self.assertRaisesRegex(ValueError,'没有已确认'):
            await self.driver.refresh_verification(self.job)
        self.driver.page.goto.assert_not_awaited()

    async def test_proven_challenge_after_restart_still_requires_no_phone_approval_confirmation(self):
        self.receipt['verification_pending']=True
        self.driver._write_receipt(self.job,self.receipt)
        self.driver.page_job_id=None
        self.driver.page.goto=AsyncMock()
        self.driver._observe_history_receipt=AsyncMock(return_value=False)
        self.driver._click_text=AsyncMock()
        with self.assertRaisesRegex(ValueError,'上次未在手机'):
            await self.driver.refresh_verification(self.job)
        self.driver.page.goto.assert_not_awaited()
        self.driver._click_text.assert_not_awaited()

    async def test_restart_with_changed_saved_body_cannot_publish(self):
        self.receipt['verification_pending']=True
        self.driver._write_receipt(self.job,self.receipt)
        self.driver.page_job_id=None
        self.driver.page.goto=AsyncMock()
        self.driver._observe_history_receipt=AsyncMock(return_value=False)
        self.driver._click_text=AsyncMock()
        await self.page.set_content('<input id="title" value="流程测试"><div id="body">改过的正文</div>')
        self.driver._editor_controls=AsyncMock(return_value=(self.page.locator('#title'),self.page.locator('#body')))
        with self.assertRaisesRegex(ValueError,'正文发生变化'):
            await self.driver.refresh_verification(self.job,previous_not_confirmed=True)
        self.driver._click_text.assert_not_awaited()

    async def test_legacy_success_text_without_submission_baseline_does_not_complete(self):
        self.driver._receipt_path(self.job).unlink()
        self.driver._publish_signal=AsyncMock(return_value=True)
        self.driver._record_publish_receipt=AsyncMock()
        with self.assertRaisesRegex(ValueError,'缺少本次提交'):
            await self.driver.verify(self.job)
        self.driver._record_publish_receipt.assert_not_awaited()
        self.assertEqual(self.store.job(self.job['id'])['status'],'needs_review')

    async def test_receipt_image_keys_ignore_only_named_empty_editor_decorations(self):
        src='https://mmbiz.qpic.cn/mmbiz_jpg/fixture-image/640?wx_fmt=jpeg'
        markup='<p><img data-src="'+src+'" src="data:image/png;base64,fixture"><img class="ProseMirror-separator"><img class="ProseMirror-widget"></p>'
        expected=['mmbiz.qpic.cn/mmbiz_jpg/fixture-image/640']
        self.assertEqual(self.driver._receipt_image_keys(markup),expected)
        self.assertEqual(self.driver._receipt_image_keys(markup+'<img>'),expected+[''])
        self.assertEqual(self.driver._receipt_image_keys(markup+markup),expected*2)
        self.assertNotEqual(self.driver._receipt_image_keys(markup.replace('fixture-image','different-image')),expected)


if __name__=='__main__':unittest.main()
