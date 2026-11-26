#!/usr/bin/env python3
"""Shared deterministic primitives for Lemvo runtime templates.

Single source of truth for atomic JSON/text writes, cross-platform file locks,
/proc process identity & start-epoch checks, YAML budget/gate inspection, and
sibling module loading. Python 3.6+ stdlib only; no model or job control.
"""
from contextlib import contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import uuid

try:
    import fcntl
except ImportError:
    fcntl = None

HARD_STATUSES = frozenset(('STOP', 'STOPPED', 'PAUSED'))
HUMAN_STATUSES = frozenset((
    'WAITING_USER', 'HOLD', 'EVIDENCE_HOLD', 'STOPPED_INCONCLUSIVE',
    'BLOCKED', 'BLOCKED_USER', 'BLOCKED-USER',
))
DONE_STATUSES = frozenset(('COMPLETED', 'CONCLUDED', 'CAMPAIGN_DONE', 'CAMPAIGN-DONE'))
INVALID_CAP_SOURCES = frozenset(('', 'unknown', 'none', 'null', '-', 'n/a'))


def load_json(path, default=None, encoding='utf-8-sig'):
    """Read JSON from path safely; return default on missing/corrupt/unreadable files."""
    try:
        return json.loads(Path(path).read_text(encoding=encoding))
    except (OSError, ValueError, UnicodeError):
        return default


def atomic_write(path, value, indent=2, sort_keys=False, fsync=True,
                 trailing_newline=False, compact=False):
    """Atomically write string or JSON value to path via temp file in same directory."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.' + p.name + '-', dir=str(p.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            if isinstance(value, str):
                stream.write(value)
            elif compact:
                json.dump(value, stream, ensure_ascii=False, sort_keys=sort_keys,
                          separators=(',', ':'))
                if trailing_newline:
                    stream.write('\n')
            else:
                json.dump(value, stream, ensure_ascii=False, indent=indent,
                          sort_keys=sort_keys)
                if trailing_newline:
                    stream.write('\n')
            stream.flush()
            if fsync:
                os.fsync(stream.fileno())
        os.replace(tmp, str(p))
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


@contextmanager
def locked_file(path):
    """Acquire an exclusive advisory lock (POSIX flock / Windows msvcrt)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open('a+b') as stream:
        if fcntl is not None:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        else:
            import msvcrt
            if stream.seek(0, 2) == 0:
                stream.write(b'0')
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            else:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def proc_identity(pid, proc_root='/proc', include_argv=False):
    """Return /proc identity dict {pid, start, state, ppid, uid[, argv]} or None."""
    try:
        pid_i = int(pid)
        if pid_i <= 0:
            return None
        pdir = Path(proc_root) / str(pid_i)
        fields = (pdir / 'stat').read_text().rsplit(')', 1)[1].split()
        out = {
            'pid': pid_i,
            'start': fields[19],
            'state': fields[0],
            'ppid': int(fields[1]),
            'uid': pdir.stat().st_uid,
        }
        if include_argv:
            out['argv'] = (pdir / 'cmdline').read_bytes().split(b'\0')
        return out
    except (OSError, ValueError, TypeError, IndexError):
        return None


def process_start_ticks(pid, proc_root='/proc'):
    """Return starttime ticks for a live non-zombie process, else None."""
    item = proc_identity(pid, proc_root=proc_root)
    if not item or item['state'] in ('Z', 'X'):
        return None
    return item['start']


def proc_start_epoch(start_ticks, proc_root='/proc'):
    """Convert /proc/<pid>/stat starttime ticks to Unix epoch seconds, or None."""
    if not start_ticks:
        return None
    try:
        boot = next(
            int(line.split()[1])
            for line in (Path(proc_root) / 'stat').read_text().splitlines()
            if line.startswith('btime ')
        )
        return boot + float(start_ticks) / os.sysconf('SC_CLK_TCK')
    except (OSError, ValueError, TypeError, StopIteration, AttributeError):
        return None


def pid_alive(pid, expected_start=None, require_same_uid=False, proc_root='/proc'):
    """Verify live non-zombie process matching optional UID and /proc start ticks."""
    item = proc_identity(pid, proc_root=proc_root)
    if not item or item['state'] in ('Z', 'X'):
        return False
    if require_same_uid and hasattr(os, 'getuid') and item['uid'] != os.getuid():
        return False
    if expected_start is not None and str(expected_start) != item['start']:
        return False
    return True


def ancestor_pids(proc_root='/proc'):
    """Collect current process PID, ppid chain up to PID 1, and os.getppid()."""
    result = set()
    pid = os.getpid()
    for _ in range(64):
        if pid in result or pid <= 1:
            break
        result.add(pid)
        item = proc_identity(pid, proc_root=proc_root)
        if not item:
            break
        pid = item['ppid']
    result.add(os.getppid())
    return result


