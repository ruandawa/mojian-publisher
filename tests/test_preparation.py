import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image

from publisher.app import create_app
from publisher.preparation import prepare_snapshot, verify_snapshot
from publisher.rendering import save_image
from publisher.store import Store

ARTICLE = {'title':'图文流程检查','author':'','digest':'摘要','body':'## 小标题\n\n正文 **重点**',
           'format':'markdown','theme':'jade','cover':'','source':'manual'}


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.assets = self.store.root/'assets'
        raw = io.BytesIO()
        Image.new('RGB',(32,24),'#24765d').save(raw,'PNG')
        self.raw = raw.getvalue()
        self.image = save_image(self.raw,self.assets)
        self.client = TestClient(create_app(self.store),base_url='http://127.0.0.1')
        self.headers = {'X-Mojian-Token':self.client.get('/api/bootstrap').json()['token']}

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()

    def post(self,path,values):
        return self.client.post(path,json=values,headers=self.headers)

    def test_preview_and_queued_plan_are_same_and_frozen(self):
        data={**ARTICLE,'body':'## 标题\n\n![图]('+self.image+')\n\n**结尾**','cover':self.image}
        article=self.post('/api/articles',data).json()
        preview=self.post('/api/preview',data).json()
        job=self.post('/api/jobs',{'article_id':article['id'],'action':'draft'}).json()
        self.assertEqual(preview['html'],job['snapshot']['_prepared']['html'])
        self.assertEqual(job['snapshot']['_prepared']['body_image_count'],1)
        self.store.save_article({**article,'body':'修改以后的正文'},article['id'])
        self.assertEqual(self.store.job(job['id'])['snapshot']['_prepared']['html'],preview['html'])

    def test_external_image_downloads_before_queue_without_opening_browser(self):
        article=self.post('/api/articles',{**ARTICLE,'body':'![图](https://example.org/photo.png)\n\n正文'}).json()
        with patch('publisher.importing.download_image',return_value=self.raw) as download:
            response=self.post('/api/jobs',{'article_id':article['id'],'action':'draft'})
        self.assertEqual(response.status_code,200,response.text)
        download.assert_called_once()
        snapshot=response.json()['snapshot']
        self.assertEqual(snapshot['format'],'html')
        self.assertNotIn('example.org',snapshot['body'])
        self.assertTrue(snapshot['cover'].startswith('/assets/'))
        self.assertIsNone(self.client.app.state.browser.page)

    def test_prepare_is_local_ready_and_returns_canonical_preview(self):
        with patch('publisher.importing.download_image',return_value=self.raw):
            response=self.post('/api/prepare',{'body':'![图](https://example.org/a.png)','format':'markdown','theme':'ink'})
        self.assertEqual(response.status_code,200,response.text)
        result=response.json()
        self.assertTrue(result['ready'])
        self.assertEqual(result['format'],'html')
        self.assertEqual(self.post('/api/preview',{**result,'theme':'ink'}).json()['html'],result['html'])

    def test_missing_local_image_blocks_before_creating_job(self):
        article=self.post('/api/articles',{**ARTICLE,'body':'![图](/assets/'+'f'*32+'.jpg)'}).json()
        result=self.post('/api/jobs',{'article_id':article['id'],'action':'publish'})
        self.assertEqual(result.status_code,400)
        self.assertEqual(self.store.jobs(),[])
        self.assertIsNone(self.client.app.state.browser.page)

    def test_changed_asset_and_markup_cannot_use_saved_plan(self):
        plan=prepare_snapshot({**ARTICLE,'cover':self.image},self.assets)
        verify_snapshot(plan,self.assets)
        with self.assertRaisesRegex(ValueError,'排版内容'):
            verify_snapshot({**plan,'html':plan['html']+'坏内容'},self.assets)
        (self.assets/self.image.rsplit('/',1)[-1]).write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'图片已变化'):
            verify_snapshot(plan,self.assets)

    def test_utf8_md_import_has_normalized_body_and_metadata(self):
        content='---\ntitle: "导入标题"\nauthor: "作者"\n---\n\n## 小标题\n\n正文 **重点**'
        response=self.client.post('/api/import',files={'file':('article.md',content.encode('utf-8'),'text/markdown')},headers=self.headers)
        self.assertEqual(response.status_code,200,response.text)
        result=response.json()
        self.assertEqual(result['title'],'导入标题')
        self.assertEqual(result['format'],'html')
        self.assertIn('<strong>重点</strong>',result['body'])
        self.assertNotIn('---',result['body'])


if __name__=='__main__':
    unittest.main()
