from __future__ import annotations

import json
import datetime
import os
import pathlib
import shutil
import subprocess
import sys
import re
from functools import lru_cache
from dataclasses import replace

from backend import Chat, DeliveryError, MAX_MESSAGE, Message
from desktop_records import ClaudeDesktopRecords, claude_desktop_roots


def validate_model(model: str) -> str:
    if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/+@\[\]-]{0,199}", model):
        raise ValueError("请选择模型，或填写有效的模型 ID。")
    return model


def default_claude_home() -> pathlib.Path:
    return pathlib.Path(os.environ.get('CLAUDE_CONFIG_DIR') or os.environ.get("CLAUDE_HOME", str(pathlib.Path.home() / ".claude")))


def discover_claude_cli() -> str:
    """Find Claude Code without depending on a global PATH entry."""
    found = shutil.which("claude") or shutil.which("claude.exe")
    if found:
        return found
    candidates: list[pathlib.Path] = []
    if os.name == "nt":
        roots = [
            pathlib.Path(os.environ.get("USERPROFILE", str(pathlib.Path.home()))) / ".vscode" / "extensions",
            pathlib.Path(os.environ.get("USERPROFILE", str(pathlib.Path.home()))) / ".vscode-server" / "extensions",
            pathlib.Path(os.environ.get("LOCALAPPDATA", "")) / "Programs",
        ]
        for root in roots:
            candidates.extend(root.glob("anthropic.claude-code-* /resources/native-binary/claude.exe".replace(" ", "")))
        candidates.extend([
            pathlib.Path(os.environ.get("LOCALAPPDATA", "")) / "Claude Code" / "claude.exe",
            pathlib.Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Claude Code" / "claude.exe",
        ])
    elif sys.platform == "darwin":
        for root in [pathlib.Path.home() / ".vscode" / "extensions", pathlib.Path.home() / ".vscode-server" / "extensions"]:
            candidates.extend(root.glob("anthropic.claude-code-*/resources/native-binary/claude"))
        candidates.extend([
            pathlib.Path("/Applications/Claude Code.app/Contents/MacOS/claude"),
            pathlib.Path("/Applications/Claude.app/Contents/Resources/claude"),
            pathlib.Path.home() / ".local" / "bin" / "claude",
            pathlib.Path("/opt/homebrew/bin/claude"),
            pathlib.Path("/usr/local/bin/claude"),
        ])
    for candidate in candidates:
        if candidate.is_file() and (os.name == "nt" or os.access(candidate, os.X_OK)):
            return str(candidate)
    return ""


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") in ("text", "input_text", "output_text") and isinstance(item.get("text"), str):
            parts.append(item["text"])
    return "\n".join(parts)


def _entry_text(entry: dict) -> str:
    message = entry.get("message")
    if isinstance(message, dict):
        text = _text(message.get("content"))
        desktop_intro = '<system-reminder>\nThis conversation is now continuing in the Claude desktop app (Code tab).'
        if entry.get('type') == 'user' and entry.get('entrypoint') == 'claude-desktop' and text.startswith(desktop_intro):
            end = text.find('</system-reminder>')
            if end != -1:
                text = text[end + len('</system-reminder>'):].lstrip('\r\n')
        origin = entry.get('origin') or {}
        if (entry.get('type') == 'user' and entry.get('entrypoint') == 'claude-desktop' and
                entry.get('promptSource') == 'sdk' and isinstance(origin, dict) and origin.get('kind') == 'human'):
            def context_block(match):
                body = match.group(1).lstrip()
                try:
                    payload, end = json.JSONDecoder().raw_decode(body)
                except ValueError:
                    return match.group(0)
                if (not isinstance(payload, dict) or not isinstance(payload.get('context'), dict) or
                        "The JSON line above is this viewer's live state" not in body[end:]):
                    return match.group(0)
                return ''
            # SDK viewer state is attached to human prompts; retain the text
            # around it and leave quoted examples in fenced code untouched.
            pieces = re.split(r'(?ms)(^```[^\n]*\n.*?^```[^\n]*(?:\n|$)|^~~~[^\n]*\n.*?^~~~[^\n]*(?:\n|$))', text)
            pattern = r'(?ms)^<artifact-view-context\s+artifact="[a-fA-F0-9-]{36}">\s*(.*?)^</artifact-view-context>[ \t]*(?:\r?\n)?'
            cleaned = ''.join(piece if index % 2 else re.sub(pattern, context_block, piece)
                              for index, piece in enumerate(pieces))
            if cleaned != text:
                text = cleaned.strip()
        return text
    return _text(entry.get("content"))


