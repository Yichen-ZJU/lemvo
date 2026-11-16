#!/usr/bin/env python3
"""Project-local job facts and idempotent receipts. No model or process control.

A finished branch can wake the researcher while other producers remain alive.
Receipts represent execution observations, never a scientific verdict.
"""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import socket
import tempfile
import time

try:
    import fcntl
except ImportError:
    fcntl = None


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
                stream.write(b'0'); stream.flush()
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


def alive(job):
    item = identity(job.get('pid'))
    expected = job.get('pid_start') or job.get('start')
    return bool(item and item['state'] not in ('Z', 'X') and
                (not expected or str(expected) == item['start']))


def producer_started(job):
    # Process identity wins over mutable registration timestamps. Missing identity
    # retains legacy marker compatibility, without manufacturing an ownership claim.
    expected = job.get('pid_start') or job.get('start')
    if expected:
        try:
            boot = next(int(s.split()[1]) for s in Path('/proc/stat').read_text().splitlines()
                        if s.startswith('btime '))
            return boot + float(expected) / os.sysconf('SC_CLK_TCK')
        except (OSError, ValueError, TypeError, StopIteration, AttributeError):
            pass
    return None


def marker(path, job, running):
    """Return an owned, complete producer result or an explicit observation."""
    if not path:
        return None
    p = Path(path)
    try:
        stat = p.stat()
        if not p.is_file() or stat.st_size == 0:
            return None
        started = producer_started(job)
        if (started is not None and not job.get('accept_cached_markers')
                and stat.st_mtime < started - 1):
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
        # Existing plain-text completion sentinels remain compatible.
        return {'state': 'succeeded', 'result': {'sentinel': 'non-json'},
                'fingerprint': hashlib.sha256(raw.encode()).hexdigest()}
    if not isinstance(obj, dict):
        return {'state': 'incomplete', 'result': {'unparsed_result': str(p)}}
    rid = job.get('run_id')
    if rid and obj.get('run_id') and str(rid) != str(obj['run_id']):
        return {'state': 'stale', 'result': {'run_id': obj.get('run_id'),
                                            'status': obj.get('status')}}
    status = str(obj.get('status', '')).lower()
    code = obj.get('exit_code')
    state = 'unknown'
    if status in ('succeeded', 'success', 'ok', 'complete', 'completed') and (code is None or code == 0):
        state = 'succeeded'
    elif status in ('failed', 'error', 'oom', 'crashed') or (isinstance(code, int) and code != 0):
        state = 'failed'
    result = {k: obj[k] for k in ('run_id', 'status', 'exit_code', 'settings',
                                  'split', 'comparison', 'evidence_scope') if k in obj}
    return {'state': state, 'exit_code': code, 'result': result,
            'metrics': obj.get('metrics') if isinstance(obj.get('metrics'), (dict, list)) else None,
            'fingerprint': hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).hexdigest()}


def classify(job, now=None):
    now = time.time() if now is None else now
    external = bool(job.get('external_coordinator'))
    running = bool(job.get('pid') and not external and alive(job))
    row = {k: job.get(k) for k in ('name', 'run_id', 'pid', 'pid_start')}
    row.update(done_file=job.get('done_file') or job.get('wait_file'),
               external=external, producer_alive=running)
    observed = None
    if job.get('failed_file'):
        failed = marker(job['failed_file'], job, running)
        if failed and failed['state'] not in ('old-marker', 'incomplete', 'stale', 'unknown'):
            observed = failed
            observed['state'] = 'failed'
            row['failed_file'] = job['failed_file']
    if observed is None:
        observed = marker(row['done_file'], job, running)
    if observed:
        state = observed['state']
        if state in ('old-marker', 'stale', 'incomplete', 'unknown') and running:
            state = 'running'
        elif state == 'old-marker':
            state = 'stale'
        elif state == 'incomplete':
            state = 'failed' if job.get('pid') and not external else 'unknown'
        row.update({k: v for k, v in observed.items() if k not in ('state',)})
        row['status'] = state
    elif running:
        row['status'] = 'running'
    elif job.get('pid') and not external:
        row['status'] = 'ended'
    elif external:
        row['status'] = 'unknown'
    else:
        row['status'] = 'running'
    started = job.get('started_epoch')
    if row['status'] == 'running' and started and job.get('timeout_min'):
        try:
            if now - float(started) > float(job['timeout_min']) * 60:
                row['status'] = 'timed_out'
        except (ValueError, TypeError):
            pass
    if (row['status'] == 'running' and (not job.get('pid') or not job.get('pid_start'))
            and started and not job.get('timeout_min')):
        try:
            if now - float(started) >= float(os.environ.get('LEMVO_UNKNOWN_JOB_SEC', '900')):
                row['status'] = 'unknown'
                row['result'] = {'reason': 'no_observable_producer_or_result',
                                 'done_file': row['done_file']}
        except (ValueError, TypeError):
            pass
    return row


