from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time


def atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name, dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def read_text(path, limit=65536):
    try:
        with Path(path).open('rb') as f:
            return f.read(limit).decode('utf-8', errors='replace')
    except OSError:
        return ''


def load(path, default=None, limit=2 * 1024 * 1024):
    try:
        with Path(path).open('rb') as f:
            data = f.read(limit + 1)
        if len(data) > limit:
            raise ValueError('file exceeds observation limit')
        return json.loads(data)
    except (OSError, ValueError):
        return default


def tail(path, limit=256 * 1024):
    """Bounded regular-file tail; partial first/last JSONL records are not parsed."""
    try:
        with Path(path).open('rb') as f:
            f.seek(0, 2)
            size = f.tell()
            offset = max(0, size - limit)
            f.seek(offset)
            data = f.read(limit)
        if offset:
            data = data.partition(b'\n')[2]
        return data.decode('utf-8', errors='replace')
    except OSError:
        return ''


def key(*values):
    return hashlib.sha256('\0'.join(map(str, values)).encode()).hexdigest()[:20]


def redact(text):
    text = str(text)
    text = re.sub(r'(?i)\b(?:sk|ghp|github_pat|glpat)[-_][A-Za-z0-9_\-]{12,}', '[REDACTED]', text)
    text = re.sub(r'(?i)((?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|authorization)[\"\x27]?\s*[=:]\s*[\"\x27]?)([^\s,;\"\x27]+)', r'\1[REDACTED]', text)
    text = re.sub(r'(?i)(bearer\s+)\S+', r'\1[REDACTED]', text)
    text = re.sub(r'(https?://)[^/\s:@]+:[^/\s@]+@', r'\1[REDACTED]@', text)
    return text


@contextlib.contextmanager
def locked(path, nonblocking=False):
    import fcntl
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with p.open('a') as f:
        fcntl.flock(f, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


class Store:
    def __init__(self, path):
        self.path = Path(path).expanduser().resolve()
        self.path.mkdir(parents=True, exist_ok=True, mode=0o700)

    def registry(self):
        p = self.path / 'registry.json'
        data = load(p)
        if data is None and not p.exists():
            return {'schema': 1, 'projects': {}, 'sessions': {}}
        if not isinstance(data, dict) or not isinstance(data.get('projects'), dict) or not isinstance(data.get('sessions'), dict):
            raise ValueError('Console registry is invalid; it has not been overwritten.')
        return data

    def register(self, project, session=None):
        p = Path(project).expanduser().resolve(strict=True)
        if not p.is_dir():
            raise ValueError('Project must be a directory')
        with locked(self.path / 'registry.lock'):
            data = self.registry()
            data['projects'][key(p)] = {'path': str(p), 'registered_at': time.time()}
            if session:
                data['sessions'][session['id']] = dict(session, project=str(p))
            atomic(self.path / 'registry.json', data)
        return key(p)

    def event(self, value):
        with locked(self.path / 'events.lock'):
            p = self.path / 'events.jsonl'
            if p.exists() and p.stat().st_size > 4 * 1024 * 1024:
                os.replace(p, self.path / 'events.previous.jsonl')
            with p.open('a', encoding='utf-8') as f:
                f.write(json.dumps(dict(value, recorded_at=time.time()), ensure_ascii=False) + '\n')
