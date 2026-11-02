import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if os.environ.get('LEMVO_TEST_TMPDIR'):
    tempfile.tempdir = str(Path(os.environ['LEMVO_TEST_TMPDIR']).resolve(strict=True))
sys.path.insert(0, str(ROOT))
import lemvo_desktop as client
if os.name != 'nt':
    import remote_codex_bridge as bridge


@unittest.skipIf(os.name == 'nt', 'Linux bridge is tested on the SSH host')
class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.runtime = self.base / 'lemvo-run.sh'
        self.runtime.write_text('#!/bin/sh\nexit 0\n')
        self.project = self.base / 'project'
        self.project.mkdir()
        self.p = bridge.Project(self.project)
        self.p.rpc({'op': 'init', 'runtime': str(self.runtime), 'server_command': [sys.executable, '-c', 'print(1)']})

    def tearDown(self):
        self.temp.cleanup()

    def request(self, claimed=False):
        rid = 'a' * 32
        d = self.p.request_dir(rid)
        d.mkdir(parents=True, exist_ok=True)
        bridge.atomic(d / 'request.json', {'id': rid, 'status': 'queued', 'prompt': 'test', 'sid': None})
        bridge.atomic(self.p.root / 'active.json', {'pid': os.getpid(), 'start': bridge.identity(os.getpid()), 'provider': 'windows', 'request_id': rid})
        r = self.p.rpc({'op': 'claim', 'request_id': rid, 'worker_id': 'worker-a'}) if claimed else bridge.read(d / 'request.json')
        return rid, d, r

    def test_handoff_waits_for_claimed_model(self):
        rid, d, r = self.request(True)
        st = self.p.rpc({'op': 'provider', 'provider': 'server'})
        self.assertEqual(st['provider'], 'windows')
        self.assertEqual(st['requested_provider'], 'server')
        self.assertTrue(st['busy'])

    def test_provider_changes_when_idle(self):
        st = self.p.rpc({'op': 'provider', 'provider': 'server'})
        self.assertEqual(st['provider'], 'server')
        self.assertFalse(st['busy'])

    def test_same_claim_retries_but_other_worker_cannot_claim(self):
        rid, d, r = self.request(True)
        self.assertEqual(self.p.rpc({'op': 'claim', 'request_id': rid, 'worker_id': 'worker-a'})['token'], r['token'])
        with self.assertRaises(ValueError):
            self.p.rpc({'op': 'claim', 'request_id': rid, 'worker_id': 'worker-b'})

    def test_queued_claim_rejected_after_handoff_request(self):
        rid, d, r = self.request()
        self.p.rpc({'op': 'provider', 'provider': 'server'})
        with self.assertRaises(ValueError):
            self.p.rpc({'op': 'claim', 'request_id': rid, 'worker_id': 'worker-a'})

    def test_dead_bridge_does_not_steal_unfinished_windows_turn(self):
        rid, d, r = self.request(True)
        bridge.atomic(self.p.root / 'active.json', {'pid': 99999999, 'start': 'invalid', 'provider': 'windows', 'request_id': rid})
        self.assertTrue(self.p.rpc({'op': 'provider', 'provider': 'server'})['busy'])

    def test_output_retry_idempotent_and_mismatch_rejected(self):
        rid, d, r = self.request(True)
        x = {'op': 'stream', 'request_id': rid, 'token': r['token'], 'stream': 'stdout.jsonl', 'offset': 0, 'content_b64': base64.b64encode(b'hello\n').decode()}
        self.assertEqual(self.p.rpc(x)['offset'], 6)
        self.assertEqual(self.p.rpc(x)['offset'], 6)
        self.assertEqual((d / 'stdout.jsonl').read_bytes(), b'hello\n')
        x['content_b64'] = base64.b64encode(b'wrong\n').decode()
        with self.assertRaises(ValueError):
            self.p.rpc(x)

    def test_partial_output_cannot_complete(self):
        rid, d, r = self.request(True)
        (d / 'stdout.jsonl').write_bytes(b'{"type":')
        with self.assertRaises(ValueError):
            self.p.rpc({'op': 'complete', 'request_id': rid, 'token': r['token'], 'rc': 0,
                        'hashes': {'stdout.jsonl': hashlib.sha256(b'{"type":"turn.completed"}\n').hexdigest(), 'stderr.log': hashlib.sha256(b'').hexdigest()}})
        self.assertFalse((d / 'completion.json').exists())

    def test_complete_saves_real_sid_and_is_idempotent(self):
        rid, d, r = self.request(True)
        output = b'{"type":"thread.started","thread_id":"windows-sid"}\n'
        (d / 'stdout.jsonl').write_bytes(output)
        x = {'op': 'complete', 'request_id': rid, 'token': r['token'], 'rc': 0,
             'hashes': {'stdout.jsonl': hashlib.sha256(output).hexdigest(), 'stderr.log': hashlib.sha256(b'').hexdigest()}}
        first = self.p.rpc(x)
        self.assertEqual(self.p.rpc(x), first)
        self.assertEqual(self.p.config()['sessions']['windows'], 'windows-sid')

    def test_wrong_token_cannot_write_project(self):
        rid, d, r = self.request(True)
        with self.assertRaises(ValueError):
            self.p.rpc({'op': 'put', 'request_id': rid, 'token': 'wrong', 'path': 'candidate.py', 'content_b64': 'YQ=='})
        self.assertFalse((self.project / 'candidate.py').exists())

    def test_put_atomic_get_and_path_scope(self):
        self.p.rpc({'op': 'put', 'path': 'sub/result.txt', 'content_b64': base64.b64encode('结果'.encode()).decode()})
        r = self.p.rpc({'op': 'get', 'path': 'sub/result.txt'})
        self.assertEqual(base64.b64decode(r['content_b64']).decode(), '结果')
        for path in ('../outside', '.lemvo-desktop/config.json'):
            with self.assertRaises(ValueError):
                self.p.rpc({'op': 'put', 'path': path, 'content_b64': 'YQ=='})

    def test_init_does_not_touch_existing_live_campaign(self):
        proj = self.base / 'live'
        proj.mkdir()
        (proj / '.lemvo-owner.lock').mkdir()
        (proj / 'research-state.yaml').write_text('original')
        with self.assertRaises(ValueError):
            bridge.Project(proj).rpc({'op': 'init', 'runtime': str(self.runtime)})
        self.assertEqual((proj / 'research-state.yaml').read_text(), 'original')

    def test_sessions_do_not_cross_providers(self):
        argv = ['exec', '--json', '-s', 'workspace-write', 'resume', 'windows-id', 'prompt']
        self.assertEqual(bridge.arguments(argv, 'server-id', 'new'), ['exec', '--json', '-s', 'workspace-write', 'resume', 'server-id', 'new'])
        self.assertEqual(bridge.arguments(argv, None, 'new'), ['exec', '--json', '-s', 'workspace-write', 'new'])
        self.assertNotIn('--approve-for-me',bridge.server_arguments(argv,None,'new'))
        self.assertIn('workspace-write',bridge.server_arguments(argv,None,'new'))
        approved=bridge.server_arguments(argv,None,'new','auto-review')
        self.assertIn('--approve-for-me',approved)
        self.assertNotIn('-s',approved)
        self.assertNotIn('--dangerously-bypass-approvals-and-sandbox',approved)
        with self.assertRaises(ValueError):
            bridge.server_arguments(['exec','-s','read-only','prompt'],None,'new','auto-review')
        self.assertNotIn('--approve-for-me',bridge.server_arguments(['exec','-s','read-only','prompt'],None,'new'))

    def test_registration_cannot_silently_expand_existing_policy(self):
        self.assertEqual(self.p.rpc({'op':'status'})['server_approval'],'preserve')
        with self.assertRaises(ValueError):
            self.p.rpc({'op':'init','server_approval':'auto-review'})
        self.assertEqual(self.p.config()['server_approval'],'preserve')

    def test_mode_spawn_recognized_only_with_project_identity(self):
        script=self.base/'lemvo-run.sh'
        script.write_text('sleep 15\n')
        c=self.p.config()
        env=dict(os.environ,LEMVO_DESKTOP_PROJECT=c['project_id'])
        proc=subprocess.Popen(['bash',str(script)],env=env)
        try:
            lock=self.project/'.lemvo-owner.lock';lock.mkdir()
            (lock/'pid').write_text(str(proc.pid))
            (lock/'starttime').write_text(bridge.identity(proc.pid))
            self.assertTrue(self.p.rpc({'op':'status'})['controller']['bridge_aware'])
            c=self.p.config();c['controller_pids']={};c['project_id']='different-project';self.p.save_config(c)
            self.assertFalse(self.p.rpc({'op':'status'})['controller']['bridge_aware'])
        finally:
            proc.terminate();proc.wait(timeout=5)

    def test_exec_uses_authoritative_remote_directory(self):
        r = self.p.rpc({'op': 'exec', 'script': 'pwd\nprintf sample > observation.txt\n'})
        self.assertEqual(r['rc'], 0)
        self.assertIn(str(self.project), r['stdout'])
        self.assertEqual((self.project / 'observation.txt').read_text(), 'sample')

    def test_cli_usage_error_does_not_become_budget_stop(self):
        c=self.p.config();c['provider']=c['requested_provider']='server'
        c['server_command']=[sys.executable,'-c','import sys;sys.exit(2)']
        self.p.save_config(c)
        run=subprocess.run([sys.executable,str(ROOT/'remote_codex_bridge.py'),'engine','exec','--json','hello'],cwd=self.project,capture_output=True)
        self.assertEqual(run.returncode,64,run.stderr)

    def test_project_queues_are_isolated(self):
        rid, d, r = self.request(True)
        other = self.base / 'other'
        other.mkdir()
        p2 = bridge.Project(other)
        p2.rpc({'op': 'init', 'runtime': str(self.runtime), 'server_command': [sys.executable]})
        self.assertIsNone(p2.rpc({'op': 'poll', 'worker_id': 'worker-a'})['request'])

    def test_actual_engine_queued_windows_to_server_and_real_sid(self):
        fake = self.base / 'fake.py'
        fake.write_text('import json,sys,pathlib\npathlib.Path("args.json").write_text(json.dumps(sys.argv[1:]))\nprint(json.dumps({"type":"thread.started","thread_id":"server-sid"}))\nprint(json.dumps({"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":2}}))\n')
        c = self.p.config()
        c['server_command'] = [sys.executable, str(fake)]
        self.p.save_config(c)
        process = subprocess.Popen([sys.executable, str(ROOT / 'remote_codex_bridge.py'), 'engine', 'exec', '--json', 'resume', 'windows-sid', 'hello'], cwd=self.project, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            end = time.time() + 5
            while not self.p.rpc({'op': 'status'}).get('request') and time.time() < end:
                time.sleep(.02)
            self.p.rpc({'op': 'provider', 'provider': 'server'})
            out, err = process.communicate(timeout=8)
            self.assertEqual(process.returncode, 0, err)
            self.assertIn(b'server-sid', out)
            self.assertNotIn('windows-sid', json.loads((self.project / 'args.json').read_text()))
            self.assertEqual(self.p.config()['sessions']['server'], 'server-sid')
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


class ClientTests(unittest.TestCase):
    def test_claim_response_loss_recovers_before_launch_intent(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = client.Desktop(Path(tmp) / 'registry.json')
            request = {'id': 'a'*32, 'status': 'claimed', 'worker_id': d.config()['worker_id'], 'token': 't'}
            d.rpc = lambda c, x: {'request': request, 'requested_provider': 'windows'}
            d.launch = mock.Mock()
            d.sync_request = mock.Mock(return_value={'state':'MODEL_RUNNING'})
            result=d.tick('a', {}, d.config()['worker_id'])
            self.assertTrue(result['ok'])
            d.launch.assert_called_once()

    def test_aliases_share_launch_intent_and_do_not_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = client.Desktop(Path(tmp) / 'registry.json')
            cfg=d.config()
            cfg['projects']={'a':{'project_id':'same'},'b':{'project_id':'same'}}
            client.atomic(d.registry,cfg)
            request={'id':'b'*32,'token':'t','prompt':'continue','sid':None}
            c={'host':'example','project':'/x','codex':sys.executable}
            with mock.patch.object(client,'detached') as spawn:
                d.launch('a',c,request)
                d.launch('b',c,request)
                self.assertEqual(spawn.call_count,1)

    def test_dead_runner_is_visible_as_recovery_not_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = client.Desktop(Path(tmp) / 'registry.json')
            r={'id':'c'*32,'token':'t'}
            root=d.workspace('a')/'requests'/r['id']
            root.mkdir(parents=True)
            client.atomic(root/'spec.json',{})
            client.atomic(root/'runner.json',{'pid':99999999,'start':'unknown','child_pid':99999998,'child_start':'unknown'})
            self.assertEqual(d.sync_request('a',{},r)['state'],'NEEDS_RECOVERY')

    def test_handoff_missing_controller_is_not_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            d=client.Desktop(Path(tmp)/'registry.json')
            c=d.config();c['projects']={'a':{}};client.atomic(d.registry,c)
            d.rpc=lambda conf,data:{'provider':'server','active':None,'continuation':'controller_missing'}
            self.assertFalse(d.handoff(['a'],'server',0)['ready'])

    def test_live_process_identity_is_stable(self):
        value=client.process_identity(os.getpid())
        self.assertIsNotNone(value)
        self.assertEqual(client.process_identity(os.getpid()),value)

    def test_worker_lock_excludes_second_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / 'lock'
            with client.exclusive(lock):
                with self.assertRaises(RuntimeError):
                    with client.exclusive(lock):
                        pass

    def test_local_child_finishes_and_survives_parent_style_detach(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            command = [sys.executable, '-c', 'import time;time.sleep(.2);print("complete")']
            client.atomic(root / 'spec.json', {'command': command, 'cwd': tmp, 'request_id': 'test'})
            (root / 'prompt.md').write_text('prompt')
            p = client.detached([sys.executable, str(ROOT / 'lemvo_desktop.py'), 'run-local', str(root / 'spec.json')], root, root / 'runner.log')
            self.assertEqual(p.wait(timeout=10), 0)
            self.assertEqual(client.read(root / 'done.json')['rc'], 0)
            self.assertIn(b'complete', (root / 'stdout.jsonl').read_bytes())

    def test_handoff_does_not_report_ready_during_windows_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = client.Desktop(Path(tmp) / 'registry.json')
            c = d.config()
            c['projects'] = {'a': {}}
            client.atomic(d.registry, c)
            d.rpc = lambda conf, data: {'provider': 'windows', 'active': {'provider': 'windows'}}
            self.assertFalse(d.handoff(['a'], 'server', 0)['ready'])

    def test_guidance_and_research_state_not_copied_to_local_truth(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = client.Desktop(Path(tmp) / 'registry.json')
            prompt = d.model_prompt('a', {'host': 'yyc@example', 'project': '/research'}, {'id': 'a', 'token': 't', 'prompt': 'continue'})
            self.assertIn('/research', prompt)
            self.assertIn('authoritative', prompt)
            self.assertFalse((d.home / 'research-state.yaml').exists())
            quoted=d.model_prompt('project with spaces',{'host':'example','project':'/x'},{'id':'a','token':'t','prompt':'continue'})
            self.assertIn("exec 'project with spaces'",quoted)


if __name__ == '__main__':
    unittest.main(verbosity=2)
