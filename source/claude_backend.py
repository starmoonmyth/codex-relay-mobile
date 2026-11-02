from __future__ import annotations

import json
import datetime
import os
import pathlib
import shutil
import subprocess
import sys
import re

from backend import Chat, DeliveryError, MAX_MESSAGE, Message


def validate_model(model: str) -> str:
    if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/+@\[\]-]{0,199}", model):
        raise ValueError("请选择 Claude 模型，或填写有效的模型 ID。")
    return model


def default_claude_home() -> pathlib.Path:
    return pathlib.Path(os.environ.get("CLAUDE_HOME", str(pathlib.Path.home() / ".claude")))


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
        return _text(message.get("content"))
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


class ClaudeData:
    """Read-only view over Claude Code's project/session JSONL transcripts."""

    def __init__(self, home: pathlib.Path | None = None):
        self.home = pathlib.Path(home or default_claude_home())
        self.projects = self.home / "projects"

    def _files(self) -> list[pathlib.Path]:
        if not self.projects.is_dir():
            return []
        return [path for path in self.projects.rglob("*.jsonl") if path.is_file()]

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
        session_id = path.stem
        title = ""
        cwd = ""
        updated = int(path.stat().st_mtime)
        for entry in ClaudeData._read(path, 160):
            session_id = entry.get("sessionId") or session_id
            cwd = entry.get("cwd") or cwd
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
        title = " ".join(title.split())[:180] or path.stem
        return str(session_id), title, cwd, updated

    def chats(self, archived: bool = False) -> list[Chat]:
        result = {}
        for path in self._files():
            session_id, title, cwd, updated = self._metadata(path)
            if not cwd:
                cwd = str(path.parent)
            public_id = f"claude:{session_id}"
            model = ""
            for entry in self._read(path, max_bytes=4 * 1024 * 1024):
                if entry.get("type") == "assistant" and not entry.get("isSidechain"):
                    reported = (entry.get("message") or {}).get("model")
                    if isinstance(reported, str) and reported and not reported.startswith("<"):
                        model = reported
            chat = Chat(public_id, title, cwd, str(path), updated, model=model, provider="claude")
            previous = result.get(public_id)
            if previous is None or chat.updated_at > previous.updated_at:
                result[public_id] = chat
        return sorted(result.values(), key=lambda chat: (chat.pinned, chat.updated_at), reverse=True)

    def history(self, chat: Chat, limit: int = 60) -> list[Message]:
        path = pathlib.Path(chat.rollout_path)
        if not path.is_file():
            return []
        messages = []
        for ordinal, entry in enumerate(self._read(path, max_bytes=4 * 1024 * 1024)):
            role = entry.get("type")
            if role not in ("user", "assistant"):
                continue
            text = _entry_text(entry)
            # Tool results and empty assistant tool calls are not chat bubbles.
            if not text:
                continue
            message = entry.get("message") if isinstance(entry.get("message"), dict) else entry
            message_id = (message or {}).get("id") or entry.get("uuid") or f"{chat.id}:{ordinal}"
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

    def historical_receipt_message(self, receipt: dict):
        return None

    def receipt_status(self, receipt: dict, messages: list[Message], queue_ids: set[str]) -> str:
        match = self.receipt_message(receipt, messages)
        if not match:
            return "投递结果待确认" if receipt.get("result") == "uncertain" else receipt.get("status", "正在投递")
        if any(message.role == "assistant" and message.ordinal > match.ordinal for message in messages):
            return "已完成"
        return "已接收"


def _claude_prompt(text: str, image_paths: list[pathlib.Path]) -> str:
    if not image_paths:
        return text
    lines = [text, "", "[手机图片附件]", "请使用 Claude Code 的图片读取能力查看以下附件，再完成上面的任务："]
    lines.extend(f"- {path}" for path in image_paths)
    return "\n".join(lines)


def deliver_claude(cli: str, chat: Chat, text: str, image_paths: list[pathlib.Path], request_id: str,
                   permission_mode: str = "bypassPermissions", run_dir: pathlib.Path | None = None,
                   model: str = "") -> dict:
    model = validate_model(model)
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
    prompt = _claude_prompt(text, image_paths)
    session_id = chat.id.split(":", 1)[1] if chat.id.startswith("claude:") else chat.id
    command = [cli, "--resume", session_id, "--print", "--model", model, "--output-format", "json", prompt]
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
                  "shell": False, "close_fds": os.name != "nt"}
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