def load_sibling(anchor_file, filename, cache=None):
    """Import a sibling template module by filename without altering sys.path permanently."""
    path = Path(anchor_file).resolve().with_name(filename)
    key = str(path)
    if cache is not None and key in cache:
        return cache[key]
    mod_name = '_lemvo_' + filename.replace('.', '_').replace('-', '_')
    spec = importlib.util.spec_from_file_location(mod_name, key)
    if spec is None or spec.loader is None:
        raise OSError('Cannot load sibling helper: ' + key)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if cache is not None:
        cache[key] = mod
    return mod


def _strip_comment_scalar(raw):
    quote, chars = None, []
    for ch in raw.strip():
        if ch in ('"', "'"):
            if quote == ch:
                quote = None
            elif quote is None:
                quote = ch
        if ch == '#' and quote is None:
            break
        chars.append(ch)
    return ''.join(chars).strip().strip("'\"")


def parse_budget_yaml(state_path):
    """Parse indented budget: keys from research-state.yaml (comment-aware)."""
    out = {}
    p = Path(state_path)
    if not p.is_file():
        return out
    top = ''
    try:
        lines = p.read_text(encoding='utf-8', errors='replace').splitlines()
    except OSError:
        return out
    for raw in lines:
        line = re.sub(r'\s+#.*$', '', raw).rstrip()
        m = re.match(r'^(\s*)([A-Za-z_][\w-]*)\s*:\s*(.*)$', line)
        if not m:
            continue
        indent, key, val = len(m.group(1)), m.group(2), m.group(3).strip().strip('"\'')
        if indent == 0:
            top = key
        elif top == 'budget' and indent == 2:
            out[key] = val
    return out


def budget_exhausted(root):
    """True only when unit=usd, cap_authorized=yes, provenance valid, and used >= cap."""
    b = parse_budget_yaml(Path(root) / 'research-state.yaml')
    if b.get('unit', '').strip().lower() != 'usd':
        return False
    auth = b.get('cap_authorized', '').strip().lower()
    src = b.get('cap_source', '').strip().lower()
    if auth != 'yes' or src in INVALID_CAP_SOURCES:
        return False
    try:
        cap = float(b.get('total_cap', ''))
        used = float(b.get('used', ''))
    except (ValueError, TypeError):
        return False
    return used >= cap


def contract_gate_state(root, include_resource=True, check_pause_files=False):
    """Inspect WAIT/HARD/DONE/HUMAN status consistently across runtime & owner."""
    root = Path(root)
    state_path = root / 'research-state.yaml'
    statuses = set()
    runtime_wait = {}
    if state_path.is_file():
        top = ''
        try:
            lines = state_path.read_text(encoding='utf-8', errors='replace').splitlines()
        except OSError:
            lines = []
        for raw in lines:
            m = re.match(r'^(\s*)([\w-]+)\s*:\s*(.*)$', raw)
            if not m:
                continue
            indent, key, raw_val = len(m.group(1)), m.group(2), m.group(3)
            val = _strip_comment_scalar(raw_val)
            if indent == 0:
                top = key
                if key in ('campaign_status', 'measurement_state', 'root_status', 'status') and val:
                    statuses.add(val.upper())
            elif indent == 2:
                if top == 'project' and key == 'status' and val:
                    statuses.add(val.upper())
                elif top == 'runtime_wait' and key in ('question', 'question_id'):
                    runtime_wait[key] = val
    markers = {n for n in ('STOP', 'HOLD', 'BLOCKED-USER', 'CAMPAIGN-DONE') if (root / n).is_file()}
    values = statuses | markers
    if values & HARD_STATUSES or (check_pause_files and ((root / 'PAUSE').is_file() or (root / '.runtime-events/PAUSE').is_file())):
        return 'hard', '|'.join(sorted(values & HARD_STATUSES)) or 'PAUSE', runtime_wait
    if values & DONE_STATUSES:
        return 'done', '|'.join(sorted(values & DONE_STATUSES)), runtime_wait
    if values & HUMAN_STATUSES:
        return 'human', '|'.join(sorted(values & HUMAN_STATUSES)), runtime_wait
    if include_resource and 'WAITING_RESOURCE' in values:
        return 'resource', 'resource', runtime_wait
    return 'ready', 'ready', runtime_wait


def has_pending_steer(root):
    """Return True if any queued steer guidance file is waiting."""
    qdir = Path(root) / '.lemvo-steer-queue'
    if not qdir.is_dir():
        return False
    return any(True for _ in qdir.glob('q-*.md'))
