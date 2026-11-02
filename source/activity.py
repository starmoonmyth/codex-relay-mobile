from __future__ import annotations

import hashlib
import json
import pathlib
import sqlite3
import threading

from claude_backend import ClaudeData


STATES = {'inProgress': 'working', 'needsInput': 'needs_input', 'completed': 'completed',
          'failed': 'failed', 'interrupted': 'interrupted'}


def completed(identity, message_id=''):
    key = hashlib.sha256(str(identity).encode()).hexdigest()[:32]
    return {'state': 'completed', 'completion_id': key, 'message_id': message_id}


def transcript_activity(chat):
    activity = {'state': 'idle', 'completion_id': ''}
    last_agent, group = '', None
    for index, entry in enumerate(ClaudeData._read(pathlib.Path(chat.rollout_path), max_bytes=2 * 1024 * 1024)):
        if chat.provider == 'claude':
            if entry.get('isSidechain'):
                continue
            message = entry.get('message') or {}
            if entry.get('type') == 'user':
                activity = {'state': 'working', 'completion_id': ''}
            elif entry.get('type') == 'assistant':
                activity = {'state': 'working', 'completion_id': ''}
                if entry.get('isApiErrorMessage'):
                    activity['state'] = 'failed'
                elif message.get('stop_reason') in ('end_turn', 'stop_sequence'):
                    content = message.get('content')
                    text = content if isinstance(content, str) else ''.join(part.get('text', '') for part in content or [] if isinstance(part, dict) and part.get('type') == 'text')
                    if text.strip():
                        identity = message.get('id') or entry.get('uuid') or str(index)
                        activity = completed(identity, identity)
                elif message.get('stop_reason') == 'max_tokens':
                    activity['state'] = 'interrupted'
        else:
            params = entry.get('params') or {}
            update, meta = params.get('update') or {}, params.get('_meta') or {}
            kind = update.get('sessionUpdate')
            if kind == 'user_message_chunk':
                activity = {'state': 'working', 'completion_id': ''}
                group, last_agent = None, ''
            elif kind == 'agent_message_chunk':
                next_group = (meta.get('promptId'), meta.get('streamStartMs'))
                if next_group != group or not last_agent:
                    last_agent = str(meta.get('eventId') or '')
                group = next_group
            elif kind == 'turn_completed':
                reason = str(update.get('stop_reason', '')).lower()
                if reason in ('end_turn', 'endturn'):
                    activity = completed(update.get('prompt_id') or meta.get('eventId') or last_agent or str(index), last_agent)
                else:
                    activity = {'state': 'interrupted' if 'cancel' in reason else 'failed', 'completion_id': ''}
    return activity


class Activities:
    def __init__(self):
        self.cache = {}
        self.lock = threading.Lock()

    def transcript(self, chat):
        try:
            stat = pathlib.Path(chat.rollout_path).stat()
            signature = (chat.rollout_path, stat.st_mtime_ns, stat.st_size)
        except OSError:
            return {'state': 'idle', 'completion_id': ''}
        with self.lock:
            old = self.cache.get(chat.id)
            if old and old[0] == signature:
                return old[1]
        value = transcript_activity(chat)
        with self.lock:
            self.cache[chat.id] = (signature, value)
        return value

    def read(self, relay, chats):
        result = {chat.id: {'state': 'idle', 'completion_id': ''} for chat in chats}
        codex = [chat for chat in chats if chat.provider == 'codex']
        if codex:
            try:
                if relay.data.database('thread_history'):
                    with relay.data.connect('thread_history') as connection:
                        columns = {row[1] for row in connection.execute('PRAGMA table_info(thread_turns)')}
                        final = ',t.final_agent_item_id' if 'final_agent_item_id' in columns else ''
                        rows = connection.execute('SELECT t.thread_id,t.turn_id,t.status' + final +
                            ' FROM thread_turns t WHERE t.thread_id IN (' + ','.join('?' for _ in codex) +
                            ') AND t.rollout_ordinal=(SELECT MAX(n.rollout_ordinal) FROM thread_turns n WHERE n.thread_id=t.thread_id)',
                            [chat.id for chat in codex]).fetchall()
                    for row in rows:
                        if row['thread_id'] not in result:
                            continue
                        value = {'state': STATES.get(row['status'], 'idle'), 'completion_id': '', 'turn_id': row['turn_id']}
                        if row['status'] == 'completed':
                            value.update(completed(row['turn_id'], row['final_agent_item_id'] if 'final_agent_item_id' in columns else ''))
                        result[row['thread_id']] = value
                if relay.data.database('queue'):
                    with relay.data.connect('queue') as connection:
                        queued = connection.execute('SELECT DISTINCT thread_id FROM queued_items').fetchall()
                    for row in queued:
                        if row[0] in result and result[row[0]]['state'] in ('idle', 'completed'):
                            result[row[0]] = {'state': 'queued', 'completion_id': ''}
            except (OSError, sqlite3.Error):
                pass
        for chat in chats:
            if chat.provider != 'codex':
                result[chat.id] = dict(self.transcript(chat))
        if not chats:
            return result
        with relay.connect() as connection:
            rows = connection.execute('SELECT d.id,d.thread_id,d.receipt FROM deliveries d WHERE d.thread_id IN (' +
                ','.join('?' for _ in chats) + ') AND d.rowid=(SELECT MAX(n.rowid) FROM deliveries n WHERE n.thread_id=d.thread_id)',
                [chat.id for chat in chats]).fetchall()
        for row in rows:
            if row['thread_id'] not in result:
                continue
            receipt = json.loads(row['receipt'])
            value = result[row['thread_id']]
            if receipt.get('status') == '正在投递':
                result[row['thread_id']] = {'state': 'sending', 'completion_id': ''}
            elif receipt.get('provider') == 'grok' and receipt.get('result') == 'sent':
                path = relay.root / 'grok-runs' / row['id'] / 'result.json'
                try:
                    finished = json.loads(path.read_text('utf-8'))
                except (OSError, ValueError):
                    finished = None
                if finished is None:
                    result[row['thread_id']] = {'state': 'working', 'completion_id': ''}
                elif not finished.get('ok') and value['state'] == 'working':
                    result[row['thread_id']] = {'state': 'failed', 'completion_id': ''}
        return result

    def visible(self, activity, messages):
        value = dict(activity)
        identity, turn = value.get('message_id'), value.get('turn_id')
        value['visible_completion'] = value['state'] == 'completed' and any(
            message.role == 'assistant' and message.phase != 'commentary' and
            (message.id == identity if identity else bool(turn and message.turn_id == turn)) for message in messages)
        return value
