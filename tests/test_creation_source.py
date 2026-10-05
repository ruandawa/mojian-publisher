"""Per-article declaration metadata without live WeChat or model access."""
import io
import concurrent.futures
import json
import sqlite3
import tempfile
import time
import unittest
import zipfile
from contextlib import closing
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from publisher.app import create_app
from publisher.importing import import_document, parse_frontmatter
from publisher.store import Conflict, Store, now


ARTICLE = {'title':'来源测试', 'author':'', 'digest':'', 'body':'正文内容',
           'format':'markdown', 'theme':'jade', 'cover':'', 'source':'import'}


class CreationSourceStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_missing_source_remains_unspecified_for_import_and_manual_articles(self):
        for source in ('import', 'manual', 'ai'):
            with self.subTest(source=source):
                result = self.store.save_article({**ARTICLE, 'source':source})
                self.assertEqual(result['creation_source'], 'unspecified')

    def test_explicit_source_survives_edits_and_is_frozen_in_queue(self):
        article = self.store.save_article({**ARTICLE, 'creation_source':'ai'})
        job = self.store.enqueue(article['id'], 'publish', now())
        unchanged = self.store.save_article({'revision':article['revision'], 'title':'改标题'}, article['id'])
        self.assertEqual(unchanged['creation_source'], 'ai')
        updated = self.store.save_article({**unchanged, 'creation_source':'non_ai'}, article['id'])
        self.assertEqual(updated['creation_source'], 'non_ai')
        self.assertEqual(self.store.job(job['id'])['snapshot']['creation_source'], 'ai')

    def test_invalid_store_source_does_not_create_article(self):
        with self.assertRaisesRegex(ValueError, '创作来源'):
            self.store.save_article({**ARTICLE, 'creation_source':'auto'})
        self.assertEqual(self.store.articles(), [])

    def test_old_database_migration_preserves_articles_and_frozen_jobs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = json.dumps({**ARTICLE, 'id':'legacy', 'source':'ai', 'revision':1}, ensure_ascii=False)
            with closing(sqlite3.connect(root / 'workspace.sqlite3')) as db:
                db.executescript('''CREATE TABLE articles (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, author TEXT NOT NULL DEFAULT '',
                    digest TEXT NOT NULL DEFAULT '', body TEXT NOT NULL, format TEXT NOT NULL DEFAULT 'markdown',
                    theme TEXT NOT NULL DEFAULT 'jade', cover TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT 'manual', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1);
                    CREATE TABLE jobs (
                    id TEXT PRIMARY KEY, article_id TEXT NOT NULL, snapshot TEXT NOT NULL,
                    action TEXT NOT NULL, scheduled_at TEXT NOT NULL, status TEXT NOT NULL,
                    stage TEXT NOT NULL DEFAULT '', message TEXT NOT NULL DEFAULT '',
                    remote_url TEXT NOT NULL DEFAULT '', article_url TEXT NOT NULL DEFAULT '',
                    evidence TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);''')
                db.execute("INSERT INTO articles(id,title,body,source,created_at,updated_at) VALUES ('legacy','旧文章','保留正文','ai',?,?)", (now(), now()))
                db.execute("INSERT INTO jobs(id,article_id,snapshot,action,scheduled_at,status,created_at,updated_at) VALUES ('job','legacy',?,'publish',?,'needs_review',?,?)", (snapshot, now(), now(), now()))
                db.commit()
            migrated = Store(root)
            self.assertEqual(migrated.article('legacy')['body'], '保留正文')
            self.assertEqual(migrated.article('legacy')['creation_source'], 'unspecified')
            self.assertEqual(migrated.job('job')['snapshot']['creation_source'], 'unspecified')
            self.assertEqual(migrated.job('job')['status'], 'needs_review')
            with migrated.connect() as db:
                self.assertEqual(db.execute("SELECT snapshot FROM jobs WHERE id='job'").fetchone()[0], snapshot)
            self.assertEqual(Store(root).article('legacy')['creation_source'], 'unspecified')


