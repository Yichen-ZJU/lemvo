"""Isolated, CPU-only acceptance. Never attaches to a real research process."""
import contextlib
import http.client
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lemvo_console.collector import Collector, Processes, describe_job, engine_of
from lemvo_console.cli import register_process
from lemvo_console.server import App, Server
from lemvo_console.store import Store, atomic, key, load, redact
from lemvo_console.transcripts import summarize


class BaseFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='lemvo-console-test-')
        self.root = Path(self.tmp.name)
        self.home = self.root / 'home'
        self.home.mkdir()
        self.project = self.home / 'project'
        self.project.mkdir()
        self.store = Store(self.root / 'console')
        self.proc = Processes()
        self.proc.items = {111: {'pid': 111, 'start': '22', 'state': 'S', 'ppid': 1,
            'args': ['python3', 'train.py'], 'cwd': str(self.project), 'rss_bytes': 1024, 'cpu_ticks': 100}}
        self.child = []

    def tearDown(self):
        for child in self.child:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=5)
        self.tmp.cleanup()

    def log(self, events):
        p = self.root / 'session.jsonl'
        p.write_text(''.join(json.dumps(e) + '\n' for e in events))
        return p

    def job(self, **kwargs):
        return dict(run_id='fit-1', pid=111, pid_start='22', status='running', **kwargs)

    def inspect(self, job=None, previous=None, now=100):
        return describe_job(job or self.job(), str(self.project), self.proc,
                            previous if previous is not None else {}, now, 30, self.store.path)

    def progress(self, current=1, phase='training', **kwargs):
        atomic(self.store.path / 'progress' / (key(str(self.project), 'fit-1') + '.json'),
               dict(run_id='fit-1', pid=111, pid_start='22', phase=phase,
                    current=current, total=100, unit='steps', at=100, **kwargs))

