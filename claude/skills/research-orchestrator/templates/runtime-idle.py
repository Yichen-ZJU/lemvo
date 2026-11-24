#!/usr/bin/env python3
"""CPU-only idle handoff for Lemvo; no model, job or scientific decisions.

Public API: scientific_state_text(text), scientific_snapshot(root, context_files=None),
settle(root, turn_dir), check(root, wake=None). From a project cwd:
  runtime-idle.py capture-worker turns/TURN
  runtime-idle.py settle turns/TURN
  runtime-idle.py check-shell [--wake REAL_CONTROLLER_EVENT]
  runtime-idle.py check-json [--wake REAL_CONTROLLER_EVENT]

Checks are read-only. Only successful settlement changes the durable baseline.
Existing controller locks, event delivery and failure backoff retain ownership.
"""
import argparse
import contextlib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import runpy
import shlex
import sys
import tempfile
import time


STATE_FILE = '.lemvo-idle-state.json'
WORKER_SNAPSHOT = 'idle-worker-snapshot.json'
MAX_SOURCE_BYTES = 16 * 1024 * 1024
DEFAULT_SOURCES = ('CHARTER.md', 'research-state.yaml', 'STATE.md', 'findings.md',
                   'task-profile.md', 'task/TASK.md', 'champion/champion.md')
# Exact controller-owned paths, not an exclusion of whole budget/prep sections.
ACCOUNTING_PATHS = frozenset((section, key) for section, keys in (
    ('budget', ('used', 'remaining', 'last_budget_turn')),
    ('prep', ('prep_turns', 'prep_wall_time_min', 'prep_cost', 'session_id',
              'last_seen_observation', 'last_artifact_sha', 'last_settled_turn')),
    ('total', ('total_turns', 'total_wall_time_min', 'total_tokens', 'total_tokens_in',
               'total_tokens_out', 'total_cost', 'cost_usd_total',
               'last_accounted_turn', 'last_budget_turn')),
    ('gpu_usage', ('used_hours', 'basis', 'source', 'migrated')),
) for key in keys)
# These exact neutral values are inserted by the accounting helper when
# a worker rewrites a short research-state document. Non-default values remain.
NEUTRAL_PREP_PATHS = frozenset(('prep', key) for key in (
    'last_informative_observation', 'last_informative_artifact',
    'ready_blocker', 'next_min_probe'))
ACCOUNTING_SECTIONS = ('prep', 'total', 'gpu_usage')
IGNORED_DIRECTORIES = frozenset(('turns', 'logs', 'log', 'ledger', 'ledgers',
    '__pycache__', 'node_modules', 'checkpoints', 'models', 'datasets', 'data',
    'backups', 'poisoned-archive'))
IGNORED_NAMES = frozenset(('work-result.json', 'work-settlement.json', 'work-unit.json',
    'context-plan.json', 'context-prompt.md', 'context-handoff.md', 'usage.json',
    'usage.jsonl', 'cost_summary.json', 'ledger.json', 'ledger.jsonl', 'heartbeat.json',
    'owner_heartbeat.json', 'runtime-heartbeat.json', 'engine-stdout.jsonl',
    'engine-stderr.log', 'campaign_log.txt', 'research-log.md', 'CHANGELOG.md'))
BINARY_SUFFIXES = frozenset(('.bin', '.pt', '.pth', '.ckpt', '.safetensors', '.onnx',
    '.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.tiff', '.avif', '.mp4', '.wav',
    '.mp3', '.npy', '.npz', '.parquet', '.sqlite', '.sqlite3', '.db', '.zip', '.gz'))
KEY_LINE = re.compile(r"^( *)([A-Za-z_][\w-]*|'(?:[^']|'')+'|\"(?:[^\"\\]|\\.)+\")\s*:\s*(.*)$")


def _load(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return default


def _atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, str(path))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextlib.contextmanager
def _settlement_lock(root):
    with (root / (STATE_FILE + '.lock')).open('a+b') as stream:
        if os.name == 'nt':
            import msvcrt
            stream.seek(0)
            if not stream.read(1):
                stream.write(b' ')
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == 'nt':
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def _strip_json_accounting(value, path=()):
    if isinstance(value, dict):
        return {k: _strip_json_accounting(v, path + (str(k),)) for k, v in value.items()
                if path + (str(k),) not in ACCOUNTING_PATHS}
    if isinstance(value, list):
        return [_strip_json_accounting(v, path + ('[]',)) for v in value]
    return value


