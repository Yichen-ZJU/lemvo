from __future__ import annotations

import copy
import csv
import io
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import time

from .store import key, load, read_text, redact, tail
from .transcripts import metadata, TranscriptCache

UUID = re.compile(r'^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$', re.I)
ID = re.compile(r'^[A-Za-z0-9_.-]{1,100}$')
TERMINAL = {'succeeded', 'failed', 'cancelled', 'timed_out'}


def command(args, timeout=3):
    try:
        p = subprocess.run(args, capture_output=True, text=True, errors='replace', timeout=timeout)
        return p.stdout if p.returncode == 0 else ''
    except (OSError, subprocess.TimeoutExpired):
        return ''


class Processes:
    def __init__(self, root='/proc', uid=None):
        self.root = Path(root)
        self.uid = os.getuid() if uid is None else uid
        self.items = {}

    def scan(self):
        self.items = {}
        for p in self.root.iterdir():
            if not p.name.isdigit():
                continue
            try:
                if p.stat().st_uid != self.uid:
                    continue
                stat = read_text(p / 'stat').rsplit(')', 1)[1].split()
                args = (p / 'cmdline').read_bytes()[:16384].decode(errors='replace').strip('\0').split('\0')
                if not args or not args[0]:
                    continue
                self.items[int(p.name)] = {'pid': int(p.name), 'ppid': int(stat[1]), 'state': stat[0],
                    'start': stat[19], 'cpu_ticks': int(stat[11]) + int(stat[12]),
                    'rss_bytes': int(stat[21]) * os.sysconf('SC_PAGE_SIZE'),
                    'args': args, 'cwd': os.readlink(p / 'cwd')}
            except (OSError, ValueError, IndexError):
                continue
        return self.items

    def same(self, pid, start):
        try:
            p = self.items.get(int(pid))
            return bool(p and p['state'] != 'Z' and start and p['start'] == str(start))
        except (ValueError, TypeError):
            return False

    def descendants(self, pid):
        found, parents = [], {int(pid)}
        for _ in range(32):
            new = [p for p in self.items.values() if p['ppid'] in parents and p['pid'] not in parents]
            if not new:
                break
            found.extend(new)
            parents.update(p['pid'] for p in new)
        return found

    def ancestors(self, pid):
        out = []
        for _ in range(32):
            p = self.items.get(pid)
            if not p or p in out:
                break
            out.append(p)
            pid = p['ppid']
        return out

    def env(self, pid):
        try:
            raw = (self.root / str(pid) / 'environ').read_bytes()[:262144]
            return {k: v for k, _, v in (s.decode(errors='replace').partition('=') for s in raw.split(b'\0'))
                    if k in ('CLAUDE_CONFIG_DIR', 'CODEX_HOME')}
        except OSError:
            return {}

    def transcripts(self, pid):
        out = []
        try:
            for f in (self.root / str(pid) / 'fd').iterdir():
                try:
                    name = os.readlink(f)
                    if name.endswith('.jsonl') and ('/sessions/' in name or '/projects/' in name or name.endswith('/engine-stdout.jsonl')):
                        if Path(name).is_file():
                            out.append(name)
                except OSError:
                    continue
        except OSError:
            pass
        return sorted(set(out))


def engine_of(p):
    args = p['args']
    base = Path(args[0]).name.lower()
    if base in ('claude', 'claude.exe'):
        return 'claude'
    if base in ('codex', 'codex.exe') and 'pid-update-loop' not in args:
        return 'codex'
    if base in ('node', 'nodejs') and len(args) > 1:
        if '@anthropic-ai/claude-code' in args[1]:
            return 'claude'
        if '@openai/codex' in args[1]:
            return 'codex'
    return None


def session_hint(args):
    for i, a in enumerate(args):
        if a in ('--resume', '--session-id', 'resume') and i + 1 < len(args) and UUID.fullmatch(args[i+1]):
            return args[i+1]
        if a.startswith('--resume=') and UUID.fullmatch(a.split('=', 1)[1]):
            return a.split('=', 1)[1]
    # Shell launchers sometimes exec a wrapper function and lose --resume in the child argv.
    for a in args:
        m = re.search(r'(?:^|\s)--resume\s+([0-9a-f-]{36})(?:\s|$)', a)
        if m and UUID.fullmatch(m.group(1)):
            return m.group(1)
    return None


