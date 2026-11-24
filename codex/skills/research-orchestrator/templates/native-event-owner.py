#!/usr/bin/env python3
"""CPU lifecycle owner for an ORIGINAL native scientist CLI, not a science loop.

Adopts a live CLI; a stream result is NOT process exit. Once it really exits,
producer terminal events wake the same session exactly once. No metric, method,
team, seed, scientific stop or promotion policy belongs in this module.
Python 3.6+ stdlib only.
"""
from pathlib import Path
import argparse
import datetime
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import time

try:
    BOOT_EPOCH = time.time() - time.clock_gettime(time.CLOCK_BOOTTIME)
except (AttributeError, OSError):
    BOOT_EPOCH = time.time() - float(Path('/proc/uptime').read_text().split()[0])
TICKS_PER_SECOND = os.sysconf('SC_CLK_TCK')

_HARD_STATUSES = frozenset(('STOP', 'STOPPED', 'PAUSED'))
_HUMAN_STATUSES = frozenset(('WAITING_USER', 'HOLD', 'EVIDENCE_HOLD', 'STOPPED_INCONCLUSIVE',
                             'BLOCKED', 'BLOCKED_USER', 'BLOCKED-USER'))
_DONE_STATUSES = frozenset(('COMPLETED', 'CONCLUDED', 'CAMPAIGN_DONE', 'CAMPAIGN-DONE'))
_INVALID_CAP_SOURCES = frozenset(('', 'unknown', 'none', 'null', '-', 'n/a'))


def read(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {} if default is None else default


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name('.' + path.name + '.owner-' + str(os.getpid()))
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    os.replace(temp, path)


def identity(pid):
    try:
        pid = int(pid)
        p = Path('/proc') / str(pid)
        fields = (p / 'stat').read_text().rsplit(')', 1)[1].split()
        return dict(pid=pid, start=fields[19], state=fields[0],
                    uid=p.stat().st_uid,
                    argv=(p / 'cmdline').read_bytes().split(b'\0'))
    except (OSError, ValueError, TypeError, IndexError):
        return None


def alive(record):
    now = identity(record.get('pid')) if isinstance(record, dict) else None
    return bool(now and now['state'] not in ('Z', 'X') and
                now['uid'] == os.getuid() and
                str(record.get('start', record.get('pid_start'))) == now['start'])


def append(path, item):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as stream:
        stream.write(json.dumps(item, ensure_ascii=False) + '\n')


def valid_session_id(value):
    if isinstance(value, str) and re.fullmatch(
            r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}',
            value):
        return value.lower()
    return None


def tail_session_id(path):
    """Recover identity only; never replay an old result/action/usage record."""
    try:
        with Path(path).open('rb') as stream:
            stream.seek(max(0, stream.seek(0, 2) - 1024 * 1024))
            lines = stream.read().splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            value = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(value, dict):
            sid = valid_session_id(value.get('session_id') or value.get('thread_id'))
            if sid:
                return sid
    return None


def _load_runtime_contract():
    p = Path(__file__).with_name('runtime-contract.py')
    if not p.is_file():
        return None
    try:
        spec = importlib.util.spec_from_file_location('_lemvo_owner_contract', str(p))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None


def _parse_budget_yaml(state_path):
    out = {}
    if not state_path.is_file():
        return out
    top = ''
    try:
        lines = state_path.read_text(encoding='utf-8', errors='replace').splitlines()
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


def _budget_exhausted(root):
    b = _parse_budget_yaml(root / 'research-state.yaml')
    if b.get('unit', '').strip().lower() != 'usd':
        return False
    auth = b.get('cap_authorized', '').strip().lower()
    src = b.get('cap_source', '').strip().lower()
    if auth != 'yes' or src in _INVALID_CAP_SOURCES:
        return False
    try:
        cap = float(b.get('total_cap', ''))
        used = float(b.get('used', ''))
    except (ValueError, TypeError):
        return False
    return used >= cap


