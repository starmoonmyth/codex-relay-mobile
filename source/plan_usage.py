from __future__ import annotations

import hashlib
import json
import math
import os
import pathlib
import queue
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

from usage import NoRedirect, load_json, number


def claude_credentials(home):
    credentials = load_json(home / '.credentials.json')
    if (credentials.get('claudeAiOauth') or {}).get('accessToken') or sys.platform != 'darwin':
        return credentials
    if home.resolve() != (pathlib.Path.home() / '.claude').resolve():
        return credentials
    # Read the default CLI login only when the Keychain already allows access.
    import ctypes
    security = ctypes.CDLL('/System/Library/Frameworks/Security.framework/Security')
    security.SecKeychainGetUserInteractionAllowed.argtypes = [ctypes.POINTER(ctypes.c_bool)]
    security.SecKeychainSetUserInteractionAllowed.argtypes = [ctypes.c_bool]
    security.SecKeychainFindGenericPassword.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p,
        ctypes.c_uint32, ctypes.c_char_p, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    security.SecKeychainItemFreeContent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    allowed, length, payload = ctypes.c_bool(), ctypes.c_uint32(), ctypes.c_void_p()
    if security.SecKeychainGetUserInteractionAllowed(ctypes.byref(allowed)) != 0:
        return credentials
    if security.SecKeychainSetUserInteractionAllowed(False) != 0:
        return credentials
    try:
        service = b'Claude Code-credentials'
        status = security.SecKeychainFindGenericPassword(None, len(service), service, 0, None,
            ctypes.byref(length), ctypes.byref(payload), None)
        if status == 0:
            try:
                value = json.loads(ctypes.string_at(payload, length.value)) if length.value <= 1024 * 1024 else {}
                if isinstance(value, dict) and (value.get('claudeAiOauth') or {}).get('accessToken'):
                    return value
            finally:
                security.SecKeychainItemFreeContent(None, payload)
    except (ValueError, UnicodeError):
        pass
    finally:
        security.SecKeychainSetUserInteractionAllowed(allowed)
    return credentials


def rpc_read(cli, home, provider):
    grok = provider == 'grok'
    command = [cli, 'agent', 'stdio'] if grok else [cli, 'app-server', '--listen', 'stdio://']
    process = subprocess.Popen(command, env=dict(os.environ, **{('GROK_HOME' if grok else 'CODEX_HOME'): str(home)}),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, encoding='utf-8', errors='replace', creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    lines = queue.Queue()
    def read():
        try:
            for line in process.stdout:
                try:
                    value = json.loads(line)
                    if isinstance(value, dict):
                        lines.put(value)
                except ValueError:
                    continue
        finally:
            lines.put(None)
    thread = threading.Thread(target=read, daemon=True)
    thread.start()
    deadline = time.monotonic() + 14
    def write(value):
        process.stdin.write(json.dumps(value) + '\n')
        process.stdin.flush()
    def request(identity, method, params):
        write({'jsonrpc': '2.0', 'id': identity, 'method': method, 'params': params})
        while True:
            value = lines.get(timeout=max(.01, deadline - time.monotonic()))
            if value is None:
                raise ValueError('CLI 未返回套餐额度。')
            if value.get('id') == identity:
                if 'error' in value:
                    raise ValueError('CLI 套餐接口暂不可用，请检查电脑端登录。')
                return value.get('result') or {}
    try:
        params = {'protocolVersion': 1, 'clientCapabilities': {'fs': {'readTextFile': False, 'writeTextFile': False}, 'terminal': False}} if grok else {'clientInfo': {'name': 'codex_relay_mobile', 'version': '1.3.7'}}
        request(1, 'initialize', params)
        if not grok:
            write({'method': 'initialized'})
        return request(2, '_x.ai/billing' if grok else 'account/rateLimits/read', {})
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        thread.join(timeout=1)
        process.stdout.close()


def claude_read(token):
    request = urllib.request.Request('https://api.anthropic.com/api/oauth/usage', headers={
        'Authorization': 'Bearer ' + token, 'anthropic-beta': 'oauth-2025-04-20',
        'Accept': 'application/json', 'User-Agent': 'CodexRelayMobile/1.3.7'})
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=8) as response:
        content = response.read(1024 * 1024 + 1)
    if len(content) > 1024 * 1024:
        raise ValueError('套餐接口响应过大。')
    return json.loads(content)