def _prompt_text(text: str) -> str:
    """Return the original task text from a CLI prompt with attachment notes."""
    marker = "\n\n[手机图片附件]\n"
    return text.split(marker, 1)[0].rstrip("\r\n") if marker in text else text.rstrip("\r\n")


def _attachment_ids(text: str) -> set[str]:
    marker = "\n[手机图片附件]\n"
    if marker not in text:
        return set()
    values = set()
    for line in text.split(marker, 1)[1].splitlines():
        value = line.strip()
        if not value.startswith("-"):
            continue
        filename = re.split(r"[\\/]", value[1:].strip())[-1]
        values.add(filename.rsplit(".", 1)[0])
    return values


@lru_cache(maxsize=12)
def _transcript_nodes(path: str, modified: int, size: int):
    nodes, legacy = {}, []
    session_id = ''
    try:
        with pathlib.Path(path).open(encoding='utf-8', errors='replace') as handle:
            for ordinal, line in enumerate(handle):
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict) or entry.get('isSidechain'):
                    continue
                if not entry.get('uuid') and entry.get('type') not in ('user', 'assistant', 'system'):
                    continue
                if entry.get('sessionId'):
                    session_id = session_id or entry['sessionId']
                    if entry['sessionId'] != session_id:
                        continue
                entry['_ordinal'] = ordinal
                if entry.get('uuid'):
                    nodes[entry['uuid']] = entry
                else:
                    legacy.append(entry)
    except OSError:
        return {}, []
    return nodes, legacy


def _parent(entry, nodes):
    parent = entry.get('parentUuid')
    if entry.get('subtype') == 'compact_boundary':
        if parent in nodes:
            return parent
        logical = entry.get('logicalParentUuid')
        if logical in nodes:
            return logical
        # Claude can omit the last tool node while retaining an explicit
        # segment of the original branch in the compaction metadata.
        tail = (entry.get('compactMetadata') or {}).get('preservedSegment', {}).get('tailUuid')
        if tail in nodes and nodes[tail]['_ordinal'] < entry['_ordinal']:
            return tail
    return parent


def internal_message(entry):
    if entry.get('isCompactSummary') is True or entry.get('isMeta') is True:
        return True
    if entry.get('type') != 'user':
        return False
    origin = entry.get('origin') or {}
    if isinstance(origin, dict) and origin.get('kind') == 'task-notification':
        return True
    message = entry.get('message') or {}
    raw = _text(message.get('content')) if isinstance(message, dict) else ''
    return bool(raw.strip() and not _entry_text(entry).strip())


def claude_branch(chat):
    if chat.source == 'claude-desktop' and not chat.history_head:
        return []
    path = pathlib.Path(chat.rollout_path)
    try:
        stat = path.stat()
    except OSError:
        return []
    nodes, legacy = _transcript_nodes(str(path), stat.st_mtime_ns, stat.st_size)
    if not nodes or (legacy and not any('parentUuid' in item for item in nodes.values())):
        return sorted([*nodes.values(), *legacy], key=lambda item: item['_ordinal']) if not chat.history_head else []
    head = chat.history_head or next(reversed(nodes))
    if head not in nodes:
        return []
    chain, visited = [], set()
    while head and head in nodes and head not in visited:
        visited.add(head)
        entry = nodes[head]
        chain.append(entry)
        head = _parent(entry, nodes)
    return list(reversed(chain))


