#!/usr/bin/env python3
"""Project-local job facts and idempotent receipts. No model or process control.

A finished branch can wake the researcher while other producers remain alive.
Receipts represent execution observations, never a scientific verdict.
Python 3.6+ stdlib only.
"""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import tempfile
import time

try:
    import fcntl
except ImportError:
    fcntl = None

SENTINEL_KEYS = ('done_file', 'done_path', 'terminal_receipt', 'wait_file')
RESULT_KEYS = ('result_path', 'result_file')
FAILED_KEYS = ('failed_file', 'failed_path')
TERMINAL_REGISTRY_STATUSES = (
    'succeeded', 'success', 'ok', 'complete', 'completed',
    'failed', 'error', 'oom', 'crashed', 'timed_out', 'ended', 'stale',
)
_NON_TERMINAL_JSON_STATUSES = (
    'running', 'pending', 'queued', 'active', 'in_progress', 'loading', 'training',
)
_GENERIC_DIR_NAMES = frozenset((
    'tmp', 'temp', 'work', 'workspace', 'sub', 'logs', 'log', 'outputs', 'output',
    'data', 'home', 'root', 'runs', 'run', 'test', 'tests', 'agents',
))
_SENTINEL_META_KEYS = frozenset((
    'run_id', 'status', 'exit_code', 'pid', 'pid_start', 'attempt_id',
    'resume_pid', 'resume_pid_start', 'resume_attempt_id',
    'settings', 'split', 'comparison', 'evidence_scope', 'seed', 'eval_contract',
))


def atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=1)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, str(path))
    finally:
        if os.path.exists(name):
            os.unlink(name)


def load(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError, UnicodeError):
        return default


