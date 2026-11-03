from __future__ import annotations

import json
import pathlib
import re
import threading
import time
from urllib.parse import quote

import requests

from version import APP_VERSION, PUBLIC_BASELINE


REPOSITORY = 'starmoonmyth/codex-relay-mobile'
RELEASES_URL = f'https://github.com/{REPOSITORY}/releases'
CHECK_INTERVAL = 24 * 60 * 60


def version_key(value):
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r'v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+[0-9A-Za-z.-]+)?', value)
    if not match:
        return None
    major, minor, patch, preview = match.groups()
    parts = tuple((0, int(part)) if part.isdigit() else (1, part) for part in preview.split('.')) if preview else ()
    return (int(major), int(minor), int(patch), not preview, parts)


class Updates:
    def __init__(self, root, current=APP_VERSION, baseline=PUBLIC_BASELINE):
        self.current = current
        self.baseline = baseline
        self.preview = not version_key(baseline)[3]
        self.path = pathlib.Path(root) / 'update-check.json'
        self.lock = threading.Lock()
        self.cache = {}
        try:
            saved = json.loads(self.path.read_text('utf-8'))
            if (isinstance(saved, dict) and saved.get('channel') == self.channel and
                    isinstance(saved.get('checked_at'), (int, float)) and
                    0 <= saved['checked_at'] <= time.time()):
                self.cache = saved
        except (OSError, ValueError):
            pass

    @property
    def channel(self):
        return 'preview' if self.preview else 'stable'

    def _fetch(self):
        suffix = '/releases?per_page=30' if self.preview else '/releases/latest'
        response = requests.get(f'https://api.github.com/repos/{REPOSITORY}' + suffix,
            headers={'Accept': 'application/vnd.github+json', 'User-Agent': f'CodexRelayMobile/{self.current}'},
            timeout=(3, 6), allow_redirects=False)
        if response.status_code in (403, 429):
            raise ValueError('GitHub 暂时限制了查询，请稍后再试。')
        response.raise_for_status()
        data = response.json()
        entries = data if isinstance(data, list) else [data]
        candidates = []
        for entry in entries:
            if not isinstance(entry, dict) or entry.get('draft'):
                continue
            key = version_key(entry.get('tag_name'))
            if key is None or (not self.preview and (entry.get('prerelease') or not key[3])):
                continue
            candidates.append((key, entry['tag_name']))
        if not candidates:
            raise ValueError('未能读取可用的 GitHub 版本，请稍后再试。')
        _, tag = max(candidates)
        return tag

    def check(self, force=False):
        with self.lock:
            now = time.time()
            age = now - self.cache.get('checked_at', 0)
            # Manual clicks share a short cooldown across all connected devices.
            if not self.cache or age >= (60 if force else CHECK_INTERVAL):
                previous = self.cache.get('latest')
                self.cache = {'channel': self.channel, 'checked_at': now}
                try:
                    self.cache['latest'] = self._fetch()
                except (requests.RequestException, ValueError):
                    if version_key(previous):
                        self.cache['latest'] = previous
                    self.cache['error'] = '暂时无法检查更新，请稍后重试或打开 GitHub 发布页。'
                try:
                    temporary = self.path.with_suffix('.tmp')
                    temporary.write_text(json.dumps(self.cache), encoding='utf-8')
                    temporary.replace(self.path)
                except OSError:
                    pass
            latest = self.cache.get('latest')
            key = version_key(latest)
            available = key is not None and key > version_key(self.baseline)
            return {'current_version': self.current, 'public_baseline': self.baseline,
                    'channel': self.channel, 'latest_version': latest if key else None,
                    'available': available, 'checked_at': self.cache.get('checked_at'),
                    'error': self.cache.get('error', ''),
                    'release_url': RELEASES_URL + '/tag/' + quote(latest, safe='') if key else RELEASES_URL}
