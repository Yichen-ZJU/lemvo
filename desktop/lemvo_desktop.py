#!/usr/bin/env python3
"""Windows Codex worker and SSH control surface for existing Lemvo projects."""
import argparse
import base64
import concurrent.futures
import contextlib
import ctypes
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time
import uuid

DEFAULT_HOME = Path.home() / '.lemvo-desktop'


def atomic(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    t = path.with_name('.' + path.name + '.' + uuid.uuid4().hex)
    with t.open('w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(str(t), str(path))


def read(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return default


@contextlib.contextmanager
def exclusive(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    f = path.open('a+b')
    try:
        if os.fstat(f.fileno()).st_size == 0:
            f.write(b'0')
            f.flush()
        f.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        raise RuntimeError('Another worker already owns this registry')
    try:
        yield
    finally:
        f.close()


def detached(argv, cwd, log):
    kwargs = {'creationflags': subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {'start_new_session': True}
    with Path(log).open('ab') as f:
        return subprocess.Popen(argv, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=f, stderr=f, **kwargs)


def psquote(value):
    return "'" + str(value).replace("'", "''") + "'"


def discover_codex():
    found=shutil.which('codex')
    if found:return found
    root=Path(os.environ.get('LOCALAPPDATA',str(Path.home()/'AppData/Local')))/'OpenAI/Codex/bin'
    candidates=list(root.glob('*/codex.exe'))
    return str(max(candidates,key=lambda p:p.stat().st_mtime)) if candidates else None


def process_identity(pid):
    try:
        pid = int(pid)
        if os.name != 'nt':
            fields = Path('/proc/%s/stat' % pid).read_text().rsplit(')', 1)[1].split()
            return fields[19] if fields[0] != 'Z' else None
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            created, exited, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
            kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
            code = wintypes.DWORD()
            kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:
                return None
            if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel_time), ctypes.byref(user_time)):
                return None
            return str((created.dwHighDateTime << 32) | created.dwLowDateTime)
        finally:
            kernel.CloseHandle(handle)
    except (OSError, ValueError, TypeError, IndexError):
        return None


class Desktop:
    def __init__(self, registry):
        self.registry = Path(registry).resolve()
        self.home = self.registry.parent
        self.home.mkdir(parents=True, exist_ok=True)
        if not self.registry.exists():
            atomic(self.registry, {'version': 1, 'worker_id': uuid.uuid4().hex, 'projects': {}})

    def config(self):
        return read(self.registry)

    def project(self, name):
        return self.config()['projects'][name]

    def ssh(self, c, command, data=None, timeout=45):
        args = ['ssh', '-p', str(c.get('port', 22)), '-o', 'BatchMode=yes', '-o', 'ClearAllForwardings=yes',
                '-o', 'ConnectTimeout=10', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=2', c['host'], command]
        r = subprocess.run(args, input=data, capture_output=True, timeout=timeout)
        if r.returncode:
            raise RuntimeError(r.stderr.decode('utf-8', 'replace')[-3000:] or r.stdout.decode('utf-8', 'replace')[-3000:])
        return r.stdout

    def rpc(self, c, obj):
        cmd = ' '.join(shlex.quote(x) for x in [c.get('python', 'python3'), c['bridge'], 'rpc', c['project']])
        return json.loads(self.ssh(c, cmd, json.dumps(obj).encode('utf-8'), max(45, float(obj.get('timeout', 0)) + 15)))

    def register(self, args):
        if any(x not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for x in args.name) or not args.name:
            raise ValueError('Name must contain letters, numbers, hyphen or underscore')
        cfg = self.config()
        if args.name in cfg['projects']:
            raise ValueError('Name already registered; use status/provider/mode without re-registering')
        c = {'host': args.host, 'port': args.port, 'project': args.project, 'python': args.remote_python,
             'codex': args.codex or discover_codex(), 'codex_auto': not bool(args.codex), 'model': args.model}
        if not c['codex']:
            raise ValueError('Local codex executable not found; pass --codex')
        bridge = Path(__file__).with_name('remote_codex_bridge.py').read_bytes()
        digest = hashlib.sha256(bridge).hexdigest()
        # Installation is versioned; never overwrite a helper imported by an active bridge.
        bootstrap = 'import pathlib,sys,base64,json; p=pathlib.Path.home()/".local/share/lemvo-desktop"/%r; p.mkdir(parents=True,exist_ok=True); f=p/"remote_codex_bridge.py"; b=base64.b64decode(sys.stdin.buffer.read()); f.write_bytes(b); print(json.dumps({"bridge":str(f)}))' % digest[:16]
        uploaded = json.loads(self.ssh(c, shlex.quote(c['python']) + ' -c ' + shlex.quote(bootstrap), base64.b64encode(bridge)))
        c['bridge'] = uploaded['bridge']
        init = {'op': 'init', 'runtime': args.runtime, 'provider': args.provider,
                'server_approval':getattr(args,'server_approval','preserve')}
        if getattr(args, 'server_codex', None):
            init['server_command'] = [args.server_codex]
        result = self.rpc(c, init)
        c['project'] = result['project']
        c['project_id'] = result['project_id']
        with exclusive(self.home / 'registry.lock'):
            cfg = self.config()
            if args.name in cfg['projects']:
                raise ValueError('Name registered concurrently; existing mapping preserved')
            if any(x.get('project_id') == c['project_id'] for x in cfg['projects'].values()):
                raise ValueError('This canonical server project is already registered under another name')
            cfg['projects'][args.name] = c
            atomic(self.registry, cfg)
        return result

    def workspace(self, name):
        c = self.config().get('projects', {}).get(name, {})
        p = self.home / 'projects' / c.get('project_id', name)
        p.mkdir(parents=True, exist_ok=True)
        return p

    def model_prompt(self, name, c, r):
        cli = '%s %s --registry %s' % (psquote(sys.executable), psquote(Path(__file__).resolve()), psquote(self.registry))
        auth = '--request %s --token %s' % (r['id'], r['token'])
        return '''You are the Windows Codex decision worker for a Lemvo research project.
The authoritative project, research-state.yaml, STATE.md, results, jobs, turn artifacts,
git repository and guidance queue all remain on the SSH server. The local working
directory is only a command/patch scratchpad. Make research changes on the server.
Host: %s
Remote project: %s
The same Linux Lemvo controller owns the outer loop, jobs, guidance and accounting.
Read the current remote state after resuming: another provider may have advanced it.
Use installed Lemvo research skills; execute their Linux helpers on the remote host.
Do not start another controller or a second model worker for this project.

Remote actions from PowerShell (write Bash commands to a UTF-8 local file first):
& %s exec %s %s --script <local-bash-file>
& %s put %s %s --local <local-file> --remote <project-relative-file>
& %s get %s %s --remote <project-relative-file> --local <local-file>
The exec helper runs Bash in the authoritative remote project. Use it for git,
reading state, running tests, and existing Lemvo job-registration commands. Keep
long training detached with logs and register it in the existing job ledger.
Do not copy server PIDs into Windows process operations. Preserve user constraints.
Use the provided helper for project changes; its request token binds actions to this turn.
Research success is decided from results, not this transport arrangement.

CURRENT CONTROLLER MESSAGE:
%s
''' % (c['host'], c['project'], cli, psquote(name), auth, cli, psquote(name), auth, cli, psquote(name), auth, r['prompt'])

    def launch(self, name, c, request):
        root = self.workspace(name) / 'requests' / request['id']
        root.mkdir(parents=True, exist_ok=True)
        with exclusive(root / 'launch.lock'):
            return self.launch_locked(name, c, request, root)

    def launch_locked(self, name, c, request, root):
        spec_path = root / 'spec.json'
        if spec_path.exists():
            return root
        prompt = self.model_prompt(name, c, request)
        (root / 'prompt.md').write_text(prompt, encoding='utf-8')
        executable=discover_codex() if c.get('codex_auto') else c['codex']
        if not executable:raise ValueError('Local Codex CLI unavailable')
        prefix = executable if isinstance(executable, list) else [executable]
        command = prefix + ['exec', '--approve-for-me', '--skip-git-repo-check', '--json']
        if c.get('model'):
            command += ['--model', c['model']]
        if request.get('sid'):
            command += ['resume', request['sid']]
        command += ['-']
        spec = {'command': command, 'cwd': str(self.workspace(name)), 'request_id': request['id'],
                'token': request['token'], 'created_at': time.time()}
        # Persist launch intent first. An ambiguous interrupted launch is never blindly repeated.
        atomic(spec_path, spec)
        try:
            detached([sys.executable, str(Path(__file__).resolve()), 'run-local', str(spec_path)], root, root / 'runner.log')
        except Exception as exc:
            (root / 'stdout.jsonl').write_text('')
            (root / 'stderr.log').write_text(str(exc), encoding='utf-8')
            atomic(root / 'done.json', {'rc': 75, 'launch_failed': True, 'error': str(exc)})
        return root

    def sync_request(self, name, c, request):
        root = self.workspace(name) / 'requests' / request['id']
        if not (root / 'spec.json').exists():
            return {'state': 'CLAIM_WITHOUT_LOCAL_LAUNCH', 'request_id': request['id']}
        auth = {'request_id': request['id'], 'token': request['token']}
        for stream in ('stdout.jsonl', 'stderr.log'):
            path = root / stream
            if not path.exists():
                continue
            offset = request.get('stream_offsets', {}).get(stream, 0)
            # Bounded chunk transfer keeps one noisy project from starving other servers.
            for _ in range(8):
                with path.open('rb') as f:
                    f.seek(offset)
                    b = f.read(262144)
                if not b:
                    break
                resp = self.rpc(c, dict(auth, op='stream', stream=stream, offset=offset, content_b64=base64.b64encode(b).decode()))
                offset = resp['offset']
            if offset < path.stat().st_size:
                return {'state': 'SYNCING', 'request_id': request['id']}
        done = read(root / 'done.json')
        if done:
            hashes = {n: hashlib.sha256((root / n).read_bytes()).hexdigest() for n in ('stdout.jsonl', 'stderr.log')}
            response = self.rpc(c, dict(auth, op='complete', rc=done['rc'], hashes=hashes))
            return {'state': 'DELIVERED', 'request_id': request['id'], 'rc': response['rc']}
        runner = read(root / 'runner.json')
        if not runner:
            state = 'LAUNCH_PENDING'
        elif runner.get('start') and process_identity(runner['pid']) == runner['start']:
            state = 'MODEL_RUNNING'
        elif runner.get('child_start') and process_identity(runner.get('child_pid')) == runner['child_start']:
            state = 'ORPHAN_MODEL_RUNNING'
        else:
            state = 'NEEDS_RECOVERY'
        return {'state': state, 'request_id': request['id'], 'runner': runner}

    def tick(self, name, c, worker_id):
        try:
            status = self.rpc(c, {'op': 'poll', 'worker_id': worker_id})
            request = status.get('request')
            if request and request['status'] == 'queued' and status['requested_provider'] == 'windows':
                request = self.rpc(c, {'op': 'claim', 'request_id': request['id'], 'worker_id': worker_id})
                self.launch(name, c, request)
            if request and request['status'] == 'claimed' and request.get('worker_id') == worker_id:
                # A lost claim response occurs before durable launch intent, so this is safe.
                if not (self.workspace(name) / 'requests' / request['id'] / 'spec.json').exists():
                    self.launch(name, c, request)
                status['local'] = self.sync_request(name, c, request)
            return dict(name=name, ok=True, status=status)
        except Exception as exc:
            return {'name': name, 'ok': False, 'error': str(exc)}

    def worker(self, once=False, interval=3):
        with exclusive(self.home / 'worker.lock'):
            stop = self.home / 'worker.stop'
            stop.unlink(missing_ok=True)
            try:
                while not stop.exists():
                    cfg = self.config()
                    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(8, len(cfg['projects'])))) as pool:
                        futures = [pool.submit(self.tick, n, c, cfg['worker_id']) for n, c in cfg['projects'].items()]
                        results = [f.result() for f in futures]
                    atomic(self.home / 'worker-status.json', {'pid': os.getpid(), 'updated': time.time(), 'projects': results})
                    # Keep the system awake while supervising Windows-owned work. No persistent power-setting change.
                    if os.name == 'nt':
                        busy = any(not x['ok'] or x.get('status', {}).get('requested_provider') == 'windows' or x.get('status', {}).get('provider') == 'windows' for x in results)
                        ctypes.windll.kernel32.SetThreadExecutionState(0x80000001 if busy else 0x80000000)
                    if once:
                        return results
                    time.sleep(interval)
            finally:
                if os.name == 'nt':
                    ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)

    def handoff(self, names, provider, timeout):
        for name in names:
            self.rpc(self.project(name), {'op': 'provider', 'provider': provider})
        deadline = time.monotonic() + timeout
        while True:
            states = {n: self.rpc(self.project(n), {'op': 'status'}) for n in names}
            ready = all(s['provider'] == provider and not (s['active'] and s['active']['provider'] != provider)
                        and s.get('continuation') != 'controller_missing' for s in states.values())
            if provider == 'windows':
                ready = ready and all(s.get('worker_id') == self.config()['worker_id'] and time.time() - (s.get('worker_seen') or 0) < 30 for s in states.values())
            if ready or time.monotonic() >= deadline:
                return {'ready': ready, 'provider': provider, 'projects': states,
                        'note': 'Safe to close Windows; continuation shows whether each project is running, completed or paused' if ready and provider == 'server' else 'Pending turns are retained; check continuation/controller state; no forced restart or termination'}
            time.sleep(2)


def run_local(spec_path):
    spec_path = Path(spec_path)
    root = spec_path.parent
    spec = read(spec_path)
    atomic(root / 'runner.json', {'pid': os.getpid(), 'start': process_identity(os.getpid()), 'started_at': time.time(), 'request_id': spec['request_id']})
    rc = 75
    try:
        with (root / 'stdout.jsonl').open('wb') as out, (root / 'stderr.log').open('wb') as err:
            p = subprocess.Popen(spec['command'], cwd=spec['cwd'], stdin=subprocess.PIPE, stdout=out, stderr=err)
            atomic(root / 'runner.json', {'pid': os.getpid(), 'start': process_identity(os.getpid()), 'child_pid': p.pid, 'child_start': process_identity(p.pid), 'started_at': time.time(), 'request_id': spec['request_id']})
            p.communicate((root / 'prompt.md').read_bytes())
            rc = p.returncode
    except Exception as exc:
        (root / 'stdout.jsonl').touch(exist_ok=True)
        with (root / 'stderr.log').open('ab') as f:
            f.write(str(exc).encode())
    atomic(root / 'done.json', {'rc': rc, 'finished_at': time.time()})
    return rc


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--registry', default=str(DEFAULT_HOME / 'registry.json'))
    s = p.add_subparsers(dest='action', required=True)
    r = s.add_parser('register')
    r.add_argument('name')
    r.add_argument('--host', required=True)
    r.add_argument('--port', type=int, default=22)
    r.add_argument('--project', required=True)
    r.add_argument('--runtime', required=True)
    r.add_argument('--remote-python', default='python3')
    r.add_argument('--provider', choices=('windows', 'server'), default='windows')
    r.add_argument('--codex')
    r.add_argument('--server-codex', help='Absolute server Codex command; otherwise discover PATH or standard user bin locations')
    r.add_argument('--server-approval',choices=('preserve','auto-review'),default='preserve',help='Explicit per-project automatic approval-review opt-in; leaves global configuration untouched')
    r.add_argument('--model')
    for op in ('status', 'start', 'advance', 'mode', 'steer', 'provider', 'exec', 'put', 'get'):
        q = s.add_parser(op)
        q.add_argument('name')
        if op in ('start', 'mode'):
            q.add_argument('--mode', choices=('guided', 'unattended'), required=op == 'mode')
        if op == 'provider':
            q.add_argument('provider', choices=('windows', 'server'))
        if op == 'steer':
            q.add_argument('--message', required=True)
        if op in ('exec', 'put', 'get'):
            q.add_argument('--request')
            q.add_argument('--token')
        if op == 'exec':
            q.add_argument('--script', required=True)
            q.add_argument('--timeout', type=float, default=120)
        if op in ('put', 'get'):
            q.add_argument('--local', required=True)
            q.add_argument('--remote', required=True)
    q = s.add_parser('worker')
    q.add_argument('--once', action='store_true')
    q.add_argument('--interval', type=float, default=3)
    s.add_parser('start-worker')
    s.add_parser('stop-worker')
    q = s.add_parser('handoff')
    q.add_argument('name', help='Project name or all')
    q.add_argument('--to', choices=('server', 'windows'), required=True)
    q.add_argument('--wait', type=float, default=0)
    q = s.add_parser('run-local')
    q.add_argument('spec')
    return p


def main():
    a = parser().parse_args()
    if a.action == 'run-local':
        return run_local(a.spec)
    d = Desktop(a.registry)
    if a.action == 'register':
        result = d.register(a)
    elif a.action == 'worker':
        result = d.worker(a.once, a.interval)
    elif a.action == 'start-worker':
        proc = detached([sys.executable, str(Path(__file__).resolve()), '--registry', str(d.registry), 'worker'], d.home, d.home / 'worker.log')
        result = {'spawned_pid': proc.pid, 'status_file': str(d.home / 'worker-status.json')}
    elif a.action == 'stop-worker':
        # Planned shutdown requires completed handoff; do not abandon an active Windows model.
        result = d.handoff(list(d.config()['projects']), 'server', 0)
        if not result['ready']:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 2
        (d.home / 'worker.stop').write_text('stop\n')
    elif a.action == 'handoff':
        names = list(d.config()['projects']) if a.name == 'all' else [a.name]
        result = d.handoff(names, a.to, a.wait)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result['ready'] else 2
    else:
        c = d.project(a.name)
        if a.action in ('status', 'provider'):
            payload = {'op': a.action}
            if a.action == 'provider':
                payload['provider'] = a.provider
        elif a.action in ('exec', 'put', 'get'):
            payload = {'op': a.action, 'request_id': a.request, 'token': a.token}
            if a.action == 'exec':
                payload.update(script=Path(a.script).read_text(encoding='utf-8-sig'), timeout=a.timeout)
            else:
                payload['path'] = a.remote
                if a.action == 'put':
                    payload['content_b64'] = base64.b64encode(Path(a.local).read_bytes()).decode()
        else:
            payload = {'op': 'control', 'command': 'switch' if a.action == 'mode' else a.action}
            if a.action in ('start', 'mode'):
                payload['mode'] = a.mode
            if a.action == 'steer':
                payload['message'] = a.message
        result = d.rpc(c, payload)
        if a.action == 'get':
            Path(a.local).write_bytes(base64.b64decode(result.pop('content_b64')))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(result.get('rc', 0)) if isinstance(result, dict) else 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