class ObservationTests(BaseFixture):
    def test_claude_waiting_not_hung_after_tool_completion(self):
        p = self.log([
            {'type':'assistant','timestamp':'2026-01-01T00:00:00Z','message':{'content':[{'type':'tool_use','id':'a','name':'Bash','input':{'command':'find /'}}]}},
            {'type':'user','timestamp':'2026-01-01T00:00:01Z','message':{'content':[{'type':'tool_result','tool_use_id':'a','content':'done'}]}},
            {'type':'assistant','timestamp':'2026-01-01T00:00:02Z','message':{'stop_reason':'end_turn','content':[{'type':'text','text':'Please choose'}]}},
            {'type':'progress','timestamp':'2026-01-02T00:00:00Z'}])
        s = summarize(p, 'claude')
        self.assertEqual(s['phase'], 'waiting_user')
        self.assertEqual(s['pending_tools'], [])
        self.assertLess(s['last_event_at'], 1767312000)

    def test_claude_tool_request_not_claimed_running(self):
        p = self.log([{'type':'assistant','message':{'content':[{'type':'tool_use','id':'a','name':'Bash','input':{'command':'curl --max-time 10 example.com'}}]}}])
        self.assertEqual(summarize(p, 'claude')['phase'], 'tool_pending')

    def test_codex_call_completed_field_not_completion_receipt(self):
        p = self.log([{'type':'response_item','payload':{'type':'custom_tool_call','call_id':'x','name':'exec','status':'completed','input':'sleep(999)'}}])
        self.assertEqual(len(summarize(p, 'codex')['pending_tools']), 1)

    def test_codex_new_format_and_end_of_turn(self):
        p = self.log([
            {'type':'event_msg','payload':{'type':'task_started'}},
            {'type':'response_item','payload':{'type':'custom_tool_call','call_id':'x','name':'exec','input':'x'}},
            {'type':'response_item','payload':{'type':'custom_tool_call_output','call_id':'x','output':'done'}},
            {'type':'token_usage_record','payload':{'thread_token_usage': {'total_tokens':10}}},
            {'type':'event_msg','payload':{'type':'task_complete'}}])
        s = summarize(p, 'codex')
        self.assertEqual(s['phase'], 'waiting_user')
        self.assertEqual(s['pending_tools'], [])

    def test_unknown_schema_is_unknown_not_green(self):
        p = self.log([{'type':'future_schema','payload':{'success':True}}])
        s = summarize(p, 'codex')
        self.assertEqual(s['phase'], 'unknown')
        self.assertTrue(s['parse_warning'])

    def test_partial_json_does_not_complete_job(self):
        result = self.project / 'result.json'
        result.write_text('{"run_id":"fit-1","status":"succeeded",')
        job = self.job(done_file=str(result))
        job['status'] = 'succeeded'
        s = self.inspect(job)
        self.assertEqual(s['status'], 'running')
        self.assertTrue(s['warning'])

    def test_wrong_run_cannot_complete_job(self):
        result = self.project / 'result.json'
        atomic(result, {'run_id':'other','status':'succeeded','exit_code':0})
        self.assertNotEqual(self.inspect(self.job(done_file=str(result)))['status'], 'succeeded')

    def test_terminal_registry_without_receipt_not_success(self):
        j=self.job();j['status']='succeeded'
        self.assertNotEqual(self.inspect(j)['status'],'succeeded')

    def test_stale_progress_report_not_fresh_on_first_attachment(self):
        self.progress(1)
        self.assertEqual(self.inspect(now=500)['health'],'attention')

    def test_valid_result_is_recognized_and_live_process_warning(self):
        result = self.project / 'result.json'
        atomic(result, {'run_id':'fit-1','status':'succeeded','exit_code':0})
        s = self.inspect(self.job(done_file=str(result)))
        self.assertEqual(s['status'], 'succeeded')
        self.assertIn('still alive', s['warning'])

    def test_dead_job_wakes_attention_without_success(self):
        self.proc.items = {}
        s = self.inspect()
        self.assertEqual((s['status'], s['health']), ('ended', 'attention'))

    def test_reused_pid_not_adopted(self):
        self.proc.items[111]['start'] = '33'
        self.assertFalse(self.inspect()['alive'])

    def test_missing_identity_is_unknown(self):
        j = self.job()
        del j['pid_start']
        self.assertEqual(self.inspect(j)['status'], 'unknown')

    def test_heartbeat_updates_do_not_refresh_progress(self):
        previous = {}
        self.progress(1)
        self.assertEqual(self.inspect(previous=previous)['health'], 'progressing')
        self.progress(1, detail='Still working')
        s = self.inspect(previous=previous, now=140)
        self.assertEqual(s['last_progress_at'], 100)
        self.assertEqual(s['health'], 'attention')

    def test_real_counter_resets_progress_warning(self):
        previous = {}
        self.progress(1)
        self.inspect(previous=previous)
        self.progress(2)
        self.assertEqual(self.inspect(previous=previous, now=140)['health'], 'progressing')

    def test_loading_shards_and_training_are_distinct(self):
        self.progress(3, 'loading_model')
        self.assertEqual(self.inspect()['phase'], 'loading_model')
        self.progress(4, 'loading_data')
        self.assertEqual(self.inspect()['phase'], 'loading_data')

    def test_silent_training_not_killed(self):
        previous = {}
        self.inspect(previous=previous)
        with patch('os.kill', side_effect=AssertionError('observer must never send signals')):
            s = self.inspect(previous=previous, now=1000)
        self.assertTrue(s['alive'])
        self.assertIn('No automatic', s['warning'])

    def test_other_job_progress_not_shared(self):
        self.progress(8)
        j = self.job()
        j['run_id'] = 'different'
        self.assertIsNone(self.inspect(j)['progress'])

    def test_invalid_registry_is_not_overwritten(self):
        p = self.store.path / 'registry.json'
        p.write_text('{broken')
        with self.assertRaises(ValueError):
            self.store.register(self.project)
        self.assertEqual(p.read_text(), '{broken')

    def test_no_secrets_in_common_output_forms(self):
        value = redact('api_key=abc123 password=secret Bearer xxxxx sk-testabcdefghijklmnopqrstuvwxyz https://u:p@host "access_token": "sensitiveABC"')
        for secret in ('abc123','secret','xxxxx','sk-testabc','u:p@','sensitiveABC'):
            self.assertNotIn(secret, value)

    def test_service_helpers_not_conversations(self):
        for args in (['python3','gpu_pool.py'],['codex','app-server','daemon','pid-update-loop'],['codex-code-mode-host'],['bash','-c','claude']):
            self.assertIsNone(engine_of({'args':args}))

    def test_corrupt_job_registry_warning(self):
        self.store.register(self.project)
        (self.project / '.lemvo-jobs.json').write_text('{partial')
        collector = Collector(self.store, self.home)
        with patch.object(collector.processes, 'scan', return_value={}), patch('lemvo_console.collector.tmux_panes', return_value={}), patch.object(collector, 'hardware', return_value={}):
            s = collector.collect()
        self.assertTrue(s['projects'][0]['warning'])

    def test_multi_session_one_backend_and_no_project_job_misattribution(self):
        logs = []
        for name in ('one','two'):
            p = self.root / (name+'.jsonl')
            p.write_text(json.dumps({'type':'session_meta','payload':{'id':name,'cwd':str(self.project)}})+'\n'+json.dumps({'type':'event_msg','payload':{'type':'task_complete'}})+'\n')
            logs.append(str(p))
        collector = Collector(self.store, self.home)
        collector.processes.items = {111:dict(self.proc.items[111],args=['codex','app-server'])}
        atomic(self.project / '.lemvo-jobs.json', [self.job()])
        with patch.object(collector.processes,'scan'),patch.object(collector.processes,'transcripts',return_value=logs),patch.object(collector.processes,'env',return_value={}),patch('lemvo_console.collector.tmux_panes',return_value={}),patch.object(collector,'hardware',return_value={}):
            s = collector.collect()
        self.assertEqual(len(s['sessions']),2)
        self.assertTrue(all(x['shared_process'] for x in s['sessions']))
        self.assertTrue(all(x['job_ids']==[] for x in s['sessions']))
        self.assertTrue(all(x['health']=='waiting' for x in s['sessions']))

    def test_explicit_project_binding_does_not_leak_to_shared_threads(self):
        logs=[]
        for name in ('one','two'):
            path=self.root/(name+'.jsonl')
            path.write_text(json.dumps({'type':'session_meta','payload':{'id':name,'cwd':str(self.home)}})+'\n')
            logs.append(str(path))
        self.store.register(str(self.project),{'id':'binding-one','engine':'codex','pid':111,
            'pid_start':'22','session_id':'one','transcript':logs[0]})
        collector=Collector(self.store,self.home)
        collector.processes.items={111:dict(self.proc.items[111],args=['codex','app-server'])}
        with patch.object(collector.processes,'scan'),patch.object(collector.processes,'transcripts',return_value=logs),patch.object(collector.processes,'env',return_value={}),patch('lemvo_console.collector.tmux_panes',return_value={}),patch.object(collector,'hardware',return_value={}):
            snapshot=collector.collect()
        projects={s['session_id']:s['project'] for s in snapshot['sessions']}
        self.assertEqual(projects['one'],str(self.project))
        self.assertEqual(projects['two'],str(self.home))

    def test_registered_real_process_is_unchanged_by_collection(self):
        child = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(60)'])
        self.child.append(child)
        log = self.log([{'type':'assistant','message':{'stop_reason':'end_turn','content':[{'type':'text','text':'waiting'}]}}])
        s = register_process(self.store, child.pid, 'claude', str(self.project), str(log))
        collector = Collector(self.store, self.home)
        # Only enumerate the isolated child, not production sessions.
        original_scan = collector.processes.scan
        def scan():
            original_scan()
            collector.processes.items = {k:v for k,v in collector.processes.items.items() if k==child.pid}
        with patch.object(collector.processes,'scan',side_effect=scan),patch('lemvo_console.collector.tmux_panes',return_value={}),patch.object(collector,'hardware',return_value={}):
            snapshot = collector.collect()
        self.assertIsNone(child.poll())
        self.assertEqual(snapshot['sessions'][0]['phase'],'waiting_user')


