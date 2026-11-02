from __future__ import annotations

import contextlib
import base64
import hashlib
import json
import os
import pathlib
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass

MAX_MESSAGE = 12000
APP_NAME = "CodexRelay"


def clean_path(value: str) -> str:
    if value.startswith("\\\\?\\UNC\\"):
        return "\\\\" + value[8:]
    return value[4:] if value.startswith("\\\\?\\") else value


def default_home() -> pathlib.Path:
    return pathlib.Path(os.environ.get("CODEX_HOME", str(pathlib.Path.home() / ".codex")))


def discover_cli() -> str:
    if sys.platform == "darwin":
        applications = [pathlib.Path("/Applications"), pathlib.Path.home() / "Applications"]
        relative_paths = ["ChatGPT.app/Contents/Resources/codex-cli/bin/codex",
                          "Codex.app/Contents/Resources/codex-cli/bin/codex",
                          "Codex.app/Contents/Resources/codex"]
        candidates = [root / relative for root in applications for relative in relative_paths]
        candidates += [pathlib.Path("/opt/homebrew/bin/codex"), pathlib.Path("/usr/local/bin/codex")]
        for candidate in candidates:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
        return shutil.which("codex") or ""
    root = pathlib.Path(os.environ.get("LOCALAPPDATA", "")) / "OpenAI" / "Codex" / "bin"
    candidates = list(root.glob("*/codex.exe")) if root.is_dir() else []
    if candidates:
        return str(max(candidates, key=lambda p: p.stat().st_mtime))
    found = shutil.which("codex.exe")
    return found or ""


def state_dir() -> pathlib.Path:
    if sys.platform == "darwin":
        return pathlib.Path.home() / "Library" / "Application Support" / APP_NAME
    return pathlib.Path(os.environ.get("LOCALAPPDATA", str(pathlib.Path.home()))) / APP_NAME


def valid_cli(path: str) -> bool:
    candidate = pathlib.Path(path)
    return candidate.is_file() and (candidate.suffix.lower() == ".exe" if os.name == "nt" else os.access(candidate, os.X_OK))


class LocalStore:
    def __init__(self, root: pathlib.Path | None = None):
        self.root = root or state_dir()
        self.path = self.root / "preferences.json"
        self.data = {"settings": {}, "drafts": {}, "outbox": []}
        if self.path.exists():
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError("本地设置格式无效。请移走 preferences.json 后重新打开。")
            for key, kind in [("settings", dict), ("drafts", dict), ("outbox", list)]:
                if isinstance(loaded.get(key), kind):
                    self.data[key] = loaded[key]

    def save(self):
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.path)


@dataclass(frozen=True)
class Chat:
    id: str
    title: str
    cwd: str
    rollout_path: str
    updated_at: int
    model: str = ""
    archived: bool = False
    pinned: bool = False
    provider: str = "codex"
    source: str = ""
    history_head: str = ""
    writable: bool = True


@dataclass(frozen=True)
class Message:
    id: str
    role: str
    text: str
    turn_id: str = ""
    ordinal: int = 0
    phase: str = ""
    images: tuple[str, ...] = ()
    questions: tuple[dict, ...] = ()
    client_id: str = ''
    model: str = ''
    timestamp_ms: int = 0


def user_images(item: dict) -> tuple[str, ...]:
    result = []
    for part in item.get("content", []):
        if not isinstance(part, dict) or part.get("type") not in ("image", "localImage", "input_image"):
            continue
        value = part.get("url") or part.get("path") or part.get("image_url")
        if isinstance(value, dict):
            value = value.get("url")
        if isinstance(value, str):
            result.append(value)
    return tuple(result)


def user_text(item: dict) -> str:
    return "\n".join(part.get("text", "") for part in item.get("content", [])
                     if isinstance(part, dict) and part.get("type") in ("text", "input_text"))


