"""Idle settlement, P0 job-events, Codex engine, YAML multiline, and log rotation tests."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import runtime_liveness_test as base

TEMPLATES = base.TEMPLATES
RUN = TEMPLATES / 'lemvo-run.sh'
AUTO = TEMPLATES / 'autoloop-reference.sh'

def _load(name):
    spec = importlib.util.spec_from_file_location('lemvo_' + name.replace('-', '_'), str(TEMPLATES / name))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

events = _load('job-events.py')

WAIT_CLI = r"""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
is_claude = '-p' in args
msg = args[args.index('-p') + 1] if is_claude else args[-1]
with open('.test-calls.jsonl', 'a', encoding='utf-8') as f:
    f.write(json.dumps({'engine': 'claude' if is_claude else 'codex', 'message': msg, 'args': args}) + '\n')
td_env = os.environ.get('LEMVO_TURN_DIR')
mode_file = Path('.fake-next-mode')
mode = mode_file.read_text().strip() if mode_file.exists() else 'waiting'
if mode == 'waiting' and td_env:
    td = Path(td_env)
    work = {'schema': 'lemvo-work-result', 'schema_version': 1, 'turn_id': td.name,
            'status': 'waiting', 'summary': 'waiting on external signal',
            'artifact_refs': ['research-state.yaml'], 'metrics': [], 'engine_rc': 0,
            'next_task': {'kind': 'research', 'objective': 'wait'}}
    (td / 'work-result.json').write_text(json.dumps(work), encoding='utf-8')
elif mode == 'continue':
    Path('src').mkdir(exist_ok=True)
    Path('src/out.txt').write_text('done\n', encoding='utf-8')
    mode_file.write_text('waiting', encoding='utf-8')
sid = '11111111-2222-4333-8444-555555555555'
if is_claude:
    print(json.dumps({'type': 'result', 'session_id': sid, 'is_error': False,
                      'result': 'ok', 'usage': {'input_tokens': 10, 'output_tokens': 2}}))
else:
    print(json.dumps({'type': 'thread.started', 'thread_id': sid}))
    print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'ok'}}))
    print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 10, 'output_tokens': 2}}))
