#!/usr/bin/env python3
"""Per-project Linux bridge. Existing Lemvo owns turns/jobs/accounting.

Only the Codex invocation moves between a Windows worker and the server.
No TCP listener, shared /tmp queue, automatic stale-claim stealing or GPU work.
"""
import base64
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid


def atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name('.' + path.name + '.' + uuid.uuid4().hex)
    with tmp.open('w', encoding='utf-8') as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(str(tmp), str(path))


def read(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return default


def identity(pid):
    try:
        fields = Path('/proc/%s/stat' % int(pid)).read_text().rsplit(')', 1)[1].split()
        return fields[19] if fields[0] != 'Z' else None
    except (OSError, ValueError, IndexError):
        return None


class Project:
    def __init__(self, path):
        self.project = Path(path).resolve(strict=True)
        self.root = self.project / '.lemvo-desktop'

    @contextlib.contextmanager
    def lock(self):
        self.root.mkdir(exist_ok=True)
        with (self.root / 'bridge.lock').open('a') as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            yield

    def config(self):
        c = read(self.root / 'config.json')
        if not c:
            raise ValueError('Project is not registered for desktop dispatch')
        return c

    def request_dir(self, request_id):
        if not isinstance(request_id, str) or not request_id or any(x not in '0123456789abcdef-' for x in request_id):
            raise ValueError('Invalid request id')
        return self.root / 'requests' / request_id

    def save_config(self, c):
        atomic(self.root / 'config.json', c)

    def event(self, kind, **data):
        with (self.root / 'events.jsonl').open('a', encoding='utf-8') as f:
            f.write(json.dumps(dict(ts=time.time(), kind=kind, **data), ensure_ascii=False) + '\n')

    def active(self):
        return read(self.root / 'active.json')

    def pending(self, a):
        if not a:
            return False
        if identity(a.get('pid')) == a.get('start') and a.get('start') is not None:
            return True
        if a.get('child_pid') and identity(a['child_pid']) == a.get('child_start'):
            return True
        if a.get('request_id'):
            d = self.request_dir(a['request_id'])
            r = read(d / 'request.json', {})
            if r.get('status') == 'claimed' and not (d / 'completion.json').exists():
                return True
        return False

    def status_locked(self):
        c = self.config()
        if not c.get('project_id'):
            c['project_id'] = uuid.uuid4().hex
            self.save_config(c)
        a = self.active()
        busy = self.pending(a)
        if not busy and c['provider'] != c['requested_provider']:
            c['provider'] = c['requested_provider']
            self.save_config(c)
            self.event('provider-changed', provider=c['provider'])
        request = None
        if a and a.get('request_id'):
            d = self.request_dir(a['request_id'])
            request = read(d / 'request.json')
            if request:
                request['stream_offsets'] = {n: (d / n).stat().st_size if (d / n).exists() else 0 for n in ('stdout.jsonl', 'stderr.log')}
        controller = {'alive': False, 'bridge_aware': False, 'pid': None}
        lock = self.project / '.lemvo-owner.lock'
        try:
            pid = int((lock / 'pid').read_text().strip())
            start = (lock / 'starttime').read_text().strip()
            alive = bool(start and identity(pid) == start and b'lemvo-run' in Path('/proc/%s/cmdline' % pid).read_bytes())
            aware = alive and c.get('controller_pids', {}).get(str(pid)) == start
            if alive and not aware:
                # `switch unattended` can spawn through the existing runtime.
                # Its inherited route, not an unrelated process name, proves adoption.
                environ = Path('/proc/%s/environ' % pid).read_bytes().split(b'\0')
                aware = ('LEMVO_DESKTOP_PROJECT=' + c['project_id']).encode() in environ
                if aware:
                    c.setdefault('controller_pids', {})[str(pid)] = start
                    c['started'] = True
                    self.save_config(c)
            controller = {'alive': alive, 'bridge_aware': aware, 'pid': pid}
        except (OSError, ValueError):
            pass
        runtime_status = (self.project / '.lemvo-status').read_text() if (self.project / '.lemvo-status').exists() else None
        state = (runtime_status or '').split('|')[0]
        completed = (self.project / 'CAMPAIGN-DONE').exists() or state == 'COMPLETED'
        stopped = state in ('PAUSED', 'BLOCKED', 'STALLED') or any((self.project / n).exists() for n in ('STOP', 'HOLD', 'BLOCKED-USER'))
        continuation = 'completed' if completed else 'paused' if stopped else 'running' if controller['bridge_aware'] else 'not_started' if not c.get('started') else 'controller_missing'
        return {'project': str(self.project), 'project_id': c['project_id'], 'provider': c['provider'],
                'requested_provider': c['requested_provider'], 'busy': busy,
                'server_approval': c.get('server_approval','preserve'),
                'active': a if busy else None, 'request': request,
                'worker_seen': c.get('worker_seen'), 'worker_id': c.get('worker_id'),
                'runtime_status': runtime_status, 'controller': controller, 'continuation': continuation,
                'mode': (self.project / '.lemvo-mode').read_text().strip() if (self.project / '.lemvo-mode').exists() else None}

    def owned_request(self, data):
        d = self.request_dir(data['request_id'])
        r = read(d / 'request.json')
        if not r or r.get('status') != 'claimed' or r.get('token') != data.get('token'):
            raise ValueError('Request claim is absent, finished or belongs to another worker')
        return d, r

    def check_agent(self, data):
        if data.get('request_id'):
            self.owned_request(data)

    def file_path(self, name):
        path = (self.project / name).resolve()
        if self.project != path and self.project not in path.parents:
            raise ValueError('File must stay within the registered project')
        if path == self.root or self.root in path.parents:
            raise ValueError('Bridge state is managed by the bridge, not file commands')
        return path

    def rpc(self, data):
        op = data['op']
        # Ordinary execution does not hold the metadata lock while commands run.
        if op in ('exec', 'put', 'get'):
            with self.lock():
                self.config()
                self.check_agent(data)
            if op == 'exec':
                p = subprocess.run(['bash', '-s'], input=data['script'].encode(), cwd=str(self.project),
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   timeout=float(data.get('timeout', 120)))
                return {'rc': p.returncode, 'stdout': p.stdout[-2097152:].decode('utf-8', 'replace'),
                        'stderr': p.stderr[-2097152:].decode('utf-8', 'replace')}
            p = self.file_path(data['path'])
            if op == 'get':
                b = p.read_bytes()
                if len(b) > 8388608:
                    raise ValueError('Use scp for files larger than 8 MiB')
            else:
                b = base64.b64decode(data['content_b64'], validate=True)
                p.parent.mkdir(parents=True, exist_ok=True)
                tmp = p.with_name('.' + p.name + '.desktop-' + uuid.uuid4().hex)
                tmp.write_bytes(b)
                os.replace(str(tmp), str(p))
            return {'path': str(p), 'sha256': hashlib.sha256(b).hexdigest(),
                    'content_b64': base64.b64encode(b).decode() if op == 'get' else None}
        with self.lock():
            if op == 'init':
                if (self.root / 'config.json').exists():
                    if data.get('server_approval',self.config().get('server_approval','preserve'))!=self.config().get('server_approval','preserve'):
                        raise ValueError('Existing project approval policy is preserved; registration cannot silently change it')
                    return self.status_locked()
                # Existing live campaigns must be adopted at a safe boundary, not silently stolen.
                if (self.project / '.lemvo-owner.lock').exists() or (self.project / '.autoloop-turn-active').exists():
                    raise ValueError('Existing controller/turn: register only after a safe controller boundary')
                default_codex = shutil.which('codex')
                if not default_codex:
                    for candidate in (Path.home() / '.local/bin/codex', Path.home() / '.npm-global/bin/codex'):
                        if candidate.is_file() and os.access(str(candidate), os.X_OK):
                            default_codex = str(candidate)
                            break
                command = data.get('server_command') or [default_codex]
                if not command or not command[0]:
                    raise ValueError('Server codex command is missing')
                runtime = Path(data['runtime']).resolve(strict=True)
                c = {'version': 1, 'project_id': uuid.uuid4().hex, 'provider': data.get('provider', 'windows'),
                     'requested_provider': data.get('provider', 'windows'), 'server_command': command,
                     'runtime': str(runtime), 'sessions': {}, 'created': time.time()}
                approval=data.get('server_approval','preserve')
                if approval not in ('preserve','auto-review'):
                    raise ValueError('Invalid server approval mode')
                c['server_approval']=approval
                sid = self.project / '.autoloop-session-id'
                if sid.exists():
                    c['sessions']['server'] = sid.read_text().strip()
                self.save_config(c)
                bindir = self.root / 'bin'
                bindir.mkdir(exist_ok=True)
                wrapper = bindir / 'codex'
                wrapper.write_text('#!/bin/sh\nexec %s %s engine "$@"\n' %
                                   (shlex.quote(sys.executable), shlex.quote(str(Path(__file__).resolve()))))
                wrapper.chmod(0o700)
                self.event('registered')
                return self.status_locked()
            c = self.config()
            if op == 'status':
                return self.status_locked()
            if op == 'provider':
                if data['provider'] not in ('windows', 'server'):
                    raise ValueError('Provider must be windows or server')
                c['requested_provider'] = data['provider']
                self.save_config(c)
                self.event('provider-requested', provider=data['provider'])
                return self.status_locked()
            if op == 'poll':
                c['worker_seen'] = time.time()
                c['worker_id'] = data['worker_id']
                self.save_config(c)
                s = self.status_locked()
                # A claim is durable: never steal because a heartbeat expires.
                if s['request'] and s['request']['status'] == 'claimed' and s['request'].get('worker_id') != data['worker_id']:
                    s['request'] = None
                return s
            if op == 'claim':
                d = self.request_dir(data['request_id'])
                r = read(d / 'request.json')
                a = self.active()
                if not a or a.get('request_id') != data['request_id'] or c['requested_provider'] != 'windows':
                    raise ValueError('Request is no longer eligible')
                if r['status'] == 'claimed' and r.get('worker_id') == data['worker_id']:
                    return r
                if r['status'] != 'queued':
                    raise ValueError('Request already claimed or cancelled')
                r.update(status='claimed', worker_id=data['worker_id'], token=uuid.uuid4().hex, claimed_at=time.time())
                atomic(d / 'request.json', r)
                self.event('claimed', request_id=r['id'], worker_id=data['worker_id'])
                return r
            if op == 'stream':
                d, r = self.owned_request(data)
                name = data['stream']
                if name not in ('stdout.jsonl', 'stderr.log'):
                    raise ValueError('Invalid stream')
                p = d / name
                size = p.stat().st_size if p.exists() else 0
                offset = int(data['offset'])
                chunk = base64.b64decode(data['content_b64'], validate=True)
                if offset < size:
                    with p.open('rb') as f:
                        f.seek(offset)
                        if f.read(len(chunk)) != chunk:
                            raise ValueError('Retry bytes differ from accepted stream')
                    return {'offset': size}
                if offset != size:
                    raise ValueError('Non-contiguous stream')
                with p.open('ab') as f:
                    f.write(chunk)
                    f.flush()
                return {'offset': size + len(chunk)}
            if op == 'complete':
                d = self.request_dir(data['request_id'])
                old = read(d / 'completion.json')
                if old:
                    if old.get('token') != data['token']:
                        raise ValueError('Wrong completion owner')
                    return old
                d, r = self.owned_request(data)
                for name in ('stdout.jsonl', 'stderr.log'):
                    p = d / name
                    b = p.read_bytes() if p.exists() else b''
                    if hashlib.sha256(b).hexdigest() != data['hashes'][name]:
                        raise ValueError('Completion requires all output bytes')
                result = {'rc': int(data['rc']), 'token': data['token'], 'finished_at': time.time(), 'hashes': data['hashes']}
                # Preserve the real Windows SID even if the Linux adapter was interrupted.
                for line in (d / 'stdout.jsonl').read_text(errors='replace').splitlines() if (d / 'stdout.jsonl').exists() else []:
                    try:
                        event = json.loads(line)
                        if event.get('type') == 'thread.started' and event.get('thread_id'):
                            c.setdefault('sessions', {})['windows'] = event['thread_id']
                    except ValueError:
                        pass
                self.save_config(c)
                atomic(d / 'completion.json', result)
                self.event('completed', request_id=r['id'], rc=result['rc'])
                return result
            if op == 'control':
                runtime = c['runtime']
                command = data['command']
                if command not in ('start', 'status', 'switch', 'advance', 'steer'):
                    raise ValueError('Unsupported control command')
                env = dict(os.environ)
                env['PATH'] = str(self.root / 'bin') + os.pathsep + env.get('PATH', '')
                env['LEMVO_DESKTOP_PROJECT'] = c['project_id']
                args = ['bash', runtime, command, str(self.project)]
                if command == 'start':
                    existing = self.status_locked()['controller']
                    if existing['alive']:
                        if not existing['bridge_aware']:
                            raise ValueError('Active controller was not launched through this bridge; adopt at a safe boundary')
                        return {'already_running': True, 'pid': existing['pid']}
                    args += ['--engine', 'codex']
                    if data.get('mode'):
                        if data['mode'] not in ('guided', 'unattended'):
                            raise ValueError('Invalid mode')
                        args += ['--mode', data['mode']]
                    with (self.root / 'controller.log').open('ab') as log:
                        p = subprocess.Popen(args, cwd=str(self.project), env=env, stdin=subprocess.DEVNULL,
                                             stdout=log, stderr=log, start_new_session=True)
                    c['started'] = True
                    c['controller_pids'] = {str(p.pid): identity(p.pid)}
                    self.save_config(c)
                    return {'spawned_pid': p.pid, 'note': 'Existing Lemvo owner lock decides whether this launch owns the campaign'}
                if command == 'switch':
                    if data['mode'] not in ('guided', 'unattended'):
                        raise ValueError('Invalid mode')
                    args.append(data['mode'])
                if command == 'steer':
                    src = self.root / ('guidance-' + uuid.uuid4().hex + '.md')
                    src.write_text(data['message'], encoding='utf-8')
                    args.append(str(src))
                # advance can run a whole model round. Detach it, preserving existing owner semantics.
                if command == 'advance':
                    with (self.root / 'controller.log').open('ab') as log:
                        p = subprocess.Popen(args, cwd=str(self.project), env=env, stdin=subprocess.DEVNULL,
                                             stdout=log, stderr=log, start_new_session=True)
                    return {'spawned_pid': p.pid}
                p = subprocess.run(args, cwd=str(self.project), env=env, capture_output=True, timeout=30)
                return {'rc': p.returncode, 'stdout': p.stdout.decode('utf-8', 'replace'), 'stderr': p.stderr.decode('utf-8', 'replace')}
            raise ValueError('Unknown operation: ' + op)


def arguments(argv, sid, prompt):
    # Existing adapter emits exec [flags] [resume SID] PROMPT. Preserve flags.
    argv = list(argv)
    if not argv or argv[0] != 'exec':
        raise ValueError('Desktop bridge accepts codex exec only')
    argv = argv[:-1]
    if 'resume' in argv:
        idx = argv.index('resume')
        del argv[idx:idx + 2]
    if sid:
        argv += ['resume', sid]
    return argv + [prompt]


def server_arguments(argv, sid, prompt, approval='preserve'):
    argv=list(argv)
    # Explicit per-project opt-in only; never change server/global configuration.
    if approval=='auto-review':
        for i,arg in enumerate(argv[:-1]):
            if arg in ('-s','--sandbox'):
                if argv[i+1]!='workspace-write':
                    raise ValueError('auto-review requires workspace-write; explicit sandbox policy preserved')
                del argv[i:i+2]
                break
            if arg.startswith('--sandbox='):
                if arg!='--sandbox=workspace-write':
                    raise ValueError('auto-review requires workspace-write; explicit sandbox policy preserved')
                del argv[i]
                break
        if '--approve-for-me' not in argv:argv.insert(1,'--approve-for-me')
    elif approval!='preserve':
        raise ValueError('Invalid server approval mode')
    prompt='''Server decision worker: research files and job records are authoritative here.
Background experiments must survive the current tool call and record HOST PIDs.
A process started inside a temporary PID sandbox may die when the command returns;
nohup/start_new_session alone does not solve that. Use the project's existing durable
job runner, or request approval for the authorized host-level launch. Never disable
the sandbox globally. If launch approval is unavailable, report that specific launch
blocker rather than registering a sandbox PID as a running host job. Preserve logs,
job identities, and existing training. This is execution guidance, not a research gate.

'''+prompt
    return arguments(argv,sid,prompt)


def engine(argv):
    p = Project(os.getcwd())
    rid = uuid.uuid4().hex
    my = {'pid': os.getpid(), 'start': identity(os.getpid()), 'request_id': None}
    prompt = argv[-1]
    with p.lock():
        if p.pending(p.active()):
            raise RuntimeError('service unavailable: another desktop/server model invocation is still in flight')
        c = p.config()
        c['provider'] = c['requested_provider']
        p.save_config(c)
        my['provider'] = c['provider']
        atomic(p.root / 'active.json', my)
    child = None
    try:
        if c['provider'] == 'windows':
            d = p.request_dir(rid)
            d.mkdir(parents=True)
            with p.lock():
                my['request_id'] = rid
                atomic(p.root / 'active.json', my)
                r = {'id': rid, 'status': 'queued', 'prompt': prompt,
                     'sid': c.get('sessions', {}).get('windows'), 'created_at': time.time(),
                     'project': str(p.project)}
                atomic(d / 'request.json', r)
            offsets = {'stdout.jsonl': 0, 'stderr.log': 0}
            while True:
                with p.lock():
                    c = p.config()
                    r = read(d / 'request.json')
                    if c['requested_provider'] == 'server' and r['status'] == 'queued':
                        r['status'] = 'cancelled'
                        atomic(d / 'request.json', r)
                        c['provider'] = 'server'
                        p.save_config(c)
                        my.update(provider='server', request_id=None)
                        atomic(p.root / 'active.json', my)
                        break
                for name, stream in (('stdout.jsonl', sys.stdout.buffer), ('stderr.log', sys.stderr.buffer)):
                    if (d / name).exists():
                        with (d / name).open('rb') as f:
                            f.seek(offsets[name])
                            b = f.read()
                        if b:
                            stream.write(b)
                            stream.flush()
                            offsets[name] += len(b)
                done = read(d / 'completion.json')
                if done:
                    # completion is only published after full streams; flush the last tail too.
                    for name, stream in (('stdout.jsonl', sys.stdout.buffer), ('stderr.log', sys.stderr.buffer)):
                        if (d / name).exists():
                            with (d / name).open('rb') as f:
                                f.seek(offsets[name])
                                stream.write(f.read())
                                stream.flush()
                    for line in (d / 'stdout.jsonl').read_text(errors='replace').splitlines():
                        try:
                            ev = json.loads(line)
                            if ev.get('type') == 'thread.started' and ev.get('thread_id'):
                                with p.lock():
                                    c = p.config()
                                    c.setdefault('sessions', {})['windows'] = ev['thread_id']
                                    p.save_config(c)
                        except ValueError:
                            pass
                    return 64 if done['rc']==2 else done['rc']
                time.sleep(0.3)
        with p.lock():
            c = p.config()
        cmd = c['server_command'] + server_arguments(argv, c.get('sessions', {}).get('server'), prompt, c.get('server_approval','preserve'))
        env = dict(os.environ)
        env['PATH'] = os.pathsep.join(x for x in env.get('PATH', '').split(os.pathsep) if x != str(p.root / 'bin'))
        child = subprocess.Popen(cmd, cwd=str(p.project), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE)
        with p.lock():
            my.update(child_pid=child.pid, child_start=identity(child.pid))
            atomic(p.root / 'active.json', my)
        def terminate(signum, frame):
            if child.poll() is None:
                child.terminate()
            raise SystemExit(128 + signum)
        signal.signal(signal.SIGTERM, terminate)
        signal.signal(signal.SIGINT, terminate)
        for line in iter(child.stdout.readline, b''):
            sys.stdout.buffer.write(line)
            sys.stdout.buffer.flush()
            try:
                ev = json.loads(line)
                if ev.get('type') == 'thread.started' and ev.get('thread_id'):
                    with p.lock():
                        c = p.config()
                        c.setdefault('sessions', {})['server'] = ev['thread_id']
                        p.save_config(c)
            except ValueError:
                pass
        rc=child.wait()
        return 64 if rc==2 else rc
    finally:
        with p.lock():
            a = p.active()
            if a and a.get('pid') == my['pid'] and a.get('start') == my['start']:
                # Keep a claimed Windows turn visible if this Linux wrapper was interrupted.
                unfinished = False
                if a.get('request_id'):
                    d = p.request_dir(a['request_id'])
                    r = read(d / 'request.json', {})
                    unfinished = r.get('status') == 'claimed' and not (d / 'completion.json').exists()
                if not unfinished and (child is None or child.poll() is not None):
                    atomic(p.root / 'active.json', None)
                    p.status_locked()


if __name__ == '__main__':
    try:
        if sys.argv[1] == 'rpc':
            print(json.dumps(Project(sys.argv[2]).rpc(json.load(sys.stdin)), ensure_ascii=False))
        elif sys.argv[1] == 'engine':
            sys.exit(engine(sys.argv[2:]))
        else:
            raise ValueError('Use rpc PROJECT or engine CODEX_ARGUMENTS')
    except Exception as exc:
        if len(sys.argv) > 1 and sys.argv[1] == 'engine':
            print(json.dumps({'type': 'turn.failed', 'error': {'message': 'service unavailable: desktop bridge: ' + str(exc)}}), flush=True)
            sys.exit(75)
        print(json.dumps({'error': str(exc)}), file=sys.stderr)
        sys.exit(1)