@contextmanager
def locked(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        if fcntl:
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
            if fcntl:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            else:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def identity(pid):
    try:
        pid = int(pid)
        if pid <= 0:
            return None
        p = Path('/proc') / str(pid)
        fields = (p / 'stat').read_text().rsplit(')', 1)[1].split()
        return {'pid': pid, 'start': fields[19], 'state': fields[0],
                'uid': p.stat().st_uid}
    except (OSError, ValueError, TypeError, IndexError):
        return None


def _alive_identity(pid, expected_start=None):
    if not pid:
        return False
    item = identity(pid)
    return bool(
        item
        and item['state'] not in ('Z', 'X')
        and (not expected_start or str(expected_start) == item['start'])
    )


def alive(job):
    expected = job.get('pid_start') or job.get('start')
    return _alive_identity(job.get('pid'), expected)


def _proc_start_epoch(start_ticks):
    if not start_ticks:
        return None
    try:
        boot = next(
            int(s.split()[1])
            for s in Path('/proc/stat').read_text().splitlines()
            if s.startswith('btime ')
        )
        return boot + float(start_ticks) / os.sysconf('SC_CLK_TCK')
    except (OSError, ValueError, TypeError, StopIteration, AttributeError):
        return None


def _to_float_opt(val):
    if isinstance(val, bool) or val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _corroborated_unbound_resume(pid, job):
    """Verify an unbound resume_pid (lacking resume_pid_start) against /proc cmdline tokens."""
    item = identity(pid)
    if not item or item['state'] in ('Z', 'X'):
        return None
    if hasattr(os, 'getuid') and item.get('uid') != os.getuid():
        return None
    ref_epoch = _to_float_opt(job.get('resume_started_epoch'))
    if ref_epoch is None:
        ref_epoch = _to_float_opt(job.get('started_epoch'))
    proc_epoch = _proc_start_epoch(item['start'])
    if ref_epoch is not None and proc_epoch is not None and proc_epoch < ref_epoch - 60.0:
        return None
    try:
        cmd_raw = (Path('/proc') / str(item['pid']) / 'cmdline').read_bytes()
    except OSError:
        return None
    cmd_args = [a.decode('utf-8', 'replace') for a in cmd_raw.split(b'\0') if a]
    cmd_text = ' '.join(cmd_args)
    if not cmd_text.strip():
        return None

    rcmd = job.get('resume_cmdline')
    if isinstance(rcmd, str) and rcmd.strip():
        rcmd_s = rcmd.strip()
        tail = os.path.basename(rcmd_s.split()[-1])
        if rcmd_s in cmd_text or (len(tail) >= 4 and tail in cmd_text):
            return item
        return None

    tokens = []
    for k in ('run_id', 'job_id'):
        v = job.get(k)
        if isinstance(v, str) and len(v.strip()) >= 4:
            tokens.append(v.strip())
    for k in ('recovery', 'producer_cmdline'):
        v = job.get(k)
        if isinstance(v, str) and v.strip():
            for scr in re.findall(r'[A-Za-z0-9_.-]+\.(?:sh|py)\b', v):
                if scr.lower() not in ('python.py', 'python3.py'):
                    tokens.append(scr)
    for k in SENTINEL_KEYS + RESULT_KEYS + FAILED_KEYS + ('progress_path', 'progress_file'):
        v = job.get(k)
        if isinstance(v, str) and v.strip():
            pname = Path(v.strip()).parent.name
            if len(pname) >= 4 and pname.lower() not in _GENERIC_DIR_NAMES:
                tokens.append(pname)

    if not tokens:
        return None
    for tok in tokens:
        if tok in cmd_text:
            return item
    return None


def active_attempt(job):
    """Return canonical active producer attempt identity (preferring verified resumed attempt)."""
    orig_pid = job.get('pid')
    orig_start = job.get('pid_start') or job.get('start')
    orig_aid = job.get('attempt_id')
    if job.get('resume_pid'):
        rpid = job.get('resume_pid')
        rstart = job.get('resume_pid_start') or job.get('resume_start')
        raid = job.get('resume_attempt_id') or orig_aid
        r_epoch = job.get('resume_started_epoch')
        if rstart:
            return {
                'pid': rpid,
                'pid_start': str(rstart),
                'attempt_id': raid,
                'resumed': True,
                'bound': True,
                'alive': _alive_identity(rpid, rstart),
                'started_epoch': r_epoch,
                'unbound_resume_pid': False,
            }
        corrob = _corroborated_unbound_resume(rpid, job)
        if corrob:
            return {
                'pid': rpid,
                'pid_start': corrob['start'],
                'attempt_id': raid,
                'resumed': True,
                'bound': True,
                'corroborated': True,
                'alive': True,
                'started_epoch': r_epoch,
                'unbound_resume_pid': False,
            }
        return {
            'pid': orig_pid,
            'pid_start': orig_start,
            'attempt_id': orig_aid,
            'resumed': False,
            'bound': bool(orig_start),
            'alive': _alive_identity(orig_pid, orig_start),
            'started_epoch': job.get('started_epoch'),
            'unbound_resume_pid': True,
        }
    return {
        'pid': orig_pid,
        'pid_start': orig_start,
        'attempt_id': orig_aid,
        'resumed': False,
        'bound': bool(orig_start),
        'alive': _alive_identity(orig_pid, orig_start),
        'started_epoch': job.get('started_epoch'),
        'unbound_resume_pid': False,
    }


def producer_started(job, attempt=None):
    if attempt is None:
        attempt = active_attempt(job)
    expected = attempt.get('pid_start')
    epoch = _proc_start_epoch(expected)
    if epoch is not None:
        return epoch
    if attempt.get('resumed'):
        return _to_float_opt(attempt.get('started_epoch'))
    return None


def _matches_resumed_attempt(obj, attempt):
    obj_pid = obj.get('pid') if obj.get('pid') is not None else obj.get('resume_pid')
    obj_pstart = obj.get('pid_start') if obj.get('pid_start') is not None else obj.get('resume_pid_start')
    obj_aid = obj.get('attempt_id') if obj.get('attempt_id') is not None else obj.get('resume_attempt_id')
    if attempt.get('attempt_id') is not None and obj_aid is not None:
        return str(obj_aid) == str(attempt['attempt_id'])
    if attempt.get('pid_start') is not None and obj_pstart is not None:
        return str(obj_pstart) == str(attempt['pid_start']) and (
            obj_pid is None or str(obj_pid) == str(attempt.get('pid'))
        )
    if attempt.get('pid') is not None and obj_pid is not None and obj_pstart is None and obj_aid is None:
        return str(obj_pid) == str(attempt['pid'])
    return False


def _explicit_attempt_match(obj, attempt):
    """True when marker JSON explicitly names a matching attempt_id / pid_start / pid."""
    if not isinstance(obj, dict):
        return False
    obj_aid = obj.get('attempt_id') if obj.get('attempt_id') is not None else obj.get('resume_attempt_id')
    obj_pstart = obj.get('pid_start') if obj.get('pid_start') is not None else obj.get('resume_pid_start')
    obj_pid = obj.get('pid') if obj.get('pid') is not None else obj.get('resume_pid')
    att_aid = attempt.get('attempt_id')
    att_pstart = attempt.get('pid_start')
    att_pid = attempt.get('pid')
    if att_aid is not None and obj_aid is not None:
        if str(obj_aid) != str(att_aid):
            return False
        if att_pstart is not None and obj_pstart is not None and str(obj_pstart) != str(att_pstart):
            return False
        if att_pid is not None and obj_pid is not None and str(obj_pid) != str(att_pid):
            return False
        return True
    if att_pstart is not None and obj_pstart is not None:
        return str(obj_pstart) == str(attempt['pid_start']) and (
            obj_pid is None or str(obj_pid) == str(att_pid)
        )
    if att_pid is not None and obj_pid is not None:
        return str(obj_pid) == str(att_pid)
    return False


def _extract_metrics_payload(obj):
    if not isinstance(obj, dict):
        return None
    m = obj.get('metrics')
    if isinstance(m, (dict, list)) and m:
        return m
    r = obj.get('result')
    if isinstance(r, dict) and r:
        return r
    extra = {k: v for k, v in obj.items() if k not in _SENTINEL_META_KEYS}
    return extra if extra else None


def marker(path, job, running, allow_plain_text_sentinel=True, is_failed_marker=False,
           attempt=None, default_succeeded_without_status=False):
    """Return an owned, complete producer result or an explicit observation."""
    if not path:
        return None
    if attempt is None:
        attempt = active_attempt(job)
    p = Path(path)
    try:
        stat = p.stat()
        if not p.is_file() or stat.st_size == 0:
            return None
        started = producer_started(job, attempt)
        if started is not None and not job.get('accept_cached_markers'):
            tol = 0.0 if attempt.get('resumed') else 1.0
            if stat.st_mtime < started - tol:
                return {'state': 'old-marker'}
        with p.open(encoding='utf-8', errors='replace') as stream:
            raw = stream.read(262145)
    except OSError:
        return None
    if not raw.strip():
        return None
    if len(raw) > 262144:
        return {'state': 'incomplete', 'result': {'unparsed_result': str(p),
                                                 'reason': 'result-size-bound'}}
    try:
        obj = json.loads(raw)
    except ValueError:
        if raw.lstrip()[:1] in ('{', '['):
            return {'state': 'incomplete', 'result': {'corrupt_result': str(p)}}
        if is_failed_marker and attempt.get('resumed') and running:
            pid_s = str(attempt.get('pid') or '')
            aid_s = str(attempt.get('attempt_id') or '')
            if (not pid_s or pid_s not in raw) and (not aid_s or aid_s not in raw):
                return {'state': 'old-marker'}
        if not allow_plain_text_sentinel:
            return {'state': 'non-terminal',
                    'result': {'non_terminal_result': str(p), 'reason': 'non_json_result_path'}}
        return {'state': 'succeeded', 'result': {'sentinel': 'non-json'}, 'attributed': False,
                'fingerprint': hashlib.sha256(raw.encode()).hexdigest()}
    if not isinstance(obj, dict):
        return {'state': 'incomplete', 'result': {'unparsed_result': str(p)}}

    rid = job.get('run_id')
    if rid and obj.get('run_id') and str(rid) != str(obj['run_id']):
        return {'state': 'stale', 'result': {'run_id': obj.get('run_id'),
                                            'status': obj.get('status')}}

    obj_pid = obj.get('pid') if obj.get('pid') is not None else obj.get('resume_pid')
    obj_pstart = obj.get('pid_start') if obj.get('pid_start') is not None else obj.get('resume_pid_start')
    obj_aid = obj.get('attempt_id') if obj.get('attempt_id') is not None else obj.get('resume_attempt_id')
    if obj_aid is not None and attempt.get('attempt_id') is not None and str(obj_aid) != str(attempt['attempt_id']):
        return {'state': 'stale', 'result': {'attempt_id': obj_aid, 'status': obj.get('status')}}
    if obj_pstart is not None and attempt.get('pid_start') is not None and str(obj_pstart) != str(attempt['pid_start']):
        return {'state': 'stale', 'result': {'pid_start': obj_pstart, 'status': obj.get('status')}}
    if obj_pid is not None and attempt.get('pid') is not None and str(obj_pid) != str(attempt['pid']):
        return {'state': 'stale', 'result': {'pid': obj_pid, 'status': obj.get('status')}}

    if is_failed_marker and attempt.get('resumed') and running and not _matches_resumed_attempt(obj, attempt):
        return {'state': 'old-marker'}

    status = str(obj.get('status', '')).lower()
    code = obj.get('exit_code')
    is_int_code = isinstance(code, int) and not isinstance(code, bool)
    state = 'unknown'
    if status in ('succeeded', 'success', 'ok', 'complete', 'completed') and (code is None or (is_int_code and code == 0)):
        state = 'succeeded'
    elif status in ('failed', 'error', 'oom', 'crashed') or (is_int_code and code != 0):
        state = 'failed'
    elif status in _NON_TERMINAL_JSON_STATUSES:
        state = 'non-terminal'
    elif default_succeeded_without_status and not status and (code is None or (is_int_code and code == 0)):
        state = 'succeeded'

    result = {k: obj[k] for k in ('run_id', 'status', 'exit_code', 'settings',
                                  'split', 'comparison', 'evidence_scope', 'seed', 'eval_contract') if k in obj}
    return {
        'state': state,
        'exit_code': code if is_int_code else None,
        'result': result,
        'metrics': _extract_metrics_payload(obj),
        'attributed': _explicit_attempt_match(obj, attempt),
        'fingerprint': hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
    }


def _select_path(job, keys):
    fallback = None
    for k in keys:
        v = job.get(k)
        if isinstance(v, str) and v.strip():
            s = v.strip()
            if fallback is None:
                fallback = s
            if Path(s).is_file():
                return s
    return fallback


def _nonempty_harvest_val(val):
    if val is None or isinstance(val, bool):
        return False
    if isinstance(val, str):
        return bool(val.strip())
    return True


def classify(job, now=None):
    now = time.time() if now is None else now
    external = bool(job.get('external_coordinator'))
    attempt = active_attempt(job)
    running = bool(not external and attempt.get('alive'))

    sentinel_path = _select_path(job, SENTINEL_KEYS)
    result_path = _select_path(job, RESULT_KEYS)
    if sentinel_path is not None:
        done_path = sentinel_path
        allow_plain_sentinel = True
    else:
        done_path = result_path
        allow_plain_sentinel = False
    failed_path = _select_path(job, FAILED_KEYS)

    raw_status = str(job.get('status', '')).lower()
    row = {
        'name': job.get('name'),
        'run_id': job.get('run_id'),
        'pid': attempt.get('pid'),
        'pid_start': attempt.get('pid_start'),
    }
    if attempt.get('attempt_id') is not None:
        row['attempt_id'] = attempt.get('attempt_id')
    if attempt.get('resumed') and job.get('pid') is not None:
        row['original_pid'] = job.get('pid')
    if attempt.get('unbound_resume_pid'):
        row['unbound_resume_pid'] = True
    row.update(done_file=done_path, result_file=result_path, external=external, producer_alive=running)

    failed_obs = None
    if failed_path:
        cand = marker(
            failed_path, job, running,
            allow_plain_text_sentinel=True, is_failed_marker=True, attempt=attempt,
        )
        if cand and cand['state'] not in ('old-marker', 'incomplete', 'stale', 'unknown', 'non-terminal'):
            cand['state'] = 'failed'
            failed_obs = cand

    done_obs = None
    if done_path:
        done_obs = marker(
            done_path, job, running,
            allow_plain_text_sentinel=allow_plain_sentinel, is_failed_marker=False, attempt=attempt,
        )

    observed = None
    if failed_obs and done_obs and done_obs.get('state') == 'succeeded':
        if done_obs.get('attributed') and not failed_obs.get('attributed'):
            observed = done_obs
        else:
            observed = failed_obs
            row['failed_file'] = failed_path
    elif failed_obs:
        observed = failed_obs
        row['failed_file'] = failed_path
    elif done_obs:
        observed = done_obs

    # Enrich plain-text or sentinel done_file completion with separate result_file payload
    if observed and observed.get('state') == 'succeeded' and result_path and result_path != done_path:
        res_obs = marker(
            result_path, job, running,
            allow_plain_text_sentinel=False, is_failed_marker=False,
            attempt=attempt, default_succeeded_without_status=True,
        )
        if res_obs is None:
            if not running:
                observed.setdefault('result', {})['missing_result_file'] = result_path
        elif res_obs.get('state') in ('incomplete', 'non-terminal') and running:
            observed = {'state': 'running'}
        elif res_obs.get('state') == 'succeeded':
            if not observed.get('metrics') and res_obs.get('metrics'):
                observed['metrics'] = res_obs['metrics']
            if isinstance(res_obs.get('result'), dict):
                merged_res = dict(res_obs['result'])
                if isinstance(observed.get('result'), dict):
                    merged_res.update(observed['result'])
                observed['result'] = merged_res
            observed['fingerprint'] = hashlib.sha256(
                (str(observed.get('fingerprint') or '') + ':' + str(res_obs.get('fingerprint') or '')).encode()
            ).hexdigest()
        elif res_obs.get('state') == 'old-marker':
            observed.setdefault('result', {})['stale_result_file'] = result_path

    harvested = False
    if not running:
        if job.get('harvested') is True:
            harvested = True
        elif raw_status in TERMINAL_REGISTRY_STATUSES and (
            _nonempty_harvest_val(job.get('harvest_summary'))
            or _nonempty_harvest_val(job.get('harvest_cycle'))
        ):
            harvested = True

    if harvested:
        if attempt.get('resumed') and job.get('harvested_attempt_pid') != attempt.get('pid'):
            harvested = False
        elif job.get('harvested_fingerprint') and observed and observed.get('fingerprint') and job.get('harvested_fingerprint') != observed.get('fingerprint'):
            harvested = False
        elif job.get('harvested_run_id') and str(job.get('harvested_run_id')) != str(job.get('run_id')):
            harvested = False
        elif observed and observed.get('state') == 'failed' and raw_status in ('succeeded', 'success', 'ok', 'complete', 'completed'):
            harvested = False

    if harvested:
        row['already_harvested'] = True

    if observed:
        state = observed['state']
        if state in ('old-marker', 'stale', 'incomplete', 'unknown', 'non-terminal') and running:
            state = 'running'
        elif state == 'old-marker':
            state = 'stale'
        elif state == 'incomplete':
            state = 'failed' if attempt.get('pid') and not external else 'unknown'
        elif state == 'non-terminal':
            state = 'failed' if attempt.get('pid') and not external else 'running'
        if harvested and state in ('unknown', 'stale', 'old-marker'):
            if raw_status in ('succeeded', 'success', 'ok', 'complete', 'completed'):
                state = 'succeeded'
            elif raw_status in ('failed', 'error', 'oom', 'crashed'):
                state = 'failed'
        row.update({k: v for k, v in observed.items() if k not in ('state', 'attributed')})
        row['status'] = state
    elif running:
        row['status'] = 'running'
    elif attempt.get('pid') and not external:
        row['status'] = 'ended'
    elif harvested and raw_status in ('succeeded', 'success', 'ok', 'complete', 'completed'):
        row['status'] = 'succeeded'
    elif harvested and raw_status in ('failed', 'error', 'oom', 'crashed'):
        row['status'] = 'failed'
    elif harvested and raw_status in ('timed_out', 'ended', 'stale'):
        row['status'] = raw_status
    elif external:
        row['status'] = 'unknown'
    else:
        row['status'] = 'running'

    started = attempt.get('started_epoch') if attempt.get('resumed') and attempt.get('started_epoch') else job.get('started_epoch')
    if row['status'] == 'running' and started and job.get('timeout_min'):
        try:
            if now - float(started) > float(job['timeout_min']) * 60:
                row['status'] = 'timed_out'
        except (ValueError, TypeError):
            pass
    if (row['status'] == 'running' and (not attempt.get('pid') or not attempt.get('pid_start'))
            and started and not job.get('timeout_min')):
        try:
            if now - float(started) >= float(os.environ.get('LEMVO_UNKNOWN_JOB_SEC', '900')):
                row['status'] = 'unknown'
                if not row.get('result'):
                    row['result'] = {'reason': 'no_observable_producer_or_result',
                                     'done_file': row['done_file']}
        except (ValueError, TypeError):
            pass
    return row


def event_key(row):
    value = {k: row.get(k) for k in ('run_id', 'name', 'pid', 'pid_start', 'done_file',
                                    'result_file', 'failed_file', 'status', 'fingerprint', 'external')}
    if row.get('attempt_id') is not None:
        value['attempt_id'] = row.get('attempt_id')
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def publish(rows, directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with locked(directory / '.events.lock'):
        for row in rows:
            if row['status'] == 'running' or row.get('already_harvested'):
                continue
            key = event_key(row)
            p = directory / ('job-event-' + key + '.json')
            if p.exists():
                continue
            rec = {k: row.get(k) for k in ('name', 'run_id', 'status', 'pid', 'pid_start',
                                          'done_file', 'result_file', 'failed_file', 'exit_code', 'external',
                                          'metrics', 'result')}
            if row.get('attempt_id') is not None:
                rec['attempt_id'] = row.get('attempt_id')
            rec.update(schema_version=3, schema='lemvo-job-receipt', event_key=key,
                       trigger='job-terminal', host=socket.gethostname(),
                       ended_epoch=time.time(), consumed=False,
                       interpretation='Execution observation only; preserve tested conditions and live jobs.')
            atomic(p, rec)
            history = directory.parent / '.lemvo-jobs-history.jsonl'
            with history.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps({'ts': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                                         'event': 'terminal', 'name': row.get('name'),
                                         'status': row['status'], 'run_id': row.get('run_id'),
                                         'trigger': 'job-terminal', 'event_key': key}) + '\n')


def pending(directory):
    for p in Path(directory).glob('*.json'):
        r = load(p)
        if isinstance(r, dict) and not r.get('consumed', True):
            return True
    return False


def stamp_started(jobs_file):
    p = Path(jobs_file)
    if not p.exists():
        return False
    with locked(p.parent / '.jobs.lock'):
        jobs = load(p, None)
        if not isinstance(jobs, list):
            return False
        changed = False
        now = time.time()
        for j in jobs:
            if isinstance(j, dict) and not j.get('started_epoch'):
                j['started_epoch'] = now
                changed = True
        if changed:
            atomic(p, jobs)
        return changed


def reconcile(jobs_file, states_file, receipts, stamp_started_flag=False):
    if stamp_started_flag:
        stamp_started(jobs_file)
    p = Path(jobs_file)
    if p.exists():
        jobs = load(p, None)
        if jobs is None:
            try:
                if not p.read_text(encoding='utf-8').strip():
                    jobs = []
            except OSError:
                pass
        if jobs is None:
            time.sleep(.02)
            jobs = load(p, None)
        if not isinstance(jobs, list) or any(not isinstance(j, dict) for j in jobs):
            return 'CORRUPT'
    else:
        jobs = []
    rows = [classify(j) for j in jobs]
    atomic(states_file, rows)
    publish(rows, receipts)
    active = [r['name'] or '?' for r in rows
              if r['status'] == 'running' or r.get('producer_alive')]
    if active:
        return 'EVENTS' if pending(receipts) else 'PENDING ' + ','.join(active)
    if not rows and pending(receipts):
        return 'EVENTS'
    if any(r['status'] == 'timed_out' for r in rows):
        return 'TIMEOUT'
    if any(r['status'] in ('ended', 'failed', 'stale', 'unknown') for r in rows):
        return 'ENDED'
    return 'DONE'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command')
    p = sub.add_parser('reconcile')
    p.add_argument('--stamp-started', action='store_true')
    p.add_argument('jobs')
    p.add_argument('states')
    p.add_argument('receipts')
    p = sub.add_parser('publish')
    p.add_argument('states')
    p.add_argument('receipts')
    p = sub.add_parser('stamp-started')
    p.add_argument('jobs')
    args = parser.parse_args()
    if args.command == 'reconcile':
        print(reconcile(args.jobs, args.states, args.receipts, stamp_started_flag=args.stamp_started))
    elif args.command == 'publish':
        rows = load(args.states, [])
        publish(rows if isinstance(rows, list) else [], args.receipts)
    elif args.command == 'stamp-started':
        stamp_started(args.jobs)
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