def decimal(value):
    if number(value) is not None:
        return value
    if isinstance(value, str):
        try:
            result = float(value)
            return result if math.isfinite(result) else None
        except ValueError:
            pass
    return None


def window(label, used, reset=None):
    return {'label': label, 'used_percent': used, 'resets_at': reset}


def normalize_codex(value):
    result = {'windows': [], 'credits': [], 'plan': ''}
    limits = value.get('rateLimitsByLimitId') or {'codex': value.get('rateLimits') or {}}
    for identity, limit in limits.items():
        if not isinstance(limit, dict):
            continue
        result['plan'] = result['plan'] or str(limit.get('planType') or '')
        prefix = '' if identity == 'codex' else str(limit.get('limitName') or identity) + ' · '
        for key, fallback in [('primary', '主要周期'), ('secondary', '次要周期')]:
            item = limit.get(key) or {}
            used, minutes = number(item.get('usedPercent')), number(item.get('windowDurationMins'))
            if used is None:
                continue
            label = f'{minutes / 1440:g} 天' if minutes and minutes >= 1440 else f'{minutes / 60:g} 小时' if minutes else fallback
            result['windows'].append(window(prefix + label, used, item.get('resetsAt')))
        credits = limit.get('credits') or {}
        if credits.get('unlimited') is True:
            result['credits'].append({'label': prefix + '额外额度', 'unlimited': True, 'unit': 'credits'})
        elif decimal(credits.get('balance')) is not None:
            result['credits'].append({'label': prefix + '额外额度', 'value': decimal(credits['balance']), 'unit': 'credits'})
    return result


def normalize_claude(value):
    result = {'windows': [], 'credits': [], 'plan': ''}
    labels = {'five_hour': '5 小时', 'seven_day': '7 天', 'seven_day_opus': 'Opus · 7 天', 'seven_day_sonnet': 'Sonnet · 7 天', 'seven_day_oauth_apps': 'OAuth 应用 · 7 天'}
    for key, label in labels.items():
        item = value.get(key) or {}
        if number(item.get('utilization')) is not None:
            result['windows'].append(window(label, item['utilization'], item.get('resets_at')))
    extra = value.get('extra_usage') or {}
    if extra.get('is_enabled'):
        limit, used = number(extra.get('monthly_limit')), number(extra.get('used_credits'))
        if limit is not None and used is not None:
            result['credits'].append({'label': '本月额外用量剩余额度', 'value': max(0, limit - used) / 100,
                'unit': str(extra.get('currency') or 'USD')[:20]})
    spend = value.get('spend') or {}
    if spend.get('enabled') and not result['credits']:
        def amount(item):
            exponent = number(item.get('exponent'))
            raw = number(item.get('amount_minor'))
            return raw / 10 ** int(exponent) if raw is not None and exponent is not None and 0 <= exponent <= 8 else None
        used, limit = amount(spend.get('used') or {}), amount(spend.get('limit') or {})
        if used is not None and limit is not None:
            unit = spend.get('currency')
            if isinstance(unit, dict):
                unit = unit.get('code')
            result['credits'].append({'label': '本月额外用量剩余额度', 'value': max(0, limit - used), 'unit': str(unit or '接口金额单位')[:20]})
    return result


