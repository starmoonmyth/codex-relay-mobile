from __future__ import annotations

import contextlib
import base64
import dataclasses
import hashlib
import html
import http.cookies
import io
import json
import os
import pathlib
import re
import secrets
import socket
import sqlite3
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from backend import (CodexData, DeliveryError, MAX_MESSAGE, default_home, deliver,
                     discover_cli, display_user_text, new_receipt, state_dir)
from claude_backend import (ClaudeData, default_claude_home, deliver_claude,
                            discover_claude_cli, validate_model)
from image_delivery import deliver_images
from notifications import Notifications, question_answers
from app_bridge import deliver_answer, desktop_model
from codex_models import list_models
from grok_backend import GrokData, default_grok_home, discover_grok_cli, deliver_grok, read_json, run_status
from usage import Usage
from activity import Activities
from plan_usage import Plans
from claude_desktop import deliver_desktop
from updates import Updates
from version import APP_VERSION

STATIC = pathlib.Path(__file__).resolve().parent / "web"
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_ATTACHMENT_BYTES = 32 * 1024 * 1024
IMAGE_MIMES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}
IMAGE_SUFFIX = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}


def mobile_state():
    return state_dir() / "Mobile"


class Relay:
    def __init__(self, root=None, config=None, sender=deliver, image_sender=deliver_images,
                 answer_sender=deliver_answer, claude_sender=deliver_claude,
                 model_bridge=desktop_model, model_catalog=list_models, grok_sender=deliver_grok):
        self.root = pathlib.Path(root or mobile_state())
        self.root.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            self.root.chmod(0o700)
        config_path = self.root / "config.json"
        self.config_path = config_path
        self.config = config if config is not None else json.loads(config_path.read_text("utf-8"))
        self.port = int(self.config.get("port", 8879))
        self.origin = self.config["origin"].rstrip("/")
        self.local_origin = f"http://127.0.0.1:{self.port}"
        self.origins = {self.origin, self.local_origin}
        self.hosts = {urllib.parse.urlsplit(origin).netloc for origin in self.origins}
        self.data = CodexData(pathlib.Path(self.config.get("codex_home") or default_home()))
        self.cli = self.config.get("cli") or discover_cli()
        self.claude = ClaudeData(pathlib.Path(self.config.get("claude_home") or default_claude_home()))
        self.claude_cli = self.config.get("claude_cli") or discover_claude_cli()
        self.grok = GrokData(self.config.get('grok_home') or default_grok_home())
        self.grok_cli = self.config.get('grok_cli') or discover_grok_cli()
        self.grok_sender = grok_sender
        self.usage = Usage()
        self.activities = Activities()
        self.plans = Plans()
        self.provider_mode = self.config.get("provider", "codex")
        if self.provider_mode not in ("codex", "claude", "grok", "all"):
            self.provider_mode = "codex"
        self.sender = sender
        self.image_sender = image_sender
        self.answer_sender = answer_sender
        self.claude_sender = claude_sender
        self.model_bridge = model_bridge
        self.model_catalog = model_catalog
        self.model_lock = threading.Lock()
        self.codex_model_cache = {}
        self.codex_catalog_cache = (0, [])
        self.model_refreshing = set()
        self.model_refresh_attempts = {}
        self.lock = threading.Lock()
        self.answer_lock = threading.Lock()
        self.attempts = {}
        self.db = self.root / "mobile.sqlite"
        token_file = self.root / "pairing-token"
        if not token_file.exists():
            with token_file.open("x", encoding="ascii") as handle:
                handle.write(secrets.token_urlsafe(32))
            if os.name != "nt":
                token_file.chmod(0o600)
        self.token = token_file.read_text("ascii").strip()
        with self.connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (hash TEXT PRIMARY KEY, expires INTEGER);
                CREATE TABLE IF NOT EXISTS deliveries (
                    id TEXT PRIMARY KEY, thread_id TEXT, text TEXT, receipt TEXT, created INTEGER
                );
                CREATE TABLE IF NOT EXISTS uploads (
                    id TEXT PRIMARY KEY, name TEXT, mime TEXT, size INTEGER,
                    width INTEGER, height INTEGER, sha256 TEXT, created INTEGER
                );
            """)
        if os.name != "nt":
            self.db.chmod(0o600)
        # An interrupted CLI call may already have queued its message.
        with self.connect() as connection:
            for row in connection.execute("SELECT id,receipt FROM deliveries"):
                receipt = json.loads(row[1])
                if receipt.get("status") == "正在投递":
                    receipt.update(status="投递结果待确认", result="uncertain")
                    self.save_receipt(connection, receipt)
        self.notifications = Notifications(self)
        self.updates = Updates(self.root)

    @contextlib.contextmanager
    def connect(self):
        connection = sqlite3.connect(self.db, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def session(self, value):
        with self.connect() as connection:
            return bool(connection.execute("SELECT 1 FROM sessions WHERE hash=? AND expires>?",
                        (hashlib.sha256(value.encode()).hexdigest(), time.time())).fetchone())

    def pair(self, token, client):
        now = time.time()
        with self.lock:
            times = [stamp for stamp in self.attempts.get(client, []) if stamp > now - 60]
            if len(times) >= 10:
                raise ValueError("尝试次数过多，请一分钟后重试。")
            times.append(now)
            self.attempts[client] = times
            if not isinstance(token, str) or not secrets.compare_digest(token, self.token):
                raise ValueError("配对码不正确。请在电脑上重新打开手机连接页。")
            self.attempts.pop(client, None)
        value = secrets.token_urlsafe(32)
        with self.connect() as connection:
            connection.execute("DELETE FROM sessions WHERE expires<=?", (now,))
            connection.execute("INSERT INTO sessions VALUES (?,?)",
                               (hashlib.sha256(value.encode()).hexdigest(), now + 30 * 86400))
        return value

    def chat(self, identity):
        return next((chat for chat in self.chats() if chat.id == identity), None)

    def chats(self):
        chats = []
        if self.provider_mode in ("codex", "all"):
            chats.extend(self.data.chats())
        if self.provider_mode in ("claude", "all"):
            chats.extend(self.claude.chats())
        if self.provider_mode in ('grok', 'all'):
            chats.extend(self.grok.chats())
        return sorted(chats, key=lambda chat: (chat.pinned, chat.updated_at), reverse=True)

    def data_for(self, chat):
        return self.grok if chat.provider == 'grok' else self.claude if chat.provider == "claude" else self.data

    @staticmethod
    def record_key(chat):
        source = [chat.provider, chat.source, chat.id, os.path.normcase(str(pathlib.Path(chat.rollout_path).resolve()))]
        return hashlib.sha256(json.dumps(source).encode()).hexdigest()

    def conversation_info(self, chat):
        labels = {'claude-desktop': 'Claude 桌面 · 当前分支', 'claude-cli': 'Claude Code CLI',
                  'grok-bot': 'Grok Bot 桌面 · 本地缓存', 'grok-cli': 'Grok CLI'}
        detail = ''
        if chat.source == 'claude-desktop':
            detail = '通过 Claude 原生输入框发送到此聊天。电脑需解锁并打开 Claude；发送时会切换窗口，已有草稿会阻止代发。当前支持文字。'
        elif chat.source == 'grok-bot':
            detail = '显示 Grok Bot 保存到此电脑的聊天缓存，可能只包含最近部分记录。请在 Grok Bot 中发送。'
        return {'source': chat.source, 'label': labels.get(chat.source, 'Codex'), 'writable': chat.writable,
                'detail': detail, 'record_key': self.record_key(chat), 'history_head': chat.history_head}

    def set_provider(self, provider):
        if provider not in ("codex", "claude", "grok", "all"):
            raise ValueError("provider 必须是 codex、claude、grok 或 all。")
        with self.lock:
            self.provider_mode = provider
            self.config["provider"] = provider
            self.save_config()
        return self.provider_info()

    def save_config(self):
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.config_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.config, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.config_path)

    def claude_model_info(self, chat):
        if chat.source == 'claude-desktop':
            return {'selected': '', 'requested_model': '', 'automatic_model': chat.model,
                    'automatic_source': 'desktop', 'last_response_model': chat.model,
                    'choices': [], 'native_desktop': True}
        selected = self.config.get("claude_chat_models", {}).get(chat.id, "")
        default = self.config.get("claude_model") or ""
        automatic = default or chat.model
        choices = [chat.model, default, selected, "opus", "sonnet", "haiku"]
        choices.extend(self.config.get("claude_models") or [])
        return {"selected": selected, "requested_model": selected or automatic,
                "automatic_model": automatic, "automatic_source": "default" if default else "history",
                "last_response_model": chat.model,
                "choices": list(dict.fromkeys(item for item in choices if isinstance(item, str) and item))}

    def codex_model_info(self, chat, refresh=False):
        warning = ''
        if refresh:
            with self.model_lock:
                try:
                    model = self.model_bridge(chat, self.data.home)
                    self.codex_model_cache[chat.id] = (time.monotonic(), model)
                except (DeliveryError, ValueError, OSError) as error:
                    warning = str(error)
                stamp, choices = self.codex_catalog_cache
                if not stamp or time.monotonic() - stamp > 300:
                    try:
                        choices = self.model_catalog(self.cli, self.data.home)
                        self.codex_catalog_cache = (time.monotonic(), choices)
                    except (ValueError, OSError) as error:
                        warning = warning or str(error)
        stamp, model = self.codex_model_cache.get(chat.id, (0, chat.model))
        choices = list(dict.fromkeys([item for item in [model, chat.model, *self.codex_catalog_cache[1]] if item]))
        return {'selected': model, 'requested_model': model, 'choices': choices,
                'automatic_model': '', 'last_response_model': '', 'warning': warning,
                'confirmed': bool(stamp)}

    def refresh_codex_model(self, chat):
        with self.model_lock:
            stamp = self.model_refresh_attempts.get(chat.id, self.codex_model_cache.get(chat.id, (0, ''))[0])
            if (stamp and time.monotonic() - stamp < 30) or chat.id in self.model_refreshing:
                return
            self.model_refreshing.add(chat.id)
            self.model_refresh_attempts[chat.id] = time.monotonic()
        def refresh():
            try:
                with self.model_lock:
                    model = self.model_bridge(chat, self.data.home)
                    self.codex_model_cache[chat.id] = (time.monotonic(), model)
            except (DeliveryError, ValueError, OSError):
                pass
            finally:
                with self.model_lock:
                    self.model_refreshing.discard(chat.id)
        threading.Thread(target=refresh, daemon=True).start()

    def set_codex_model(self, identity, model):
        validate_model(model)
        chat = self.chat(identity)
        if chat is None or chat.provider != 'codex':
            raise ValueError('请选择一个 Codex 聊天。')
        with self.model_lock:
            confirmed = self.model_bridge(chat, self.data.home, model=model)
            if confirmed != model:
                raise ValueError('桌面未确认所选模型，请刷新后重试。')
            self.codex_model_cache[identity] = (time.monotonic(), confirmed)
        return self.codex_model_info(chat)

    def set_claude_model(self, identity, model):
        if model != "":
            validate_model(model)
        with self.lock:
            chat = self.chat(identity)
            if chat is None or chat.provider != "claude":
                raise ValueError("请选择一个 Claude Code 聊天。")
            if chat.source == 'claude-desktop' and model:
                raise ValueError('桌面代发跟随 Claude 原生模型，请在 Claude 桌面调整。')
            models = self.config.setdefault("claude_chat_models", {})
            if model:
                models[identity] = model
            else:
                models.pop(identity, None)
            self.save_config()
            return self.claude_model_info(chat)

    def grok_model_info(self, chat, messages=None):
        selected = self.config.get('grok_chat_models', {}).get(chat.id, '')
        automatic = self.config.get('grok_model') or chat.model
        choices = [selected, automatic, chat.model, *self.grok.model_choices()]
        messages = self.grok.history(chat) if messages is None else messages
        last_model = next((message.model for message in reversed(messages) if message.role == 'assistant' and message.model), '')
        return {'selected': selected, 'requested_model': selected or automatic, 'automatic_model': automatic,
                'automatic_source': 'default' if self.config.get('grok_model') else 'history',
                'last_response_model': last_model, 'choices': list(dict.fromkeys(model for model in choices if model))}

    def set_grok_model(self, identity, model):
        if model != '':
            validate_model(model)
        with self.lock:
            chat = self.chat(identity)
            if not chat or chat.provider != 'grok':
                raise ValueError('请选择一个 Grok 聊天。')
            models = self.config.setdefault('grok_chat_models', {})
            if model:
                models[identity] = model
            else:
                models.pop(identity, None)
            self.save_config()
            return self.grok_model_info(chat)

    def provider_info(self):
        return {
            "provider": self.provider_mode,
            "available": {
                "codex": bool(self.cli),
                "claude": bool(self.claude_cli and self.claude.has_sessions()),
                'grok': bool(self.grok_cli and self.grok.has_sessions()),
            },
        }

    @staticmethod
    def save_receipt(connection, receipt):
        connection.execute("UPDATE deliveries SET receipt=? WHERE id=?",
                           (json.dumps(receipt, ensure_ascii=False), receipt["id"]))

    def upload(self, name, data):
        from PIL import Image, UnidentifiedImageError
        if not isinstance(data, str) or len(data) > (MAX_IMAGE_BYTES + 2) // 3 * 4:
            raise ValueError("单张图片最大 8 MB，请缩小后重试。")
        try:
            raw = base64.b64decode(data, validate=True)
        except ValueError as error:
            raise ValueError("图片内容无效，请重新选择。") from error
        if not raw or len(raw) > MAX_IMAGE_BYTES:
            raise ValueError("单张图片最大 8 MB。")
        try:
            with Image.open(io.BytesIO(raw)) as image:
                if image.format not in IMAGE_MIMES:
                    raise ValueError("请上传 JPEG、PNG 或 WebP 图片。")
                width, height = image.size
                if width * height > 20000000:
                    raise ValueError("图片尺寸过大，请缩小后重试。")
                mime = IMAGE_MIMES[image.format]
                image.load()
        except (UnidentifiedImageError, Image.DecompressionBombError, OSError) as error:
            raise ValueError("图片损坏或无法读取，请重新选择。") from error
        identity = secrets.token_hex(16)
        name = re.sub(r"[\x00-\x1f\x7f]", "", str(name or "图片").replace("\\", "/").rsplit("/", 1)[-1])[:100] or "图片"
        directory = self.root / "uploads"
        directory.mkdir(exist_ok=True)
        path = directory / (identity + IMAGE_SUFFIX[mime])
        with path.open("xb") as handle:
            handle.write(raw)
        if os.name != "nt":
            path.chmod(0o600)
        try:
            with self.connect() as connection:
                connection.execute("INSERT INTO uploads VALUES (?,?,?,?,?,?,?,?)",
                    (identity, name, mime, len(raw), width, height, hashlib.sha256(raw).hexdigest(), int(time.time())))
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return self.image(identity)

    def image(self, identity):
        if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{32}", identity):
            raise ValueError("图片编号无效。")
        with self.connect() as connection:
            row = connection.execute("SELECT id,name,mime,size,width,height,sha256 FROM uploads WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise ValueError("图片已不存在，请重新选择。")
        result = dict(row)
        result["url"] = "/api/image/" + identity
        if not self.image_path(result).is_file():
            raise ValueError("图片文件已不存在，请重新选择。")
        return result

    def image_path(self, image):
        return self.root / "uploads" / (image["id"] + IMAGE_SUFFIX[image["mime"]])

    def send(self, identity, text, request_id, image_ids=None, immediate=False, claude_model=None, codex_model=None, grok_model=None,
             record_key=None, history_head=None):
        image_ids = [] if image_ids is None else image_ids
        if not isinstance(image_ids, list) or any(not isinstance(item, str) for item in image_ids):
            raise ValueError("图片列表无效。")
        if not isinstance(text, str) or (not text.strip() and not image_ids) or len(text) > MAX_MESSAGE:
            raise ValueError(f"请输入消息或选择图片；文字最多 {MAX_MESSAGE:,} 字。")
        if not isinstance(request_id, str) or not 16 <= len(request_id) <= 100:
            raise ValueError("消息编号无效，请刷新后重试。")
        with self.lock:
            retry_queue_ids = []
            with self.connect() as connection:
                existing = connection.execute("SELECT * FROM deliveries WHERE id=?", (request_id,)).fetchone()
            if existing:
                old = json.loads(existing["receipt"])
                if existing["thread_id"] != identity or existing["text"] != text or [image["id"] for image in old.get("images", [])] != image_ids:
                    raise ValueError("此消息编号已使用，请检查已发送的消息。")
                if not immediate or old.get('delivery_mode') == 'immediate' or old.get('result') != 'sent':
                    return old
                queued = set(old.get('queue_ids', []))
                chat = self.chat(identity)
                old_data = self.data_for(chat) if chat else self.data
                if not queued or not queued <= old_data.queue_ids(identity, text) or not chat or old_data.receipt_message(old, old_data.history(chat)):
                    return old
                retry_queue_ids = sorted(queued)
            chat = self.chat(identity)
            if chat is None:
                raise ValueError("聊天不存在或已归档，请重新选择。")
            if not chat.writable:
                raise ValueError(self.conversation_info(chat)['detail'])
            if record_key is not None and record_key != self.record_key(chat):
                raise ValueError('聊天记录来源已变化，请重新选择聊天。本次未发送。')
            if chat.provider == 'claude' and history_head is not None and history_head != chat.history_head:
                raise ValueError('聊天消息链已更新，请刷新后发送。本次未发送。')
            data = self.data_for(chat)
            images = [self.image(item) for item in image_ids]
            if chat.provider == 'grok':
                if images:
                    raise ValueError('Grok 当前支持文字续聊，图片投递尚未开放。')
                if data.turn(identity).get('status') in ('inProgress', 'needsInput'):
                    raise ValueError('这个 Grok 聊天正在执行任务，请完成后再发送。')
                with self.connect() as connection:
                    previous = connection.execute('SELECT id,receipt FROM deliveries WHERE thread_id=? ORDER BY rowid DESC LIMIT 1', (identity,)).fetchone()
                if previous and json.loads(previous['receipt']).get('result') == 'sent':
                    old = json.loads(previous['receipt'])
                    activity = self.activities.transcript(chat)
                    native_finished = (activity['state'] in ('completed', 'failed', 'interrupted') and
                        old.get('sent_at_ms') and activity.get('user_timestamp_ms', 0) >= old['sent_at_ms'])
                    if not native_finished and not run_status(self.root / 'grok-runs' / previous['id'], identity.split(':', 1)[1]):
                        raise ValueError('上一条 Grok 任务尚未结束，请先查看原聊天。本次未发送。')
            if sum(image['size'] for image in images) > MAX_ATTACHMENT_BYTES:
                raise ValueError('每条消息的图片总大小最多 32 MB，请移除部分图片后发送。')
            messages = data.history(chat)
            before = data.queue_ids(chat.id, text)
            receipt = new_receipt(chat, text, messages)
            receipt["id"] = request_id
            receipt["images"] = images
            receipt["provider"] = chat.provider
            receipt['record_key'] = self.record_key(chat)
            receipt['history_head'] = chat.history_head
            if chat.provider == 'grok':
                receipt['requested_model'] = validate_model(self.grok_model_info(chat)['requested_model'] if grok_model is None else grok_model)
            elif chat.provider == "claude" and chat.source != 'claude-desktop':
                # Pin the model before starting the CLI; desktop UI settings are independent.
                receipt["requested_model"] = validate_model(
                    self.claude_model_info(chat)["requested_model"] if claude_model is None else claude_model)
            elif codex_model is not None:
                validate_model(codex_model)
                current = self.model_bridge(chat, self.data.home)
                with self.model_lock:
                    self.codex_model_cache[chat.id] = (time.monotonic(), current)
                if current != codex_model:
                    raise DeliveryError('Codex 桌面模型已变化，请重新确认模型后发送。本次未发送。')
                receipt['requested_model'] = current
            with self.connect() as connection:
                if existing:
                    self.save_receipt(connection, receipt)
                else:
                    connection.execute("INSERT INTO deliveries VALUES (?,?,?,?,?)",
                        (request_id, identity, text, json.dumps(receipt, ensure_ascii=False), int(time.time())))
            try:
                if chat.provider == 'grok':
                    result = self.grok_sender(self.grok_cli, self.grok.home, chat, text, request_id,
                        self.root / 'grok-runs', receipt['requested_model'], self.config.get('grok_permission_mode', ''))
                elif chat.source == 'claude-desktop':
                    if images:
                        raise ValueError('Claude 桌面代发当前支持文字，图片请在原生桌面添加。')
                    if claude_model:
                        raise ValueError('Claude 桌面代发跟随原生模型，请刷新手机后发送。')
                    result = deliver_desktop(chat, text, [], request_id, self.root / 'claude-desktop-runs')
                    receipt['requested_model'] = result.get('requested_model', '')
                elif chat.provider == "claude":
                    result = self.claude_sender(
                        self.claude_cli, chat, text, [self.image_path(image) for image in images],
                        request_id, self.config.get("claude_permission_mode", "bypassPermissions"),
                        self.root / "claude-runs", model=receipt["requested_model"], home=self.claude.home)
                else:
                    result = (self.answer_sender(chat,text,request_id,self.data.home,retry_queue_ids) if immediate else self.image_sender(self.cli, self.data.home, chat, text,
                              [self.image_path(image) for image in images], request_id) if images
                              else self.sender(self.cli, self.data.home, chat, text))
                receipt.update(result="sent", status="已入队 · 等待回执", warning=result.get("warning", False))
                receipt["queue_ids"] = result.get("queue_ids", [])
                receipt['delivery_mode'] = result.get('delivery_mode','queue')
                if chat.provider == "claude":
                    receipt['status'] = 'Claude 桌面已提交 · 等待回执' if chat.source == 'claude-desktop' else 'Claude Code 已启动 · 等待回复'
                elif chat.provider == 'grok':
                    receipt['status'] = 'Grok 已启动 · 等待回复'
                if immediate:
                    receipt['turn_id'] = result.get('turn_id')
                    receipt['status'] = '桌面已接收 · 已立即提交'
            except DeliveryError as error:
                receipt.update(result="uncertain" if error.uncertain else "failed",
                               status="投递结果待确认" if error.uncertain else "投递失败", error=str(error))
            except (ValueError, OSError) as error:
                receipt.update(result="failed", status="投递失败", error=str(error))
            except Exception:
                receipt.update(result="uncertain", status="投递结果待确认",
                               error="投递状态读取失败，请先检查原聊天，避免重复发送。")
            if retry_queue_ids and receipt['result'] != 'sent':
                remaining = set(retry_queue_ids) & data.queue_ids(chat.id, text)
                if remaining == set(retry_queue_ids) and receipt['result'] == 'failed':
                    receipt.update(result='sent', delivery_mode='queue', queue_ids=retry_queue_ids,
                                   status='旧回复仍在队列，请打开桌面聊天后点击立即提交')
                elif remaining:
                    receipt.update(result='uncertain', queue_ids=sorted(remaining), status='投递结果待确认')
            try:
                receipt["queue_ids"] = list(set(receipt["queue_ids"]) | (data.queue_ids(chat.id, text) - before))
            except (sqlite3.Error, OSError):
                pass
            with self.connect() as connection:
                self.save_receipt(connection, receipt)
            return receipt

    def answer(self, identity, message_id, answers, request_id):
        with self.answer_lock:
            chat = self.chat(identity)
            if chat is not None and chat.provider in ('claude', 'grok'):
                raise ValueError("这个工作台的提问回复请直接发送文字；原生结构化选项暂未开放。")
            snapshot = self.snapshot(identity)
            message = next((item for item in snapshot['messages'] if item['id'] == message_id and item['questions']), None)
            if message is None:
                raise ValueError('找不到这个提问，请刷新聊天。')
            if not isinstance(answers, list) or len(answers) != len(message['questions']) or any(
                    not isinstance(value, str) or not value.strip() or len(value) > 4000 for value in answers):
                raise ValueError('请回答每个问题；每项最多 4000 字。')
            values = [{'questionItemId': json.dumps(['request_user_input_async', message_id, index], separators=(',', ':')),
                       'question': question['title'], 'answer': value.strip()}
                      for index, (question, value) in enumerate(zip(message['questions'], answers))]
            text = '<send_user_message_question_reply>\n' + json.dumps(values, ensure_ascii=False) + '\n</send_user_message_question_reply>'
            # A lost HTTP response can be retried with the same delivery identifier.
            with self.connect() as c:
                existing = c.execute('SELECT text FROM deliveries WHERE id=?', (request_id,)).fetchone()
            if not existing and any(question.get('answer') is not None for question in message['questions']):
                raise ValueError('这个提问已有回复或正在投递，请刷新聊天查看。')
            return self.send(identity, text, request_id, immediate=True)

    def snapshot(self, identity):
        chat = self.chat(identity)
        if chat is None:
            raise ValueError("聊天不存在或已归档。")
        data = self.data_for(chat)
        if chat.provider == 'codex':
            self.refresh_codex_model(chat)
        messages = data.history(chat)
        queue_ids = data.queue_ids(identity)
        with self.connect() as connection:
            receipts = [json.loads(row[0]) for row in connection.execute(
                "SELECT receipt FROM deliveries WHERE thread_id=? ORDER BY created DESC, rowid DESC LIMIT 30", (identity,))]
            for receipt in receipts:
                if chat.provider == 'grok' and receipt.get('result') == 'sent':
                    finished = run_status(self.root / 'grok-runs' / receipt['id'], identity.split(':', 1)[1])
                    if finished:
                        if finished.get('ok'):
                            receipt['grok_finished'] = True
                        else:
                            receipt.update(result='failed', status='执行失败', error=finished.get('error', 'Grok 执行失败。'))
                match = data.receipt_message(receipt, messages)
                if receipt.get('record_key') and receipt['record_key'] != self.record_key(chat):
                    match = None
                    receipt['detached'] = True
                if not match and receipt.get('result') == 'sent' and not set(receipt.get('queue_ids', [])) & queue_ids:
                    if not receipt.get('detached'):
                        match = data.historical_receipt_message(receipt)
                if chat.source == 'claude-desktop' and not match and receipt.get('delivery_mode') != 'claude-desktop':
                    receipt['detached'] = True
                    receipt['status'] = '不在桌面当前分支'
                    receipt['seen_in_history'] = False
                if match:
                    receipt["seen_in_history"] = True
                    receipt['message_id'] = match.id
                    receipt['turn_id'] = match.turn_id
                    if chat.provider == 'grok':
                        activity = self.activities.transcript(chat)
                        if activity.get('user_message_id') == match.id:
                            receipt['native_status'] = activity['state']
                            if activity['state'] == 'completed':
                                receipt['grok_finished'] = True
                if not receipt.get('detached') and receipt["status"] != "正在投递":
                    receipt["status"] = data.receipt_status(receipt, messages, queue_ids)
                self.save_receipt(connection, receipt)
        output = []
        answers = {}
        answer_status = {}
        answer_requests = {}
        for message in messages:
            if message.role == 'user':
                actual = question_answers(message.text)
                answers.update(actual)
                answer_status.update({key: 'received' for key in actual})
        for receipt in receipts:
            receipt['display_text'] = display_user_text(receipt['text'])
            if not receipt.get('detached') and receipt.get('result') != 'failed':
                for key, answer in question_answers(receipt['text']).items():
                    answers.setdefault(key, answer)
                    if receipt.get('delivery_mode') == 'immediate' and receipt.get('result') == 'sent':
                        status = 'submitted'
                    elif receipt.get('result') == 'sent' and set(receipt.get('queue_ids', [])) & queue_ids:
                        status = 'queued'
                    else:
                        status = receipt.get('result', 'uncertain')
                    answer_status.setdefault(key, status)
                    answer_requests.setdefault(key, receipt['id'])
        uploaded = {}
        requested_models = {}
        for receipt in receipts:
            if receipt.get('detached'):
                continue
            match = data.receipt_message(receipt, messages)
            if match and receipt.get("images"):
                uploaded[match.id] = receipt["images"]
            if match and receipt.get("requested_model"):
                requested_models[match.id] = receipt["requested_model"]
        for message in messages:
            item = dataclasses.asdict(message)
            item["images"] = uploaded.get(message.id, [])
            item["image_count"] = len(message.images)
            item["requested_model"] = requested_models.get(message.id, "")
            item["text"] = display_user_text(message.text) if message.role == "user" else message.text
            item['questions'] = [dict(question, index=index, answer=answers.get((message.id, index)),
                                      reply_status=answer_status.get((message.id, index), ''),
                                      reply_request_id=answer_requests.get((message.id, index)))
                                 for index, question in enumerate(message.questions)]
            if item["text"].strip() or message.images or message.questions:
                item["text"] = item["text"][:48000]
                output.append(item)
        activity = self.activities.visible(self.activities.read(self, [chat])[chat.id], messages)
        return {"chat": dataclasses.asdict(chat), "messages": output, 'activity': activity,
                'conversation': self.conversation_info(chat),
                "claude_model": self.claude_model_info(chat) if chat.provider == "claude" else None,
                "codex_model": self.codex_model_info(chat) if chat.provider == 'codex' else None,
                'grok_model': self.grok_model_info(chat, messages) if chat.provider == 'grok' else None,
                "receipts": list(reversed(receipts)), "turn": data.turn(identity)}


class HTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, relay):
        self.relay = relay
        super().__init__(("127.0.0.1", relay.port), Handler)
        relay.notifications.start()

    def server_close(self):
        self.relay.notifications.close()
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    server_version = "CodexRelayMobile/1.3"

    @property
    def relay(self):
        return self.server.relay

    def log_message(self, *_):
        pass

    def local(self):
        forwarded = any(self.headers.get(name) for name in (
            "X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto", "Forwarded", "Tailscale-User-Login"))
        return not forwarded and self.headers.get("Host") == urllib.parse.urlsplit(self.relay.local_origin).netloc

    def cookie(self):
        try:
            cookie = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
            return cookie["relay_session"].value if "relay_session" in cookie else ""
        except http.cookies.CookieError:
            return ""

    def response(self, value, status=200, mime="application/json; charset=utf-8", headers=None):
        raw = json.dumps(value, ensure_ascii=False).encode() if mime.startswith("application/json") else value
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self.route(False)

    def do_POST(self):
        self.route(True)

    def route(self, post):
        self.connection.settimeout(55)
        if self.headers.get("Host") not in self.relay.hosts:
            self.response({"error": "Host 不受信任。"}, 403)
            return
        try:
            path = urllib.parse.urlsplit(self.path).path
            if post:
                if self.headers.get("Origin") != (self.relay.local_origin if self.local() else self.relay.origin):
                    self.response({"error": "来源校验失败，请从手机连接地址重新打开。"}, 403)
                    return
                if path == "/api/upload" and not self.relay.session(self.cookie()):
                    self.response({"error": "请先配对这台电脑。"}, 401)
                    return
                if self.headers.get_content_type() != "application/json":
                    raise ValueError("请求需要 JSON 格式。")
                length = int(self.headers.get("Content-Length", "0"))
                maximum = (MAX_IMAGE_BYTES + 2) // 3 * 4 + 1000 if path == "/api/upload" else 100000
                if not 0 < length <= maximum:
                    raise ValueError("请求大小无效。")
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError("请求格式无效。")
                if path == "/api/pair":
                    value = self.relay.pair(body.get("token"), self.headers.get("Tailscale-User-Login", "local"))
                    secure = "; Secure" if not self.local() else ""
                    self.response({"ok": True}, headers={"Set-Cookie": f"relay_session={value}; Path=/; HttpOnly; SameSite=Strict; Max-Age=2592000{secure}"})
                    return
                if path.startswith('/api/notification-relay/'):
                    try:
                        result = self.relay.notifications.receive_forward(path.rsplit('/', 1)[-1], body,
                            self.headers.get('Authorization', ''))
                    except PermissionError:
                        self.response({'error': '通知转发认证失败。'}, 403)
                    else:
                        self.response(result)
                    return
                if path == "/api/local/stop" and self.local():
                    self.response({"ok": True})
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                    return
                if path == "/api/local/provider" and self.local():
                    self.response(self.relay.set_provider(body.get("provider")))
                    return
                if path == '/api/notifications/ack':
                    self.response(self.relay.notifications.acknowledge(body.get('token'),body.get('stage'),body.get('error','')))
                    return
            if path == "/api/health" and not post:
                self.response({"app": "CodexRelayMobile", "version": APP_VERSION, "label": self.relay.config.get("label", socket.gethostname())})
                return
            if path == '/api/updates':
                if not self.local() and not self.relay.session(self.cookie()):
                    self.response({'error': '请先配对这台电脑。'}, 401)
                    return
                self.response(self.relay.updates.check(force=post))
                return
            if path.startswith("/api/local/"):
                if not self.local() or post:
                    self.response({"error": "请在电脑本机打开连接页。"}, 403)
                    return
                link = self.relay.origin + "/#pair=" + self.relay.token
                if path == "/api/local/setup":
                    self.response({"link": link, "origin": self.relay.origin, "label": self.relay.config.get("label"),
                                   "token": self.relay.token, **self.relay.provider_info()})
                elif path == "/api/local/qr.svg":
                    import qrcode
                    import qrcode.image.svg
                    output = io.BytesIO()
                    qrcode.make(link, image_factory=qrcode.image.svg.SvgPathImage, border=4).save(output)
                    self.response(output.getvalue(), mime="image/svg+xml")
                else:
                    self.response({"error": "找不到此操作。"}, 404)
                return
            if path.startswith("/api/"):
                if not self.relay.session(self.cookie()):
                    self.response({"error": "请先配对这台电脑。"}, 401)
                    return
                if path == "/api/logout" and post:
                    self.relay.notifications.revoke(self.cookie())
                    with self.relay.connect() as connection:
                        connection.execute("DELETE FROM sessions WHERE hash=?", (hashlib.sha256(self.cookie().encode()).hexdigest(),))
                    self.response({"ok": True}, headers={"Set-Cookie": "relay_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"})
                elif path == "/api/meta" and not post:
                    self.response({"label": self.relay.config.get("label"), "origin": self.relay.origin,
                                   "peers": self.relay.config.get("peers", []), "max_message": MAX_MESSAGE,
                                   "max_attachment_bytes": MAX_ATTACHMENT_BYTES,
                                   **self.relay.provider_info()})
                elif path == '/api/notifications' and not post:
                    self.response(self.relay.notifications.settings(self.cookie()))
                elif path == '/api/notifications' and post:
                    self.response(self.relay.notifications.configure(self.cookie(), body.get('enabled'), body.get('subscription')))
                elif path == '/api/notifications/events' and not post:
                    query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                    self.response(self.relay.notifications.poll(self.cookie(), query.get('after', [0])[0]))
                elif path == '/api/notifications/test' and post:
                    self.response(self.relay.notifications.test(self.cookie()))
                elif path == '/api/notifications/client' and post:
                    self.response(self.relay.notifications.client_info(self.cookie(),body))
                elif path == '/api/notifications/sync' and post:
                    self.response(self.relay.notifications.sync(self.cookie(),body.get('subscription')))
                elif path == '/api/provider' and post:
                    self.response(self.relay.set_provider(body.get('provider')))
                elif path == '/api/claude/model' and post:
                    self.response(self.relay.set_claude_model(body.get('thread_id'), body.get('model')))
                elif path == '/api/grok/model' and post:
                    self.response(self.relay.set_grok_model(body.get('thread_id'), body.get('model')))
                elif path == '/api/codex/model' and post:
                    self.response(self.relay.set_codex_model(body.get('thread_id'), body.get('model')))
                elif path == '/api/codex/models' and post:
                    chat = self.relay.chat(body.get('thread_id'))
                    if chat is None or chat.provider != 'codex':
                        raise ValueError('请选择一个 Codex 聊天。')
                    self.response(self.relay.codex_model_info(chat, refresh=True))
                elif path.startswith('/api/notifications/delivery/') and not post:
                    self.response(self.relay.notifications.delivery(self.cookie(),path.rsplit('/',1)[-1]))
                elif path == '/api/answer' and post:
                    self.response(self.relay.answer(body.get('thread_id'), body.get('message_id'), body.get('answers'), body.get('request_id')))
                elif path == "/api/upload" and post:
                    self.response(self.relay.upload(body.get("name"), body.get("data")))
                elif path.startswith("/api/image/") and not post:
                    picture = self.relay.image(path[len("/api/image/"):])
                    self.response(self.relay.image_path(picture).read_bytes(), mime=picture["mime"])
                elif path == "/api/chats" and not post:
                    chats = self.relay.chats()
                    activities = self.relay.activities.read(self.relay, chats)
                    self.response({"chats": [dict(dataclasses.asdict(chat), activity=activities[chat.id]) for chat in chats]})
                elif path.startswith("/api/chat/") and not post:
                    self.response(self.relay.snapshot(urllib.parse.unquote(path[len("/api/chat/"):])))
                elif path.startswith('/api/usage/') and not post:
                    chat = self.relay.chat(urllib.parse.unquote(path[len('/api/usage/'):]))
                    if chat is None:
                        raise ValueError('聊天不存在，请重新选择。')
                    home = self.relay.data_for(chat).home
                    usage = self.relay.usage.snapshot(chat, home)
                    cli = {'codex': self.relay.cli, 'claude': self.relay.claude_cli, 'grok': self.relay.grok_cli}[chat.provider]
                    usage['plan'] = self.relay.plans.read(chat.provider, home, cli)
                    self.response(usage)
                elif path == "/api/send" and post:
                    self.response(self.relay.send(body.get("thread_id"), body.get("text"), body.get("request_id"), body.get("image_ids"),
                                                  claude_model=body.get("claude_model"), codex_model=body.get('codex_model'), grok_model=body.get('grok_model'),
                                                  record_key=body.get('record_key'), history_head=body.get('history_head')))
                else:
                    self.response({"error": "找不到此操作。"}, 404)
                return
            if post:
                self.response({"error": "找不到此操作。"}, 404)
                return
            name = {"/": "index.html", "/connect": "connect.html", "/app.js": "app.js", "/style.css": "style.css",
                    "/connect.js": "connect.js", "/updates.js": "updates.js", "/manifest.webmanifest": "manifest.webmanifest", "/sw.js": "sw.js",
                    "/icon-192.png": "icon-192.png", "/icon-512.png": "icon-512.png",
                    "/marked.js": "marked.js", "/purify.js": "purify.js"}.get(path)
            if name is None:
                self.response({"error": "找不到页面。"}, 404)
                return
            mime = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                    ".css": "text/css; charset=utf-8", ".webmanifest": "application/manifest+json", ".png": "image/png"}[pathlib.Path(name).suffix]
            raw = (STATIC / name).read_bytes()
            app_name = 'Codex · ' + str(self.relay.config.get('label') or socket.gethostname())[:60]
            if name == 'manifest.webmanifest':
                manifest = json.loads(raw)
                manifest.update(name=app_name, short_name=app_name)
                raw = json.dumps(manifest, ensure_ascii=False).encode('utf-8')
            elif name == 'index.html':
                raw = raw.decode('utf-8').replace('name="apple-mobile-web-app-title" content="Codex 消息"',
                    'name="apple-mobile-web-app-title" content="' + html.escape(app_name, quote=True) + '"').encode('utf-8')
            self.response(raw, mime=mime)
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            self.response({"error": str(error)}, 400)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except DeliveryError as error:
            self.response({'error': str(error)}, 409)
        except (OSError, sqlite3.Error) as error:
            self.response({"error": f"读取或投递失败：{error}"}, 503)


def serve(root=None):
    server = HTTPServer(Relay(root))
    try:
        server.serve_forever(poll_interval=0.3)
    finally:
        server.server_close()


if __name__ == "__main__":
    serve()