class ClaudeData:
    """Read-only view over Claude Code's project/session JSONL transcripts."""

    def __init__(self, home: pathlib.Path | None = None, desktop_roots=None):
        self.home = pathlib.Path(home or default_claude_home())
        self.projects = self.home / "projects"
        roots = claude_desktop_roots() if self.home == default_claude_home() else []
        self.desktop = ClaudeDesktopRecords(roots if desktop_roots is None else desktop_roots)

    def _files(self) -> list[pathlib.Path]:
        if not self.projects.is_dir():
            return []
        return [path for path in self.projects.glob("*/*.jsonl") if path.is_file() and not path.name.startswith('agent-')]

    def has_sessions(self) -> bool:
        if not self.projects.is_dir():
            return False
        return next(self.projects.rglob("*.jsonl"), None) is not None

    @staticmethod
    def _read(path: pathlib.Path, limit: int | None = None, max_bytes: int | None = None) -> list[dict]:
        entries = []
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                if max_bytes is not None:
                    size = path.stat().st_size
                    handle.seek(max(0, size - max_bytes))
                    if handle.tell():
                        handle.readline()
                for index, line in enumerate(handle):
                    if limit is not None and index >= limit:
                        break
                    try:
                        value = json.loads(line)
                    except (ValueError, UnicodeDecodeError):
                        continue
                    if isinstance(value, dict):
                        entries.append(value)
        except OSError:
            return []
        return entries

    @staticmethod
    def _metadata(path: pathlib.Path) -> tuple[str, str, str, int]:
        session_id = ""
        title = ""
        cwd = ""
        updated = int(path.stat().st_mtime)
        for entry in ClaudeData._read(path, 160):
            session_id = session_id or entry.get("sessionId") or ""
            cwd = cwd or entry.get("cwd") or ""
            if entry.get("type") == "custom-title" and isinstance(entry.get("customTitle"), str):
                title = entry["customTitle"].strip()
            timestamp = entry.get("timestamp")
            if isinstance(timestamp, str):
                try:
                    updated = max(updated, int(datetime.datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()))
                except ValueError:
                    pass
        if not title:
            for entry in ClaudeData._read(path, 240):
                if entry.get("type") == "user":
                    title = _entry_text(entry).strip()
                    if title:
                        break
        session_id = session_id or path.stem
        title = " ".join(title.split())[:180] or path.stem
        return str(session_id), title, cwd, updated

    @staticmethod
    def _last_prompt_head(path: pathlib.Path, session_id: str) -> str:
        """Use Claude's persisted leaf when the desktop session metadata lags."""
        head = ''
        for entry in ClaudeData._read(path, max_bytes=256 * 1024):
            if entry.get('type') == 'last-prompt' and entry.get('sessionId') in ('', session_id):
                leaf = entry.get('leafUuid')
                if isinstance(leaf, str) and leaf:
                    head = leaf
        return head

    @staticmethod
    def _desktop_head(path, selected, leaf):
        if not selected or not leaf or selected == leaf:
            return selected
        stat = path.stat()
        nodes, _ = _transcript_nodes(str(path), stat.st_mtime_ns, stat.st_size)
        entry = nodes.get(leaf, {})
        if entry.get('entrypoint') not in (None, 'claude-desktop'):
            return selected
        cursor, visited = leaf, set()
        while cursor and cursor in nodes and cursor not in visited:
            if cursor == selected:
                return leaf
            visited.add(cursor)
            cursor = _parent(nodes[cursor], nodes)
        return selected

    def chats(self, archived: bool = False) -> list[Chat]:
        result = {}
        desktop = self.desktop.sessions()
        for path in self._files():
            session_id, title, cwd, updated = self._metadata(path)
            if not cwd:
                cwd = str(path.parent)
            public_id = f"claude:{session_id}"
            views = desktop.get(session_id, [])
            view = views[0] if len(views) == 1 else {}
            source = 'claude-desktop' if views else 'claude-cli'
            desktop_head = self._last_prompt_head(path, session_id) if views else ''
            chat = Chat(public_id, view.get('title') or title, view.get('cwd') or cwd, str(path), updated,
                        provider='claude', source=source,
                        history_head=self._desktop_head(path, view.get('lastAssistantUuid') or '', desktop_head) if views else '',
                        writable=not views or bool(view.get('sessionId')))
            # Listing must stay cheap: the full parent chain is resolved only
            # when a specific chat is opened. The tail still gives us a stable
            # head for stale-send detection without reading a large transcript.
            tail = self._read(path, max_bytes=256 * 1024) if not views else []
            tail = [entry for entry in tail if not entry.get('sessionId') or entry.get('sessionId') == session_id]
            has_parent_chain = any(entry.get('uuid') and entry.get('parentUuid') for entry in tail)
            head = next((entry.get('uuid') for entry in reversed(tail)
                         if entry.get('uuid') and entry.get('type') in ('user', 'assistant', 'system')
                         and not entry.get('isSidechain')), '') if has_parent_chain else ''
            branch = tail
            if not views and head:
                chat = replace(chat, history_head=head)
            model = ""
            for entry in tail:
                if entry.get("type") == "assistant" and not entry.get("isSidechain"):
                    reported = (entry.get("message") or {}).get("model")
                    if isinstance(reported, str) and reported and not reported.startswith("<"):
                        model = reported
            chat = replace(chat, model=view.get('model') or model)
            if len(views) > 1:
                for view in views:
                    native = replace(chat, id='claude-desktop:' + view['sessionId'], title=view.get('title') or title,
                        history_head=self._desktop_head(path, view.get('lastAssistantUuid') or '', desktop_head), model=view.get('model') or '',
                        writable=True)
                    result[native.id] = native
                continue
            previous = result.get(public_id)
            if previous is None or chat.updated_at > previous.updated_at:
                result[public_id] = chat
        return sorted(result.values(), key=lambda chat: (chat.pinned, chat.updated_at), reverse=True)

    def history(self, chat: Chat, limit: int = 60) -> list[Message]:
        path = pathlib.Path(chat.rollout_path)
        if not path.is_file():
            return []
        messages = []
        for entry in claude_branch(chat):
            ordinal = entry['_ordinal']
            role = entry.get("type")
            if role not in ("user", "assistant") or internal_message(entry):
                continue
            text = _entry_text(entry)
            # Tool results and empty assistant tool calls are not chat bubbles.
            if not text:
                continue
            message = entry.get("message") if isinstance(entry.get("message"), dict) else entry
            message_id = entry.get("uuid") or (message or {}).get("id") or f"{chat.id}:{ordinal}"
            phase = entry.get("phase") or ""
            model = message.get("model", "") if role == "assistant" else ""
            messages.append(Message(str(message_id), role, text, ordinal=ordinal, phase=phase,
                                    model=model if isinstance(model, str) else ""))
        return messages[-limit:]

    @staticmethod
    def receipt_message(receipt: dict, messages: list[Message]) -> Message | None:
        if receipt.get("message_id"):
            return next((message for message in messages if message.id == receipt["message_id"]), None)
        baseline = set(receipt.get("baseline_ids", []))
        text = _prompt_text(str(receipt.get("text", "")))
        expected_images = {str(item.get("id")) for item in receipt.get("images", []) if item.get("id")}
        for message in messages:
            if message.role != "user" or message.id in baseline:
                continue
            candidate = _prompt_text(message.text)
            if text and candidate == text:
                return message
            if not text and expected_images and expected_images <= _attachment_ids(message.text):
                return message
        return None

    def queue_ids(self, thread_id: str, text: str | None = None) -> set[str]:
        return set()

    def turn(self, thread_id: str, turn_id: str = "") -> dict:
        return {}

    def receipt_status(self, receipt: dict, messages: list[Message], queue_ids: set[str]) -> str:
        match = self.receipt_message(receipt, messages)
        if not match:
            return "投递结果待确认" if receipt.get("result") == "uncertain" else receipt.get("status", "正在投递")
        if any(message.role == "assistant" and message.ordinal > match.ordinal for message in messages):
            return "已完成"
        return "已接收"

    def historical_receipt_message(self, receipt: dict):
        chat = next((chat for chat in self.chats() if chat.id == receipt['thread_id']), None)
        return self.receipt_message(receipt, self.history(chat, limit=0)) if chat else None


