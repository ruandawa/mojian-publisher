import asyncio
import concurrent.futures
import io
import json
import tempfile
import time
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from PIL import Image
from bs4 import BeautifulSoup

from publisher.app import create_app
from publisher.rendering import export_document, image_warnings, render, save_image, wechat_compatible_html
from publisher.store import Conflict, Store, now
from publisher import ai


ARTICLE = {'title':'测试文章','author':'编辑','digest':'摘要','body':'## 一段真实的内容\n\n**重点**与正文。','format':'markdown','theme':'jade','cover':'','source':'manual'}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.tmp.name))
        self.article=self.store.save_article(ARTICLE)
    def tearDown(self):
        self.tmp.cleanup()

    def test_snapshot_is_immutable_and_edits_use_revision(self):
        job=self.store.enqueue(self.article['id'],'draft',now())
        updated={**self.article,'body':'新正文'}
        self.store.save_article(updated,self.article['id'])
        self.assertEqual(self.store.job(job['id'])['snapshot']['body'],ARTICLE['body'])
        with self.assertRaises(Conflict):
            self.store.save_article(updated,self.article['id'])

    def test_duplicate_active_and_completed_tasks(self):
        job=self.store.enqueue(self.article['id'],'publish',now())
        with self.assertRaises(Conflict):
            self.store.enqueue(self.article['id'],'publish',now())
        self.store.update_job(job['id'],status='published')
        with self.assertRaises(Conflict):
            self.store.enqueue(self.article['id'],'publish',now())

    def test_two_workers_cannot_claim_same_job(self):
        self.store.enqueue(self.article['id'],'draft',now())
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(lambda _:self.store.claim_due(),range(4)))
        self.assertEqual(sum(x is not None for x in results),1)

    def test_publish_after_drafting_reuses_the_saved_draft(self):
        job=self.store.enqueue(self.article['id'],'draft',now())
        self.store.update_job(job['id'],status='drafted',stage='draft_saved',remote_url='https://mp.weixin.qq.com/cgi-bin/appmsg?appmsgid=42')
        next_job=self.store.enqueue(self.article['id'],'publish',now())
        self.assertEqual(next_job['stage'],'draft_saved')
        self.assertIn('appmsgid=42',next_job['remote_url'])

    def test_restart_and_unknown_result_never_blindly_retry(self):
        job=self.store.enqueue(self.article['id'],'publish',now())
        self.store.claim_due()
        self.store.update_job(job['id'],stage='publishing')
        self.store.recover()
        self.assertEqual(self.store.job(job['id'])['status'],'needs_review')
        with self.assertRaises(Conflict):self.store.resume(job['id'])
        with self.assertRaises(Conflict):self.store.cancel(job['id'])
        self.assertIsNone(self.store.claim_due())

    def test_login_wait_can_resume_without_creating_another_job(self):
        job=self.store.enqueue(self.article['id'],'draft',now())
        self.store.claim_due()
        self.store.update_job(job['id'],status='waiting_user')
        self.store.resume(job['id'])
        self.assertEqual(self.store.claim_due()['id'],job['id'])

    def test_secret_is_encrypted_and_omitted_from_public_settings(self):
        key='test-secret-not-real'
        self.store.save_settings({'ai_api_key':key})
        self.assertNotIn('ai_api_key',self.store.settings())
        self.assertEqual(self.store.settings(secret=True)['ai_api_key'],key)
        with self.store.connect() as db:
            value=db.execute("SELECT value FROM settings WHERE key='ai_api_key'").fetchone()[0]
        self.assertNotIn(key,value)
        self.store.save_settings({'ai_api_key':''})
        self.assertTrue(self.store.settings()['ai_key_saved'])
        self.store.save_settings({'ai_api_key':None})
        self.assertFalse(self.store.settings()['ai_key_saved'])