def event_key(row):
    # Timestamps of this observation are excluded: an unchanged dead producer
    # must not wake the model again on every polling tick or controller restart.
    value = {k: row.get(k) for k in ('run_id', 'name', 'pid', 'pid_start', 'done_file',
                                    'failed_file', 'status', 'fingerprint', 'external')}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def publish(rows, directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with locked(directory / '.events.lock'):
        for row in rows:
            if row['status'] == 'running':
                continue
            key = event_key(row)
            p = directory / ('job-event-' + key + '.json')
            if p.exists():
                continue  # Never reset a consumed receipt or partial consumer update.
            rec = {k: row.get(k) for k in ('name', 'run_id', 'status', 'pid', 'pid_start',
                                          'done_file', 'failed_file', 'exit_code', 'external',
                                          'metrics', 'result')}
            rec.update(schema_version=3, schema='lemvo-job-receipt', event_key=key,
                       trigger='job-terminal', host=socket.gethostname(),
                       ended_epoch=time.time(), consumed=False,
                       interpretation='Execution observation only; preserve tested conditions and live jobs.')
            atomic(p, rec)
            # Preserve the existing append-only execution history. The same
            # producer event is recorded once, regardless of poll/restart count.
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


def reconcile(jobs_file, states_file, receipts):
    p = Path(jobs_file)
    if p.exists():
        jobs = load(p, None)
        if jobs is None:
            try:
                if not p.read_text(encoding='utf-8').strip():
                    jobs = []  # Legacy empty journal is an empty registration.
            except OSError:
                pass
        if jobs is None:
            # A concurrent writer may be between writes. Persistent corruption
            # still follows the existing CORRUPT path; no model is called here.
            time.sleep(.02)
            jobs = load(p, None)
        if not isinstance(jobs, list) or any(not isinstance(j, dict) for j in jobs):
            return 'CORRUPT'
    else:
        jobs = []
    rows = [classify(j) for j in jobs]
    atomic(states_file, rows)
    publish(rows, receipts)
    # A timeout/result is an event, not authority to discard the still-live
    # producer identity. Its registration stays observable until it exits or the
    # researcher explicitly updates the journal; we never terminate it here.
    active = [r['name'] or '?' for r in rows
              if r['status'] == 'running' or r.get('producer_alive')]
    if active:
        return 'EVENTS' if pending(receipts) else 'PENDING ' + ','.join(active)
    if not rows and pending(receipts):
        return 'EVENTS'  # Restart still delivers receipts after the journal moved.
    # Keep the all-terminal archival contract. The shell archives the complete
    # journal, but write_receipts below is idempotent and never duplicates events.
    if any(r['status'] == 'timed_out' for r in rows):
        return 'TIMEOUT'
    if any(r['status'] in ('ended', 'failed', 'stale', 'unknown') for r in rows):
        return 'ENDED'
    return 'DONE'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command')
    p = sub.add_parser('reconcile')
    p.add_argument('jobs'); p.add_argument('states'); p.add_argument('receipts')
    p = sub.add_parser('publish')
    p.add_argument('states'); p.add_argument('receipts')
    args = parser.parse_args()
    if args.command == 'reconcile':
        print(reconcile(args.jobs, args.states, args.receipts))
    elif args.command == 'publish':
        rows = load(args.states, [])
        publish(rows if isinstance(rows, list) else [], args.receipts)
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
