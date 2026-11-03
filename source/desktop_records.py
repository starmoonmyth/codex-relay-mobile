from __future__ import annotations

import base64
import json
import os
import pathlib
import sys

from backend import Chat, Message


def read_json(path, fallback=None):
    try:
        return json.loads(pathlib.Path(path).read_text('utf-8'))
    except (OSError, ValueError):
        return fallback


def claude_desktop_roots():
    home = pathlib.Path.home()
    if sys.platform == 'darwin':
        return [home / 'Library/Application Support/Claude']
    if os.name == 'nt':
        local = pathlib.Path(os.environ.get('LOCALAPPDATA', home / 'AppData/Local'))
        roaming = pathlib.Path(os.environ.get('APPDATA', home / 'AppData/Roaming'))
        return [roaming / 'Claude', *(local / 'Claude-Data').glob('*')]
    return []


class ClaudeDesktopRecords:
    def __init__(self, roots):
        self.roots = list(map(pathlib.Path, roots))

    def sessions(self):
        result = {}
        for root in self.roots:
            for path in (root / 'claude-code-sessions').glob('*/*/local_*.json'):
                item = read_json(path, {})
                if not isinstance(item, dict) or not item.get('cliSessionId'):
                    continue
                identity = item['cliSessionId']
                # Two desktop views can select different branches of one CLI file.
                result.setdefault(identity, []).append(dict(item, _record_path=str(path)))
        return result


def grok_bot_root():
    home = pathlib.Path.home()
    if sys.platform == 'darwin':
        return home / 'Library/Application Support/Grok Bot/sand-client-persistence'
    return pathlib.Path(os.environ.get('APPDATA', home / 'AppData/Roaming')) / 'Grok Bot/sand-client-persistence'


class GrokBotRecords:
    def __init__(self, root=None):
        self.root = pathlib.Path(root) if root is not None else grok_bot_root()

    def _records(self):
        records = {}
        for path in self.root.glob('*.blob'):
            try:
                key = base64.b32decode(path.stem.upper() + '=' * (-len(path.stem) % 8)).decode('utf-8')
            except (ValueError, UnicodeError):
                continue
            if '.replicas.' in key or key.endswith('.roster.last-roster'):
                records[key] = path
        return records

    def chats(self):
        records = self._records()
        result = []
        for key, path in records.items():
            if not key.endswith('.roster.last-roster'):
                continue
            roster = read_json(path, {}).get('value')
            if isinstance(roster, dict):
                roster = roster.get('rows')
            if not isinstance(roster, list):
                continue
            prefix = key.removesuffix('roster.last-roster')
            for item in roster:
                if not isinstance(item, dict) or not isinstance(item.get('id'), str):
                    continue
                replica = records.get(prefix + 'transcript.replicas.' + item['id'])
                if replica is None:
                    continue
                value = read_json(replica, {}).get('value') or {}
                if not isinstance(value, dict):
                    continue
                result.append(Chat('grok-bot:' + item['id'], str(item.get('title') or item.get('name') or 'Grok Bot'),
                    str(item.get('path') or ''), str(replica), int(replica.stat().st_mtime), provider='grok',
                    source='grok-bot', writable=False))
        return result

    def history(self, chat, limit=60):
        value = read_json(chat.rollout_path, {}).get('value') or {}
        messages = []
        for ordinal, entry in enumerate(value.get('entries', [])):
            kind = entry.get('kind')
            if kind == 'send-message':
                message = entry.get('message') or {}
                if message.get('type') != 'text':
                    continue
                role, text = 'user', message.get('content')
            elif kind == 'message':
                role, text = entry.get('role'), entry.get('content')
            else:
                continue
            if role not in ('user', 'assistant') or not isinstance(text, str) or not text.strip():
                continue
            messages.append(Message(chat.id + ':' + str(entry.get('id') or ordinal), role, text,
                ordinal=ordinal, timestamp_ms=entry.get('timestampMs') or 0))
        return messages[-limit:] if limit else messages