def scientific_state_text(raw_text, normalize_defaults=False):
    """Remove exact controller accounting keys from the supported YAML mapping.

    This is not a general YAML parser. Other fields and block scalar contents are
    preserved; in particular objectives and authorization/budget caps stay visible.
    JSON (valid YAML) is handled using the stdlib JSON parser.
    """
    raw_text = str(raw_text).replace('\r\n', '\n').lstrip('\ufeff')
    try:
        parsed = json.loads(raw_text)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        fields = _strip_json_accounting(parsed)
        if normalize_defaults and isinstance(fields.get('prep'), dict):
            for _, key in NEUTRAL_PREP_PATHS:
                if fields['prep'].get(key) == 'none':
                    fields['prep'].pop(key)
        if normalize_defaults:
            for key in ACCOUNTING_SECTIONS:
                if fields.get(key) == {}:
                    fields.pop(key)
        return json.dumps(fields, ensure_ascii=False, sort_keys=True)
    stack, lines = [], []
    skipped_indent = None
    scalar_indent = None
    for line in raw_text.splitlines():
        indent = len(line) - len(line.lstrip(' '))
        if skipped_indent is not None:
            if not line.strip() or indent > skipped_indent:
                continue
            skipped_indent = None
        if scalar_indent is not None:
            if not line.strip() or indent > scalar_indent:
                lines.append(line.rstrip())
                continue
            scalar_indent = None
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        match = KEY_LINE.match(line)
        if not match:
            lines.append(line.rstrip())
            continue
        spaces, key, value = match.groups()
        indent = len(spaces)
        while stack and stack[-1][0] >= indent:
            stack.pop()
        if key.startswith('"'):
            try:
                key = json.loads(key)
            except ValueError:
                key = key[1:-1]
        elif key.startswith("'"):
            key = key[1:-1].replace("''", "'")
        path = tuple(item[1] for item in stack) + (key,)
        if path in ACCOUNTING_PATHS:
            skipped_indent = indent
            continue
        if normalize_defaults and path in NEUTRAL_PREP_PATHS and value.strip() == 'none':
            continue
        lines.append(line.rstrip())
        if not value.strip() or value.lstrip().startswith('#'):
            stack.append((indent, key))
        elif re.match(r'^[>|][+\-0-9]*(?:\s|$)', value):
            scalar_indent = indent
    if normalize_defaults:
        empty_sections = {section + ':' for section in ACCOUNTING_SECTIONS}
        lines = [line for i, line in enumerate(lines)
                 if line not in empty_sections or (i + 1 < len(lines) and lines[i + 1].startswith(' '))]
    return '\n'.join(lines) + ('\n' if lines else '')


def _names(value):
    if isinstance(value, (str, Path)):
        return [str(value)]
    if isinstance(value, dict):
        if isinstance(value.get('path'), str):
            return [value['path']]
        return _names(value.get('files', []))
    if isinstance(value, (list, tuple)):
        return [name for item in value for name in _names(item)]
    return []


def _source_path(root, name):
    path = Path(name)
    if not path.is_absolute():
        path = root / path
    path = path.resolve()
    try:
        relative = path.relative_to(root)
    except ValueError:
        relative = path
    parts = relative.parts
    if any(part.startswith('.') or part.lower() in IGNORED_DIRECTORIES for part in parts):
        return None
    if path.name in IGNORED_NAMES or path.suffix.lower() in BINARY_SUFFIXES or path.suffix.lower() == '.log':
        return None
    return path


def scientific_snapshot(root, context_files=None, normalize_defaults=False):
    """Hash only known source/explicit artifact content, never mtimes or ledgers."""
    root = Path(root).resolve()
    names = list(DEFAULT_SOURCES)
    if context_files is None:
        names += _names((_load(root / '.lemvo-context.json', {}) or {}).get('files', []))
    else:
        names += _names(context_files)
    names += _names(_load(root / '.lemvo-progress-paths.json', []))
    paths = set()
    for name in names:
        path = _source_path(root, name)
        if path is not None:
            paths.add(path)
    paths = sorted(paths, key=str)
    files, included = {}, []
    for path in paths:
        try:
            key = str(path.relative_to(root))
        except ValueError:
            key = str(path)
        key = key.replace('\\', '/')
        if not path.exists():
            files[key] = {'state': 'absent'}
            included.append(str(path))
            continue
        try:
            if not path.is_file() or path.stat().st_size > MAX_SOURCE_BYTES:
                continue
            content = path.read_bytes()
            if b'\0' in content:
                continue
            if path.name in ('research-state.yaml', 'research-state.yml'):
                yaml_text = content.decode('utf-8-sig', errors='replace')
                content = scientific_state_text(yaml_text, normalize_defaults).encode('utf-8')
            files[key] = {'state': 'present', 'sha256': hashlib.sha256(content).hexdigest()}
            included.append(str(path))
        except OSError as exc:
            files[key] = {'state': 'unreadable', 'error': type(exc).__name__}
            included.append(str(path))
    return _snapshot_record(files, included, schema=2 if normalize_defaults else 1)