def _claude_prompt(text: str, image_paths: list[pathlib.Path]) -> str:
    if not image_paths:
        return text
    lines = [text, "", "[手机图片附件]", "请使用 Claude Code 的图片读取能力查看以下附件，再完成上面的任务："]
    lines.extend(f"- {path}" for path in image_paths)
    return "\n".join(lines)


def deliver_claude(cli: str, chat: Chat, text: str, image_paths: list[pathlib.Path], request_id: str,
                   permission_mode: str = "bypassPermissions", run_dir: pathlib.Path | None = None,
                   model: str = "", home: pathlib.Path | None = None) -> dict:
    model = validate_model(model)
    if not chat.id.startswith('claude:'):
        raise ValueError('请选择可发送的 Claude Code CLI 聊天。')
    session_id = chat.id.split(':', 1)[1]
    if not chat.writable or chat.source == 'claude-desktop' or session_id in ClaudeDesktopRecords(claude_desktop_roots()).sessions():
        raise ValueError('这是 Claude 桌面管理的聊天。当前仅可查看同一分支；请在 Claude 桌面发送，避免生成另一条记录。')
    if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", request_id):
        raise ValueError("消息编号无效。")
    if not text.strip() and not image_paths:
        raise ValueError("请输入要发送的消息或选择图片。")
    if len(text) > MAX_MESSAGE:
        raise ValueError(f"单条消息最多 {MAX_MESSAGE:,} 字，请拆分后发送。")
    if not cli or not pathlib.Path(cli).is_file():
        raise FileNotFoundError("找不到 Claude Code CLI。请安装 Claude Code 扩展，或在 config.json 设置 claude_cli。")
    if not pathlib.Path(chat.cwd).is_dir():
        raise FileNotFoundError(f"聊天的项目目录不存在：{chat.cwd}")
    home = pathlib.Path(home or default_claude_home())
    path = pathlib.Path(chat.rollout_path)
    if path.is_file():
        if not path.resolve().is_relative_to((home / 'projects').resolve()) or ClaudeData._metadata(path)[0] != session_id:
            raise ValueError('Claude 会话记录与配置目录不一致，请重新选择聊天。本次未发送。')
    prompt = _claude_prompt(text, image_paths)
    session_id = chat.id.split(":", 1)[1] if chat.id.startswith("claude:") else chat.id
    command = [cli, "--resume", session_id, "--print", "--model", model, "--output-format", "json", prompt]
    if chat.history_head:
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', chat.history_head):
            raise ValueError('聊天的消息链标识无效，请刷新。')
        current = ClaudeData._read(pathlib.Path(chat.rollout_path), max_bytes=1024 * 1024)
        latest = next((entry.get('uuid') for entry in reversed(current)
                       if entry.get('type') in ('user', 'assistant', 'system') and entry.get('uuid') and not entry.get('isSidechain')), '')
        if latest != chat.history_head:
            raise ValueError('聊天记录已变化，请刷新后再发送。本次未发送。')
        command[4:4] = ['--resume-session-at', chat.history_head]
    if permission_mode:
        command[4:4] = ["--permission-mode", permission_mode]
        if permission_mode == "bypassPermissions":
            command[4:4] = ["--dangerously-skip-permissions"]
    if image_paths:
        command[4:4] = ["--add-dir", str(image_paths[0].parent)]
    if run_dir is None:
        run_dir = pathlib.Path.home() / ".codexrelay-claude-runs"
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / f"{request_id}.log"
    try:
        log = log_path.open("ab")
        kwargs = {"cwd": chat.cwd, "stdin": subprocess.DEVNULL, "stdout": log, "stderr": subprocess.STDOUT,
                  "shell": False, "close_fds": os.name != "nt", 'env': dict(os.environ, CLAUDE_CONFIG_DIR=str(home))}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
        else:
            kwargs["start_new_session"] = True
        process = subprocess.Popen(command, **kwargs)
        log.close()
    except OSError as error:
        raise DeliveryError(f"无法启动 Claude Code CLI：{error}") from error
    return {"warning": False, "queue_ids": [], "delivery_mode": "claude-cli", "process_id": process.pid,
            "requested_model": model}
