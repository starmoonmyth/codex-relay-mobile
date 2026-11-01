from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from urllib.parse import urlsplit


def question_answers(text):
    match = re.fullmatch(r'\s*<send_user_message_question_reply>\s*(.*?)\s*</send_user_message_question_reply>\s*', text, re.S)
    if not match:
        return {}
    try:
        values = json.loads(match.group(1))
        result = {}
        for value in values:
            key = json.loads(value['questionItemId'])
            if len(key) == 3 and key[0] == 'request_user_input_async':
                result[(key[1], key[2])] = value['answer']
        return result
    except (ValueError, TypeError, KeyError):
        return {}


class Notifications:
    def __init__(self, relay, sender=None):
        self.relay = relay
        self.sender = sender or self.push
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.worker = None
        self.last_error = ''
        self.key_path = relay.root / 'webpush-private.pem'
        with relay.connect() as c:
            c.executescript('''
                CREATE TABLE IF NOT EXISTS notification_devices (
                    session TEXT PRIMARY KEY, enabled INTEGER, subscription TEXT,
                    enabled_after INTEGER, push_cursor INTEGER, retry_at INTEGER, error TEXT
                );
                CREATE TABLE IF NOT EXISTS notification_states (thread_id TEXT PRIMARY KEY, state TEXT);
                CREATE TABLE IF NOT EXISTS notification_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, thread_id TEXT, kind TEXT, payload TEXT, created INTEGER
                );
                CREATE TABLE IF NOT EXISTS notification_receipts (
                    id TEXT PRIMARY KEY, session TEXT, token_hash TEXT UNIQUE, kind TEXT, created INTEGER,
                    provider TEXT, provider_status INTEGER, received INTEGER DEFAULT 0,
                    shown INTEGER DEFAULT 0, clicked INTEGER DEFAULT 0, error TEXT DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS notification_clients (session TEXT PRIMARY KEY, info TEXT, updated INTEGER);
                CREATE TABLE IF NOT EXISTS notification_dedup (identity TEXT PRIMARY KEY, created INTEGER);
            ''')

    @staticmethod
    def session_hash(cookie):
        return hashlib.sha256(cookie.encode()).hexdigest()

    def public_key(self):
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives import serialization
        with self.lock:
            if not self.key_path.exists():
                key = ec.generate_private_key(ec.SECP256R1())
                with self.key_path.open('xb') as f:
                    f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                              serialization.NoEncryption()))
                if os.name != 'nt':
                    self.key_path.chmod(0o600)
            key = serialization.load_pem_private_key(self.key_path.read_bytes(), password=None)
        raw = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        return base64.urlsafe_b64encode(raw).decode().rstrip('=')

    def settings(self, cookie):
        with self.relay.connect() as c:
            row = c.execute('SELECT enabled,subscription,error FROM notification_devices WHERE session=?',
                            (self.session_hash(cookie),)).fetchone()
            cursor = c.execute('SELECT COALESCE(MAX(id),0) FROM notification_events').fetchone()[0]
            client = c.execute('SELECT info FROM notification_clients WHERE session=?', (self.session_hash(cookie),)).fetchone()
            receipt = c.execute('SELECT id,kind,created,provider,provider_status,received,shown,clicked,error FROM notification_receipts WHERE session=? ORDER BY created DESC,rowid DESC LIMIT 1',
                                (self.session_hash(cookie),)).fetchone()
        return {'enabled': bool(row and row['enabled']), 'push': bool(row and row['subscription']),
                'error': (row['error'] if row else '') or self.last_error, 'cursor': cursor,
                'client': json.loads(client[0]) if client else None, 'last_delivery': dict(receipt) if receipt else None,
                'public_key': self.public_key()}

    @staticmethod
    def validate_subscription(value):
        from cryptography.hazmat.primitives.asymmetric import ec
        if not isinstance(value, dict):
            raise ValueError('推送订阅无效，请重新开启通知。')
        endpoint = value.get('endpoint', '')
        if not isinstance(endpoint, str) or len(endpoint) > 4096:
            raise ValueError('推送地址无效。')
        url = urlsplit(endpoint)
        host = url.hostname or ''
        allowed = (host == 'fcm.googleapis.com' or host.endswith('.push.apple.com')
                   or host.endswith('.push.services.mozilla.com') or host.endswith('.notify.windows.com')
                   or host.endswith('.wns.windows.com'))
        if url.scheme != 'https' or not allowed or url.port not in (None, 443) or url.username or url.password or url.fragment:
            raise ValueError('仅支持 Apple、Google、Mozilla 和 Microsoft 的系统推送地址。')
        try:
            keys = value['keys']
            public, auth = (base64.urlsafe_b64decode(keys[name] + '=' * (-len(keys[name]) % 4))
                            for name in ('p256dh', 'auth'))
            ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), public)
            if len(auth) != 16:
                raise ValueError()
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError('推送密钥无效，请重新开启通知。') from error
        return {'endpoint': endpoint, 'keys': {'p256dh': keys['p256dh'], 'auth': keys['auth']}}

    def configure(self, cookie, enabled, subscription=None):
        if not isinstance(enabled, bool):
            raise ValueError('通知设置无效。')
        subscription = self.validate_subscription(subscription) if subscription is not None else None
        with self.lock:
            # Capture existing work before subscribing, so old completions do not alert.
            if enabled:
                self.collect()
            with self.relay.connect() as c:
                cursor = c.execute('SELECT COALESCE(MAX(id),0) FROM notification_events').fetchone()[0]
                if subscription:
                    c.execute('DELETE FROM notification_devices WHERE subscription=? AND session!=?',
                              (json.dumps(subscription), self.session_hash(cookie)))
                c.execute('INSERT OR REPLACE INTO notification_devices VALUES (?,?,?,?,?,?,?)',
                          (self.session_hash(cookie), int(enabled), json.dumps(subscription) if enabled and subscription else '',
                           cursor, cursor, 0, ''))
        return self.settings(cookie)

    def revoke(self, cookie):
        with self.relay.connect() as c:
            c.execute('DELETE FROM notification_devices WHERE session=?', (self.session_hash(cookie),))
            c.execute('DELETE FROM notification_clients WHERE session=?', (self.session_hash(cookie),))

    def sync(self, cookie, subscription):
        subscription = self.validate_subscription(subscription)
        session = self.session_hash(cookie)
        with self.lock, self.relay.connect() as c:
            current = c.execute('SELECT * FROM notification_devices WHERE session=?', (session,)).fetchone()
            duplicates = []
            for row in c.execute("SELECT * FROM notification_devices WHERE subscription!='' AND session!=?", (session,)):
                if json.loads(row['subscription'])['endpoint'] == subscription['endpoint']:
                    duplicates.append(row)
            if current is None and duplicates:
                old = max(duplicates, key=lambda row: row['push_cursor'])
                c.execute('INSERT INTO notification_devices VALUES (?,?,?,?,?,?,?)',
                          (session, old['enabled'], json.dumps(subscription), old['enabled_after'], old['push_cursor'], 0, ''))
            elif current and current['enabled']:
                c.execute('UPDATE notification_devices SET subscription=?,retry_at=0,error=? WHERE session=?',
                          (json.dumps(subscription), '', session))
            for row in duplicates:
                c.execute('DELETE FROM notification_devices WHERE session=?', (row['session'],))
        return self.settings(cookie)

    def client_info(self, cookie, value):
        if not isinstance(value, dict):
            raise ValueError('通知设备信息无效。')
        info = {'permission': value.get('permission') if value.get('permission') in ('granted','denied','default','unsupported') else 'unknown',
                'standalone': value.get('standalone') is True, 'worker': str(value.get('worker') or '')[:40]}
        with self.relay.connect() as c:
            c.execute('INSERT OR REPLACE INTO notification_clients VALUES (?,?,?)',
                      (self.session_hash(cookie),json.dumps(info),int(time.time())))
        return {'ok': True}

    def acknowledge(self, token, stage, error=''):
        if not isinstance(token, str) or not re.fullmatch(r'[\w-]{43}', token) or stage not in ('received','shown','clicked','failed'):
            raise ValueError('通知回执无效。')
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.relay.connect() as c:
            row = c.execute('SELECT session FROM notification_receipts WHERE token_hash=? AND created>?', (digest,int(time.time())-86400)).fetchone()
            if not row:
                raise ValueError('通知回执已失效。')
            if stage == 'failed':
                reason = error if error in ('NotAllowedError','AbortError','TypeError','InvalidStateError') else 'DisplayError'
                c.execute('UPDATE notification_receipts SET received=?,error=? WHERE token_hash=?', (int(time.time()),reason,digest))
            else:
                c.execute(f'UPDATE notification_receipts SET {stage}=? WHERE token_hash=?', (int(time.time()),digest))
        return {'ok': True}

    def delivery(self, cookie, identity):
        with self.relay.connect() as c:
            row = c.execute('SELECT id,kind,created,provider,provider_status,received,shown,clicked,error FROM notification_receipts WHERE id=? AND session=?',
                            (identity,self.session_hash(cookie))).fetchone()
        if row is None:
            raise ValueError('找不到此设备的通知回执。')
        return dict(row)

    def send_to_device(self, device, payload):
        import secrets
        identity = secrets.token_hex(16)
        token = secrets.token_urlsafe(32)
        sub = json.loads(device['subscription'])
        with self.relay.connect() as c:
            c.execute('INSERT INTO notification_receipts(id,session,token_hash,kind,created,provider) VALUES (?,?,?,?,?,?)',
                      (identity,device['session'],hashlib.sha256(token.encode()).hexdigest(),payload.get('kind',''),int(time.time()),urlsplit(sub['endpoint']).hostname))
        try:
            result = self.sender(sub,dict(payload,receipt_token=token))
        except Exception as error:
            response = getattr(error,'response',None)
            with self.relay.connect() as c:
                c.execute('UPDATE notification_receipts SET provider_status=?,error=? WHERE id=?',
                          (getattr(response,'status_code',None),'ProviderRejected' if response is not None else 'NetworkError',identity))
            raise
        with self.relay.connect() as c:
            c.execute('UPDATE notification_receipts SET provider_status=? WHERE id=?', ((result or {}).get('status',201),identity))
        return identity

    def capture(self):
        data = self.relay.data
        if not data.database('thread_history'):
            return {}
        states = {}
        with data.connect('thread_history') as c, self.relay.connect() as local:
            receipts = local.execute("SELECT thread_id,text FROM deliveries WHERE receipt NOT LIKE '%\"result\": \"failed\"%'").fetchall()
            queued_answers = {}
            for row in receipts:
                queued_answers.setdefault(row['thread_id'], {}).update(question_answers(row['text']))
            for chat in data.chats():
                turn = c.execute('SELECT turn_id,status FROM thread_turns WHERE thread_id=? ORDER BY rollout_ordinal DESC LIMIT 1',
                                 (chat.id,)).fetchone()
                questions = c.execute("SELECT item_json FROM thread_items WHERE thread_id=? AND item_type='agentMessage' "
                                      "AND json_extract(item_json,'$.delivery')='async' ORDER BY rollout_ordinal DESC LIMIT 20", (chat.id,)).fetchall()
                answered = dict(queued_answers.get(chat.id, {}))
                for row in c.execute("SELECT item_json FROM thread_items WHERE thread_id=? AND item_type='userMessage' "
                                     "AND item_json LIKE '%send_user_message_question_reply%' ORDER BY rollout_ordinal DESC LIMIT 60", (chat.id,)):
                    from backend import user_text
                    answered.update(question_answers(user_text(json.loads(row[0]))))
                pending = []
                for row in questions:
                    item = json.loads(row[0])
                    if any((item['id'], index) not in answered for index, _ in enumerate(item.get('questions') or [])):
                        pending.append(item['id'])
                states[chat.id] = {'turn': list(turn) if turn else [], 'questions': pending, 'title': chat.title}
        return states

    def collect(self):
        states = self.capture()
        with self.relay.connect() as c:
            for thread, state in states.items():
                previous = c.execute('SELECT state FROM notification_states WHERE thread_id=?', (thread,)).fetchone()
                if previous:
                    old = json.loads(previous[0])
                    # Seed the prior state when upgrading a database that predates deduplication.
                    for identity in self.state_identities(thread, old):
                        c.execute('INSERT OR IGNORE INTO notification_dedup VALUES (?,?)', (identity, int(time.time())))
                    new_questions = set(state['questions']) - set(old['questions'])
                    if new_questions:
                        identities = [f'{thread}:question:{item}' for item in sorted(new_questions)]
                        fresh = [identity for identity in identities if not c.execute('SELECT 1 FROM notification_dedup WHERE identity=?', (identity,)).fetchone()]
                        if fresh:
                            self.event(c, thread, 'needs_input', chat_title=state.get('title', ''))
                            c.executemany('INSERT INTO notification_dedup VALUES (?,?)', [(identity, int(time.time())) for identity in fresh])
                    elif state['turn'] != old['turn'] and state['turn'] and state['turn'][1] in ('completed', 'failed', 'interrupted'):
                        self.event(c, thread, state['turn'][1], self.state_identities(thread, state)[0], state.get('title', ''))
                c.execute('INSERT OR REPLACE INTO notification_states VALUES (?,?)', (thread, json.dumps(state)))

    @staticmethod
    def state_identities(thread, state):
        identities = []
        if state['turn'] and state['turn'][1] in ('completed', 'failed', 'interrupted'):
            identities.append(f"{thread}:turn:{state['turn'][0]}:{state['turn'][1]}")
        identities.extend(f'{thread}:question:{item}' for item in state['questions'])
        return identities

    def event(self, c, thread, kind, identity=None, chat_title=''):
        if identity and not c.execute('INSERT OR IGNORE INTO notification_dedup VALUES (?,?)', (identity, int(time.time()))).rowcount:
            return None
        title = {'completed': '任务已完成', 'failed': '任务执行失败', 'interrupted': '任务已中断',
                 'needs_input': '有问题需要你回复', 'test': '通知测试'}[kind]
        payload = {'title': 'Codex · ' + (self.relay.config.get('label') or '电脑'),
                   'body': (chat_title[:80] + ' · ' if chat_title else '') + title + '，点击查看。',
                   'chat_title': chat_title, 'created': int(time.time()), 'thread_id': thread, 'kind': kind}
        cursor = c.execute('INSERT INTO notification_events(thread_id,kind,payload,created) VALUES (?,?,?,?)',
                           (thread, kind, json.dumps(payload, ensure_ascii=False), int(time.time())))
        return cursor.lastrowid

    def poll(self, cookie, after):
        try:
            after = max(0, int(after))
        except (TypeError, ValueError) as error:
            raise ValueError('通知游标无效。') from error
        with self.relay.connect() as c:
            device = c.execute('SELECT * FROM notification_devices WHERE session=?', (self.session_hash(cookie),)).fetchone()
            if not device or not device['enabled']:
                return {'events': [], 'cursor': after}
            rows = c.execute('SELECT id,payload FROM notification_events WHERE id>? ORDER BY id LIMIT 100',
                             (max(after, device['enabled_after']),)).fetchall()
        return {'events': [dict(json.loads(row['payload']), id=row['id']) for row in rows],
                'cursor': rows[-1]['id'] if rows else after}

    def push(self, subscription, payload):
        from pywebpush import webpush
        import requests
        # Endpoints are allowlisted; redirects must not redirect encrypted posts elsewhere.
        class PushSession(requests.Session):
            def post(self, url, **kwargs):
                kwargs['allow_redirects'] = False
                return super().post(url, **kwargs)
        with PushSession() as session:
            response = webpush(subscription_info=subscription, data=json.dumps(payload, ensure_ascii=False),
                    vapid_private_key=str(self.key_path), vapid_claims={'sub': 'https://' + urlsplit(self.relay.origin).hostname},
                    ttl=3600, timeout=12, requests_session=session)
        return {'status': response.status_code}

    def send_pushes(self):
        now = int(time.time())
        with self.relay.connect() as c:
            c.execute('DELETE FROM notification_devices WHERE session NOT IN (SELECT hash FROM sessions WHERE expires>?)', (now,))
            devices = [dict(row) for row in c.execute('SELECT * FROM notification_devices WHERE enabled=1 AND subscription!=? AND retry_at<=?', ('', now))]
        for device in devices:
            with self.relay.connect() as c:
                rows = c.execute('SELECT id,payload FROM notification_events WHERE id>? AND created>? ORDER BY id LIMIT 20',
                                 (device['push_cursor'], now - 3600)).fetchall()
            for row in rows:
                payload = dict(json.loads(row['payload']), id=row['id'])
                try:
                    self.send_to_device(device, payload)
                except Exception as error:
                    response = getattr(error, 'response', None)
                    expired = response is not None and response.status_code in (404, 410)
                    with self.relay.connect() as c:
                        c.execute('UPDATE notification_devices SET subscription=?,retry_at=?,error=? WHERE session=?',
                                  ('' if expired else device['subscription'], now + 60,
                                   '系统推送订阅已过期，请重新开启。' if expired else '系统推送暂未送达，将自动重试；页面内提醒仍可使用。', device['session']))
                    break
                with self.relay.connect() as c:
                    c.execute('UPDATE notification_devices SET push_cursor=?,error=? WHERE session=?', (row['id'], '', device['session']))

    def test(self, cookie):
        with self.relay.connect() as c:
            row = c.execute('SELECT * FROM notification_devices WHERE session=? AND enabled=1', (self.session_hash(cookie),)).fetchone()
        if not row or not row['subscription']:
            raise ValueError('请先开启系统推送。')
        try:
            identity = self.send_to_device(dict(row), {'title': 'Codex 通知测试', 'body': '请查看手机通知栏。', 'thread_id': '', 'kind': 'test', 'id': 'test-' + str(time.time_ns())})
        except Exception as error:
            raise ValueError('测试推送未送达，请检查电脑访问系统推送服务的网络。') from error
        return {'ok': True, 'delivery_id': identity}

    def start(self):
        def work():
            while not self.stop.wait(4):
                try:
                    with self.relay.connect() as c:
                        enabled = c.execute('SELECT 1 FROM notification_devices WHERE enabled=1 LIMIT 1').fetchone()
                    if enabled:
                        with self.lock:
                            self.collect()
                        self.send_pushes()
                    self.last_error = ''
                except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
                    self.last_error = '暂时无法读取任务状态，通知服务正在重试。'
        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def close(self):
        self.stop.set()
        if self.worker:
            self.worker.join(timeout=1)