def session_files(home, engine, env, sid):
    root = Path(env.get('CLAUDE_CONFIG_DIR' if engine == 'claude' else 'CODEX_HOME',
                        str(home / ('.claude' if engine == 'claude' else '.codex'))))
    if engine == 'claude':
        return list((root / 'projects').glob('*/' + sid + '.jsonl'))[:8]
    return list((root / 'sessions').glob('*/*/*/*' + sid + '.jsonl'))[:8]


def tmux_panes():
    # Discovery only: never send keys or capture the terminal input buffer.
    fmt = '#{pane_pid}\t#{session_name}:#{window_index}.#{pane_index}\t#{pane_id}'
    out = {}
    for line in command(['tmux', 'list-panes', '-a', '-F', fmt]).splitlines():
        fields = line.split('\t')
        if len(fields) == 3 and fields[0].isdigit():
            out[int(fields[0])] = {'label': fields[1], 'id': fields[2]}
    return out


def scalar_status(project):
    text = read_text(project / '.lemvo-status', 2048).strip()
    return text.split('|', 1)[0].upper(), text


def project_info(project, processes):
    p = Path(project)
    owner_pid = read_text(p / '.lemvo-owner.lock/pid', 64).strip()
    owner_start = read_text(p / '.lemvo-owner.lock/starttime', 64).strip()
    owner = processes.same(owner_pid, owner_start)
    if owner:
        owner = any('lemvo-run' in a for a in processes.items[int(owner_pid)]['args'])
    st, text = scalar_status(p)
    return {'id': key(p), 'path': str(p), 'name': p.name, 'mode': read_text(p / '.lemvo-mode', 40).strip(),
            'status': st, 'status_detail': redact(text), 'owner_alive': owner,
            'owner_pid': int(owner_pid) if owner_pid.isdigit() else None,
            'owner_start': owner_start, 'managed': (p / '.lemvo-mode').exists(),
            'jobs': [], 'warning': None,
            'pending_guidance': len(list((p / '.lemvo-steer-queue').glob('q-*.md'))) }


