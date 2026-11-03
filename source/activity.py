from __future__ import annotations

import hashlib
import json
import pathlib
import sqlite3
import threading
import time

from claude_backend import ClaudeData, claude_branch, internal_message
from grok_backend import event_id, event_time, run_status, native_turn_state


STATES = {'inProgress': 'working', 'needsInput': 'needs_input', 'completed': 'completed',
          'failed': 'failed', 'interrupted': 'interrupted'}


def completed(identity, message_id=''):
    key = hashlib.sha256(str(identity).encode()).hexdigest()[:32]
    return {'state': 'completed', 'completion_id': key, 'message_id': message_id}


def transcript_activity(chat):
    activity = {'state': 'idle', 'completion_id': ''}
    if chat.source == 'grok-bot':
        return activity
    last_agent, group = '', None
    entries = claude_branch(chat) if chat.provider == 'claude' else ClaudeData._read(pathlib.Path(chat.rollout_path), max_bytes=2 * 1024 * 1024)
    for index, entry in enumerate(entries):
        if chat.provider == 'claude':
            if entry.get('isSidechain') or internal_message(entry):
                continue
            message = entry.get('message') or {}
            content = message.get('content')
            text = content if isinstance(content, str) else ''.join(
                part.get('text', '') for part in content or []
                if isinstance(part, dict) and part.get('type') == 'text')
            if entry.get('type') == 'user':
                if entry.get('toolUseResult') == 'User rejected tool use' or '[Request interrupted by user' in text:
                    activity = {'state': 'interrupted', 'completion_id': '', 'turn_id': entry.get('uuid') or str(index)}
                else:
                    activity = {'state': 'working', 'completion_id': ''}
            elif entry.get('type') == 'assistant':
                activity = {'state': 'working', 'completion_id': ''}
                if entry.get('isApiErrorMessage'):
                    activity['state'] = 'failed'
                elif message.get('stop_reason') in ('end_turn', 'stop_sequence'):
                    if text.strip():
                        identity = entry.get('uuid') or message.get('id') or str(index)
                        activity = completed(identity, identity)
                elif message.get('stop_reason') == 'max_tokens':
                    activity['state'] = 'interrupted'
        else:
            params = entry.get('params') or {}
            update, meta = params.get('update') or {}, params.get('_meta') or {}
            kind = update.get('sessionUpdate')
            if kind == 'user_message_chunk':
                activity = {'state': 'working', 'completion_id': '', 'user_timestamp_ms': event_time(entry),
                    'user_message_id': event_id(entry, index)}
                group, last_agent = None, ''
            elif kind == 'agent_message_chunk':
                next_group = (meta.get('promptId'), meta.get('streamStartMs'))
                if next_group != group or not last_agent:
                    last_agent = event_id(entry, index)
                group = next_group
            elif kind == 'turn_completed':
                reason = str(update.get('stop_reason', '')).lower()
                if reason in ('end_turn', 'endturn'):
                    activity.update(completed(update.get('prompt_id') or event_id(entry, index), last_agent))
                else:
                    activity.update(state='interrupted' if 'cancel' in reason else 'failed', completion_id='')
            else:
                group = None
    if chat.provider == 'grok' and activity['state'] != 'idle':
        status = {'working': 'inProgress', 'completed': 'completed', 'interrupted': 'interrupted', 'failed': 'failed'}[activity['state']]
        native = native_turn_state(chat, status, activity.get('user_message_id', ''))
        state = STATES.get(native['status'], 'idle')
        if state == 'completed' and activity['state'] != 'completed':
            activity.update(completed(native['turn_id'], last_agent))
        activity['state'] = state
        activity['turn_id'] = native['turn_id']
        activity['agent_message_id'] = last_agent
    if chat.provider == 'claude' and entries:
        activity.setdefault('turn_id', entries[-1].get('uuid') or '')
    return activity


class Activities:
    def __init__(self):
        self.cache = {}
        self.lock = threading.Lock()

    def transcript(self, chat):
        try:
            stat = pathlib.Path(chat.rollout_path).stat()
            signature = (chat.rollout_path, chat.history_head, chat.source, stat.st_mtime_ns, stat.st_size,
                         int(time.time() // 4) if chat.provider == 'grok' else 0)
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
            chat = next(chat for chat in chats if chat.id == row['thread_id'])
            if not chat.writable or receipt.get('detached'):
                continue
            value = result[row['thread_id']]
            if receipt.get('status') == '正在投递':
                result[row['thread_id']] = {'state': 'sending', 'completion_id': ''}
            elif receipt.get('provider') == 'grok' and receipt.get('result') == 'sent':
                finished = run_status(relay.root / 'grok-runs' / row['id'], row['thread_id'].split(':', 1)[1])
                native_received = (bool(value.get('user_message_id') and value['user_message_id'] == receipt.get('message_id')) or
                    bool(receipt.get('sent_at_ms') and value.get('user_timestamp_ms', 0) >= receipt['sent_at_ms']))
                if finished is None:
                    if not native_received:
                        result[row['thread_id']] = {'state': 'working', 'completion_id': ''}
                elif finished.get('ok') and value['state'] != 'completed':
                    current = value.get('user_message_id') == receipt.get('message_id')
                    if native_received and not receipt.get('message_id'):
                        match = relay.grok.historical_receipt_message(receipt)
                        current = bool(match and match.id == value.get('user_message_id'))
                    if not native_received or current:
                        value.update(completed(receipt['id'], value.get('agent_message_id', '')))
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