class HTTPTests(BaseFixture):
    def setUp(self):
        super().setUp()
        self.app = App(self.store)
        self.server = Server(('127.0.0.1', 0), self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        super().tearDown()

    def request(self,path,body=None,auth=True,origin=None):
        conn = http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=3)
        headers={'Authorization':'Bearer '+self.app.token} if auth else {}
        if origin:headers['Origin']=origin
        conn.request('POST' if body is not None else 'GET',path,json.dumps(body) if body is not None else None,headers)
        resp=conn.getresponse();status=resp.status;data=resp.read();conn.close()
        return status,data

    def test_api_requires_token(self):
        self.assertEqual(self.request('/api/snapshot',auth=False)[0],401)
        self.assertEqual(self.request('/api/snapshot')[0],200)

    def test_foreign_origin_cannot_control(self):
        self.assertEqual(self.request('/api/action',{},origin='https://elsewhere.test')[0],403)

    def test_default_read_only(self):
        status,data=self.request('/api/action',{'kind':'mode'})
        self.assertEqual(status,409)
        self.assertIn(b'read-only',data)

    def test_path_traversal_not_served(self):
        self.assertEqual(self.request('/../../token')[0],404)

    def test_stale_snapshot_visible(self):
        status,data=self.request('/api/snapshot')
        self.assertTrue(json.loads(data)['stale'])

    def test_html_has_no_embedded_token(self):
        status,data=self.request('/',auth=False)
        self.assertEqual(status,200)
        self.assertNotIn(self.app.token.encode(),data)