def describe_job(item, project, processes, previous, now, stall_seconds, state_dir):
    run_id = str(item.get('run_id', ''))
    pid, start = item.get('pid'), item.get('pid_start', item.get('starttime'))
    alive = processes.same(pid, start)
    identity_missing = bool(pid and not start)
    status = str(item.get('status', 'unknown')).lower()
    job = {'id': key(project, run_id, pid), 'run_id': run_id, 'pid': pid, 'pid_start': start,
           'session_id': item.get('session_id'), 'status': status, 'alive': alive,
           'phase': 'unknown', 'detail': '', 'progress': None, 'last_progress_at': None,
           'health': 'unknown', 'log_tail': '', 'warning': None}
    result_path = item.get('result_file', item.get('done_file'))
    verified_terminal = False
    if result_path:
        result_path = Path(result_path)
        result_path = result_path if result_path.is_absolute() else Path(project) / result_path
        if result_path.exists():
            result = load(result_path)
            if (isinstance(result, dict) and result.get('run_id') == run_id
                    and result.get('status') in TERMINAL and isinstance(result.get('exit_code'), int)
                    and not isinstance(result.get('exit_code'), bool)):
                consistent = ((result['status'] == 'succeeded') == (result['exit_code'] == 0))
                if consistent:
                    job['status'] = result['status']
                    job['health'] = 'finished' if result['status'] == 'succeeded' else 'attention'
                    verified_terminal = True
                else:
                    job['warning'] = 'Result status and exit code disagree'
            else:
                job['warning'] = 'Result incomplete or unverified; not counted as success'
            if job['warning']:
                job['status'] = 'unknown'
    if status in TERMINAL and not verified_terminal:
        job['warning'] = job['warning'] or 'Registry reports a terminal state without a verified result receipt'
        job['status'] = 'unknown'
    if job['status'] in TERMINAL:
        job['phase'] = 'completed'
        job['health'] = 'finished' if job['status'] == 'succeeded' else 'attention'
        if alive:
            job['warning'] = 'Terminal record exists but its process is still alive; inspect before relaunch'
        return job
    if not alive:
        job.update(status='unknown' if identity_missing else 'ended', health='unknown' if identity_missing else 'attention',
                   detail='Process identity missing; PID alone is not enough' if identity_missing else 'Process ended; completion has not been verified')
        return job
    job['status'] = 'running'
    p = processes.items[int(pid)]
    job['process'] = {'state': p['state'], 'rss_bytes': p['rss_bytes'], 'cpu_ticks': p['cpu_ticks']}
    progress_path = state_dir / 'progress' / (key(project, run_id) + '.json')
    report = load(progress_path, {})
    if not report and ID.fullmatch(run_id):
        report = load(Path(project) / '.lemvo-progress' / (run_id + '.json'), {})
    report_valid = (isinstance(report, dict) and str(report.get('run_id')) == run_id
            and str(report.get('pid')) == str(pid) and str(report.get('pid_start')) == str(start))
    if report_valid:
        job['phase'] = report.get('phase', 'unknown')
        job['detail'] = redact(report.get('detail', ''))[:400]
        job['progress'] = {'current': report.get('current'), 'total': report.get('total'), 'unit': report.get('unit', '')}
        job['last_report_at'] = report.get('at')
    log = item.get('log_file', item.get('log_path'))
    if log:
        lp = Path(log)
        lp = lp if lp.is_absolute() else Path(project) / lp
        job['log_tail'] = redact(tail(lp, 10000))[-8000:]
        if not report_valid:
            matches = re.findall(r'(?i)\b(step|epoch|batch|shard|samples?)\s*[:= ]\s*(\d+)\s*/\s*(\d+)', job['log_tail'])
            if matches:
                unit, cur, total = matches[-1]
                job['progress'] = {'current': int(cur), 'total': int(total), 'unit': unit}
                job['phase'] = 'loading_model' if unit.lower() == 'shard' else 'training'
                job['detail'] = 'Parsed counter from this job\u2019s log; phase is inferred'
    fingerprint = json.dumps([pid, start, job['phase'], job['progress']], sort_keys=True)
    old = previous.get(job['id'], {})
    changed = bool(job['progress'] and job['progress'].get('current') is not None and fingerprint != old.get('fingerprint'))
    # First attachment establishes a baseline, not a newly completed training step.
    last = now if changed and old else old.get('last_progress_at')
    if changed and not old and report_valid:
        try:
            reported_at = float(report.get('at', 0))
            last = reported_at if 0 < reported_at <= now else None
        except (TypeError, ValueError):
            pass
    previous[job['id']] = {'fingerprint': fingerprint, 'last_progress_at': last,
                           'first_seen': old.get('first_seen', now)}
    job['last_progress_at'] = last
    job['health'] = 'progressing' if last and now - last < stall_seconds else 'unknown'
    # No-output is never proof of a hang. Warn without signalling any process.
    age = now - (last or previous[job['id']]['first_seen'])
    if age >= stall_seconds:
        job['health'] = 'attention'
        job['warning'] = 'No observed counter progress; inspect loading/compute/I/O. No automatic interruption.'
    return job


