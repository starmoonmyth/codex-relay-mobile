import contextlib
import json
import pathlib
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace

from activity import Activities, transcript_activity
from backend import Chat, CodexData, Message
from grok_backend import event_id


class ActivityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name)
        self.path = self.root / 'chat.jsonl'
        self.chat = Chat('chat-a', 'Fixture', str(self.root), str(self.path), 0)
        self.activities = Activities()

    def write(self, entries):
        self.path.write_text('\n'.join(json.dumps(entry) for entry in entries) + '\n', 'utf-8')

    def test_claude_waits_for_final_text_and_keeps_completion_stable(self):
        chat = Chat(**dict(vars(self.chat), provider='claude'))
        entries = [{'type': 'user', 'uuid': 'user-a'}, {'type': 'assistant', 'message': {'id': 'reply-a',
            'stop_reason': 'tool_use', 'content': [{'type': 'tool_use'}]}}]
        self.write(entries)
        self.assertEqual(transcript_activity(chat)['state'], 'working')
        entries.append({'type': 'assistant', 'message': {'id': 'reply-a', 'stop_reason': 'end_turn', 'content': [{'type': 'thinking'}]}})
        self.write(entries)
        self.assertEqual(transcript_activity(chat)['state'], 'working')
        entries.append({'type': 'assistant', 'message': {'id': 'reply-a', 'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': 'Done'}]}})
        self.write(entries)
        value = self.activities.transcript(chat)
        self.assertEqual(value['state'], 'completed')
        self.assertTrue(value['completion_id'])
        self.assertEqual(value['message_id'], 'reply-a')
        entries.append({'type': 'cost-state', 'cost': 100})
        self.write(entries)
        self.assertEqual(self.activities.transcript(chat)['completion_id'], value['completion_id'])
        entries.append({'type': 'user', 'uuid': 'new-user'})
        self.write(entries)
        self.assertEqual(self.activities.transcript(chat)['state'], 'working')

    def test_grok_completion_uses_prompt_and_first_stream_chunk(self):
        chat = Chat(**dict(vars(self.chat), provider='grok'))
        def event(kind, identity, **extra):
            return {'params': {'update': {'sessionUpdate': kind, **extra},
                '_meta': {'eventId': identity, 'promptId': 'prompt', 'streamStartMs': 1}}}
        self.write([event('user_message_chunk', 'user'), event('agent_message_chunk', 'chunk-a'),
            event('agent_message_chunk', 'chunk-b'), event('turn_completed', 'done', prompt_id='prompt', stop_reason='end_turn')])
        value = transcript_activity(chat)
        self.assertEqual(value['state'], 'completed')
        identity = event_id(event('agent_message_chunk', 'chunk-a'))
        self.assertEqual(value['message_id'], identity)
        self.assertTrue(self.activities.visible(value, [Message(identity, 'assistant', 'Done')])['visible_completion'])

    def test_native_latest_turn_and_queue_have_distinct_states(self):
        with contextlib.closing(sqlite3.connect(self.root / 'thread_history_1.sqlite')) as c, c:
            c.execute('CREATE TABLE thread_turns(thread_id TEXT,turn_id TEXT,status TEXT,rollout_ordinal INT,final_agent_item_id TEXT)')
            c.executemany('INSERT INTO thread_turns VALUES (?,?,?,?,?)', [('chat-a', 'old', 'inProgress', 1, ''), ('chat-a', 'new', 'completed', 2, 'final')])
        with contextlib.closing(sqlite3.connect(self.root / 'queue_1.sqlite')) as c, c:
            c.execute('CREATE TABLE queued_items(thread_id TEXT)')
        with contextlib.closing(sqlite3.connect(self.root / 'mobile.sqlite')) as c, c:
            c.execute('CREATE TABLE deliveries(id TEXT,thread_id TEXT,receipt TEXT)')
        @contextlib.contextmanager
        def connect():
            with contextlib.closing(sqlite3.connect(self.root / 'mobile.sqlite')) as c:
                c.row_factory = sqlite3.Row
                yield c
        relay = SimpleNamespace(root=self.root, data=CodexData(self.root), connect=connect)
        value = self.activities.read(relay, [self.chat])['chat-a']
        self.assertEqual(value['state'], 'completed')
        self.assertEqual(value['message_id'], 'final')
        self.assertFalse(self.activities.visible(value, [Message('old', 'assistant', 'Old', turn_id='old')])['visible_completion'])
        self.assertTrue(self.activities.visible(value, [Message('final', 'assistant', 'Done', turn_id='new')])['visible_completion'])
        with contextlib.closing(sqlite3.connect(self.root / 'queue_1.sqlite')) as c, c:
            c.execute("INSERT INTO queued_items VALUES ('chat-a')")
        self.assertEqual(self.activities.read(relay, [self.chat])['chat-a']['state'], 'queued')
        with contextlib.closing(sqlite3.connect(self.root / 'thread_history_1.sqlite')) as c, c:
            c.execute("UPDATE thread_turns SET status='inProgress' WHERE turn_id='new'")
        self.assertEqual(self.activities.read(relay, [self.chat])['chat-a']['state'], 'working')

    def test_reading_activity_does_not_write_transcript(self):
        self.write([{'type': 'assistant', 'message': {'id': 'a', 'stop_reason': 'end_turn', 'content': 'Done'}}])
        before = self.path.read_bytes()
        self.activities.transcript(Chat(**dict(vars(self.chat), provider='claude')))
        self.assertEqual(self.path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