def display_user_text(text: str) -> str:
    match = re.fullmatch(r"\s*<send_user_message_question_reply>\s*(.*?)\s*</send_user_message_question_reply>\s*", text, re.S)
    if match:
        try:
            answers = json.loads(match.group(1))
            if isinstance(answers, list):
                return "\n\n".join(str(item["answer"]) for item in answers if isinstance(item, dict) and "answer" in item)
        except (ValueError, TypeError):
            pass
    for tag in ["environment_context", "external_codex_apps_open_page", "in-app-browser-context"]:
        text = re.sub(r"^\s*<" + tag + r"(?:\s[^>]*)?>.*?</" + tag + r">\s*", "", text, count=1, flags=re.S)
    if text.startswith("## My request:"):
        text = text[len("## My request:"):].lstrip()
    return text


class CodexData:
    def __init__(self, home: pathlib.Path):
        self.home = home

    def database(self, prefix: str) -> pathlib.Path | None:
        paths = list(self.home.glob(prefix + "_*.sqlite"))
        paths = [p for p in paths if re.fullmatch(re.escape(prefix) + r"_\d+\.sqlite", p.name)]
        return max(paths, key=lambda p: int(p.stem.rsplit("_", 1)[1]), default=None)

    @contextlib.contextmanager
    def connect(self, prefix: str):
        path = self.database(prefix)
        if path is None:
            raise FileNotFoundError(f"在 {self.home} 中找不到 {prefix} 数据库。")
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.5)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only=ON")
            yield connection
        finally:
            connection.close()

    def chats(self, archived: bool = False) -> list[Chat]:
        with self.connect("state") as c:
            columns = {r[1] for r in c.execute("PRAGMA table_info(threads)")}
            fields = ["id", "title", "cwd", "rollout_path", "updated_at", "archived"]
            fields += [name for name in ["name", "model", "is_pinned"] if name in columns]
            where = "1=1" if archived else "archived=0"
            if "agent_role" in columns:
                where += " AND agent_role IS NULL"
            if "thread_source" in columns:
                where += " AND (thread_source IS NULL OR thread_source='user')"
            order = ("is_pinned DESC, " if "is_pinned" in columns else "") + "updated_at DESC"
            rows = c.execute(f"SELECT {','.join(fields)} FROM threads WHERE {where} ORDER BY {order}").fetchall()
        result = []
        for raw in rows:
            row = dict(raw)
            title = row.get("name") or row["title"] or "未命名聊天"
            if "## My request:" in title:
                title = title.split("## My request:", 1)[1]
            title = " ".join(title.split())
            result.append(Chat(row["id"], title, clean_path(row["cwd"]),
                               clean_path(row["rollout_path"]), row["updated_at"],
                               row.get("model") or "", bool(row["archived"]), bool(row.get("is_pinned"))))
        return result

    def history(self, chat: Chat, limit: int = 60) -> list[Message]:
        if self.database("thread_history"):
            with self.connect("thread_history") as c:
                rows = c.execute(
                    "SELECT item_id,item_json,turn_id,rollout_ordinal FROM thread_items "
                    "WHERE thread_id=? AND item_type IN ('userMessage','agentMessage') "
                    "ORDER BY rollout_ordinal DESC LIMIT ?", (chat.id, limit)).fetchall()
            messages = []
            for row in reversed(rows):
                item = json.loads(row["item_json"])
                role = "user" if item.get("type") == "userMessage" else "assistant"
                text = user_text(item) if role == "user" else item.get("text", "")
                images = user_images(item) if role == "user" else ()
                if text or images or item.get('questions'):
                    messages.append(Message(row["item_id"], role, text, row["turn_id"],
                                            row["rollout_ordinal"], item.get("phase") or "", images,
                                            tuple(item.get("questions") or ()) if item.get("delivery") == "async" else (),
                                            item.get('clientId') or ''))
            if messages:
                return messages
        return self._rollout_history(chat, limit)

    def _rollout_history(self, chat: Chat, limit: int) -> list[Message]:
        path = pathlib.Path(chat.rollout_path)
        if not path.is_file():
            return []
        messages = []
        with path.open("rb") as f:
            offset = max(0, path.stat().st_size - 4 * 1024 * 1024)
            f.seek(offset)
            if offset:
                f.readline()
            for line in f:
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    continue
                item = event.get("payload", {})
                if event.get("type") != "response_item" or item.get("type") != "message":
                    continue
                role = item.get("role")
                if role not in ("user", "assistant"):
                    continue
                text = "\n".join(x.get("text", "") for x in item.get("content", [])
                                 if x.get("type") in ("input_text", "output_text", "text"))
                images = user_images(item) if role == "user" else ()
                if text or images:
                    messages.append(Message(item.get("id") or str(event.get("ordinal", len(messages))),
                                            role, text, ordinal=event.get("ordinal", 0),
                                            phase=item.get("phase") or "", images=images))
        return messages[-limit:]

    def queue_ids(self, thread_id: str, text: str | None = None) -> set[str]:
        if not self.database("queue"):
            return set()
        with self.connect("queue") as c:
            rows = c.execute("SELECT id,payload_json FROM queued_items WHERE thread_id=?", (thread_id,)).fetchall()
        def contains(value):
            if isinstance(value, str):
                return value == text
            if isinstance(value, dict):
                return any(contains(v) for v in value.values())
            if isinstance(value, list):
                return any(contains(v) for v in value)
            return False
        return {row[0] for row in rows if text is None or contains(json.loads(row[1]))}

    def turn(self, thread_id: str, turn_id: str = "") -> dict:
        if not self.database("thread_history"):
            return {}
        with self.connect("thread_history") as c:
            if turn_id:
                row = c.execute("SELECT status,error_json FROM thread_turns WHERE thread_id=? AND turn_id=?",
                                (thread_id, turn_id)).fetchone()
            else:
                row = c.execute("SELECT status,error_json FROM thread_turns WHERE thread_id=? "
                                "ORDER BY rollout_ordinal DESC LIMIT 1", (thread_id,)).fetchone()
        return dict(row) if row else {}

    @staticmethod
    def receipt_message(receipt: dict, messages: list[Message]) -> Message | None:
        if receipt.get('message_id'):
            return next((message for message in messages if message.id == receipt['message_id']), None)
        expected = receipt.get("images", [])
        for message in messages:
            if message.role != "user" or message.id in receipt.get("baseline_ids", []):
                continue
            if message.id == receipt["id"] or message.client_id == receipt['id']:
                return message
            from notifications import question_answers
            answers = question_answers(receipt['text'])
            if answers and question_answers(message.text) == answers:
                return message
            if message.text.rstrip('\r\n') != receipt["text"].rstrip('\r\n') or len(message.images) != len(expected):
                continue
            matches = True
            for source, image in zip(message.images, expected):
                if source.startswith("data:image/") and ";base64," in source:
                    try:
                        matches = hashlib.sha256(base64.b64decode(source.split(",", 1)[1], validate=True)).hexdigest() == image["sha256"]
                    except ValueError:
                        matches = False
                else:
                    matches = pathlib.Path(source).stem == image["id"]
                if not matches:
                    break
            if matches:
                return message
        return None

    def historical_receipt_message(self, receipt):
        if (receipt.get('seen_in_history') and receipt.get('turn_id')) or not self.database('thread_history'):
            return None
        with self.connect('thread_history') as c:
            baseline = receipt.get('baseline_ids', [])
            ordinal = -1
            if baseline:
                placeholders = ','.join('?' for _ in baseline)
                ordinal = c.execute(f'SELECT COALESCE(MAX(rollout_ordinal),-1) FROM thread_items WHERE thread_id=? AND item_id IN ({placeholders})',
                                    (receipt['thread_id'], *baseline)).fetchone()[0]
            rows = c.execute("SELECT item_id,item_json,turn_id FROM thread_items WHERE thread_id=? AND item_type='userMessage' AND rollout_ordinal>? ORDER BY rollout_ordinal",
                             (receipt['thread_id'], ordinal))
            for row in rows:
                item = json.loads(row['item_json'])
                message = Message(row['item_id'], 'user', user_text(item), row['turn_id'],
                                  images=user_images(item), client_id=item.get('clientId') or '')
                match = self.receipt_message(receipt, [message])
                if match:
                    return match
        return None

    def receipt_status(self, receipt: dict, messages: list[Message], queue_ids: set[str]) -> str:
        match = self.receipt_message(receipt, messages)
        if match or receipt.get('seen_in_history'):
            turn_id = match.turn_id if match else receipt.get('turn_id')
            turn = self.turn(receipt["thread_id"], turn_id) if turn_id else {}
            return {"inProgress": "处理中", "completed": "已完成", "failed": "执行失败",
                    "interrupted": "已中断"}.get(turn.get("status"), "已接收")
        if set(receipt.get("queue_ids", [])) & queue_ids:
            return "等待接收"
        if receipt.get("result") == "uncertain":
            return "投递结果待确认"
        if receipt.get("result") == "failed":
            return "投递失败"
        if receipt.get('delivery_mode') == 'immediate':
            return '桌面已接收 · 已立即提交'
        return "已入队 · 等待回执"


