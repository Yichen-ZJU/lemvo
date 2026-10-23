from __future__ import annotations

import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

from .collector import Collector, Processes, project_info, ID
from .store import atomic, key, load, locked, read_text, redact


class App:
    def __init__(self, store, collector=None, allow_controls=False, interval=5):
        self.store = store
        self.collector = collector or Collector(store)
        self.allow_controls = allow_controls
        self.interval = interval
        self.stop = threading.Event()
        self.snapshot = {'at': 0, 'sessions': [], 'projects': [], 'hardware': {}, 'host': '', 'error': 'Starting collector'}
        self.snapshot_lock = threading.Lock()
        token_path = store.path / 'token'
        with locked(store.path / 'token.lock'):
            self.token = read_text(token_path, 256).strip()
            if not self.token:
                self.token = secrets.token_urlsafe(32)
                atomic(token_path, self.token)

    def poll(self):
        while not self.stop.is_set():
            try:
                snapshot = self.collector.collect()
                snapshot['controls_enabled'] = self.allow_controls
                with self.snapshot_lock:
                    self.snapshot = snapshot
            except Exception as exc:
                with self.snapshot_lock:
                    self.snapshot = dict(self.snapshot, error=redact(str(exc)), collection_failed_at=time.time())
            self.stop.wait(self.interval)

    def view(self):
        with self.snapshot_lock:
            return dict(self.snapshot, stale=time.time() - self.snapshot.get('at', 0) > max(20, self.interval * 4))

    def contract(self, engine):
        candidates = [Path.home() / ('.claude' if engine == 'claude' else '.codex') /
                      'skills/research-orchestrator/templates/runtime-contract.py']
        root = Path(__file__).resolve().parents[2]
        candidates += [root / engine / 'skills/research-orchestrator/templates/runtime-contract.py',
                       root / 'skills/research-orchestrator/templates/runtime-contract.py']
        return next((p for p in candidates if p.is_file()), None)

    def action(self, data):
        if not self.allow_controls:
            raise ValueError('This console was started read-only')
        rid = data.get('request_id', '')
        if not isinstance(rid, str) or not ID.fullmatch(rid):
            raise ValueError('A valid request_id is required')
        snapshot = self.view()
        if snapshot['stale']:
            raise ValueError('Observation is stale; refresh before sending a control request')
        info = next((p for p in snapshot['projects'] if p['id'] == data.get('project_id')), None)
        if not info:
            raise ValueError('Unknown project')
        project = Path(info['path'])
        fingerprint = key(json.dumps(data, sort_keys=True))
        receipt_path = self.store.path / 'actions' / (rid + '.json')
        with locked(self.store.path / 'actions.lock'):
            old = load(receipt_path)
            if old:
                if old.get('fingerprint') != fingerprint:
                    raise ValueError('Request ID already used with different contents')
                return old['receipt']
            proc = Processes()
            proc.scan()
            current = project_info(project, proc)
            if not current['owner_alive']:
                raise ValueError('No verified live Lemvo controller; bare conversations are observation-only')
            if data.get('owner_start') != current['owner_start'] or data.get('owner_pid') != current['owner_pid']:
                raise ValueError('Controller changed; refresh and select the current controller')
            if current['status'] == 'COMPLETED':
                raise ValueError('Campaign completed; controls do not reopen it')
            kind = data.get('kind')
            if kind == 'mode':
                mode = data.get('mode')
                if mode not in ('guided', 'unattended'):
                    raise ValueError('Mode must be guided or unattended')
                # Existing controller's canonical mode file. No second controller is spawned.
                atomic(project / '.lemvo-mode', mode + '\n')
                receipt = {'status': 'requested', 'detail': 'Mode saved; the current controller applies it at a safe boundary', 'mode': mode}
            elif kind == 'steer':
                text = data.get('text', '')
                if not isinstance(text, str) or not text.strip() or len(text) > 12000:
                    raise ValueError('Guidance must contain 1–12000 characters')
                engine = read_text(project / '.lemvo-engine', 40).strip()
                if engine not in ('claude', 'codex'):
                    raise ValueError('Project engine is unknown')
                contract = self.contract(engine)
                if not contract:
                    raise ValueError('Installed runtime-contract.py not found')
                source = self.store.path / 'messages' / (rid + '.md')
                atomic(source, text)
                instruction_id = 'console-' + rid
                try:
                    result = subprocess.run([sys.executable, str(contract), 'enqueue', str(source), 'console', '--id', instruction_id],
                        cwd=project, capture_output=True, text=True, timeout=10)
                except subprocess.TimeoutExpired:
                    raise ValueError('Publication response timed out; retry with the same request ID to reconcile')
                if result.returncode:
                    # An interrupted HTTP response may have published already. Inspect the canonical queue/registry.
                    known = load(project / '.lemvo-instructions.json', {}).get('entries', {}).get(instruction_id)
                    queued = any(('"instruction_id": "' + instruction_id + '"') in read_text(p, 2000)
                                 for p in (project / '.lemvo-steer-queue').glob('q-*.md'))
                    if not known and not queued:
                        raise ValueError('Guidance publication failed: ' + redact(result.stderr or result.stdout)[-500:])
                receipt = {'status': 'queued', 'instruction_id': instruction_id,
                           'detail': 'Published to the existing instruction queue; queued does not mean the model has read it'}
            else:
                raise ValueError('Unsupported action')
            atomic(receipt_path, {'fingerprint': fingerprint, 'receipt': receipt})
            self.store.event({'type': 'operator_action', 'request_id': rid, 'kind': kind,
                              'project': str(project), 'result': receipt})
            return receipt


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, app):
        self.app = app
        super().__init__(address, Handler)

    def get_request(self):
        sock, address = super().get_request()
        sock.settimeout(5)
        return sock, address


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass  # Never log URLs/tokens/transcripts.

    def send(self, code, payload, content_type='application/json; charset=utf-8'):
        raw = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(raw)

    def allowed(self):
        supplied = self.headers.get('Authorization', '')
        return hmac.compare_digest(supplied, 'Bearer ' + self.server.app.token)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == '/':
            self.send(200, (Path(__file__).parent / 'static/index.html').read_bytes(), 'text/html; charset=utf-8')
        elif path == '/healthz':
            self.send(200, {'service': 'lemvo-console', 'version': '0.1.0'})
        elif not self.allowed():
            self.send(401, {'error': 'Console token required'})
        elif path == '/api/snapshot':
            self.send(200, self.server.app.view())
        elif path == '/api/events':
            from .store import tail
            from .transcripts import records
            self.send(200, list(records(tail(self.server.app.store.path / 'events.jsonl', 32000)))[-80:])
        else:
            self.send(404, {'error': 'Not found'})

    def do_POST(self):
        if not self.allowed():
            self.send(401, {'error': 'Console token required'})
            return
        origin = self.headers.get('Origin')
        if origin and origin != 'http://' + self.headers.get('Host', ''):
            self.send(403, {'error': 'Cross-origin controls are disabled'})
            return
        if urlsplit(self.path).path != '/api/action':
            self.send(404, {'error': 'Not found'})
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if length <= 0 or length > 65536:
                self.send(413, {'error': 'Invalid body size'})
                return
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError('Object expected')
            receipt = self.server.app.action(data)
            self.send(200, receipt)
        except (ValueError, OSError, TypeError) as exc:
            self.send(409, {'error': redact(str(exc))})


def serve(store, port=8765, allow_controls=False, interval=5, stall_seconds=1800):
    with locked(store.path / 'server.lock', nonblocking=True):
        app = App(store, Collector(store, stall_seconds=stall_seconds), allow_controls, interval)
        server = Server(('127.0.0.1', port), app)
        proc = Processes()
        proc.scan()
        identity = proc.items.get(os.getpid(), {}).get('start')
        atomic(store.path / 'server.json', {'pid': os.getpid(), 'pid_start': identity,
                                          'port': server.server_port, 'started_at': time.time()})
        atomic(store.path / 'url', 'http://127.0.0.1:%d/#token=%s' % (server.server_port, app.token))
        threading.Thread(target=app.poll, name='lemvo-observer', daemon=True).start()
        print('Lemvo console listening on 127.0.0.1:%d (URL stored in %s)' % (server.server_port, store.path / 'url'), flush=True)
        try:
            server.serve_forever(poll_interval=0.5)
        finally:
            app.stop.set()
            server.server_close()