def normalize_grok(value):
    config = value.get('config') or {}
    result = {'windows': [], 'credits': [], 'plan': str(value.get('subscription_tier') or config.get('subscriptionTier') or '')}
    percent = number(config.get('creditUsagePercent'))
    cap, used = decimal((config.get('onDemandCap') or {}).get('val')), decimal((config.get('onDemandUsed') or {}).get('val'))
    if percent is None and cap is not None and cap > 0 and used is not None:
        percent = used / cap * 100
    period = config.get('currentPeriod') or {}
    if percent is not None:
        result['windows'].append(window('本周期', percent, period.get('end') or config.get('billingPeriodEnd')))
    prepaid = config.get('prepaidBalance')
    if isinstance(prepaid, dict):
        cents = 0 if not prepaid else decimal(prepaid.get('val'))
        if cents is not None and cents >= 0:
            result['credits'].append({'label': '预付余额', 'value': cents / 100, 'unit': 'USD'})
    if not config:
        limit, used = decimal((value.get('monthlyLimit') or {}).get('val')), decimal((value.get('usage') or {}).get('totalUsed', {}).get('val'))
        if limit is not None and limit > 0 and used is not None:
            result['windows'].append(window('本月', used / limit * 100, (value.get('billingCycle') or {}).get('billingPeriodEnd')))
    return result


class Plans:
    def __init__(self, rpc=rpc_read, oauth=claude_read):
        self.rpc, self.oauth = rpc, oauth
        self.cache = {}
        self.lock = threading.Lock()

    def read(self, provider, home, cli=''):
        home = pathlib.Path(home)
        with self.lock:
            credentials = claude_credentials(home) if provider == 'claude' else load_json(home / 'auth.json')
        token = (credentials.get('claudeAiOauth') or {}).get('accessToken') if provider == 'claude' else ''
        api_key_only = provider == 'codex' and (credentials.get('OPENAI_API_KEY') or credentials.get('auth_mode') == 'apikey') and not (credentials.get('tokens') or {}).get('access_token')
        if (provider == 'claude' and not token) or api_key_only or (provider != 'claude' and not cli):
            return {'status': 'unavailable', 'message': '未找到此电脑的原生套餐登录。请先在对应 CLI 登录；中转站余额在下方单独显示。'}
        identity = hashlib.sha256((provider + str(home) + cli + json.dumps(credentials, sort_keys=True)).encode()).hexdigest()
        with self.lock:
            previous = self.cache.get(identity)
            if previous and time.time() - previous['updated_at'] < previous.get('retry_seconds', 60):
                return previous
            result = {'source': {'codex': 'Codex CLI · 当前登录账号', 'claude': 'Claude OAuth · 当前登录账号', 'grok': 'Grok CLI · 当前登录账号'}[provider], 'updated_at': int(time.time())}
            try:
                raw = self.oauth(token) if provider == 'claude' else self.rpc(cli, home, provider)
                parsed = {'codex': normalize_codex, 'claude': normalize_claude, 'grok': normalize_grok}[provider](raw)
                if not parsed['windows'] and not parsed['credits']:
                    raise ValueError('套餐接口未提供额度字段。')
                if provider == 'claude':
                    parsed['plan'] = str((credentials.get('claudeAiOauth') or {}).get('subscriptionType') or '')
                result.update(parsed, status='ok')
            except urllib.error.HTTPError as error:
                result.update(status='error', message=f'套餐查询失败（HTTP {error.code}）。请在电脑检查登录状态，稍后重试。')
                if error.code == 429:
                    retry = decimal(error.headers.get('Retry-After')) if error.headers else None
                    result['retry_seconds'] = min(3600, max(300, retry or 0))
            except (OSError, ValueError, TypeError, AttributeError, queue.Empty):
                result.update(status='error', message='套餐额度暂不可用，请检查电脑端 CLI 登录或版本。未将缺少的数据计为零。')
            self.cache = {key: item for key, item in self.cache.items() if time.time() - item['updated_at'] < item.get('retry_seconds', 60)}
            self.cache[identity] = result
            return result