class DeliveryError(Exception):
    def __init__(self, text: str, uncertain: bool = False):
        super().__init__(text)
        self.uncertain = uncertain


def build_command(cli: str, chat: Chat, text: str) -> list[str]:
    if not text.strip():
        raise ValueError("请输入要发送的消息。")
    if len(text) > MAX_MESSAGE:
        raise ValueError(f"单条消息最多 {MAX_MESSAGE:,} 字，请拆分后发送。")
    if not pathlib.Path(cli).is_file():
        raise FileNotFoundError("找不到 Codex CLI，请在设置中重新选择 Codex 程序。")
    if os.name != "nt" and not os.access(cli, os.X_OK):
        raise PermissionError("所选 Codex 程序没有执行权限，请选择应用内自带的 codex。")
    if not pathlib.Path(chat.cwd).is_dir():
        raise FileNotFoundError(f"聊天的项目目录不存在：{chat.cwd}")
    command = [cli, "queue", "--thread", chat.id, "--message", text, "-C", chat.cwd]
    if os.name == "nt" and len(subprocess.list2cmdline(command)) > 30000:
        raise ValueError("消息超过 Windows 命令行长度限制，请缩短或拆分后发送。")
    return command


def deliver(cli: str, home: pathlib.Path, chat: Chat, text: str, runner=None) -> dict:
    command = build_command(cli, chat, text)
    env = dict(os.environ)
    env["CODEX_HOME"] = str(home)
    env["NO_COLOR"] = "1"
    try:
        result = (runner or subprocess.run)(
            command, shell=False, capture_output=True, encoding="utf-8", errors="replace",
            timeout=40, env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired as error:
        raise DeliveryError("投递超时，消息可能已经入队。请先查看原聊天和队列状态，再决定是否重发。", True) from error
    except OSError as error:
        raise DeliveryError(f"无法启动 Codex CLI：{error}") from error
    output = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", (result.stdout + "\n" + result.stderr).strip())
    if result.returncode:
        raise DeliveryError(f"Codex 返回错误（{result.returncode}）：\n{output[-5000:]}", True)
    return {"output": output[-5000:], "warning": bool(re.search(r"warn|failed|error", output, re.I))}


def new_receipt(chat: Chat, text: str, messages: list[Message]) -> dict:
    return {"id": str(uuid.uuid4()), "thread_id": chat.id, "title": chat.title, "text": text,
            "sent_at_ms": int(time.time() * 1000), "baseline_ids": [m.id for m in messages if m.role == "user"],
            "queue_ids": [], "status": "正在投递", "result": "uncertain"}
