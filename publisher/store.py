from __future__ import annotations

import base64
import ctypes
import json
import os
import sqlite3
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
USER_DATA = Path.home() / 'AppData' / 'Local' if os.name == 'nt' else Path.home() / '.local' / 'share'
DEFAULT_DATA = USER_DATA / 'MojianPublisher' if getattr(sys, 'frozen', False) else ROOT / 'data'
DATA = Path(os.environ.get('MOJIAN_DATA', DEFAULT_DATA)).resolve()
CST = timezone(timedelta(hours=8))


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


class Conflict(ValueError):
    pass


class Vault:
    """DPAPI ties credentials to this Windows user; portable fallback uses Fernet."""
    def __init__(self, root: Path):
        self.root = root

    def _dpapi(self, raw: bytes, decrypt=False) -> bytes:
        from ctypes import wintypes
        class Blob(ctypes.Structure):
            _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]
        buf = ctypes.create_string_buffer(raw)
        source = Blob(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
        target = Blob()
        fn = ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
        if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
            raise RuntimeError('无法读取本机加密凭据，请在设置中重新填写。')
        try:
            return ctypes.string_at(target.data, target.size)
        finally:
            ctypes.windll.kernel32.LocalFree(target.data)

    def _fernet(self):
        from cryptography.fernet import Fernet
        key = self.root / 'vault.key'
        if not key.exists():
            key.write_bytes(Fernet.generate_key())
            key.chmod(0o600)
        return Fernet(key.read_bytes())

    def seal(self, value: str) -> str:
        raw = value.encode()
        return ('dpapi:' + base64.b64encode(self._dpapi(raw)).decode()) if os.name == 'nt' else 'fernet:' + self._fernet().encrypt(raw).decode()

    def open(self, value: str) -> str:
        if value.startswith('dpapi:'):
            return self._dpapi(base64.b64decode(value[6:]), True).decode()
        if value.startswith('fernet:'):
            return self._fernet().decrypt(value[7:].encode()).decode()
        return ''


DEFAULT_SETTINGS = {
    'account_name': '', 'author': '', 'theme': 'jade',
    'ai_base_url': 'https://api.deepseek.com', 'ai_model': 'deepseek-chat',
    'ai_instructions': '面向普通读者，具体、自然、有信息量。不要编造事实、来源和亲身经历。',
    'automation_enabled': False, 'default_action': 'draft',
    # Human login/verification uses the in-app connection panel as well.
    'browser_mode': 'background',
}


class Store:
    def __init__(self, root: Path = DATA):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        (root / 'assets').mkdir(exist_ok=True)
        (root / 'evidence').mkdir(exist_ok=True)
        self.path = root / 'workspace.sqlite3'
        self.vault = Vault(root)
        with self.connect() as db:
            db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS articles (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, author TEXT NOT NULL DEFAULT '',
                digest TEXT NOT NULL DEFAULT '', body TEXT NOT NULL, format TEXT NOT NULL DEFAULT 'markdown',
                theme TEXT NOT NULL DEFAULT 'jade', cover TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'manual', creation_source TEXT NOT NULL DEFAULT 'unspecified',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, article_id TEXT NOT NULL, snapshot TEXT NOT NULL,
                action TEXT NOT NULL, scheduled_at TEXT NOT NULL, status TEXT NOT NULL,
                stage TEXT NOT NULL DEFAULT '', message TEXT NOT NULL DEFAULT '',
                remote_url TEXT NOT NULL DEFAULT '', article_url TEXT NOT NULL DEFAULT '',
                evidence TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                FOREIGN KEY(article_id) REFERENCES articles(id));
            CREATE UNIQUE INDEX IF NOT EXISTS one_active_job ON jobs(article_id)
                WHERE status NOT IN ('cancelled','published','drafted','failed');
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL DEFAULT '',
                level TEXT NOT NULL, message TEXT NOT NULL, created_at TEXT NOT NULL);
            ''')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(jobs)')}
            if 'step' not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN step TEXT NOT NULL DEFAULT ''")
            article_columns = {row['name'] for row in db.execute('PRAGMA table_info(articles)')}
            if 'creation_source' not in article_columns:
                # A historical import/source label is not a declaration about
                # how its text or images were created. Preserve uncertainty.
                db.execute("ALTER TABLE articles ADD COLUMN creation_source TEXT NOT NULL DEFAULT 'unspecified'")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def settings(self, secret=False):
        with self.connect() as db:
            result = {**DEFAULT_SETTINGS, **{r['key']: json.loads(r['value']) for r in db.execute('SELECT * FROM settings')}}
        # v1.1.1 keeps the WeChat execution surface inside the app. Older
        # profiles may contain the experimental visible-mode value; normalize
        # it so an upgrade cannot silently open a browser window.
        result['browser_mode'] = 'background'
        encrypted = result.pop('ai_api_key', '')
        result['ai_key_saved'] = bool(encrypted)
        if secret:
            result['ai_api_key'] = self.vault.open(encrypted) if encrypted else ''
        return result

    def save_settings(self, values):
        allowed = set(DEFAULT_SETTINGS) | {'ai_api_key'}
        with self.connect() as db:
            for key, value in values.items():
                if key not in allowed:
                    continue
                if key == 'ai_api_key':
                    if value == '':  # Empty input preserves a saved key; null explicitly removes it.
                        continue
                    value = self.vault.seal(value) if value else ''
                db.execute('INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, json.dumps(value, ensure_ascii=False)))
        return self.settings()

    def articles(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute('SELECT * FROM articles ORDER BY updated_at DESC')]

    def article(self, article_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM articles WHERE id=?', (article_id,)).fetchone()
        if not row:
            raise KeyError('文章不存在')
        return dict(row)

    def save_article(self, values, article_id=None):
        stamp = now()
        fields = ['title', 'author', 'digest', 'body', 'format', 'theme', 'cover', 'source', 'creation_source']
        if values.get('creation_source', 'unspecified') not in {'unspecified','ai','non_ai'}:
            raise ValueError('请选择有效的创作来源。')
        with self.connect() as db:
            if article_id:
                old = db.execute('SELECT * FROM articles WHERE id=?', (article_id,)).fetchone()
                if not old:
                    raise KeyError('文章不存在')
                if values.get('revision') != old['revision']:
                    raise Conflict('文章已在另一页面修改，请重新打开后编辑。')
                merged = {**dict(old), **values}
                db.execute('UPDATE articles SET ' + ','.join(f'{f}=?' for f in fields) + ',updated_at=?,revision=revision+1 WHERE id=?', [merged[f] for f in fields] + [stamp, article_id])
            else:
                article_id = uuid.uuid4().hex
                defaults = {'creation_source':'unspecified'}
                db.execute('INSERT INTO articles(id,' + ','.join(fields) + ',created_at,updated_at) VALUES (' + ','.join('?' for _ in range(len(fields) + 3)) + ')', [article_id] + [values.get(f, defaults.get(f, '')) for f in fields] + [stamp, stamp])
        return self.article(article_id)

    def enqueue(self, article_id, action, scheduled_at, *, prepared=None):
        article = self.article(article_id)
        if prepared is not None:
            article['_prepared'] = prepared
        stamp, job_id = now(), uuid.uuid4().hex
        with self.connect() as db:
            # A sent snapshot must never be sent twice through a double click or reschedule.
            if db.execute("SELECT 1 FROM jobs WHERE article_id=? AND action=? AND (status IN ('published','drafted','review_pending') OR (status='failed' AND stage='platform_rejected')) AND json_extract(snapshot,'$.revision')=?", (article_id, action, article['revision'])).fetchone():
                raise Conflict('这一版本已提交或完成相同操作；请先修改并保存文章，再创建新任务。')
            saved = db.execute("SELECT remote_url FROM jobs WHERE article_id=? AND status='drafted' AND json_extract(snapshot,'$.revision')=? ORDER BY created_at DESC LIMIT 1", (article_id, article['revision'])).fetchone() if action == 'publish' else None
            try:
                db.execute('INSERT INTO jobs(id,article_id,snapshot,action,scheduled_at,status,stage,remote_url,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)', (job_id, article_id, json.dumps(article, ensure_ascii=False), action, scheduled_at, 'queued', 'draft_saved' if saved else '', saved['remote_url'] if saved else '', stamp, stamp))
            except sqlite3.IntegrityError as exc:
                raise Conflict('这篇文章已有未完成任务，请先在发布队列处理。') from exc
            db.execute('INSERT INTO events(job_id,level,message,created_at) VALUES (?,?,?,?)', (job_id, 'info', '已加入队列，使用排期时保存的文章版本。', stamp))
        return self.job(job_id)

    def jobs(self):
        with self.connect() as db:
            rows = db.execute('SELECT * FROM jobs ORDER BY created_at DESC LIMIT 200').fetchall()
        return [self._decode(r) for r in rows]

    def _decode(self, row):
        result = dict(row)
        result['snapshot'] = json.loads(result['snapshot'])
        # Old frozen jobs stay unknown rather than inheriting a later article
        # edit or an inferred declaration. Do not rewrite their stored JSON.
        result['snapshot'].setdefault('creation_source', 'unspecified')
        return result

    def job(self, job_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not row:
            raise KeyError('任务不存在')
        return self._decode(row)

    def update_job(self, job_id, **values):
        allowed = {'status', 'stage', 'step', 'message', 'remote_url', 'article_url', 'evidence', 'scheduled_at'}
        if not set(values).issubset(allowed):
            raise ValueError('无效任务字段')
        with self.connect() as db:
            db.execute('UPDATE jobs SET ' + ','.join(f'{k}=?' for k in values) + ',updated_at=? WHERE id=?', [*values.values(), now(), job_id])
        if 'message' in values:
            self.log(values['message'], job_id, 'warning' if values.get('status') in {'needs_review', 'waiting_user', 'failed'} else 'info')

    def claim_due(self):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            # Review is tracked on an independent read-only history page and
            # may coexist with another article. Ambiguous results and active
            # mutations still block all new sends, even for direct store users.
            if db.execute("SELECT 1 FROM jobs WHERE status IN ('running','waiting_user','needs_review')").fetchone():
                return None
            row = db.execute("SELECT * FROM jobs WHERE status='queued' AND scheduled_at<=? ORDER BY scheduled_at,created_at LIMIT 1", (now(),)).fetchone()
            if not row:
                return None
            db.execute("UPDATE jobs SET status='running',updated_at=? WHERE id=? AND status='queued'", (now(), row['id']))
        return self.job(row['id'])

    def claim_now(self, job_id, *, preserve_schedule=False):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT 1 FROM jobs WHERE id!=? AND status IN ('running','waiting_user','needs_review')",(job_id,)).fetchone():
                raise Conflict('另一篇任务仍在处理或等待核对，请先处理它，再发送这篇文章。')
            if preserve_schedule:
                changed=db.execute("UPDATE jobs SET status='running',updated_at=? WHERE id=? AND status='queued'",(now(),job_id)).rowcount
            else:
                changed=db.execute("UPDATE jobs SET status='running',scheduled_at=?,updated_at=? WHERE id=? AND status='queued'",(now(),now(),job_id)).rowcount
            if not changed:
                raise Conflict('任务已经开始或需要先处理提示，请勿重复发送。')
        return self.job(job_id)

    def recover(self):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM jobs WHERE status='running'").fetchall()
            for row in rows:
                status = ('review_pending' if row['stage'] == 'publishing' and row['step'] == 'review'
                          else 'failed' if row['stage'] == 'platform_rejected'
                          else 'needs_review' if row['stage'] else 'waiting_user')
                db.execute('UPDATE jobs SET status=?,message=?,updated_at=? WHERE id=?', (status, '程序上次运行中断，请核对微信草稿或发表记录后处理；不会自动重复提交。', now(), row['id']))

    def cancel(self, job_id):
        with self.connect() as db:
            changed = db.execute("UPDATE jobs SET status='cancelled',updated_at=? WHERE id=? AND status IN ('queued','waiting_user') AND stage NOT IN ('saving','publishing')", (now(), job_id)).rowcount
        if not changed:
            raise Conflict('当前任务不能取消。正在执行或结果待核对的任务须先核对微信后台。')

    def resume(self, job_id):
        with self.connect() as db:
            changed = db.execute("UPDATE jobs SET status='queued',scheduled_at=?,updated_at=? WHERE id=? AND status='waiting_user' AND stage IN ('','draft_saved')", (now(), now(), job_id)).rowcount
        if not changed:
            raise Conflict('不能直接重试这个任务，请先核对后台结果，避免重复发布。')

    def confirm_creation_source(self, job_id, value):
        """Restore a verified pre-submission source checkpoint, not a send result.

        The browser adapter must have verified the live prerequisite dialog
        before calling this method. Only this task's declaration changes;
        its original article, media, draft address and schedule remain frozen.
        """
        if value not in {'ai','non_ai'}:
            raise ValueError('请明确选择含 AI 生成内容或文字和配图均非 AI 生成。')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
            if not row:
                raise KeyError('任务不存在')
            eligible = (row['action'] == 'publish' and (
                (row['stage'] == 'source_required' and row['status'] in {'needs_review','waiting_user'})
                or (row['stage'] == 'publishing' and row['status'] == 'needs_review'
                    and row['step'] not in {'verification','receipt','review'})))
            if not eligible:
                raise Conflict('当前任务不在等待创作来源确认，不能重复恢复或重新发表。')
            if not row['remote_url']:
                raise Conflict('原草稿地址缺失，请先核对微信草稿，不能重新创建文章。')
            snapshot = json.loads(row['snapshot'])
            snapshot['creation_source'] = value
            label = '含 AI 生成内容' if value == 'ai' else '文字和配图均非 AI 生成'
            message = f'用户已确认本任务创作来源：{label}，将在原草稿继续发表。'
            stamp = now()
            db.execute("UPDATE jobs SET snapshot=?,status='queued',stage='draft_saved',step='saved',message=?,updated_at=? WHERE id=?",
                       (json.dumps(snapshot,ensure_ascii=False),message,stamp,job_id))
            db.execute('INSERT INTO events(job_id,level,message,created_at) VALUES (?,?,?,?)',
                       (job_id,'info',message,stamp))
        return self.job(job_id)

    def log(self, message, job_id='', level='info'):
        with self.connect() as db:
            db.execute('INSERT INTO events(job_id,level,message,created_at) VALUES (?,?,?,?)', (job_id, level, message, now()))

    def events(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute('SELECT * FROM events ORDER BY seq DESC LIMIT 150')]
