from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from .collector import Collector, Processes, ID
from .server import serve
from .store import atomic, key, load, read_text, Store
from .transcripts import metadata


def parser():
    p = argparse.ArgumentParser(description='Lemvo single-host multi-session console (no model calls)')
    p.add_argument('--state-dir', default=str(Path.home() / '.local/state/lemvo/console'))
    subs = p.add_subparsers(dest='command', required=True)
    for name in ('serve', 'start'):
        s = subs.add_parser(name)
        s.add_argument('--port', type=int, default=8765)
        s.add_argument('--allow-controls', action='store_true')
        s.add_argument('--poll-seconds', type=float, default=5)
        s.add_argument('--warn-seconds', type=float, default=1800, help='Observation warning only; never terminates a job')
    subs.add_parser('status')
    subs.add_parser('stop', help='Stop only this console service; sessions and experiments continue')
    subs.add_parser('snapshot')
    subs.add_parser('url')
    s = subs.add_parser('register', help='Observe a project, including jobs when its conversation is closed')
    s.add_argument('project')
    s = subs.add_parser('attach', help='Bind an existing process to a transcript; does not inject input')
    s.add_argument('--pid', type=int, required=True)
    s.add_argument('--engine', choices=('claude', 'codex'), required=True)
    s.add_argument('--project', required=True)
    s.add_argument('--transcript')
    s = subs.add_parser('launch', help='Launch an interactive CLI with registration; native terminal I/O is preserved')
    s.add_argument('--engine', choices=('claude', 'codex'), required=True)
    s.add_argument('--project', required=True)
    s.add_argument('argv', nargs=argparse.REMAINDER)
    s = subs.add_parser('progress', help='Publish a concrete loading/training counter for a registered job')
    s.add_argument('--project', required=True)
    s.add_argument('--run-id', required=True)
    s.add_argument('--pid', type=int, required=True)
    s.add_argument('--phase', choices=('loading_model', 'loading_data', 'training', 'evaluating', 'tool'), required=True)
    s.add_argument('--current', type=float)
    s.add_argument('--total', type=float)
    s.add_argument('--unit', default='steps')
    s.add_argument('--detail', default='')
    return p