class RenderingTests(unittest.TestCase):
    def test_untrusted_html_and_external_images_are_removed(self):
        result=render('<script>steal()</script><img src="https://evil.example/pixel" onerror="steal()"><a href="javascript:alert(1)">点我</a><p style="background:url(https://evil.example)">正文</p>','html')
        self.assertNotIn('steal',result)
        self.assertNotIn('evil.example',result)
        self.assertNotIn('javascript:',result)
        self.assertIn('正文',result)
        self.assertEqual(image_warnings('![外链](https://example.com/a.jpg)','markdown'),1)

    def test_markdown_keeps_local_images_and_formats_headings(self):
        image='/assets/'+'a'*32+'.jpg'
        result=render('## 标题\n\n![本地]('+image+')\n\n**重点**')
        self.assertIn(image,result)
        self.assertIn('border-left:4px',result)
        self.assertIn('<strong style=',result)

    def test_wechat_compatible_html_preserves_unsupported_block_text(self):
        source = render('''> 引用段落\n\n```text\n代码第一行\n代码第二行\n```\n\n| 左 | 右 |\n| --- | --- |\n| 甲 | 乙 |''')
        converted = wechat_compatible_html(source)
        self.assertNotIn('<section', converted)
        self.assertNotIn('<blockquote', converted)
        self.assertNotIn('<pre', converted)
        self.assertNotIn('<table', converted)
        actual = ''.join(BeautifulSoup(converted, 'html.parser').get_text().split())
        for value in ['引用段落','代码第一行','代码第二行','左','右','甲','乙']:
            self.assertIn(value, actual)
        self.assertLess(actual.index('甲'),actual.index('乙'))

    def test_uploaded_images_are_decoded_reencoded_and_bounded(self):
        with tempfile.TemporaryDirectory() as path:
            raw=io.BytesIO();Image.new('RGB',(3000,1000)).save(raw,'PNG')
            result=save_image(raw.getvalue(),Path(path))
            with Image.open(Path(path)/result.split('/')[-1]) as im:
                self.assertEqual(im.format,'JPEG');self.assertEqual(im.width,2400)
            with self.assertRaises(ValueError):save_image(b'not an image',Path(path))


class APITests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.tmp.name))
        self.client=TestClient(create_app(self.store),base_url='http://127.0.0.1')
        self.headers={'X-Mojian-Token':self.client.get('/api/bootstrap').json()['token']}
    def tearDown(self):
        self.client.close();self.tmp.cleanup()
    def post(self,path,data):return self.client.post(path,json=data,headers=self.headers)

    def test_local_csrf_and_cross_origin_guards(self):
        self.assertEqual(self.client.post('/api/articles',json=ARTICLE).status_code,403)
        self.assertEqual(self.client.get('/api/state',headers={**self.headers,'Origin':'https://evil.example'}).status_code,403)
        self.assertEqual(self.client.get('/api/health',headers={'Host':'evil.example'}).status_code,403)
        self.assertEqual(self.client.get('/api/state',headers=self.headers).status_code,200)

    def test_save_cover_export_and_timezone_queue(self):
        article=self.post('/api/articles',ARTICLE).json()
        result=self.post('/api/jobs',{'article_id':article['id'],'action':'draft','scheduled_at':'2050-10-03T08:30:00+08:00'})
        self.assertEqual(result.status_code,200,result.text)
        job=result.json()
        self.assertEqual(job['scheduled_at'],'2050-10-03T00:30:00+00:00')
        self.assertTrue(job['snapshot']['cover'].startswith('/assets/'))
        cover=job['snapshot']['cover']
        edited={**self.store.article(article['id']),'body':'正文\n\n![图]('+cover+')'}
        self.store.save_article(edited,article['id'])
        exported=self.client.get('/api/articles/'+article['id']+'/export',headers=self.headers)
        self.assertIn('data:image/jpeg;base64,',exported.text)
        self.assertNotIn('src="/assets/',exported.text)

    def test_external_images_block_queue_instead_of_silently_losing_them(self):
        article=self.post('/api/articles',{**ARTICLE,'body':'![外链](https://example.com/img.jpg)'}).json()
        with patch('publisher.importing.download_image',side_effect=ValueError('测试下载失败')):
            result=self.post('/api/jobs',{'article_id':article['id'],'action':'publish'})
        self.assertEqual(result.status_code,400)
        self.assertEqual(self.store.jobs(),[])

    def test_cannot_claim_published_without_explicit_confirmed_result(self):
        article=self.post('/api/articles',ARTICLE).json()
        job=self.post('/api/jobs',{'article_id':article['id'],'action':'publish'}).json()
        self.store.update_job(job['id'],status='needs_review',stage='publishing')
        path='/api/jobs/'+job['id']+'/resolve'
        result=self.client.put(path,headers=self.headers,json={'status':'published','confirmed':True,'article_url':'https://evil.example/'})
        self.assertEqual(result.status_code,400)
        self.assertEqual(self.store.job(job['id'])['status'],'needs_review')
        result=self.client.put(path,headers=self.headers,json={'status':'published','confirmed':True,'article_url':'https://mp.weixin.qq.com/s/test-only'})
        self.assertEqual(result.status_code,200)
        self.assertEqual(self.store.job(job['id'])['stage'],'user_confirmed')

    def test_missing_model_config_does_not_create_an_article_or_job(self):
        result=self.post('/api/generate',{'topic':'主题'})
        self.assertEqual(result.status_code,400)
        self.assertEqual(self.store.articles(),[])
        self.assertEqual(self.store.jobs(),[])

    def test_send_now_runs_only_requested_job_with_automation_paused(self):
        app=create_app(self.store)
        app.state.browser.execute=AsyncMock()
        first=self.store.save_article(ARTICLE)
        other=self.store.save_article({**ARTICLE,'title':'其他排期'})
        later=self.store.enqueue(other['id'],'draft',now())
        with TestClient(app,base_url='http://127.0.0.1') as client:
            headers={'X-Mojian-Token':client.get('/api/bootstrap').json()['token']}
            response=client.post('/api/jobs',headers=headers,json={'article_id':first['id'],'action':'draft','run_now':True})
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(response.json()['status'],'running')
            for _ in range(20):
                if app.state.browser.execute.await_count:break
                time.sleep(.01)
            app.state.browser.execute.assert_awaited_once()
            self.assertEqual(app.state.browser.execute.call_args.args[0]['article_id'],first['id'])
            self.assertEqual(self.store.job(later['id'])['status'],'queued')
            self.assertFalse(self.store.settings()['automation_enabled'])

    def test_send_now_does_not_create_job_when_another_needs_review(self):
        old=self.store.save_article(ARTICLE)
        blocked=self.store.enqueue(old['id'],'draft',now())
        self.store.update_job(blocked['id'],status='needs_review',stage='editing')
        next_article=self.store.save_article({**ARTICLE,'title':'下一篇'})
        result=self.post('/api/jobs',{'article_id':next_article['id'],'action':'draft','run_now':True})
        self.assertEqual(result.status_code,409)
        self.assertEqual(len(self.store.jobs()),1)