def _contract_state(root):
    """Inspect WAIT/HARD/DONE/HUMAN status consistently with runtime-contract.py."""
    state_path = root / 'research-state.yaml'
    statuses = set()
    if state_path.is_file():
        top = ''
        try:
            lines = state_path.read_text(encoding='utf-8', errors='replace').splitlines()
        except OSError:
            lines = []
        for raw in lines:
            line = re.sub(r'\s+#.*$', '', raw).rstrip()
            m = re.match(r'^(\s*)([\w-]+)\s*:\s*(.*)$', line)
            if not m:
                continue
            indent, key, val = len(m.group(1)), m.group(2), m.group(3).strip().strip("'\"").upper()
            if not indent:
                top = key
                if key in ('campaign_status', 'measurement_state', 'root_status', 'status') and val:
                    statuses.add(val)
            elif indent == 2 and top == 'project' and key == 'status' and val:
                statuses.add(val)
    markers = {n for n in ('STOP', 'HOLD', 'BLOCKED-USER', 'CAMPAIGN-DONE') if (root / n).is_file()}
    values = statuses | markers
    if values & _HARD_STATUSES or (root / 'PAUSE').is_file() or (root / '.runtime-events/PAUSE').is_file():
        return 'hard', '|'.join(sorted(values & _HARD_STATUSES)) or 'PAUSE'
    if values & _DONE_STATUSES:
        return 'done', '|'.join(sorted(values & _DONE_STATUSES))
    if values & _HUMAN_STATUSES:
        return 'human', '|'.join(sorted(values & _HUMAN_STATUSES))
    return 'ready', 'ready'


def _has_eligible_guidance(root):
    qdir = root / '.lemvo-steer-queue'
    if not qdir.is_dir():
        return False
    return any(True for _ in qdir.glob('q-*.md'))


def terminal(job):
    """A terminal observation, NEVER a scientific-success determination."""
    marks = []
    try:
        producer_started = BOOT_EPOCH + float(job['start']) / TICKS_PER_SECOND
    except (ValueError, TypeError, KeyError):
        producer_started = float('inf')
    for kind, names in [('failed_marker', job.get('failed_files', [])),
                        ('done_marker', job.get('done_files', []))]:
        found = []
        for name in names:
            p = Path(name)
            try:
                st = p.stat()
                if not job.get('accept_cached_markers') and st.st_mtime < producer_started:
                    continue
                if (job.get('done_format') == 'json' or
                        (job.get('done_format') != 'file' and p.suffix.lower() == '.json')):
                    try:
                        content = json.loads(p.read_text(encoding='utf-8'))
                        if isinstance(content, dict) and any(
                                str(content.get(key, '')).strip().lower().replace('-', '_') in
                                ('running', 'pending', 'queued', 'starting', 'started', 'in_progress',
                                 'processing', 'training', 'initializing', 'loading', 'waiting',
                                 'scheduled', 'submitted') for key in ('status', 'state')):
                            continue
                    except (OSError, ValueError, UnicodeError):
                        continue
                found.append([str(p), st.st_mtime_ns, st.st_size])
            except OSError:
                pass
        if found and (kind == 'failed_marker' or len(found) == len(names)):
            marks = found
            break
    else:
        kind = 'producer_exited'
        if not job.get('pid') or not job.get('start') or alive(job):
            return None
    payload = dict(job_id=job.get('job_id'), observation=kind,
                   producer=dict(pid=job.get('pid'), start=job.get('start')),
                   markers=marks)
    payload['signature'] = hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return payload


