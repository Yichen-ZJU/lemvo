#!/usr/bin/env python3
"""Real CPU-process mutual exclusion: native owner vs Lemvo controller.

No model, training, existing project or runtime installation is touched.
"""
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest


TEMPLATES = Path(__file__).resolve().parents[1] / 'skills/research-orchestrator/templates'
NATIVE = Path(os.environ.get('LEMVO_NATIVE_OWNER_PATH', str(TEMPLATES / 'native-event-owner.py')))
CONTROLLER = TEMPLATES / 'lemvo-run.sh'


def eventually(check, timeout=8):
    end = time.time() + timeout
    while time.time() < end:
        result = check()
        if result:
            return result
        time.sleep(.025)
    raise AssertionError('CPU process did not reach expected state within %.1fs' % timeout)


def start_ticks(pid):
    return Path('/proc/%s/stat' % pid).read_text().rsplit(')', 1)[1].split()[19]


@unittest.skipUnless(sys.platform.startswith('linux') and NATIVE.is_file(), 'Linux runtime bundle required')
class OwnerMutualExclusion(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='lemvo-owner-lock-test-')
        self.root = Path(self.tmp.name)
        self.root.joinpath('.runtime-events').mkdir()
        self.root.joinpath('START.md').write_text('Isolated CPU execution test; no model required.')
        self.root.joinpath('.lemvo-mode').write_text('guided\n')
        self.root.joinpath('.lemvo-engine').write_text('claude\n')
        self.processes = []
        self.env = os.environ.copy()
        self.env.update(LEMVO_POLL_SEC='.05', TURN_GAP_SEC='.01')

    def tearDown(self):
        self.root.joinpath('STOP').touch()
        for process in reversed(self.processes):
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        for process in reversed(self.processes):
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=3)
        self.tmp.cleanup()

    def launch(self, argv, env=None):
        process = subprocess.Popen(argv, cwd=str(self.root), env=env or self.env,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        self.processes.append(process)
        return process

    def native(self):
        producer = self.launch([sys.executable, '-c', 'import time; time.sleep(120)'])
        registry = [dict(job_id='cpu-producer', pid=producer.pid, start=start_ticks(producer.pid),
                         done_files=[], failed_files=[])]
        self.root.joinpath('.runtime-events/jobs.json').write_text(json.dumps(registry))
        owner = self.launch([sys.executable, str(NATIVE), '--root', str(self.root), '--poll', '.025'])
        eventually(lambda: self.root.joinpath('.runtime-events/native_owner_heartbeat.json').is_file())
        self.assertIsNone(owner.poll())
        return owner

    def assertSharedLocked(self):
        with self.root.joinpath('.runtime-events/execution-owner.lock').open('a+') as lock:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_native_first_rejects_controller_start_without_config_mutation(self):
        self.native()
        self.assertSharedLocked()
        before = {name: self.root.joinpath(name).read_bytes()
                  for name in ('.lemvo-mode', '.lemvo-engine')}
        attempt = self.launch(['bash', str(CONTROLLER), 'start', str(self.root),
                               '--mode', 'unattended', '--engine', 'codex'])
        self.assertEqual(attempt.wait(timeout=5), 0)
        self.assertEqual(before, {name: self.root.joinpath(name).read_bytes() for name in before})
        self.assertFalse(self.root.joinpath('.lemvo-owner.lock').exists())
        self.assertFalse(self.root.joinpath('native-stdout.jsonl').exists())

    def test_native_first_rejects_controller_advance(self):
        self.native()
        self.assertSharedLocked()
        attempt = self.launch(['bash', str(CONTROLLER), 'advance', str(self.root)])
        self.assertEqual(attempt.wait(timeout=5), 1)
        self.assertFalse(self.root.joinpath('turns').exists())
        self.assertFalse(self.root.joinpath('.lemvo-owner.lock').exists())

    def test_controller_first_rejects_native_without_model_dispatch(self):
        controller = self.launch(['bash', str(CONTROLLER), 'start', str(self.root),
                                  '--mode', 'guided', '--max-turns', '100'])
        eventually(lambda: self.root.joinpath('.lemvo-owner.lock/pid').exists())
        self.assertIsNone(controller.poll())
        self.assertSharedLocked()
        attempt = self.launch([sys.executable, str(NATIVE), '--root', str(self.root), '--poll', '.025'])
        self.assertEqual(attempt.wait(timeout=5), 0)
        self.assertFalse(self.root.joinpath('native-stdout.jsonl').exists())
        self.assertFalse(self.root.joinpath('.runtime-events/native_owner.json').exists())

    def test_adapter_does_not_inherit_shared_lock(self):
        stub = self.root / 'adapter.sh'
        stub.write_text('#!/usr/bin/env bash\n'
            'echo $$ > "$1/adapter.pid"\n'
            'python3 - "$1" <<\'PY\'\n'
            'import json,os,sys\nfrom pathlib import Path\n'
            'fds=[]\n'
            'for p in Path("/proc/self/fd").iterdir():\n'
            ' try: fds.append(os.readlink(str(p)))\n'
            ' except OSError: pass\n'
            'Path(sys.argv[1],"adapter-fds.json").write_text(json.dumps(fds))\n'
            'PY\n'
            'sleep 30\n')
        env = dict(self.env, AUTOLOOP_SCRIPT=str(stub))
        controller = self.launch(['bash', str(CONTROLLER), 'advance', str(self.root)], env)
        eventually(lambda: self.root.joinpath('adapter-fds.json').is_file())
        fds = json.loads(self.root.joinpath('adapter-fds.json').read_text())
        self.assertFalse(any('execution-owner.lock' in value for value in fds), fds)
        os.kill(controller.pid, signal.SIGTERM)
        controller.wait(timeout=5)
        def lease_released():
            with self.root.joinpath('.runtime-events/execution-owner.lock').open('a+') as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return False
                return True
        eventually(lease_released)

    def test_killed_controller_sleep_child_does_not_block_restart(self):
        env = dict(self.env, LEMVO_POLL_SEC='30')
        first = self.launch(['bash', str(CONTROLLER), 'start', str(self.root),
                             '--mode', 'guided', '--engine', 'claude'], env)
        def live_cpu_sleep():
            try:
                pids = Path('/proc/%s/task/%s/children' % (first.pid, first.pid)).read_text().split()
            except OSError:
                return False
            for pid in pids:
                try:
                    argv = Path('/proc/%s/cmdline' % pid).read_bytes().split(b'\0')
                    if argv[:2] == [b'sleep', b'30']:
                        return int(pid)
                except OSError:
                    pass
            return False
        sleeper = eventually(live_cpu_sleep)
        sleeper_start = start_ticks(sleeper)
        first.terminate()
        first.wait(timeout=5)
        # Keep the ordinary inherited child alive while the replacement starts.
        self.assertEqual(start_ticks(sleeper), sleeper_start)
        second = self.launch(['bash', str(CONTROLLER), 'start', str(self.root),
                              '--mode', 'guided', '--engine', 'claude'], env)
        def new_owner_registered():
            try:
                return self.root.joinpath('.lemvo-owner.lock/pid').read_text().strip() == str(second.pid)
            except OSError:
                return False
        eventually(new_owner_registered)
        time.sleep(.1)
        self.assertIsNone(second.poll())
        self.assertSharedLocked()
        self.assertFalse(list(self.root.glob('turns/*/manifest.json')))
        self.assertEqual(start_ticks(sleeper), sleeper_start)


if __name__ == '__main__':
    unittest.main(verbosity=2)