class AIContractTests(unittest.TestCase):
    def test_local_ai_response_is_saved_as_structured_content(self):
        class FakeResponse:
            status_code=200
            def json(self):return {'choices':[{'message':{'content':json.dumps({'title':'模型测试','digest':'测试摘要','body':'## 测试\n完整正文。'},ensure_ascii=False)}}]}
        class FakeClient:
            def __init__(self,**kwargs):pass
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
            async def post(self,*args,**kwargs):return FakeResponse()
        config={'ai_base_url':'http://127.0.0.1:9999/v1','ai_api_key':'','ai_model':'local-test','ai_instructions':'直接'}
        with patch('publisher.ai.httpx.AsyncClient',FakeClient):
            result=asyncio.run(ai.generate(config,'主题','参考材料'))
        self.assertEqual(result['title'],'模型测试')
        self.assertEqual(result['source'],'ai')


class SchedulerTests(unittest.TestCase):
    def test_due_job_pauses_for_login_and_keeps_later_jobs_queued(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(Path(directory))
            first=store.save_article(ARTICLE)
            second=store.save_article({**ARTICLE,'title':'第二篇'})
            job1=store.enqueue(first['id'],'publish',now())
            job2=store.enqueue(second['id'],'draft',now())
            store.save_settings({'account_name':'测试账号','automation_enabled':True})
            app = create_app(store)
            # Keep the scheduler test offline: only the login checkpoint is
            # under test, never launch a real WeChat session.
            app.state.browser.open = AsyncMock(side_effect=ValueError('offline login fixture'))
            with TestClient(app,base_url='http://127.0.0.1'):
                for _ in range(40):
                    if store.job(job1['id'])['status']=='waiting_user':break
                    time.sleep(.1)
                self.assertEqual(store.job(job1['id'])['status'],'waiting_user')
                time.sleep(2.1)
                self.assertEqual(store.job(job2['id'])['status'],'queued')
                self.assertEqual(store.job(job1['id'])['stage'],'')


if __name__=='__main__':unittest.main(verbosity=2)