class Collector:
    def __init__(self, store, home=None, proc_root='/proc', stall_seconds=1800):
        self.store, self.home = store, Path(home or Path.home())
        self.processes = Processes(proc_root)
        self.transcripts = TranscriptCache()
        self.stall_seconds = stall_seconds
        self.previous_jobs = load(store.path / 'observations.json', {}) or {}
        self.last_health = {}
        self.metadata_cache = {}
        self.hardware_cache = (0, {})

    def meta(self, path):
        if path not in self.metadata_cache:
            self.metadata_cache[path] = metadata(path)
        return self.metadata_cache[path]

    def hardware(self, now):
        if now - self.hardware_cache[0] < 15:
            return self.hardware_cache[1]
        mem = {}
        for line in read_text(self.processes.root / 'meminfo').splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] in ('MemTotal:', 'MemAvailable:'):
                mem[parts[0][:-1]] = int(parts[1]) * 1024
        try:
            disk = shutil.disk_usage(self.home)
            disk = {'total': disk.total, 'free': disk.free, 'path': str(self.home)}
        except OSError:
            disk = {}
        gpu = []
        raw = command(['nvidia-smi', '--query-gpu=index,name,utilization.gpu,memory.used,memory.total', '--format=csv,noheader,nounits'])
        for row in csv.reader(io.StringIO(raw)):
            if len(row) == 5:
                gpu.append(dict(zip(('index', 'name', 'utilization', 'memory_used_mib', 'memory_total_mib'), (s.strip() for s in row))))
        compute = []
        raw = command(['nvidia-smi', '--query-compute-apps=pid,used_memory,gpu_uuid', '--format=csv,noheader,nounits'])
        for row in csv.reader(io.StringIO(raw)):
            if len(row) != 3 or not row[0].strip().isdigit():
                continue
            p = self.processes.items.get(int(row[0].strip()))
            if p:
                compute.append({'pid': p['pid'], 'name': Path(p['args'][0]).name, 'cwd': p['cwd'],
                                'memory_mib': row[1].strip(), 'gpu_uuid': row[2].strip()})
        value = {'memory': mem, 'disk': disk, 'gpu': gpu, 'compute_processes': compute, 'sampled_at': now, 'load': list(os.getloadavg()),
                 'gpu_note': 'Host-wide utilization includes other jobs and reservations; it is not research progress.'}
        self.hardware_cache = (now, value)
        return value

    def collect(self):
        now = time.time()
        self.processes.scan()
        registry = self.store.registry()
        panes = tmux_panes()
        sessions = {}
        projects = {v['path'] for v in registry['projects'].values() if Path(v['path']).is_dir()}
        engines = [(p, engine_of(p)) for p in self.processes.items.values() if engine_of(p)]
        for p, engine in engines:
            # Node launcher and its native child represent one CLI, not two conversations.
            if any(q['ppid'] == p['pid'] and e == engine for q, e in engines):
                continue
            ancestors = self.processes.ancestors(p['pid'])
            pane = next((panes[a['pid']] for a in ancestors if a['pid'] in panes), None)
            hint = next((session_hint(a['args']) for a in ancestors[:3] if session_hint(a['args'])), None)
            env = self.processes.env(p['pid'])
            paths = self.processes.transcripts(p['pid'])
            registered = [s for s in registry['sessions'].values()
                          if s.get('pid') == p['pid'] and str(s.get('pid_start')) == p['start']]
            for s in registered:
                if s.get('transcript') and Path(s['transcript']).is_file():
                    paths.append(s['transcript'])
                hint = s.get('session_id') or hint
            if hint:
                paths += [str(f) for f in session_files(self.home, engine, env, hint)]
            paths = sorted(set(paths))
            for path in paths or [None]:
                meta = self.meta(path) if path else {}
                sid = meta.get('session_id') or hint
                # Explicit attachment overrides a CLI's home-directory cwd, but only
                # for that conversation, not all threads sharing its backend process.
                binding = next((r for r in registered if
                    (path and r.get('transcript') == path) or
                    (sid and r.get('session_id') == sid)), None)
                if binding is None and len(paths) <= 1 and len(registered) == 1:
                    binding = registered[0]
                project = (binding.get('project') if binding else None) or meta.get('project') or p['cwd']
                if not Path(project).is_dir():
                    project = p['cwd']
                project = str(Path(project).resolve())
                projects.add(project)
                identifier = key(engine, sid, p['pid'], p['start']) if sid else key(engine, p['pid'], p['start'])
                activity = self.transcripts.get(path, engine) if path else {'phase': 'unknown', 'messages': [],
                    'pending_tools': [], 'last_event_at': None, 'phase_since': None,
                    'parse_warning': 'Process discovered; transcript not bound. Attach it to identify the conversation.'}
                if activity.get('phase') == 'unknown' and not path:
                    activity['phase'] = 'unbound'
                sessions[identifier] = dict(activity, id=identifier, session_id=sid, engine=engine,
                    pid=p['pid'], pid_start=p['start'], project=project, project_id=key(project),
                    pane=pane, transcript=path, coverage='transcript' if path else 'process_only',
                    shared_process=len(paths) > 1, source='discovered', health='unknown',
                    children=[] if len(paths) > 1 else [{'pid': c['pid'], 'state': c['state'],
                        'name': Path(c['args'][0]).name, 'rss_bytes': c['rss_bytes']}
                        for c in self.processes.descendants(p['pid'])][:24])
        # Explicit attachments can represent a non-CLI fixture, or a recently exited session.
        for s in registry['sessions'].values():
            if any(v['pid'] == s.get('pid') and v['pid_start'] == str(s.get('pid_start'))
                   and (not s.get('session_id') or v['session_id'] == s.get('session_id')) for v in sessions.values()):
                continue
            projects.add(s['project'])
            alive = self.processes.same(s.get('pid'), s.get('pid_start'))
            activity = self.transcripts.get(s.get('transcript', ''), s['engine']) if s.get('transcript') else {
                'phase': 'unbound', 'messages': [], 'pending_tools': [], 'phase_since': None, 'last_event_at': None}
            sessions[s['id']] = dict(activity, **{k: s.get(k) for k in ('id','session_id','engine','pid','pid_start','project','transcript')},
                project_id=key(s['project']), pane=None, shared_process=False, children=[], source='attached',
                coverage='transcript' if s.get('transcript') else 'process_only', health='unknown')
            if not alive:
                sessions[s['id']].update(phase='session_ended', health='finished')
        infos = []
        for project in sorted(projects):
            info = project_info(project, self.processes)
            jp = Path(project) / '.lemvo-jobs.json'
            jobs = load(jp, None) if jp.exists() else []
            if jp.exists() and (not isinstance(jobs, list) or any(not isinstance(j, dict) for j in jobs)):
                info['warning'] = 'Job registry unreadable or malformed; no successful completion inferred'
                jobs = []
            for item in jobs[:100]:
                info['jobs'].append(describe_job(item, project, self.processes, self.previous_jobs,
                                                 now, self.stall_seconds, self.store.path))
            info['controller_present'] = info['owner_alive']
            infos.append(info)
        by_project = {i['path']: i for i in infos}
        for s in sessions.values():
            info = by_project[s['project']]
            matches = [v for v in sessions.values() if v['project'] == s['project']]
            managed = info['owner_alive'] and any(a['pid'] == info['owner_pid'] for a in self.processes.ancestors(s['pid']))
            s['managed'] = managed
            s['mode'] = info['mode'] if managed else None
            # Project controls belong to its controller, never an arbitrary bare chat in that directory.
            if managed:
                st = info['status']
                if st in ('WAITING_USER', 'BLOCKED', 'PAUSED', 'STALLED'):
                    s['phase'] = 'waiting_user'
                elif st == 'COMPLETED':
                    s['phase'] = 'completed'
            s['job_ids'] = [j['id'] for j in info['jobs'] if
                (j.get('session_id') and j['session_id'] == s.get('session_id')) or
                (not j.get('session_id') and len(matches) == 1)]
            if s['phase'] not in ('waiting_user', 'completed', 'session_ended'):
                attached = [j for j in info['jobs'] if j['id'] in s['job_ids'] and j['alive']]
                if attached:
                    s['phase'] = attached[0]['phase'] if attached[0]['phase'] != 'unknown' else 'waiting_job'
                    s['health'] = 'progressing' if any(j['health'] == 'progressing' for j in attached) else 'unknown'
                    s['warning'] = next((j['warning'] for j in attached if j.get('warning')), None)
                elif s['phase'] in ('model', 'tool_pending'):
                    age = now - (s.get('phase_since') or s.get('last_event_at') or now)
                    s['health'] = 'attention' if age >= self.stall_seconds else 'observing'
                    if s['health'] == 'attention':
                        s['warning'] = 'Long wait without a completed phase; inspect the command and children. No automatic interrupt.'
            else:
                s['health'] = 'waiting' if s['phase'] == 'waiting_user' else 'finished'
            alert = (s['phase'], s['health'], s.get('warning'))
            if alert != self.last_health.get(s['id']):
                self.store.event({'type': 'session_state', 'session': s['id'], 'phase': s['phase'],
                                  'health': s['health'], 'detail': s.get('warning')})
                self.last_health[s['id']] = alert
        from .store import atomic
        atomic(self.store.path / 'observations.json', self.previous_jobs)
        return {'schema': 1, 'version': '0.1.0', 'host': socket.gethostname(), 'at': now,
                'hardware': self.hardware(now), 'projects': infos, 'sessions': sorted(sessions.values(), key=lambda s: (s['project'], s['id'])),
                'policy': 'observe-only health checks; no automatic signals, retries, model calls or research verdicts'}