class ControlsTests(BaseFixture):
    def setup_controller(self):
        # CPU stub is deliberately a real owned process, not a production controller.
        script=self.root/'lemvo-run-stub.py'
        script.write_text('import time\ntime.sleep(60)\n')
        child=subprocess.Popen([sys.executable,str(script)]);self.child.append(child)
        proc=Processes();proc.scan();start=proc.items[child.pid]['start']
        atomic(self.project/'.lemvo-owner.lock/pid',str(child.pid));atomic(self.project/'.lemvo-owner.lock/starttime',start)
        atomic(self.project/'.lemvo-mode','guided');atomic(self.project/'.lemvo-engine','claude');atomic(self.project/'.lemvo-status','WAITING_USER|guided-idle')
        self.app=App(self.store,allow_controls=True)
        self.app.snapshot={'at':time.time(),'projects':[{'id':key(self.project),'path':str(self.project)}]}
        return {'request_id':'test-request','project_id':key(self.project),'owner_pid':child.pid,'owner_start':start}

    def test_modes_shared_file_and_idempotent_request(self):
        data=self.setup_controller();data.update(kind='mode',mode='unattended')
        a=self.app.action(data);b=self.app.action(data)
        self.assertEqual(a,b)
        self.assertEqual((self.project/'.lemvo-mode').read_text(),'unattended\n')
        self.assertIsNone(self.child[0].poll())

    def test_stale_owner_cannot_receive_action(self):
        data=self.setup_controller();data.update(kind='mode',mode='unattended',owner_start='wrong')
        with self.assertRaisesRegex(ValueError,'Controller changed'):self.app.action(data)
        self.assertEqual((self.project/'.lemvo-mode').read_text(),'guided')

    def test_completed_project_not_reopened(self):
        data=self.setup_controller();data.update(kind='mode',mode='unattended')
        atomic(self.project/'.lemvo-status','COMPLETED|done')
        with self.assertRaisesRegex(ValueError,'completed'):self.app.action(data)

    def test_guidance_uses_existing_queue_once(self):
        data=self.setup_controller();data.update(kind='steer',text='Inspect the loading progress before next experiment.')
        contract=Path(__file__).resolve().parents[2]/'claude/skills/research-orchestrator/templates/runtime-contract.py'
        if not contract.exists():contract=Path(__file__).resolve().parents[2]/'skills/research-orchestrator/templates/runtime-contract.py'
        if not contract.exists():self.skipTest('Run in the public repository to exercise the production contract')
        with patch.object(self.app,'contract',return_value=contract):
            first=self.app.action(data);second=self.app.action(data)
        self.assertEqual(first,second)
        self.assertEqual(first['status'],'queued')
        self.assertEqual(len(list((self.project/'.lemvo-steer-queue').glob('q-*.md'))),1)
        self.assertFalse((self.project/'.lemvo-advance-request').exists())


if __name__=='__main__':
    unittest.main(verbosity=2)
