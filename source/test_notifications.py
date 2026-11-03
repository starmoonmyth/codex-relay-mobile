import json
import pathlib
import tempfile
import time
import unittest
from unittest import mock

from notifications import Notifications
from server import Relay
from test_grok_backend import SESSION, make_session, update


class ProviderNotificationsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name)
        self.claude = self.root / 'claude/projects/project/session.jsonl'
        self.claude.parent.mkdir(parents=True)
        self.grok = make_session(self.root / 'grok', self.root)
        self.relay = Relay(self.root / 'relay', {'origin': 'http://localhost:8879', 'provider': 'codex', 'label': 'Test Mac',
            'codex_home': str(self.root / 'codex'), 'claude_home': str(self.root / 'claude'),
            'grok_home': str(self.root / 'grok')})
        self.sent = []
        self.relay.notifications.sender = lambda sub, payload: self.sent.append(payload)
        self.session = Notifications.session_hash('test-cookie')
        with self.relay.connect() as c:
            c.execute('INSERT INTO sessions VALUES (?,?)', (self.session, time.time() + 3600))
            c.execute('INSERT INTO notification_devices VALUES (?,?,?,?,?,?,?)',
                (self.session, 1, json.dumps({'endpoint': 'https://web.push.apple.com/isolated-test'}), 0, 0, 0, ''))

    def write(self, claude_state, grok_state):
        entries = [{'type': 'user', 'uuid': 'user', 'sessionId': 'session', 'cwd': str(self.root),
            'message': {'content': 'task'}}]
        if claude_state != 'working':
            entries.append({'type': 'assistant', 'uuid': 'answer', 'parentUuid': 'user',
                'sessionId': 'session', 'isApiErrorMessage': claude_state == 'failed',
                'message': {'content': 'answer', 'stop_reason': 'max_tokens' if claude_state == 'interrupted' else 'end_turn'}})
        self.claude.write_text('\n'.join(json.dumps(e) for e in entries), 'utf-8')
        events = [update('user_message_chunk', 'task', 'user')]
        if grok_state not in ('working', 'needs_input'):
            events.append(update('turn_completed', identity='done', stop_reason={
                'completed': 'end_turn', 'interrupted': 'cancelled', 'failed': 'error'}[grok_state]))
        (self.grok / 'updates.jsonl').write_text('\n'.join(json.dumps(e) for e in events), 'utf-8')
        (self.grok / 'events.jsonl').write_text(json.dumps({
            'type': 'permission_requested' if grok_state == 'needs_input' else 'permission_resolved',
            'ts': '1970-01-01T00:02:04Z'}), 'utf-8')
        self.relay.activities.cache.clear()

    def collect(self):
        self.relay.notifications.collect()
        self.relay.notifications.send_pushes()

    def test_background_providers_complete_and_push_once_without_codex_database(self):
        self.write('working', 'working')
        self.collect()
        self.assertEqual(self.sent, [])
        self.write('completed', 'completed')
        self.collect()
        self.collect()
        self.assertEqual({e['thread_id'] for e in self.sent}, {'claude:session', 'grok:' + SESSION})
        self.assertEqual({e['title'] for e in self.sent}, {'Claude · Test Mac', 'Grok · Test Mac'})
        self.assertEqual([e['kind'] for e in self.sent], ['completed', 'completed'])
        self.relay.notifications = Notifications(self.relay, sender=lambda sub, payload: self.sent.append(payload))
        self.collect()
        self.assertEqual(len(self.sent), 2)

    def test_existing_completions_are_baseline_and_failures_and_permissions_alert(self):
        self.write('completed', 'completed')
        self.collect()
        self.assertEqual(self.sent, [])
        self.write('working', 'working')
        self.collect()
        self.write('failed', 'needs_input')
        self.collect()
        self.collect()
        self.assertEqual([e['kind'] for e in self.sent], ['failed', 'needs_input'])
        self.write('interrupted', 'interrupted')
        self.collect()
        self.collect()
        self.assertEqual([e['kind'] for e in self.sent][-2:], ['interrupted', 'interrupted'])


class ForwardNotificationsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name)
        self.mac = 'https://mac.example.test'
        self.windows = 'https://windows.example.test'
        self.key = 'isolated-forward-key'
        config = {'origin': self.windows, 'label': 'Windows',
            'codex_home': str(self.root / 'codex'), 'claude_home': str(self.root / 'claude'),
            'grok_home': str(self.root / 'grok'), 'peers': [{'origin': self.mac, 'label': 'Mac'}],
            'notification_forward_sources': {self.mac: self.key}}
        self.hub = Relay(self.root / 'hub', config)
        source = dict(config, origin=self.mac, label='Mac', peers=[{'origin': self.windows, 'label': 'Windows'}],
            notification_forward={'origin': self.windows, 'label': 'Windows', 'key': self.key})
        self.source = Relay(self.root / 'source', source)
        self.sent = []
        self.hub.notifications.sender = lambda sub, payload: self.sent.append(payload)
        session = Notifications.session_hash('hub-phone')
        with self.hub.connect() as c:
            c.execute('INSERT INTO sessions VALUES (?,?)', (session, time.time() + 3600))
            c.execute('INSERT INTO notification_devices VALUES (?,?,?,?,?,?,?)',
                (session, 1, json.dumps({'endpoint': 'https://web.push.apple.com/isolated-forward-test'}), 0, 0, 0, ''))

    def forward(self, action, **body):
        return self.hub.notifications.receive_forward(action, dict(body, source=self.mac), 'Bearer ' + self.key)

    def test_existing_phone_subscription_receives_remote_status_once_and_keeps_origin(self):
        with self.source.connect() as c:
            self.source.notifications.event(c, 'claude:shared-id', 'completed', chat_title='Mac task')
        with mock.patch.object(self.source.notifications, 'forward_request', side_effect=self.forward):
            self.source.notifications.send_forwarded()
            self.source.notifications.send_forwarded()
            settings = self.source.notifications.settings('phone-on-mac')
            self.assertTrue(settings['push'])
            self.assertEqual(settings['forwarding']['origin'], self.windows)
            self.assertNotIn('key', settings['forwarding'])
            self.assertTrue(self.source.notifications.test('phone-on-mac')['forwarded'])
        self.hub.notifications.send_pushes()
        self.hub.notifications.send_pushes()
        self.assertEqual([p['kind'] for p in self.sent], ['completed', 'test'])
        self.assertEqual(self.sent[0]['source_origin'], self.mac)
        self.assertEqual(self.sent[0]['thread_id'], 'claude:shared-id')
        self.assertEqual(self.sent[0]['title'], 'Claude · Mac')
        with self.source.connect() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM notification_devices').fetchone()[0], 0)

    def test_network_retry_and_restart_do_not_drop_or_duplicate_remote_event(self):
        with self.source.connect() as c:
            self.source.notifications.event(c, 'chat', 'failed')
        def accepted_then_disconnected(action, **body):
            self.forward(action, **body)
            raise ValueError('lost acknowledgement')
        with mock.patch.object(self.source.notifications, 'forward_request', side_effect=accepted_then_disconnected):
            with self.assertRaises(ValueError):
                self.source.notifications.send_forwarded()
        self.source.notifications = Notifications(self.source)
        with mock.patch.object(self.source.notifications, 'forward_request', side_effect=self.forward):
            self.source.notifications.send_forwarded()
        self.hub.notifications.send_pushes()
        self.assertEqual(len(self.sent), 1)

    def test_forward_auth_is_scoped_and_event_validation_rejects_old_and_untrusted_data(self):
        event = {'id': 1, 'thread_id': 'chat', 'kind': 'completed', 'created': time.time()}
        with self.assertRaises(PermissionError):
            self.hub.notifications.receive_forward('events', {'source': self.mac, 'event': event}, 'Bearer wrong')
        with self.assertRaises(ValueError):
            self.forward('send', event=event)
        for bad in [dict(event, created=time.time() - 4000), dict(event, kind='tools/call'), dict(event, thread_id=['chat'])]:
            with self.assertRaises(ValueError):
                self.forward('events', event=bad)
        self.forward('events', event=event)
        with self.hub.connect() as c:
            self.hub.notifications.event(c, 'chat', 'completed')
            rows = c.execute('SELECT thread_id FROM notification_events').fetchall()
        self.assertEqual(len({r[0] for r in rows}), 2)

    def test_outbound_request_preserves_tls_and_blocks_redirects_and_environment_proxy(self):
        with mock.patch('requests.Session') as session_type:
            session = session_type.return_value.__enter__.return_value
            session.post.return_value.status_code = 200
            session.post.return_value.json.return_value = {'enabled': True, 'push': True}
            self.source.notifications.forward_request('status')
        self.assertFalse(session.trust_env)
        args = session.post.call_args
        self.assertEqual(args.args[0], self.windows + '/api/notification-relay/status')
        self.assertFalse(args.kwargs['allow_redirects'])
        self.assertNotEqual(args.kwargs.get('verify'), False)
        self.assertEqual(args.kwargs['headers']['Authorization'], 'Bearer ' + self.key)

    def test_existing_events_are_baselined_when_forwarding_is_first_enabled(self):
        with self.source.connect() as c:
            c.execute('DELETE FROM notification_forward_cursor')
            self.source.notifications.event(c, 'old-chat', 'completed')
        self.source.notifications = Notifications(self.source)
        with mock.patch.object(self.source.notifications, 'forward_request') as request:
            self.source.notifications.send_forwarded()
            request.assert_not_called()


if __name__ == '__main__':
    unittest.main()