def _snapshot_record(files, paths, schema=1):
    canonical = json.dumps(files, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return {'schema': schema, 'files': files, 'fingerprint': hashlib.sha256(canonical.encode()).hexdigest(),
            'context_files': paths}


_MOD_CACHE = {}


def _load_sibling(name):
    path = Path(__file__).resolve().with_name(name)
    key = str(path)
    mod = _MOD_CACHE.get(key)
    if mod is None:
        spec = importlib.util.spec_from_file_location('_lemvo_' + name.replace('.', '_').replace('-', '_'), key)
        if spec is None or spec.loader is None:
            raise OSError('Cannot load sibling helper: ' + key)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _MOD_CACHE[key] = mod
    return mod


def _declared_condition(root):
    """Observe only the installed pure declarative wait_condition; no wait_check."""
    if not (root / '.lemvo-wait.json').exists():
        return {'reason': '', 'identity': 'unspecified'}
    previous = Path.cwd()
    try:
        support = _load_sibling('runtime-support.py')
        os.chdir(root)
        reason, identity = support.wait_condition(time.time())
        return {'reason': str(reason), 'identity': str(identity)}
    except (OSError, KeyError, TypeError, ValueError) as exc:
        digest = hashlib.sha256((root / '.lemvo-wait.json').read_bytes()).hexdigest()
        return {'reason': 'wait-observer-unavailable', 'identity': digest,
                'error': type(exc).__name__}
    finally:
        os.chdir(previous)


def capture_worker(root, td):
    """Save the completed worker's view before controller acknowledgement."""
    root = Path(root).resolve()
    td = root / td
    plan = _load(td / 'context-plan.json', {}) or {}
    work = _load(td / 'work-result.json', {}) or {}
    names = _names(plan.get('files', [])) + _names(work.get('artifact_refs', []))
    try:
        delivered_condition = json.loads(os.environ.get('LEMVO_IDLE_DELIVERED_CONDITION') or 'null')
    except (ValueError, TypeError):
        delivered_condition = None
    _atomic(td / WORKER_SNAPSHOT, {
        'turn_id': td.name,
        'scientific_snapshot': scientific_snapshot(root, names, normalize_defaults=True),
        'delivered_condition': delivered_condition,
    })


def _legacy_snapshot(root, plan, previous):
    """Recover old turns using recorded inputs and the last known baseline."""
    if previous.get('schema') != 1:
        previous = {}
    files = dict(previous.get('files', {}))
    paths = set(previous.get('context_files', []))
    for name in DEFAULT_SOURCES:
        path = _source_path(root, name)
        if path is not None:
            paths.add(str(path))
    for entry in plan.get('files', []):
        if not isinstance(entry, dict) or not entry.get('path'):
            continue
        path = _source_path(root, entry['path'])
        basis = ('scientific-state-content' if path and path.name in
                 ('research-state.yaml', 'research-state.yml') else 'full-file')
        if path is None or entry.get('fingerprint_basis') != basis or not entry.get('fingerprint'):
            continue
        key = os.path.relpath(str(path), str(root)) if root in path.parents else str(path)
        files[key.replace(os.sep, '/')] = {'state': 'present', 'sha256': entry['fingerprint']}
        paths.add(str(path))
    return _snapshot_record(files, sorted(paths))


def settle(root, td):
    """Arm after successful waiting; success consumes a new snapshot, failure doesn't."""
    root = Path(root).resolve()
    td = Path(td)
    if not td.is_absolute():
        td = root / td
    result = _load(td / 'work-result.json', {}) or {}
    runtime = _load(td / 'result.json', {}) or {}
    status = result.get('status')
    engine_rc = result.get('engine_rc')
    if runtime.get('status') not in (None, 'succeeded', 'success', 'ok', 'completed', 'recovered-orphan') or runtime.get('engine_rc') not in (None, 0, '0'):
        return {'action': 'unchanged', 'reason': 'runtime-not-successful', 'turn_id': td.name}
    if status in ('failed', 'unknown') or engine_rc not in (None, 0, '0'):
        return {'action': 'unchanged', 'reason': 'work-not-successful', 'turn_id': td.name}
    if status not in ('waiting', 'completed', 'continue', 'partial', 'succeeded', 'success'):
        return {'action': 'unchanged', 'reason': 'no-structured-success', 'turn_id': td.name}
    settlement = _load(td / 'work-settlement.json', {}) or {}
    if settlement.get('promoted') is False:
        return {'action': 'unchanged', 'reason': 'older-dispatch-not-promoted', 'turn_id': td.name}
    plan = _load(td / 'context-plan.json', {}) or {}
    capture = _load(td / WORKER_SNAPSHOT, {}) or {}
    with _settlement_lock(root):
        old = _load(root / STATE_FILE, {}) or {}
        if old.get('turn_id') == td.name and old.get('settled') is True:
            return {'action': 'unchanged', 'reason': 'already-settled', 'turn_id': td.name}
        if capture.get('turn_id') == td.name:
            snapshot = capture['scientific_snapshot']
            delivered_condition = capture.get('delivered_condition')
        else:
            snapshot = _legacy_snapshot(root, plan, old.get('scientific_snapshot', {}))
            delivered_condition = None
        # Never sample readiness during confirmation. A ready condition becomes
        # acknowledged only when this successful turn received that exact event.
        condition = delivered_condition or old.get('declared_wait_condition')
        record = {'schema': 'lemvo-runtime-idle-v1', 'armed': status == 'waiting',
                  'turn_id': td.name, 'settled': True, 'work_status': status,
                  'reason': 'structured-waiting' if status == 'waiting' else 'successful-work',
                  'has_declared_wait': (root / '.lemvo-wait.json').exists(),
                  'has_jobs_file': (root / '.lemvo-jobs.json').exists(),
                  'scientific_snapshot': snapshot, 'context_files': snapshot['context_files'],
                  'declared_wait_condition': condition}
        _atomic(root / STATE_FILE, record)
    return {'action': 'armed' if record['armed'] else 'cleared',
            'reason': record['reason'], 'turn_id': td.name}


def check(root, wake=None):
    """Read-only dispatch advice. The controller owns real event consumption/retries."""
    root = Path(root).resolve()
    state = _load(root / STATE_FILE, {}) or {}
    if wake:
        return {'action': 'ready', 'reason': str(wake), 'armed': bool(state.get('armed'))}
    if state.get('armed') is not True:
        return {'action': 'ready', 'reason': 'not-armed', 'armed': False}
    baseline = state.get('scientific_snapshot', {}) or {}
    snapshot = scientific_snapshot(root, state.get('context_files', []),
                                   normalize_defaults=(baseline.get('schema') == 2))
    if snapshot.get('fingerprint') != baseline.get('fingerprint'):
        return {'action': 'ready', 'reason': 'scientific-source-changed', 'armed': True}
    condition = _declared_condition(root)
    if condition.get('reason') and condition != state.get('declared_wait_condition'):
        return {'action': 'ready', 'reason': condition['reason'], 'armed': True,
                'condition': condition}
    return {'action': 'wait', 'reason': 'unchanged-scientific-wait', 'armed': True,
            'turn_id': state.get('turn_id')}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default='.')
    sub = parser.add_subparsers(dest='command')
    for name in ('settle', 'capture-worker'):
        item = sub.add_parser(name)
        item.add_argument('--root', default=argparse.SUPPRESS)
        item.add_argument('turn_dir', type=Path)
    for name in ('check-shell', 'check-json'):
        item = sub.add_parser(name)
        item.add_argument('--root', default=argparse.SUPPRESS)
        item.add_argument('--wake')
        item.add_argument('--auto-wake', action='store_true')
    args = parser.parse_args(argv)
    if args.command is None:
        parser.error('a command is required')
    if args.command == 'capture-worker':
        capture_worker(args.root, args.turn_dir)
        return 0
    if args.command == 'settle':
        result = settle(args.root, args.turn_dir)
    else:
        wake = args.wake
        if not wake and args.auto_wake:
            root_path = Path(args.root).resolve()
            idle_state = _load(root_path / STATE_FILE, {}) or {}
            if idle_state.get('armed') is True:
                previous = Path.cwd()
                try:
                    contract = _load_sibling('runtime-contract.py')
                    os.chdir(root_path)
                    if contract.has_guidance():
                        wake = 'guidance'
                except (ValueError, OSError, KeyError, TypeError) as exc:
                    print('Runtime contract error: ' + str(exc), file=sys.stderr)
                    return 3
                finally:
                    os.chdir(previous)
                if not wake:
                    receipts_dir = root_path / '.lemvo-receipts'
                    if receipts_dir.is_dir():
                        for fn in sorted(os.listdir(receipts_dir)):
                            if not fn.endswith('.json'):
                                continue
                            rec = _load(receipts_dir / fn, None)
                            if isinstance(rec, dict) and not rec.get('consumed'):
                                wake = 'job-receipt'
                                break
        result = check(args.root, wake)
    if args.command == 'check-shell':
        print('IDLE_ACTION=' + shlex.quote(result['action']))
        print('IDLE_REASON=' + shlex.quote(result['reason']))
        print('IDLE_CONDITION=' + shlex.quote(json.dumps(result.get('condition'))))
    else:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
