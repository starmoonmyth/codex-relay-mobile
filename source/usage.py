from __future__ import annotations

import hashlib
import contextlib
import json
import math
import os
import pathlib
import sqlite3
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request


def number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def tokens(value, provider):
    if not isinstance(value, dict):
        return None
    fields = {
        'codex': ('input_tokens', 'cached_input_tokens', 'cache_write_input_tokens', 'output_tokens', 'reasoning_output_tokens'),
        'claude': ('input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens', 'output_tokens', ''),
        'grok': ('inputTokens', 'cachedReadTokens', 'cacheCreationTokens', 'outputTokens', 'reasoningTokens'),
    }[provider]
    if not any(number(value.get(key)) is not None for key in fields if key):
        return None
    result = dict(zip(('input_tokens', 'cached_input_tokens', 'cache_write_input_tokens',
                       'output_tokens', 'reasoning_output_tokens'),
                      (max(0, int(number(value.get(key)) or 0)) for key in fields)))
    if provider == 'claude':
        result['input_tokens'] += result['cached_input_tokens'] + result['cache_write_input_tokens']
        result['reasoning_output_tokens'] = max(0, int(number((value.get('output_tokens_details') or {}).get('thinking_tokens')) or 0))
    result['total_tokens'] = result['input_tokens'] + result['output_tokens']
    return result


def entries(path):
    with path.open(encoding='utf-8', errors='replace') as handle:
        for line in handle:
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                yield value


def local_usage(chat):
    result = {'available': False, 'total': None, 'last': None, 'windows': [], 'context_window': None, 'updated_at': None}
    path = pathlib.Path(chat.rollout_path)
    try:
        result['updated_at'] = int(path.stat().st_mtime)
        messages = {}
        for index, entry in enumerate(entries(path)):
            if chat.provider == 'codex':
                payload = entry.get('payload') or {}
                if entry.get('type') != 'event_msg' or payload.get('type') != 'token_count':
                    continue
                info = payload.get('info') or {}
                result['total'] = tokens(info.get('total_token_usage'), 'codex') or result['total']
                result['last'] = tokens(info.get('last_token_usage'), 'codex') or result['last']
                result['context_window'] = number(info.get('model_context_window')) or result['context_window']
                limits = payload.get('rate_limits') or {}
                windows = []
                for key in ('primary', 'secondary'):
                    window = limits.get(key) or {}
                    used = number(window.get('used_percent'))
                    if used is not None:
                        windows.append({'used_percent': used, 'minutes': number(window.get('window_minutes')),
                                        'resets_at': number(window.get('resets_at'))})
                if windows:
                    result['windows'] = windows
            elif chat.provider == 'claude':
                message = entry.get('message') or {}
                if entry.get('type') != 'assistant' or entry.get('isSidechain'):
                    continue
                usage = tokens(message.get('usage'), 'claude')
                if usage is not None:
                    identity = message.get('id') or entry.get('uuid') or str(index)
                    previous = messages.get(identity)
                    messages[identity] = {key: max(value, (previous or {}).get(key, 0)) for key, value in usage.items()}
                    messages[identity]['total_tokens'] = messages[identity]['input_tokens'] + messages[identity]['output_tokens']
                    result['last'] = messages[identity]
            else:
                update = (entry.get('params') or {}).get('update') or {}
                if update.get('sessionUpdate') != 'turn_completed':
                    continue
                usage = tokens(update.get('usage'), 'grok')
                if usage is not None:
                    identity = update.get('prompt_id') or str(index)
                    messages[identity] = usage
                    result['last'] = usage
        if chat.provider != 'codex' and messages:
            result['total'] = {key: sum(value[key] for value in messages.values()) for key in result['last']}
        result['available'] = result['total'] is not None
    except OSError:
        result['error'] = '聊天用量记录暂不可读。'
    return result


def load_json(path):
    try:
        value = json.loads(path.read_text('utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def codex_provider(config):
    provider = (config.get('model_providers') or {}).get(config.get('model_provider'), {})
    return provider.get('base_url') or '', provider.get('env_key') or ''


def usage_url(base):
    parts = urllib.parse.urlsplit(base)
    if (parts.scheme != 'https' and not (parts.scheme == 'http' and parts.hostname in ('127.0.0.1', 'localhost', '::1'))):
        raise ValueError('余额查询需要 HTTPS 服务地址。')
    if not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError('余额查询服务地址无效。')
    path = parts.path.rstrip('/').removesuffix('/v1') + '/v1/usage'
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, '', ''))


