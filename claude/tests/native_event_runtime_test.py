#!/usr/bin/env python3
"""Linux CPU-only regression: actual child processes, no model or GPU calls."""
from pathlib import Path
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest

test_parent = Path(__file__).resolve().parent
default_templates = test_parent if (test_parent / 'native-event-owner.py').exists() else test_parent.parent / 'skills' / 'research-orchestrator' / 'templates'
HERE = Path(os.environ.get('LEMVO_RUNTIME_TEMPLATES', str(default_templates)))


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, str(HERE / filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


owner_mod = load('event_owner_test_target', 'native-event-owner.py')
wait_mod = load('event_wait_test_target', 'event-wait.py')
heartbeat_mod = load('event_heartbeat_test_target', 'runtime-heartbeat.py')
SID = '96540c97-33d2-4e6e-b810-902c352ccedd'


def until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.02)
    raise AssertionError('Timed out waiting for test event')


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='lemvo-event-cpu-')
        self.root = Path(self.temp.name)
        self.children = []
        self.runtime = wait_mod.Runtime(self.root, interval=.01)

    def tearDown(self):
        for child in reversed(self.children):
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=2)
            for handle in (child.stdout, child.stderr):
                if handle:
                    handle.close()
        self.temp.cleanup()

    def process(self, code, *args):
        child = subprocess.Popen([sys.executable, '-c', code] + list(args), cwd=str(self.root),
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.children.append(child)
        return child

    def sleeper(self, seconds=10):
        return self.process('import time; time.sleep(%s)' % seconds)

    def register(self, child, **kwargs):
        return self.runtime.register(child.pid, **kwargs)

    def test_json_partial_live_then_complete(self):
        producer = self.sleeper()
        path = self.root / 'result.json'
        job = self.register(producer, done=[str(path)])
        path.write_text('{"score":')
        self.assertIsNone(self.runtime.event(job))
        self.assertIsNone(owner_mod.terminal(job))
        path.write_text('{"score":0.91}')
        self.assertEqual('completed', self.runtime.event(job)['state'])
        self.assertEqual('done_marker', owner_mod.terminal(job)['observation'])

    def test_json_partial_dead_is_non_success(self):
        producer = self.sleeper()
        path = self.root / 'result.json'
        job = self.register(producer, done=[str(path)])
        path.write_text('{"score":')
        producer.terminate(); producer.wait(timeout=2)
        self.assertEqual('producer_exited', self.runtime.event(job)['state'])
        self.assertEqual('producer_exited', owner_mod.terminal(job)['observation'])

    def test_valid_intermediate_json_is_not_completion(self):
        producer = self.sleeper()
        path = self.root/'result.json'
        job = self.register(producer, done=[str(path)])
        for status in ('running', 'pending', 'queued', 'starting', 'in-progress'):
            path.write_text(json.dumps(dict(status=status, metrics={})))
            self.assertIsNone(self.runtime.event(job), status)
            self.assertIsNone(owner_mod.terminal(job), status)
        path.write_text('{"metrics":{"accuracy":0.91}}')
        self.assertEqual('completed', self.runtime.event(job)['state'])
        self.assertEqual('done_marker', owner_mod.terminal(job)['observation'])

    def test_declared_json_without_suffix(self):
        producer = self.sleeper()
        path = self.root / 'DONE'
        job = self.register(producer, done=[str(path)], done_format='json')
        path.write_text('{')
        self.assertIsNone(self.runtime.event(job))
        self.assertIsNone(owner_mod.terminal(job))
        path.write_text('{"ok":true}')
        self.assertEqual('completed', self.runtime.event(job)['state'])

    def test_failed_marker_is_non_success(self):
        producer = self.sleeper()
        path = self.root / 'FAILED'
        job = self.register(producer, failed=[str(path)])
        path.write_text('error')
        self.assertEqual('failed', self.runtime.event(job)['state'])
        self.assertEqual('failed_marker', owner_mod.terminal(job)['observation'])

    def test_register_identity_and_binding(self):
        producer = self.sleeper()
        job = self.register(producer, job_id='train', task_ids=['task1'])
        resolved = self.runtime.resolve(dict(tool_name='TaskOutput', tool_input={'task_id':'task1'}))
        self.assertEqual([job], resolved)
        with self.assertRaises(ValueError):
            self.runtime.register(producer.pid, start='0')
        with self.assertRaises(ValueError):
            self.runtime.register(producer.pid, done=['/tmp/outside-this-project'])
        with self.assertRaises(ValueError):
            self.runtime.register(os.getpid())

    def test_register_job_id_not_rebound(self):
        first, second = self.sleeper(), self.sleeper()
        self.register(first, job_id='run1')
        with self.assertRaises(ValueError):
            self.register(second, job_id='run1')

    def test_sleep_semicolon_and_and_queries(self):
        for value in ('sleep 3; tail -n 5 /tmp/train.log',
                      'sleep 3 && tail -n 5 /tmp/train.log',
                      'sleep .5; cat /tmp/DONE'):
            # The minimal numeric parser deliberately accepts decimal with leading digit.
            if '.5' in value:
                self.assertIsNone(self.runtime.status_poll(value))
            else:
                self.assertNotIn('sleep', self.runtime.status_poll(value))

    def test_mutations_unknown_commands_fail_open(self):
        for value in ('sleep 3; python train.py', 'sleep 3 && rm -rf /tmp/x',
                      'sleep 3; nvidia-smi --gpu-reset', 'sleep 3; nvidia-smi -pm 1',
                      'sleep 3; sed -i x /tmp/log', 'sleep 3; kill -9 456',
                      'sleep 3; tail -f /tmp/log', 'sleep 3; find / -name x',
                      'sleep 3; echo x > /tmp/log'):
            self.assertIsNone(self.runtime.status_poll(value), value)
            self.assertEqual({}, self.runtime.hook(dict(hook_event_name='PreToolUse',
                                                       tool_name='Bash', tool_input={'command':value})))

    def test_hook_blocks_cpu_until_real_terminal_and_strips_sleep(self):
        path = self.root / 'DONE.json'
        producer = self.process('from pathlib import Path; import time; time.sleep(.35); Path("DONE.json").write_text("{}")')
        self.register(producer, done=[str(path)], task_ids=['task2'])
        event = dict(hook_event_name='PreToolUse', session_id=SID, tool_use_id='query1',
                     tool_name='Bash', tool_input={'command':'sleep 30 && cat ' + str(path)})
        start = time.monotonic()
        out = self.runtime.hook(event)
        self.assertGreater(time.monotonic() - start, .2)
        self.assertEqual('cat ' + str(path), out['hookSpecificOutput']['updatedInput']['command'])
        self.assertTrue(path.exists())
        self.assertEqual({}, self.runtime.hook(dict(hook_event_name='PreToolUse', tool_name='TaskOutput',
                                                   tool_input={'task_id':'unrelated'})))

    def test_stop_receipt_deduplicated(self):
        producer = self.sleeper()
        marker = self.root / 'DONE'
        job = self.register(producer, done=[str(marker)])
        marker.touch()
        event = dict(hook_event_name='Stop', session_id=SID,
                     last_assistant_message='<runtime_wait>' + json.dumps(dict(
                         independent_work_pending=False, job_ids=[job['job_id']])) + '</runtime_wait>')
        self.assertEqual('block', self.runtime.hook(event)['decision'])
        self.assertEqual({}, self.runtime.hook(event))

    def test_fresh_terminal_does_not_hide_behind_other_running_job(self):
        completed, running = self.sleeper(), self.sleeper()
        marker = self.root / 'DONE'
        first = self.register(completed, done=[str(marker)])
        second = self.register(running)
        marker.touch()
        event = dict(session_id=SID, tool_use_id='fresh')
        start = time.monotonic()
        value = self.runtime.wait(event, [first, second], 'pretool')
        self.assertEqual(first['job_id'], value['job_id'])
        self.assertLess(time.monotonic() - start, .15)
        # A later query skips the already delivered first receipt, but returns
        # the genuinely new death of the other producer.
        def finish():
            time.sleep(.2)
            running.terminate(); running.wait(timeout=2)
        worker = threading.Thread(target=finish)
        worker.start()
        event['tool_use_id'] = 'later'
        start = time.monotonic()
        value = self.runtime.wait(event, [first, second], 'pretool')
        worker.join(timeout=2)
        self.assertGreater(time.monotonic() - start, .1)
        self.assertEqual(second['job_id'], value['job_id'])

    def test_stop_group_delivers_a_then_waits_b_then_allows_stop(self):
        first_process, second_process = self.sleeper(), self.sleeper()
        a, b = self.root/'A', self.root/'B'
        first = self.register(first_process, done=[str(a)])
        second = self.register(second_process, done=[str(b)])
        a.touch()
        event = dict(hook_event_name='Stop', session_id=SID,
                     last_assistant_message='<runtime_wait>' + json.dumps(dict(
                         independent_work_pending=False, job_ids=[first['job_id'], second['job_id']])) + '</runtime_wait>')
        self.assertEqual('block', self.runtime.hook(event)['decision'])
        def finish():
            time.sleep(.2); b.touch()
        worker = threading.Thread(target=finish); worker.start()
        start = time.monotonic()
        self.assertEqual('block', self.runtime.hook(event)['decision'])
        self.assertGreater(time.monotonic() - start, .1)
        worker.join(timeout=2)
        self.assertEqual({}, self.runtime.hook(event))

    def test_explicit_wait_group_does_not_reuse_old_a(self):
        first_process, second_process = self.sleeper(), self.sleeper()
        a, b = self.root/'A', self.root/'B'
        first = self.register(first_process, done=[str(a)])
        second = self.register(second_process, done=[str(b)])
        a.touch()
        self.assertEqual(first['job_id'], self.runtime.wait({}, [first, second], 'explicit')['job_id'])
        def finish():
            time.sleep(.2); b.touch()
        worker = threading.Thread(target=finish); worker.start()
        self.assertEqual(second['job_id'], self.runtime.wait({}, [first, second], 'explicit')['job_id'])
        worker.join(timeout=2)

    def test_heartbeat_only_exact_live_waits(self):
        own = self.runtime.identity(os.getpid())
        path = self.root / '.runtime-events/waits/a.json'
        wait_mod.atomic(path, dict(pid=os.getpid(), start=own['start'], state='waiting'))
        self.assertEqual(['a'], heartbeat_mod.active_waits(self.root))
        wait_mod.atomic(path, dict(pid=os.getpid(), start='0', state='waiting'))
        self.assertEqual([], heartbeat_mod.active_waits(self.root))

    def test_ack_is_stable_and_only_delivered_events(self):
        owner = owner_mod.Owner(self.root)
        owner.state['pending_delivery'] = dict(events=[dict(job_id='a', signature='s')],
                                                wake=dict(event_id='w'))
        owner.acknowledge_delivery()
        owner.acknowledge_delivery()
        restarted = owner_mod.Owner(self.root)
        self.assertEqual({'a':'s'}, restarted.state['consumed'])
        self.assertEqual(['w'], restarted.state['consumed_wakes'])
        self.assertNotIn('pending_delivery', restarted.state)

    def test_unknown_adopted_exit_never_acknowledged(self):
        owner = owner_mod.Owner(self.root, poll=.01)
        owner.science_fingerprint = lambda:'same'
        owner.fingerprint_before = 'same'
        owner.jobs = lambda:[]
        owner.consume_stream = lambda:None
        owner.pending_terminals = lambda jobs:[]
        owner.pending_wake = lambda:None
        owner.state['pending_delivery'] = dict(reason='unknownexit', events=[dict(job_id='j', signature='s')])
        def adopt():
            owner.cli = dict(pid=99999999, start='0', stdout=str(self.root/'absent'))
            return True
        owner.adopt = adopt
        original = owner.status
        def status(phase, **kwargs):
            original(phase, **kwargs)
            if phase == 'waiting_event':
                (self.root / 'PAUSE').touch()
        owner.status = status
        owner.run()
        self.assertEqual({}, owner.state['consumed'])
        self.assertIn('pending_delivery', owner.state)
        self.assertEqual(1, owner.state['failure_retries'])

    def test_clean_os_exit_without_protocol_result_never_acknowledged(self):
        owner = owner_mod.Owner(self.root, poll=.01)
        owner.science_fingerprint = lambda:'same'; owner.fingerprint_before = 'same'
        owner.jobs = lambda:[]; owner.consume_stream = lambda:None
        owner.pending_terminals = lambda jobs:[]; owner.pending_wake = lambda:None
        owner.state['pending_delivery'] = dict(reason='zero', events=[dict(job_id='j', signature='s')])
        class Exited:
            def poll(self): return 0
        def adopt():
            owner.cli = dict(pid=99999999, start='0', stdout=str(self.root/'absent'))
            owner.child = Exited()
            return True
        owner.adopt = adopt
        original = owner.status
        def status(phase, **kwargs):
            original(phase, **kwargs)
            if phase == 'waiting_event': (self.root/'PAUSE').touch()
        owner.status = status
        owner.run()
        self.assertEqual({}, owner.state['consumed'])
        self.assertIn('pending_delivery', owner.state)
        self.assertEqual(1, owner.state['failure_retries'])

    def test_codex_stream_thread_and_final_response(self):
        owner = owner_mod.Owner(self.root, engine='codex')
        stream = self.root / 'stream.jsonl'
        records = [dict(type='thread.started', thread_id=SID),
                   dict(type='item.completed', item=dict(type='agent_message', text='Waiting for producer')),
                   dict(type='turn.completed', usage=dict(input_tokens=10, output_tokens=2))]
        stream.write_text('\n'.join(json.dumps(j) for j in records) + '\n')
        owner.cli = {'stdout':str(stream)}
        owner.consume_stream()
        self.assertEqual(SID, owner.state['session_id'])
        self.assertEqual('Waiting for producer', owner.last_result['result'])
        self.assertFalse(owner.last_result['is_error'])

    def test_missing_first_prompt_does_not_launch(self):
        owner = owner_mod.Owner(self.root, launcher=['/does/not/exist'])
        owner.spawn('first')
        self.assertEqual('missing_initial_prompt_without_session_id', owner.state['fault'])
        self.assertIsNone(owner.child)

    def test_explicit_stops_preempt_dispatch(self):
        for name in ('PAUSE', 'STOP', 'CAMPAIGN-DONE'):
            path = self.root / name
            path.touch()
            owner = owner_mod.Owner(self.root, launcher=['/does/not/exist'], session_id=SID)
            owner.spawn('event', events=[dict(job_id='j', signature='s')])
            self.assertIsNone(owner.child)
            self.assertNotIn('pending_delivery', owner.state)
            path.unlink()

    def fake_cli(self):
        script = self.root / 'fake_cli.py'
        script.write_text('''import json,sys,time
from pathlib import Path
args=sys.argv[1:]; engine=args.pop(0); root=Path.cwd()
resume=('--resume' in args if engine=='claude' else 'resume' in args)
sid='96540c97-33d2-4e6e-b810-902c352ccedd'
if resume:
 actual=args[args.index('--resume')+1] if engine=='claude' else args[args.index('resume')+1]
 assert actual==sid
assert 'AUTHORIZED FIRST TASK' in args[args.index('-p')+1] if engine=='claude' and not resume else True
with (root/'calls.jsonl').open('a') as out: out.write(json.dumps(dict(engine=engine,resume=resume,args=args,at=time.time()))+'\\n')
if engine=='claude': print(json.dumps(dict(type='system',session_id=sid)),flush=True)
else: print(json.dumps(dict(type='thread.started',thread_id=sid)),flush=True)
if resume:
 assert json.loads((root/'DONE.json').read_text())['finished']
 (root/'RESUMED.json').write_text(json.dumps(dict(sid=sid,at=time.time())))
 (root/'CAMPAIGN-DONE').touch()
message='Complete' if resume else 'Waiting for producer, no independent work'
if engine=='claude': print(json.dumps(dict(type='result',subtype='success',is_error=False,session_id=sid,result=message)),flush=True)
else:
 print(json.dumps(dict(type='item.completed',item=dict(type='agent_message',text=message))),flush=True)
 print(json.dumps(dict(type='turn.completed',usage=dict(input_tokens=10,output_tokens=2))),flush=True)
''')
        return script

    def owner_process(self, engine, script):
        child = subprocess.Popen([sys.executable, str(HERE/'native-event-owner.py'),
                                  '--root', str(self.root), '--poll', '.025', '--engine', engine,
                                  '--launcher-json', json.dumps([sys.executable, str(script), engine])],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.children.append(child)
        return child

    def full_recovery(self, engine):
        (self.root/'START.md').write_text('AUTHORIZED FIRST TASK')
        script = self.fake_cli()
        producer = self.sleeper()
        marker = self.root/'DONE.json'
        self.register(producer, done=[str(marker)])
        # Initial request is explicit; existing active work by itself must not
        # create a model call. This is also the supported operational wake path.
        wait_mod.atomic(self.root/'.runtime-events/wake.json', dict(event_id='start', reason='initial authorized launch'))
        first = self.owner_process(engine, script)
        until(lambda: (self.root/'calls.jsonl').exists())
        until(lambda: any(j.get('type')=='cli_os_exit' for j in owner_events(self.root)))
        calls = read_calls(self.root)
        self.assertEqual(1, len(calls))
        # Normal successful CLI has ended. Waiting uses the CPU owner, then a
        # process restart must recover exactly the same SID and registrations.
        first.terminate(); first.wait(timeout=2)
        restarted = self.owner_process(engine, script)
        time.sleep(.25)
        for _ in range(4):
            (self.root/'execution-stderr.log').write_text('telemetry refresh ' + str(time.time()))
            wait_mod.atomic(self.root/'.runtime-events/native_owner_heartbeat.json', {'at':time.time()})
            time.sleep(.04)
        self.assertEqual(1, len(read_calls(self.root)), 'Waiting/restart/telemetry must not call the model')
        # Half a JSON document is not an event and cannot trigger a resume.
        marker.write_text('{"finished":')
        time.sleep(.15)
        self.assertEqual(1, len(read_calls(self.root)))
        marker.write_text('{"finished":true}')
        until(lambda:(self.root/'RESUMED.json').exists())
        restarted.wait(timeout=4)
        self.assertEqual(0, restarted.returncode)
        calls = read_calls(self.root)
        self.assertEqual(2, len(calls))
        self.assertEqual([False, True], [j['resume'] for j in calls])
        if engine == 'codex':
            for call in calls:
                self.assertIn('--skip-git-repo-check', call['args'])
                self.assertIn('--json', call['args'])
                self.assertEqual(os.environ.get('CODEX_SANDBOX', 'workspace-write'),
                                 call['args'][call['args'].index('-s')+1])
        self.assertEqual(SID, json.loads((self.root/'RESUMED.json').read_text())['sid'])
        self.assertIsNone(producer.poll(), 'Controller must not kill the real producer')
        # Restart after explicit final sentinel cannot make another model call.
        stopped = self.owner_process(engine, script)
        stopped.wait(timeout=3)
        self.assertEqual(2, len(read_calls(self.root)))

    def test_actual_claude_shaped_exit_restart_wait_resume(self):
        self.full_recovery('claude')

    def test_actual_codex_shaped_exit_restart_wait_resume(self):
        self.full_recovery('codex')

    def test_owner_restart_adopts_live_cli_without_second_model_call(self):
        (self.root/'START.md').write_text('AUTHORIZED FIRST TASK')
        script = self.root/'live_cli.py'
        script.write_text('''import json,time
from pathlib import Path
root=Path.cwd()
with (root/'calls.jsonl').open('a') as out: out.write(json.dumps(dict(at=time.time()))+'\\n')
print(json.dumps(dict(type='system',session_id='96540c97-33d2-4e6e-b810-902c352ccedd')),flush=True)
while not (root/'RELEASE').exists(): time.sleep(.02)
print(json.dumps(dict(type='result',subtype='success',is_error=False,result='Waiting for producer')),flush=True)
(root/'PAUSE').touch()
''')
        wait_mod.atomic(self.root/'.runtime-events/wake.json', dict(event_id='start', reason='test launch'))
        first = self.owner_process('claude', script)
        until(lambda:(self.root/'calls.jsonl').exists())
        entry = until(lambda: owner_mod.read(self.root/'EXECUTION_PRODUCER.json'))
        until(lambda: owner_mod.read(self.root/'.runtime-events/native_owner_state.json').get('session_id') == SID)
        first.terminate(); first.wait(timeout=2)
        restarted = self.owner_process('claude', script)
        until(lambda:any(j.get('type')=='cli_adopted' for j in owner_events(self.root)))
        time.sleep(.15)
        self.assertEqual(1, len(read_calls(self.root)))
        self.assertTrue(owner_mod.alive(entry))
        (self.root/'RELEASE').touch()
        restarted.wait(timeout=3)
        until(lambda:not owner_mod.alive(entry))
        self.assertEqual(1, len(read_calls(self.root)))

    def test_pre_dispatch_blocks_hold_indented_state_guided_and_exhausted_usd_cap(self):
        fake = self.root / 'fake_cli.py'
        fake.write_text('print("SHOULD_NOT_RUN")\n')
        owner = owner_mod.Owner(self.root, poll=0.01, launcher=[sys.executable, str(fake)], engine='codex')

        # 1) HOLD marker
        (self.root / 'HOLD').write_text('hold\n')
        self.assertIsNotNone(owner.dispatch_blocked())
        owner.spawn('job_terminal', events=[{'job_id': 'j1', 'signature': 's1'}])
        self.assertEqual(owner.state.get('launch_count', 0), 0)
        (self.root / 'HOLD').unlink()

        # 2) BLOCKED-USER marker
        (self.root / 'BLOCKED-USER').write_text('need input\n')
        owner.spawn('job_terminal', events=[{'job_id': 'j2', 'signature': 's2'}])
        self.assertEqual(owner.state.get('launch_count', 0), 0)
        (self.root / 'BLOCKED-USER').unlink()

        # 3) Indented project.status: HOLD in research-state.yaml (exercises indent == 2 int check)
        (self.root / 'research-state.yaml').write_text('project:\n  status: HOLD\nbudget:\n  unit: usd\n  used: 0\n  total_cap: 1\n  cap_authorized: no\n')
        owner.spawn('job_terminal', events=[{'job_id': 'j3', 'signature': 's3'}])
        self.assertEqual(owner.state.get('launch_count', 0), 0)

        # 4) Guided mode without steer guidance or .lemvo-advance-once blocks, then steer unblocks
        (self.root / 'research-state.yaml').write_text('project:\n  status: ACTIVE\nbudget:\n  unit: usd\n  used: 0\n  total_cap: 1\n  cap_authorized: yes\n  cap_source: user-test\n')
        (self.root / '.lemvo-mode').write_text('guided\n')
        blocked = owner.dispatch_blocked()
        self.assertEqual(blocked, ('waiting_user', 'guided-idle'))
        owner.spawn('job_terminal', events=[{'job_id': 'j4', 'signature': 's4'}])
        self.assertEqual(owner.state.get('launch_count', 0), 0)

        (self.root / '.lemvo-steer-queue').mkdir(exist_ok=True)
        (self.root / '.lemvo-steer-queue/q-1.md').write_text('approve next step\n')
        self.assertIsNone(owner.dispatch_blocked())
        (self.root / '.lemvo-mode').write_text('unattended\n')

        # 5) Exhausted authorized USD cap blocks
        (self.root / 'research-state.yaml').write_text('project:\n  status: ACTIVE\nbudget:\n  unit: usd\n  used: 1.5\n  total_cap: 1.0\n  cap_authorized: yes\n  cap_source: user-test\n')
        blocked = owner.dispatch_blocked()
        self.assertEqual(blocked, ('over_budget', 'authorized_usd_budget_exhausted'))
        owner.spawn('job_terminal', events=[{'job_id': 'j5', 'signature': 's5'}])
        self.assertEqual(owner.state.get('launch_count', 0), 0)


def read_calls(root):
    return [json.loads(line) for line in (root/'calls.jsonl').read_text().splitlines()]


def owner_events(root):
    try:
        return [json.loads(line) for line in (root/'.runtime-events/owner_events.jsonl').read_text().splitlines()]
    except OSError:
        return []


if __name__ == '__main__':
    unittest.main(verbosity=2)