def register_process(store, pid, engine, project, transcript=None, session_id=None):
    proc = Processes()
    proc.scan()
    identity = proc.items.get(pid)
    if not identity or identity['state'] == 'Z':
        raise ValueError('Process is not a live process owned by this user')
    meta = metadata(transcript) if transcript else {}
    if transcript:
        tp = Path(transcript).resolve(strict=True)
        if not tp.is_file() or tp.suffix != '.jsonl' or tp.stat().st_uid != os.getuid():
            raise ValueError('Transcript must be a JSONL file owned by this user')
        if meta.get('engine') and meta['engine'] != engine:
            raise ValueError('Transcript engine does not match')
        transcript = str(tp)
    sid = meta.get('session_id') or session_id
    ident = key(engine, sid, pid, identity['start']) if sid else key(engine, pid, identity['start'])
    session = {'id': ident, 'engine': engine, 'pid': pid, 'pid_start': identity['start'],
               'session_id': sid, 'transcript': transcript, 'registered_at': time.time()}
    store.register(project, session)
    return session


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    if sys.platform != 'linux':
        p.error('Run the console on the Linux research server; open its forwarded URL from Windows.')
    store = Store(args.state_dir)
    try:
        if args.command in ('serve', 'start'):
            if not 1 <= args.port <= 65535 or not 1 <= args.poll_seconds <= 300 or args.warn_seconds < 1:
                raise ValueError('Invalid port/poll interval/warning interval')
            if args.command == 'serve':
                serve(store, args.port, args.allow_controls, args.poll_seconds, args.warn_seconds)
            else:
                proc = Processes()
                proc.scan()
                old = load(store.path / 'server.json', {})
                if proc.same(old.get('pid'), old.get('pid_start')):
                    print('Console already running; use url or status. Existing configuration preserved.')
                    return
                entry = Path(__file__).resolve().parents[1] / 'lemvo-console.py'
                cmd = [sys.executable, str(entry), '--state-dir', str(store.path), 'serve', '--port', str(args.port),
                       '--poll-seconds', str(args.poll_seconds), '--warn-seconds', str(args.warn_seconds)]
                if args.allow_controls:
                    cmd.append('--allow-controls')
                with (store.path / 'server.log').open('ab') as log:
                    child = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
                for _ in range(50):
                    info = load(store.path / 'server.json', {})
                    if info.get('pid') == child.pid:
                        print('Console started. Run the url subcommand to open it. No research session was started or interrupted.')
                        return
                    if child.poll() is not None:
                        raise ValueError('Console failed to start; see ' + str(store.path / 'server.log'))
                    time.sleep(.1)
                raise ValueError('Startup not confirmed; see server.log')
        elif args.command in ('status', 'stop'):
            info = load(store.path / 'server.json', {})
            proc = Processes()
            proc.scan()
            alive = proc.same(info.get('pid'), info.get('pid_start'))
            if args.command == 'stop' and alive:
                a = proc.items[int(info['pid'])]['args']
                if not any('lemvo-console.py' in v for v in a) or 'serve' not in a:
                    raise ValueError('Console process identity mismatch; no signal sent')
                os.kill(int(info['pid']), signal.SIGTERM)
            print(json.dumps(dict(info, alive=alive, operation=args.command)))
        elif args.command == 'url':
            print(read_text(store.path / 'url').strip() or 'Not started')
        elif args.command == 'snapshot':
            print(json.dumps(Collector(store).collect(), ensure_ascii=False, indent=2))
        elif args.command == 'register':
            print(store.register(args.project))
        elif args.command == 'attach':
            print(json.dumps(register_process(store, args.pid, args.engine, args.project, args.transcript)))
        elif args.command == 'launch':
            project = Path(args.project).expanduser().resolve(strict=True)
            cmd = args.argv[1:] if args.argv and args.argv[0] == '--' else args.argv
            if not cmd:
                cmd = [args.engine]
            from .collector import session_hint
            sid = session_hint(cmd)
            if (args.engine == 'claude' and Path(cmd[0]).name in ('claude', 'claude.exe')
                    and not any(a in cmd for a in ('-c', '--continue', '--resume', '-r', '--fork-session', '--session-id'))
                    and not any(a.startswith('--resume=') or a.startswith('--session-id=') for a in cmd)):
                import uuid
                sid = str(uuid.uuid4())
                cmd = [cmd[0], '--session-id', sid] + cmd[1:]
            child = subprocess.Popen(cmd, cwd=project)  # Keep terminal I/O; do not run through a shell.
            register_process(store, child.pid, args.engine, project, session_id=sid)
            raise SystemExit(child.wait())
        elif args.command == 'progress':
            import math
            if not ID.fullmatch(args.run_id):
                raise ValueError('Invalid run ID')
            if any(n is not None and (not math.isfinite(n) or n < 0) for n in (args.current, args.total)):
                raise ValueError('Counters must be finite and nonnegative')
            project = str(Path(args.project).expanduser().resolve(strict=True))
            proc = Processes()
            proc.scan()
            target = proc.items.get(args.pid)
            if not target or target['state'] == 'Z':
                raise ValueError('Progress PID is not a live process owned by this user')
            report = {'schema': 1, 'run_id': args.run_id, 'pid': args.pid, 'pid_start': target['start'],
                      'phase': args.phase, 'current': args.current, 'total': args.total,
                      'unit': args.unit, 'detail': args.detail[:400], 'at': time.time()}
            atomic(store.path / 'progress' / (key(project, args.run_id) + '.json'), report)
            print('Progress recorded; repeating the same counter does not count as new progress.')
    except (ValueError, OSError, KeyError) as exc:
        p.exit(2, str(exc) + '\n')