"""


class IdleSettlementAndArchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='lemvo-arch-idle-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.p = self.root / 'project'
        self.p.mkdir()
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        for name in ('claude', 'codex'):
            cli = self.bin / name
            cli.write_text(WAIT_CLI, encoding='utf-8')
            cli.chmod(0o700)
        self.env = dict(
            os.environ,
            PATH=str(self.bin) + os.pathsep + os.environ['PATH'],
            ENGINE='claude',
            CLAUDE_BIN=str(self.bin / 'claude'),
            LEMVO_RC_MODE='structured',
            LEMVO_POLL_SEC='0.05',
            TURN_GAP_SEC='0',
            LEMVO_WAIT_RECHECK_SEC='0.2',
            LEMVO_UNKNOWN_JOB_SEC='0.2',
            LEMVO_TURN_POLL_SEC='0.03',
        )

    def write_state(self, proj=None, extra=''):
        target = proj or self.p
        (target / 'research-state.yaml').write_text(
            'campaign_status: ACTIVE\nproject:\n  status: ACTIVE\n  objective: CPU verification\n'
            'budget:\n  unit: usd\n  used: 1.0\n  total_cap: null\n'
            'prep:\n  prep_turns: 0\n  last_informative_artifact: none\n' + extra,
            encoding='utf-8',
        )

    def calls(self, proj=None):
        f = (proj or self.p) / '.test-calls.jsonl'
        return [json.loads(l) for l in f.read_text(encoding='utf-8').splitlines()] if f.exists() else []

    def until(self, pred, timeout=10):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if pred():
                return True
            time.sleep(0.05)
        return False

    def start_controller(self, proj, engine='claude'):
        out = open(self.root / ('ctrl-%s-%d.log' % (engine, time.time_ns())), 'w', encoding='utf-8')
        self.addCleanup(out.close)
        env = dict(self.env, ENGINE=engine)
        proc = subprocess.Popen(
            ['bash', str(RUN), 'start', str(proj), '--engine', engine, '--mode', 'unattended'],
            env=env, stdout=out, stderr=out, start_new_session=True,
        )
        def stop():
            import signal
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
        self.addCleanup(stop)
        return proc

    def test_claude_and_codex_idle_quiet_and_steer_wake(self):
        for engine in ('claude', 'codex'):
            with self.subTest(engine=engine):
                proj = self.root / ('proj-' + engine)
                proj.mkdir()
                self.write_state(proj)
                self.start_controller(proj, engine=engine)
                self.assertTrue(self.until(lambda: (proj / '.lemvo-idle-state.json').exists()))
                idle_doc = json.loads((proj / '.lemvo-idle-state.json').read_text(encoding='utf-8'))
                self.assertTrue(idle_doc['armed'])
                self.assertIn('has_declared_wait', idle_doc)
                self.assertIn('has_jobs_file', idle_doc)
                self.assertEqual(len(self.calls(proj)), 1)
                self.assertEqual(self.calls(proj)[0]['engine'], engine)
                txt = (proj / 'research-state.yaml').read_text(encoding='utf-8')
                (proj / 'research-state.yaml').write_text(txt.replace('used: 1.0', 'used: 2.5').replace('prep_turns: 0', 'prep_turns: 7'), encoding='utf-8')
                time.sleep(0.35)
                self.assertEqual(len(self.calls(proj)), 1)
                steer_src = self.root / f'steer-{engine}.md'
                steer_src.write_text('Handle new scoped direction once and park again.', encoding='utf-8')
                r = subprocess.run(['bash', str(RUN), 'steer', str(proj), str(steer_src)],
                                   env=dict(self.env, ENGINE=engine), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                self.assertEqual(r.returncode, 0, r.stdout)
                self.assertTrue(self.until(lambda: len(self.calls(proj)) == 2))
                time.sleep(0.35)
                self.assertEqual(len(self.calls(proj)), 2)

    def test_job_events_done_path_harvested_resume_pid_and_atomic_stamp(self):
        done_file = self.p / 'sub/SELECTIVE_DONE.json'
        done_file.parent.mkdir(parents=True)
        done_file.write_text(json.dumps({'run_id': 'v11a', 'four_grid': 220}), encoding='utf-8')
        jobs_file = self.p / '.lemvo-jobs.json'
        receipts = self.p / '.lemvo-receipts'
        states = self.p / '.lemvo-jobstates.json'
        jobs_file.write_text(json.dumps([
            {'name': 'v11a', 'run_id': 'v11a', 'pid': None, 'pid_start': None,
             'status': 'succeeded', 'done_path': str(done_file),
             'harvest_summary': 'four 220/360', 'started_epoch': time.time() - 5000}
        ]), encoding='utf-8')
        res = events.reconcile(str(jobs_file), str(states), str(receipts), stamp_started_flag=True)
        self.assertEqual(res, 'DONE')
        self.assertFalse(list(receipts.glob('*.json')), 'already_harvested record must not emit wake receipt')

        resumed_proc = subprocess.Popen(['sleep', '30'])
        self.addCleanup(lambda: (resumed_proc.terminate(), resumed_proc.wait()))
        resumed_start = events.identity(resumed_proc.pid)['start']
        jobs_file.write_text(json.dumps([
            {'name': 'v10d', 'run_id': 'v10d', 'pid': 99999998, 'pid_start': '1',
             'resume_pid': resumed_proc.pid, 'resume_pid_start': resumed_start,
             'done_path': str(self.p / 'sub/missing.json')}
        ]), encoding='utf-8')
        res2 = events.reconcile(str(jobs_file), str(states), str(receipts), stamp_started_flag=True)
        self.assertEqual(res2, 'PENDING v10d')
        stamped = json.loads(jobs_file.read_text(encoding='utf-8'))
        self.assertTrue(stamped[0].get('started_epoch'))

    def test_multiline_yaml_and_campaign_log_rotation_and_malformed_idle_env(self):
        self.write_state(
            extra="  last_informative_artifact: 'outputs/cycle20_summary.json\n    (multiline note)''s'\n"
                  "  last_informative_observation: 'first line\n    second line'\n"
        )
        (self.p / 'outputs').mkdir()
        (self.p / 'outputs/cycle20_summary.json').write_text('{"ok":true}\n', encoding='utf-8')
        (self.p / 'campaign_log.txt').write_text('X' * 300 + '\n', encoding='utf-8')
        msg = self.root / 'msg.md'
        msg.write_text('Run single turn and verify YAML & rotation.', encoding='utf-8')
        r = subprocess.run(
            ['bash', str(AUTO), str(self.p), str(msg)],
            env=dict(self.env, LEMVO_CAMPAIGN_LOG_MAX_BYTES='200'),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=20,
        )
        self.assertEqual(r.returncode, 0, r.stdout)
        turn_dirs = list((self.p / 'turns').glob('*'))
        self.assertEqual(len(turn_dirs), 1)
        res = json.loads((turn_dirs[0] / 'result.json').read_text(encoding='utf-8'))
        for ref in res.get('artifact_refs', []):
            self.assertNotIn('\n', ref)
            self.assertFalse(ref.startswith("'"))
        self.assertTrue((self.p / 'campaign_log.prev.txt').is_file())
        self.assertTrue((self.p / 'campaign_log.txt').is_file())
        self.assertLess((self.p / 'campaign_log.txt').stat().st_size, 4096)

        bad_env = dict(os.environ, LEMVO_IDLE_DELIVERED_CONDITION='{not-json')
        cp = subprocess.run(
            [sys.executable, str(TEMPLATES / 'runtime-idle.py'), '--root', str(self.p),
             'capture-worker', str(Path('turns') / turn_dirs[0].name)],
            env=bad_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.assertEqual(cp.returncode, 0, cp.stderr)
        snap = json.loads((turn_dirs[0] / 'idle-worker-snapshot.json').read_text(encoding='utf-8'))
        self.assertIsNone(snap['delivered_condition'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