class Owner:
    def __init__(self, root, poll=3, launcher=None, engine='claude', session_id=None, initial_prompt_file=None):
        self.root = Path(root).resolve()
        self.runtime = self.root / '.runtime-events'
        self.runtime.mkdir(exist_ok=True)
        self.path = self.runtime / 'native_owner_state.json'
        self.state = read(self.path)
        if not isinstance(self.state, dict):
            self.state = {}
        if not isinstance(self.state.get('consumed'), dict):
            self.state['consumed'] = {}
        if engine not in ('claude', 'codex'):
            raise ValueError('Unsupported engine')
        self.engine = engine
        self.poll = max(0.01, poll)
        self.launcher = launcher or [engine]
        if not isinstance(self.launcher, list) or not self.launcher or not all(
                isinstance(arg, str) and arg for arg in self.launcher):
            raise ValueError('--launcher-json must be a nonempty JSON argv list')
        self.initial_prompt_file = Path(initial_prompt_file).resolve() if initial_prompt_file else None
        if session_id:
            sid = valid_session_id(session_id)
            if not sid:
                raise ValueError('Invalid session id')
            self.state['session_id'] = sid
        previous_engine = self.state.get('engine')
        if previous_engine and previous_engine != engine and self.state.get('session_id'):
            raise ValueError('An existing native session belongs to another engine')
        self.state['engine'] = engine
        self.child = None
        self.cli = None
        self.partial = b''
        self.tools = {}
        self.useful = False
        self.last_result = {}
        self.fingerprint_before = self.science_fingerprint()

    def event(self, kind, **extra):
        append(self.runtime / 'owner_events.jsonl',
               dict(at=time.time(), type=kind, **extra))

    def status(self, phase, **extra):
        self.state.update(state=phase, observed_at=time.time(), **extra)
        save(self.path, self.state)
        save(self.runtime / 'native_owner_heartbeat.json',
             dict(pid=os.getpid(), state=phase, at=time.time(),
                  cli=self.cli, session_id=self.state.get('session_id')))

    def dispatch_blocked(self, wake=None):
        """Enforce WAIT/HOLD/BLOCKED-USER, guided mode, and authorized USD cap pre-dispatch."""
        if self.stopped():
            return 'owner_paused_no_further_dispatch', 'stop_or_pause_file'
        kind, reason = _contract_state(self.root)
        if kind in ('hard', 'done'):
            return 'owner_paused_no_further_dispatch', reason
        adv_file = self.root / '.lemvo-advance-once'
        user_wake = (
            adv_file.is_file()
            or _has_eligible_guidance(self.root)
            or (isinstance(wake, dict) and str(wake.get('reason') or '') in ('user-advance', 'advance'))
        )
        if kind == 'human' and not user_wake:
            return 'waiting_user', reason
        mode_file = self.root / '.lemvo-mode'
        if mode_file.is_file():
            try:
                mode_val = mode_file.read_text(encoding='utf-8').strip().lower()
            except OSError:
                mode_val = ''
            if mode_val == 'guided' and not user_wake:
                return 'waiting_user', 'guided-idle'
        if _budget_exhausted(self.root):
            return 'over_budget', 'authorized_usd_budget_exhausted'
        return None

    def science_fingerprint(self):
        """Bounded stat-only change detection; excludes telemetry/weights/images."""
        paths = []
        for name in ['TASK.md', 'CLAUDE.md', 'AGENTS.md', 'CHARTER.md', 'START.md',
                     'runbook.md', 'RUNBOOK.md', '.claude/settings.local.json']:
            p = self.root / name
            if p.is_file():
                paths.append(p)
        for pattern in ['USER*.md', 'task/*.md', 'teams/**/*.md',
                        'reports/**/*', 'champion/*', '*.py', '*.sh', '*.yaml', '*.yml', '*.toml']:
            paths.extend(p for p in self.root.glob(pattern) if p.is_file())
        allowed = {'.py', '.sh', '.md', '.yaml', '.yml', '.toml'}
        for base in self.root.glob('agents/*/workspace'):
            seen = 0
            for here, dirs, names in os.walk(base):
                dirs[:] = [d for d in dirs if d not in
                           ('images', 'data', '.git', '__pycache__', 'node_modules',
                            'initial_adapter', 'train_out', 'logs') and
                           not d.startswith('checkpoint-')]
                for name in names:
                    seen += 1
                    if seen > 5000:
                        break
                    p = Path(here) / name
                    if p.suffix in allowed or name in ('RESULTS.json', 'cycle_result.json'):
                        paths.append(p)
                if seen > 5000:
                    break
        values = []
        for p in sorted(set(paths)):
            try:
                st = p.stat()
                values.append([str(p), st.st_mtime_ns, st.st_size])
            except OSError:
                pass
        return hashlib.sha256(json.dumps(values).encode()).hexdigest()

    def jobs(self):
        """Merge original framework receipts into the runtime registry under lock."""
        registry = self.runtime / 'jobs.json'
        with (self.runtime / 'jobs.lock').open('a+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            existing = read(registry, [])
            if not isinstance(existing, list):
                self.event('registry_schema_error', path=str(registry))
                existing = []
            merged = {j['job_id']: j for j in existing
                      if isinstance(j, dict) and j.get('job_id')}
            candidates = []
            for p in self.root.glob('agents/*/workspace/result_latest.json'):
                j = read(p)
                if not isinstance(j, dict):
                    continue
                for key in ('driver_pid', 'pid'):
                    x = identity(j.get(key))
                    if not x or x['state'] in ('Z', 'X') or x['uid'] != os.getuid():
                        continue
                    argv = b' '.join(x['argv']).decode(errors='replace')
                    try:
                        cwd = (Path('/proc') / str(x['pid']) / 'cwd').resolve()
                        cwd_owned = cwd == self.root or self.root in cwd.parents
                    except OSError:
                        cwd_owned = False
                    if str(self.root) not in argv and not cwd_owned:
                        continue
                    claimed_start = j.get('pid_start', j.get('driver_pid_start'))
                    if claimed_start is not None and str(claimed_start) != x['start']:
                        continue
                    candidates.append(dict(
                        job_id='native:' + str(p.relative_to(self.root)) + ':' + str(x['pid']) + ':' + x['start'],
                        pid=x['pid'], start=x['start'], label=j.get('exp_id', p.parent.name),
                        done_files=[j[k] for k in ('done_file', 'result_path')
                                    if isinstance(j.get(k), str) and j[k].startswith('/')],
                        failed_files=[j[k] for k in ('failed_file',)
                                      if isinstance(j.get(k), str) and j[k].startswith('/')],
                        source_path=str(p)))
                    break
            lemvo_jobs = read(self.root / '.lemvo-jobs.json', [])
            if isinstance(lemvo_jobs, dict):
                lemvo_jobs = lemvo_jobs.get('jobs', [])
            for j in lemvo_jobs if isinstance(lemvo_jobs, list) else []:
                if not isinstance(j, dict) or j.get('verification_only'):
                    continue
                x = identity(j.get('pid'))
                if not x or x['state'] in ('Z', 'X') or x['uid'] != os.getuid():
                    continue
                if str(j.get('pid_start', j.get('start'))) != x['start']:
                    continue
                candidates.append(dict(
                    job_id='lemvo:' + str(j.get('run_id', x['pid'])) + ':' + x['start'],
                    pid=x['pid'], start=x['start'], label=j.get('name'),
                    done_files=[j['done_file']] if j.get('done_file') else [],
                    failed_files=[j['failed_file']] if j.get('failed_file') else [],
                    source_path=str(self.root / '.lemvo-jobs.json')))
            changed = False
            for j in candidates:
                if j['job_id'] not in merged:
                    merged[j['job_id']] = j
                    changed = True
            if changed:
                save(registry, list(merged.values()))
            return [j for j in merged.values() if not j.get('verification_only')]

    def pending_terminals(self, jobs):
        out = []
        for job in jobs:
            event = terminal(job)
            if event and self.state['consumed'].get(job['job_id']) != event['signature']:
                out.append(event)
        return out

    def pending_wake(self):
        event = read(self.runtime / 'wake.json')
        if not isinstance(event, dict):
            return None
        event_id = event.get('event_id')
        if event_id and event_id not in self.state.get('consumed_wakes', []):
            return dict(event_id=str(event_id), reason=str(event.get('reason', 'runtime event')))
        return None

    def acknowledge_delivery(self):
        delivery = self.state.pop('pending_delivery', {})
        if not delivery:
            return
        for event in delivery.get('events', []):
            self.state['consumed'][event['job_id']] = event['signature']
        if delivery.get('wake'):
            self.state.setdefault('consumed_wakes', []).append(delivery['wake']['event_id'])
        save(self.path, self.state)
        self.event('continuation_delivery_acknowledged',
                   event_signatures=[e['signature'] for e in delivery.get('events', [])],
                   wake=delivery.get('wake'))

    def adopt(self):
        entry = read(self.root / 'EXECUTION_PRODUCER.json')
        if not isinstance(entry, dict):
            entry = {}
        if not alive(entry):
            for path in Path('/proc').iterdir():
                if not path.name.isdigit():
                    continue
                x = identity(int(path.name))
                if not x or x['state'] in ('Z', 'X') or x['uid'] != os.getuid():
                    continue
                argv = [a.decode(errors='replace') for a in x['argv'] if a]
                try:
                    cwd_matches = (path / 'cwd').resolve() == self.root
                except OSError:
                    continue
                command_is_cli = bool(argv and
                    (Path(argv[0]).name in (self.engine, self.engine + '.exe') or
                     any(a in self.launcher[1:] for a in argv) or
                     (len(self.launcher) == 1 and argv[0] == self.launcher[0])))
                if cwd_matches and command_is_cli:
                    entry.update(pid=x['pid'], start=x['start'],
                                 stdout=str(self.root / 'native-stdout.jsonl'))
                    break
        if alive(entry):
            self.cli = dict(pid=entry['pid'], start=str(entry['start']),
                            stdout=entry.get('stdout', str(self.root / 'native-stdout.jsonl')))
            self.state['session_id'] = (valid_session_id(entry.get('session_id')) or
                                        valid_session_id(self.state.get('session_id')) or
                                        tail_session_id(self.cli['stdout']))
            try:
                self.state['offset'] = Path(self.cli['stdout']).stat().st_size
            except OSError:
                self.state['offset'] = 0
            self.last_result = {}
            self.useful = False
            self.event('cli_adopted', cli=self.cli, session_id=self.state.get('session_id'),
                       offset=self.state['offset'])
            self.status('adopted_cli_running', cli=self.cli)
            return True
        self.state['session_id'] = (valid_session_id(self.state.get('session_id')) or
                                    valid_session_id(entry.get('session_id')) or
                                    tail_session_id(entry.get('stdout', self.root / 'native-stdout.jsonl')))
        return False

    def consume_stream(self):
        path = Path(self.cli['stdout'])
        try:
            with path.open('rb') as stream:
                stream.seek(self.state.get('offset', 0))
                raw = stream.read(8 * 1024 * 1024)
                self.state['offset'] = stream.tell()
        except OSError:
            return
        lines = (self.partial + raw).split(b'\n')
        self.partial = lines.pop()
        for line in lines:
            try:
                j = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(j, dict):
                continue
            sid = valid_session_id(j.get('session_id') or j.get('thread_id'))
            if sid:
                self.state['session_id'] = sid
            if self.engine == 'codex':
                typ = j.get('type')
                if typ in ('turn.completed', 'turn.failed'):
                    self.last_result = dict(type='result',
                        subtype='success' if typ == 'turn.completed' else 'error',
                        is_error=typ == 'turn.failed', usage=j.get('usage'),
                        result=self.last_result.get('result', '') if typ == 'turn.completed' else str(j.get('error', '')))
                    self.event('cli_result_observed_not_exit', subtype=typ, cli=self.cli)
                    if typ == 'turn.completed':
                        self.acknowledge_delivery()
                item = j.get('item', {})
                if typ == 'item.completed' and isinstance(item, dict):
                    if item.get('type') == 'agent_message':
                        self.last_result['result'] = str(item.get('text', ''))
                    if item.get('type') in ('file_change', 'mcp_tool_call') and item.get('status') == 'completed':
                        self.useful = True
            if j.get('type') == 'result' and not j.get('parent_tool_use_id'):
                self.last_result = j
                self.event('cli_result_observed_not_exit',
                           subtype=j.get('subtype'), is_error=j.get('is_error'),
                           cli=self.cli)
                if (j.get('subtype') == 'success' and not j.get('is_error') and
                        not j.get('parent_tool_use_id')):
                    self.acknowledge_delivery()
            message = j.get('message', {})
            if not isinstance(message, dict):
                continue
            for item in message.get('content', []):
                if not isinstance(item, dict):
                    continue
                if item.get('type') == 'tool_use':
                    self.tools[item.get('id')] = item.get('name')
                elif item.get('type') == 'tool_result' and not item.get('is_error'):
                    if self.tools.get(item.get('tool_use_id')) in (
                            'Write', 'Edit', 'NotebookEdit', 'Agent', 'Task', 'SendMessage'):
                        self.useful = True

    def spawn(self, reason, events=None, wake=None):
        blocked = self.dispatch_blocked(wake=wake)
        if blocked:
            phase, fault = blocked
            self.event('dispatch_blocked', reason=reason, phase=phase, fault=fault)
            self.status(phase, fault=fault)
            return
        if self.child is not None and self.child.poll() is None:
            return
        if self.adopt():
            return
        sid = valid_session_id(self.state.get('session_id'))
        if events or wake:
            self.state['pending_delivery'] = dict(reason=reason, events=events or [],
                                                  wake=wake, at=time.time())
            save(self.path, self.state)
        brief = json.dumps(events or [], ensure_ascii=False)[:10000]
        prompt = (
            'Runtime event continuation, SAME original research workspace/session. '
            'Reason: ' + reason + '. Event observations: ' + brief + '. '
            'A producer exit or marker is not proof of scientific success: inspect actual '
            'result/failed receipts once, preserve completed stages and adopt live work. '
            'Continue the ORIGINAL runbook, roles and self-selected research. No new '
            'scientific gate, method restriction, team-size cap or result-direction rule '
            'is imposed by this CPU lifecycle owner. If no independent work remains, '
            'register real producer identities/DONE/FAILED using the runtime wait tools '
            'and park without timed model polling. Do not terminate other jobs. '
            'Runtime owner—not the model—will wake this same session on the next event.')
        if wake:
            prompt += '\nExplicit operational wake event: ' + json.dumps(wake, ensure_ascii=False)
        if not sid:
            files = ([self.initial_prompt_file] if self.initial_prompt_file else
                     [self.root / name for name in ('START.md', 'runbook.md', 'RUNBOOK.md')])
            initial = next((p for p in files if p and p.is_file()), None)
            if initial is None:
                self.state['failure_retries'] = 4
                self.event('configuration_fault', fault='missing_initial_prompt_without_session_id')
                self.status('runtime_fault', fault='missing_initial_prompt_without_session_id')
                return
            prompt = initial.read_text(encoding='utf-8') + '\n' + prompt
        if reason == 'repair_missing_runtime_handoff':
            prompt += (' Your previous CLI returned a wait/idle response without a live '
                       'registered dependency. Repair that runtime handoff once or '
                       'continue actual independent native work; do not repeatedly '
                       'return identical status or invent a user confirmation.')
        if self.engine == 'claude':
            args = list(self.launcher) + ['-p', prompt, '--verbose', '--output-format', 'stream-json']
            if sid:
                args += ['--resume', sid]
        else:
            args = list(self.launcher) + ['exec', '--skip-git-repo-check', '--json',
                                        '-s', os.environ.get('CODEX_SANDBOX', 'workspace-write')]
            if sid:
                args += ['resume', sid, prompt]
            else:
                args += [prompt]
        stdout = self.root / 'native-stdout.jsonl'
        stderr = self.root / 'execution-stderr.log'
        self.state['offset'] = stdout.stat().st_size if stdout.exists() else 0
        self.partial = b''
        self.tools = {}
        self.useful = False
        self.last_result = {}
        self.fingerprint_before = self.science_fingerprint()
        templates = Path(__file__).resolve().parent
        installer = templates / 'configure-event-runtime.py'
        if installer.is_file():
            try:
                configured = subprocess.run([sys.executable, str(installer), 'install',
                    '--root', str(self.root), '--engine', self.engine,
                    '--templates', str(templates)], stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, timeout=30)
                self.event('runtime_hooks_configured', returncode=configured.returncode)
                settings = self.root / '.claude' / 'settings.local.json'
                if self.engine == 'claude' and configured.returncode == 0 and settings.is_file():
                    args += ['--settings', str(settings)]
            except (OSError, subprocess.TimeoutExpired) as error:
                self.event('runtime_hooks_configuration_warning', error=type(error).__name__)
        env = os.environ.copy()
        if self.engine == 'claude':
            env['CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS'] = '0'
        try:
            with stdout.open('ab') as out, stderr.open('ab') as err:
                self.child = subprocess.Popen(args, cwd=self.root, stdin=subprocess.DEVNULL,
                                              stdout=out, stderr=err, start_new_session=True, env=env)
                heartbeat = templates / 'runtime-heartbeat.py'
                if heartbeat.is_file():
                    subprocess.Popen([sys.executable, str(heartbeat), '--root', str(self.root),
                        '--parent', str(self.child.pid)], cwd=self.root, stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL, stderr=err, start_new_session=True)
        except OSError as exc:
            retries = self.state.get('failure_retries', 0) + 1
            self.state['failure_retries'] = retries
            if retries <= 3:
                self.state['retry_not_before'] = time.time() + 60
                self.state['retry_reason'] = 'bounded_launch_recovery'
            self.event('cli_launch_failed', error=type(exc).__name__, retries=retries)
            self.status('cpu_backoff' if retries <= 3 else 'runtime_fault',
                        fault='cli_launch_failed')
            return
        adv_file = self.root / '.lemvo-advance-once'
        if adv_file.is_file():
            try:
                adv_file.unlink()
            except OSError:
                pass
        x = identity(self.child.pid)
        self.cli = dict(pid=self.child.pid, start=x['start'] if x else None, stdout=str(stdout))
        entry = dict(self.cli, session_id=sid, engine=self.engine,
                     execution='direct native research',
                     lifecycle_owner_pid=os.getpid(), at=time.time())
        entry['continuation_id'] = 'native-cont-' + str(int(time.time() * 1000000000))
        entry['dispatch_event_id'] = wake.get('event_id') if wake else None
        save(self.root / 'EXECUTION_PRODUCER.json', entry)
        self.state['last_dispatch_at'] = time.time()
        self.state['launch_count'] = self.state.get('launch_count', 0) + 1
        self.event('cli_spawned', reason=reason, cli=self.cli, session_id=sid,
                   terminal_signatures=[e['signature'] for e in events or []],
                   continuation_id=entry['continuation_id'], wake=wake)
        self.status('cli_running', cli=self.cli)

    def quota(self, text):
        lower = text.lower()
        hit = ('429' in text or '1308' in text) and any(
            word in lower for word in ('quota', 'limit', '1308', '额度', '上限'))
        if not hit:
            return None
        helper = self.runtime / 'quota_runtime.py'
        if helper.exists():
            try:
                spec = importlib.util.spec_from_file_location('project_quota_runtime', helper)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                info = module.quota_info(429, text)
                if info.get('reset_not_before_epoch'):
                    return max(time.time() + 10, float(info['reset_not_before_epoch']) + 10)
            except Exception as exc:
                self.event('quota_parser_error_cpu_fallback', error=type(exc).__name__)
        match = re.search(r'(?:reset_at|reset_not_before_epoch)[\s\":=]+(\d{10}(?:\.\d+)?)', text)
        if match:
            return max(time.time() + 10, float(match.group(1)) + 10)
        return time.time() + 1800

    def stopped(self):
        return any((self.root / name).exists() for name in ('PAUSE', 'STOP', 'CAMPAIGN-DONE')) or (self.runtime / 'PAUSE').exists()

    def run(self):
        with (self.runtime / 'execution-owner.lock').open('a+') as common:
            try:
                fcntl.flock(common, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self.event('execution_owner_busy_no_dispatch')
                return
            self._run_owned()

    def _run_owned(self):
        with (self.runtime / 'native_owner.lock').open('a+') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            me = identity(os.getpid())
            save(self.runtime / 'native_owner.json',
                 dict(pid=os.getpid(), start=me['start'], root=str(self.root),
                      scientific_policy=False))
            adopted = self.adopt()
            idle_fp = self.science_fingerprint()
            self.state.setdefault('handoff_repair_used', False)
            self.state.setdefault('failure_retries', 0)
            self.state.setdefault('retry_not_before', 0)
            if not adopted:
                self.status('waiting_event', cli=None)
            while not self.stopped():
                jobs = self.jobs()
                if self.cli:
                    self.consume_stream()
                    if alive(self.cli) or (self.child is not None and self.child.poll() is None):
                        self.status('cli_running', cli=self.cli)
                        time.sleep(self.poll)
                        continue
                    self.consume_stream()
                    rc = self.child.poll() if self.child else None
                    result = self.last_result
                    self.event('cli_os_exit', cli=self.cli, returncode=rc,
                               result_subtype=result.get('subtype'),
                               result_error=result.get('is_error'),
                               result_usage_snapshot=result.get('usage'),
                               cumulative_model_usage_do_not_sum=result.get('modelUsage'))
                    last = str(result.get('result', ''))
                    reset = self.quota(last)
                    changed = self.science_fingerprint() != self.fingerprint_before
                    self.cli = None
                    self.child = None
                    self.state['cli'] = None
                    if reset:
                        self.state['retry_not_before'] = reset
                        self.state['retry_reason'] = 'provider_quota'
                    elif (result.get('is_error') or rc not in (None, 0) or
                          result.get('type') != 'result' or result.get('subtype') != 'success'):
                        self.state['failure_retries'] += 1
                        if self.state['failure_retries'] <= 3:
                            self.state['retry_not_before'] = time.time() + 60
                            self.state['retry_reason'] = 'bounded_runtime_recovery'
                        else:
                            self.status('runtime_fault', fault='repeated_runtime_error')
                    else:
                        self.state['failure_retries'] = 0
                        self.acknowledge_delivery()
                        wait_intent = bool(re.search(
                            r'quiet|wait|await|turn ended|ending turn|in.flight|等待|待.*完成',
                            last.lower()))
                        event_pending = bool(self.pending_terminals(jobs) or self.pending_wake())
                        if (self.useful or changed) and not wait_intent and not event_pending:
                            self.state['handoff_repair_used'] = False
                            self.spawn('native_work_handoff')
                            continue
                        self.state['handoff_repair_used'] = True
                    idle_fp = self.science_fingerprint()
                    self.status('waiting_event', idle_fingerprint=idle_fp)
                wake = self.pending_wake()
                blocked = self.dispatch_blocked(wake=wake)
                if blocked:
                    self.status(blocked[0], fault=blocked[1])
                    time.sleep(self.poll)
                    continue
                retry = float(self.state.get('retry_not_before', 0))
                if self.state.get('failure_retries', 0) > 3:
                    changed = self.science_fingerprint() != idle_fp
                    if not wake and not changed:
                        self.status('runtime_fault', fault='bounded_runtime_retries_exhausted')
                        time.sleep(self.poll)
                        continue
                    self.state['failure_retries'] = 0
                    self.state['retry_not_before'] = 0
                    if wake:
                        self.spawn('explicit_runtime_repair_wake', wake=wake)
                        continue
                    idle_fp = self.science_fingerprint()
                if retry and time.time() < retry:
                    self.status('cpu_backoff')
                    time.sleep(self.poll)
                    continue
                delivery = self.state.get('pending_delivery')
                if delivery:
                    self.state['retry_not_before'] = 0
                    self.spawn(delivery['reason'], delivery.get('events'), delivery.get('wake'))
                    continue
                events = self.pending_terminals(jobs)
                if events or wake:
                    self.state['handoff_repair_used'] = False
                    self.state['retry_not_before'] = 0
                    self.spawn('explicit_operational_wake' if wake else
                               'registered_producer_terminal', events, wake)
                    continue
                if retry:
                    if time.time() >= retry:
                        reason = self.state.pop('retry_reason', 'runtime_recovery')
                        self.state['retry_not_before'] = 0
                        self.spawn(reason)
                        continue
                    self.status('cpu_backoff')
                else:
                    current = self.science_fingerprint()
                    active = any(terminal(j) is None for j in jobs)
                    if current != idle_fp:
                        idle_fp = current
                        self.state['handoff_repair_used'] = False
                        self.spawn('actual_native_handoff_file_change')
                        continue
                    if not active and not self.state['handoff_repair_used']:
                        self.state['handoff_repair_used'] = True
                        self.spawn('repair_missing_runtime_handoff')
                        continue
                    if not active:
                        self.status('runtime_fault', fault='missing_event_dependency_after_repair')
                    else:
                        self.status('cpu_waiting_registered_jobs')
                time.sleep(self.poll)
            self.status('owner_paused_no_further_dispatch')
            self.event('owner_paused', active_cli_preserved=self.cli)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--poll', type=float, default=3)
    parser.add_argument('--engine', choices=['claude', 'codex'], default='claude')
    parser.add_argument('--launcher-json', help='JSON argv prefix for the actual CLI or provider launcher; no shell interpolation')
    parser.add_argument('--session-id', help='Existing Claude session UUID or Codex thread UUID')
    parser.add_argument('--initial-prompt-file', type=Path, help='First-launch authorized task; defaults to START.md or runbook.md')
    args = parser.parse_args()
    try:
        Owner(args.root, args.poll,
              json.loads(args.launcher_json) if args.launcher_json else None,
              args.engine, args.session_id, args.initial_prompt_file).run()
    except (ValueError, TypeError) as error:
        parser.error(str(error))


if __name__ == '__main__':
    main()