def balance_credentials(provider, home, switch_home=None):
    base, key = '', ''
    source = '此电脑当前 API Key'
    if provider == 'codex':
        try:
            config = tomllib.loads((home / 'config.toml').read_text('utf-8'))
        except (OSError, ValueError):
            return None
        base, env_key = codex_provider(config)
        key = os.environ.get(env_key, '') if env_key else ''
        key = key or load_json(home / 'auth.json').get('OPENAI_API_KEY')
        if base and not key:
            database = pathlib.Path(switch_home or pathlib.Path.home() / '.cc-switch') / 'cc-switch.db'
            try:
                with contextlib.closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True, timeout=.5)) as connection:
                    rows = connection.execute("SELECT settings_config FROM providers WHERE app_type='codex' AND is_current=1").fetchall()
                for row in rows:
                    settings = json.loads(row[0])
                    saved_base, _ = codex_provider(tomllib.loads(settings.get('config') or ''))
                    if usage_url(saved_base) == usage_url(base):
                        key = (settings.get('auth') or {}).get('OPENAI_API_KEY')
                        if key:
                            source = 'CC Switch 当前配置的 API Key（与聊天登录账号可能不同）'
                            break
            except (sqlite3.Error, OSError, ValueError, TypeError):
                pass
    elif provider == 'claude':
        environment = dict(os.environ, **(load_json(home / 'settings.json').get('env') or {}))
        base = environment.get('ANTHROPIC_BASE_URL') or ''
        key = environment.get('ANTHROPIC_AUTH_TOKEN') or environment.get('ANTHROPIC_API_KEY') or ''
    if not isinstance(base, str) or not isinstance(key, str) or not base or not key:
        return None
    try:
        url = usage_url(base)
    except ValueError:
        return None
    return url, key, source


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def fetch_balance(url, key):
    request = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + key,
        'Accept': 'application/json', 'User-Agent': 'CodexRelayMobile/1.3.6'})
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=8) as response:
        content = response.read(1024 * 1024 + 1)
    if len(content) > 1024 * 1024:
        raise ValueError('余额接口响应过大。')
    return json.loads(content)


def normalize_balance(value):
    if not isinstance(value, dict):
        raise ValueError('余额接口未返回有效数据。')
    if value.get('isValid') is False or value.get('is_active') is False:
        raise ValueError('服务商报告 API Key 已失效。')
    quota = value.get('quota') if isinstance(value.get('quota'), dict) else {}
    remaining = next((n for n in (number(value.get('remaining')), number(quota.get('remaining')), number(value.get('balance'))) if n is not None), None)
    result = {'remaining': remaining, 'unit': str(value.get('unit') or quota.get('unit') or '')[:20],
              'label': '钱包余额' if number(value.get('balance')) is not None else '可用额度', 'limits': [], 'today': None}
    for window in value.get('rate_limits') or []:
        if not isinstance(window, dict):
            continue
        result['limits'].append({'label': str(window.get('window') or '额度')[:30],
            'used': number(window.get('used')), 'limit': number(window.get('limit')),
            'remaining': number(window.get('remaining')), 'resets_at': window.get('reset_at')})
    subscription = value.get('subscription') or {}
    for period, label in [('daily', '每日'), ('weekly', '每周'), ('monthly', '每月')]:
        limit, used = number(subscription.get(period + '_limit_usd')), number(subscription.get(period + '_usage_usd'))
        if limit is not None and used is not None and limit > 0:
            result['limits'].append({'label': label, 'used': used, 'limit': limit,
                'remaining': max(0, limit - used), 'unit': 'USD'})
    today = (value.get('usage') or {}).get('today') or {}
    if isinstance(today, dict) and today:
        result['today'] = {'cost': number(today.get('actual_cost')), 'requests': number(today.get('requests')),
                           'total_tokens': number(today.get('total_tokens'))}
    if remaining is None and not result['limits'] and result['today'] is None:
        raise ValueError('服务商未提供余额或用量字段。')
    return result


class Usage:
    def __init__(self, fetcher=fetch_balance):
        self.fetcher = fetcher
        self.cache = {}
        self.lock = threading.Lock()

    def balance(self, provider, home):
        credentials = balance_credentials(provider, home)
        if credentials is None:
            return {'status': 'unavailable', 'message': '未找到此工作台可查询余额的服务地址和 API Key。聊天用量仍可查看。'}
        url, key, source = credentials
        identity = hashlib.sha256((url + '\0' + key).encode()).hexdigest()
        with self.lock:
            cached = self.cache.get(identity)
            if cached and time.time() - cached['updated_at'] < 60:
                return dict(cached, source=source)
            result = {'source': source, 'service': urllib.parse.urlsplit(url).hostname, 'updated_at': int(time.time())}
            try:
                result.update(normalize_balance(self.fetcher(url, key)), status='ok')
            except urllib.error.HTTPError as error:
                result.update(status='error', message=f'余额查询失败（HTTP {error.code}），请检查服务商是否支持 /v1/usage 及当前 API Key。')
            except (OSError, ValueError, TypeError, AttributeError):
                result.update(status='error', message='余额查询暂不可用，请稍后刷新或到服务商查看。')
            self.cache = {k: v for k, v in self.cache.items() if time.time() - v['updated_at'] < 60}
            self.cache[identity] = result
            return result

    def snapshot(self, chat, home):
        return {'provider': chat.provider, 'local': local_usage(chat), 'balance': self.balance(chat.provider, home)}