class CreationSourceImportTests(unittest.TestCase):
    def test_only_explicit_frontmatter_sets_source(self):
        for metadata, expected in (
            ('', 'unspecified'),
            ('source: ai\n', 'unspecified'),
            ('creation_source: unspecified\n', 'unspecified'),
            ('creation_source: ai\n', 'ai'),
            ('creation_source: non_ai\n', 'non_ai'),
            ('ai_generated: true\n', 'ai'),
            ('ai_generated: false\n', 'non_ai'),
            ('creation_source: ai\nai_generated: true\n', 'ai'),
        ):
            with self.subTest(metadata=metadata):
                body = ('---\n' + metadata + '---\n' if metadata else '') + '# Article\n\nBody'
                result = parse_frontmatter(body, 'fallback', '.md')
                self.assertEqual(result['creation_source'], expected)
                self.assertEqual(result['source'], 'import')

    def test_invalid_or_conflicting_metadata_is_rejected(self):
        for metadata in ('creation_source: guess\n', 'ai_generated: maybe\n',
                         'creation_source: non_ai\nai_generated: true\n',
                         'creation_source: ai\nai_generated: false\n'):
            with self.subTest(metadata=metadata), self.assertRaises(ValueError):
                parse_frontmatter('---\n' + metadata + '---\n# Article\n\nBody', 'fallback', '.md')

    def test_zip_and_plain_import_keep_declared_source_through_preparation(self):
        for source in ('unspecified', 'ai', 'non_ai'):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as directory:
                assets = Path(directory) / 'assets'
                assets.mkdir()
                raw = ('---\ncreation_source: ' + source + '\n---\n# Article\n\nBody').encode()
                plain = import_document(raw, 'article.md', assets)
                self.assertEqual(plain['creation_source'], source)
                archive = io.BytesIO()
                with zipfile.ZipFile(archive, 'w') as package:
                    package.writestr('article.md', raw)
                packed = import_document(archive.getvalue(), 'article.zip', assets)
                self.assertEqual(packed['creation_source'], source)
                self.assertTrue(packed['ready'])


class CreationSourceAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name))
        self.client = TestClient(create_app(self.store), base_url='http://127.0.0.1')
        self.headers = {'X-Mojian-Token':self.client.get('/api/bootstrap').json()['token']}

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def test_api_validates_and_preserves_explicit_source(self):
        invalid = self.client.post('/api/articles', headers=self.headers, json={**ARTICLE, 'creation_source':'guess'})
        self.assertEqual(invalid.status_code, 422)
        created = self.client.post('/api/articles', headers=self.headers, json={**ARTICLE, 'creation_source':'ai'})
        self.assertEqual(created.status_code, 200, created.text)
        article = created.json()
        old_client_values = {key:value for key,value in article.items() if key != 'creation_source'}
        old_client_values['title'] = '旧客户端改标题'
        updated = self.client.put('/api/articles/' + article['id'], headers=self.headers, json=old_client_values)
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()['creation_source'], 'ai')
        changed = self.client.put('/api/articles/' + article['id'], headers=self.headers,
                                  json={**updated.json(), 'creation_source':'non_ai'})
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertEqual(changed.json()['creation_source'], 'non_ai')

    def test_generated_article_is_explicit_ai_even_if_model_metadata_disagrees(self):
        generated = {**ARTICLE, 'source':'manual', 'creation_source':'non_ai'}
        with patch('publisher.app.ai.generate', AsyncMock(return_value=generated)) as model:
            result = self.client.post('/api/generate', headers=self.headers, json={'topic':'主题'})
        self.assertEqual(result.status_code, 200, result.text)
        article = result.json()
        self.assertEqual(article['source'], 'ai')
        self.assertEqual(article['creation_source'], 'ai')
        model.assert_awaited_once()
        job = self.store.enqueue(article['id'], 'publish', now())
        self.assertEqual(job['snapshot']['creation_source'], 'ai')


class CreationSourceRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name))
        self.article = self.store.save_article({**ARTICLE, 'body':'正文\n\n![图](/assets/frozen.jpg)'})
        self.job = self.store.enqueue(self.article['id'], 'publish', '2050-10-03T00:30:00+00:00',
                                      prepared={'html':'<p>冻结正文</p>', 'images':[{'src':'/assets/frozen.jpg', 'sha256':'fixture'}]})
        self.store.update_job(self.job['id'], status='needs_review', stage='source_required',
                              remote_url='https://mp.weixin.qq.com/cgi-bin/appmsg?appmsgid=42', step='receipt')
        self.app = create_app(self.store)
        self.browser = self.app.state.browser
        self.browser.confirm_creation_source = AsyncMock(
            side_effect=lambda job,value:self.store.confirm_creation_source(job['id'],value))
        self.browser.execute = AsyncMock()
        self.client = TestClient(self.app, base_url='http://127.0.0.1')
        self.headers = {'X-Mojian-Token':self.client.get('/api/bootstrap').json()['token']}
        self.path = '/api/jobs/' + self.job['id'] + '/creation-source'

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def test_atomic_confirmation_changes_only_task_source_and_keeps_original_draft(self):
        before = self.store.job(self.job['id'])
        result = self.store.confirm_creation_source(self.job['id'], 'non_ai')
        expected = {**before['snapshot'], 'creation_source':'non_ai'}
        self.assertEqual(result['snapshot'], expected)
        for field in ('scheduled_at', 'created_at', 'remote_url', 'article_id', 'action', 'article_url'):
            self.assertEqual(result[field], before[field])
        self.assertEqual(result['status'], 'queued')
        self.assertEqual(result['stage'], 'draft_saved')
        self.assertEqual(result['step'], 'saved')
        self.assertEqual(self.store.article(self.article['id'])['creation_source'], 'unspecified')
        event = self.store.events()[0]
        self.assertIn('用户已确认本任务创作来源', event['message'])
        self.assertIn('原草稿继续发表', event['message'])
        self.assertEqual(event['job_id'], self.job['id'])
        claimed = self.store.claim_now(self.job['id'], preserve_schedule=True)
        self.assertEqual(claimed['status'], 'running')
        self.assertEqual(claimed['scheduled_at'], before['scheduled_at'])

    def test_repeat_confirmation_cannot_requeue_or_overwrite_the_first_answer(self):
        self.store.confirm_creation_source(self.job['id'], 'ai')
        before = self.store.job(self.job['id'])
        count = len(self.store.events())
        with self.assertRaises(Conflict):
            self.store.confirm_creation_source(self.job['id'], 'non_ai')
        self.assertEqual(self.store.job(self.job['id']), before)
        self.assertEqual(len(self.store.events()), count)

    def test_only_supported_pre_submission_checkpoint_states_can_resume(self):
        for status,stage in (('needs_review','source_required'), ('waiting_user','source_required'),
                             ('needs_review','publishing')):
            with self.subTest(status=status,stage=stage):
                self.store.update_job(self.job['id'], status=status, stage=stage)
                result = self.store.confirm_creation_source(self.job['id'], 'non_ai')
                self.assertEqual(result['status'], 'queued')
                self.assertEqual(result['stage'], 'draft_saved')

    def test_concurrent_confirmation_restores_the_task_only_once(self):
        def confirm(value):
            try:
                self.store.confirm_creation_source(self.job['id'], value)
                return True
            except Conflict:
                return False
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(confirm, ('ai','non_ai')))
        self.assertEqual(outcomes.count(True), 1)
        self.assertEqual(sum('用户已确认本任务创作来源' in event['message'] for event in self.store.events()), 1)

    def test_invalid_status_action_checkpoint_and_missing_draft_are_rejected(self):
        for status,stage,action,remote in (
            ('running','source_required','publish','draft'),
            ('published','source_required','publish','draft'),
            ('queued','source_required','publish','draft'),
            ('cancelled','source_required','publish','draft'),
            ('drafted','source_required','publish','draft'),
            ('needs_review','editing','publish','draft'),
            ('waiting_user','publishing','publish','draft'),
            ('needs_review','source_required','draft','draft'),
            ('needs_review','source_required','publish',''),
        ):
            with self.subTest(status=status,stage=stage,action=action,remote=remote):
                with self.store.connect() as db:
                    db.execute('UPDATE jobs SET status=?,stage=?,action=?,remote_url=? WHERE id=?',
                               (status,stage,action,remote,self.job['id']))
                before = self.store.job(self.job['id'])
                with self.assertRaises(Conflict):
                    self.store.confirm_creation_source(self.job['id'], 'non_ai')
                self.assertEqual(self.store.job(self.job['id']), before)

    def test_api_live_verification_precedes_resume_and_no_publication_result_is_claimed(self):
        before = self.store.job(self.job['id'])
        result = self.client.post(self.path, headers=self.headers, json={'creation_source':'non_ai'})
        self.assertEqual(result.status_code, 200, result.text)
        self.browser.confirm_creation_source.assert_awaited_once()
        arguments = self.browser.confirm_creation_source.call_args.args
        self.assertEqual(arguments[0]['status'], 'needs_review')
        self.assertEqual(arguments[1], 'non_ai')
        after = self.store.job(self.job['id'])
        self.assertEqual(after['status'], 'running')
        self.assertEqual(after['stage'], 'draft_saved')
        self.assertEqual(after['scheduled_at'], before['scheduled_at'])
        self.assertEqual(after['snapshot'], {**before['snapshot'], 'creation_source':'non_ai'})
        self.assertEqual(after['article_url'], '')
        for _ in range(20):
            if self.browser.execute.await_count:
                break
            time.sleep(.01)
        self.browser.execute.assert_awaited_once()
        duplicate = self.client.post(self.path, headers=self.headers, json={'creation_source':'ai'})
        self.assertEqual(duplicate.status_code, 409)
        self.browser.confirm_creation_source.assert_awaited_once()
        self.browser.execute.assert_awaited_once()

    def test_api_rejects_panel_lock_invalid_source_and_running_task_before_browser_access(self):
        self.browser.interaction_until = time.monotonic() + 30
        panel = self.client.post(self.path, headers=self.headers, json={'creation_source':'non_ai'})
        self.assertEqual(panel.status_code, 409)
        self.browser.interaction_until = 0
        with patch.object(self.browser.lock, 'locked', return_value=True):
            locked = self.client.post(self.path, headers=self.headers, json={'creation_source':'non_ai'})
        self.assertEqual(locked.status_code, 409)
        unknown = self.client.post(self.path, headers=self.headers, json={'creation_source':'unspecified'})
        self.assertEqual(unknown.status_code, 422)
        self.store.update_job(self.job['id'], status='running')
        running = self.client.post(self.path, headers=self.headers, json={'creation_source':'non_ai'})
        self.assertEqual(running.status_code, 409)
        self.browser.confirm_creation_source.assert_not_awaited()
        self.browser.execute.assert_not_awaited()

    def test_api_failed_live_verification_preserves_waiting_snapshot(self):
        self.browser.confirm_creation_source.side_effect = ValueError('现场不是来源声明弹窗')
        before = self.store.job(self.job['id'])
        result = self.client.post(self.path, headers=self.headers, json={'creation_source':'non_ai'})
        self.assertEqual(result.status_code, 400)
        self.assertEqual(self.store.job(self.job['id']), before)
        self.browser.execute.assert_not_awaited()

    def test_api_rejects_another_unresolved_task_before_live_verification(self):
        article = self.store.save_article({**ARTICLE, 'title':'其他任务'})
        other = self.store.enqueue(article['id'], 'draft', now())
        self.store.update_job(other['id'], status='waiting_user')
        result = self.client.post(self.path, headers=self.headers, json={'creation_source':'non_ai'})
        self.assertEqual(result.status_code, 409)
        self.browser.confirm_creation_source.assert_not_awaited()
        self.browser.execute.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
