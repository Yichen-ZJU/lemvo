"""Real bash controller/adapter, isolated fake CLI; no models or GPU jobs."""
import json
import os
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

DEFAULT = Path(__file__).resolve().parents[1] / 'skills/research-orchestrator/templates'
TEMPLATES = Path(os.environ.get('LEMVO_TEMPLATE_ROOT', str(DEFAULT)))
RUN = TEMPLATES / 'lemvo-run.sh'
AUTO = TEMPLATES / 'autoloop-reference.sh'
SID = '11111111-2222-4333-8444-555555555555'

FAKE = r'''#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path
args=sys.argv[1:]
msg=args[args.index('-p')+1] if '-p' in args else args[-1]
with open('.test-calls.jsonl','a') as f: f.write(json.dumps({'message':msg,'args':args})+'\n')
time.sleep(float(os.environ.get('FAKE_DELAY','0')))
kind=os.environ.get('FAKE_KIND','ok')
if kind=='garbage':
    print('{"type":"unrecognized.completed","payload":"ok"}'); sys.exit(0)
if kind=='blocked-text':
    Path('STATE.md').write_text('ready_blocker\n')
if kind=='construct':
    Path('method.py').write_text('# method change '+str(time.time_ns()))
if os.environ.get('FAKE_DONE')=='1': Path('CAMPAIGN-DONE').write_text('test done')
event={'type':'result','session_id':'11111111-2222-4333-8444-555555555555',
       'is_error':False, 'result':'ready_blocker' if kind=='blocked-text' else 'work done'}
if kind!='no-usage': event['usage']={'input_tokens':10,'output_tokens':2}
if kind=='pretty': print(json.dumps(event,indent=2))
else: print(json.dumps(event))
'''


class Liveness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='lemvo-liveness-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.p = self.root / 'project'
        self.p.mkdir()
        self.fake = self.root / 'fake-claude'
        self.fake.write_text(FAKE)
        self.fake.chmod(0o700)
        self.env = dict(os.environ, ENGINE='claude', CLAUDE_BIN=str(self.fake),
                        LEMVO_RC_MODE='structured', LEMVO_POLL_SEC='0.05',
                        TURN_GAP_SEC='0', LEMVO_WAIT_RECHECK_SEC='0.25',
                        LEMVO_UNKNOWN_JOB_SEC='0.25',
                        # This suite bounds waits to 8s. The production in-turn
                        # watchdog polls every 5s; two settled turns legitimately
                        # exceed that bound. Accelerate only the fixture clock,
                        # not acknowledgement/queue assertions or production defaults.
                        LEMVO_TURN_POLL_SEC='0.03')

    def state(self, status='ACTIVE'):
        (self.p/'research-state.yaml').write_text('campaign_status: '+status+'\n'
            'project:\n  status: ACTIVE\nbudget:\n  unit: usd\n  total_cap: null\n')

    def call(self, command, *args, **env):
        return subprocess.run(['bash', str(RUN), command, str(self.p), *map(str,args)],
            env=dict(self.env,**env), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            universal_newlines=True, timeout=15)

    def adapter(self, *args, **env):
        return subprocess.run(['bash',str(AUTO),str(self.p),*map(str,args)],
            env=dict(self.env,**env),stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
            universal_newlines=True,timeout=15)

    def start(self, *args, **env):
        out=open(self.root/('controller-%s.log'%time.time_ns()),'w')
        self.addCleanup(out.close)
        proc=subprocess.Popen(['bash',str(RUN),'start',str(self.p),*map(str,args)],
            env=dict(self.env,**env),stdout=out,stderr=out,start_new_session=True)
        def stop():
            try: os.killpg(proc.pid,signal.SIGTERM)
            except ProcessLookupError: pass
            try: proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid,signal.SIGKILL); proc.wait()
        self.addCleanup(stop)
        return proc

    def until(self, pred, timeout=8):
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            if pred(): return True
            time.sleep(.05)
        return False

    def calls(self):
        f=self.p/'.test-calls.jsonl'
        return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []

    def test_bootstrap_with_caller_initializes_machine_state(self):
        (self.p/'CHARTER.md').write_text('Research the convergence of a CPU estimator.')
        msg=self.root/'instruction.md'; msg.write_text('Start the authorized task.')
        result=self.adapter(msg)
        self.assertEqual(result.returncode,0,result.stdout)
        self.assertTrue((self.p/'research-state.yaml').exists(),result.stdout)
        self.assertIn('CHARTER.md',self.calls()[0]['message'])
        self.assertIn('Start the authorized task.',self.calls()[0]['message'])

    def test_waiting_resource_reassesses_without_job(self):
        self.state('WAITING_RESOURCE')
        proc=self.start('--engine','claude','--mode','unattended',FAKE_DONE='1')
        self.assertTrue(self.until(lambda:bool(self.calls())), 'WAITING_RESOURCE never reassessed')
        self.assertTrue(self.until(lambda:proc.poll() is not None))
        self.assertEqual(len(self.calls()),1)

    def test_unknown_done_file_job_is_not_infinite_wait(self):
        self.state()
        (self.p/'.lemvo-jobs.json').write_text(json.dumps([{'name':'lost-worker',
            'run_id':'r1','done_file':'missing-result.json','started_epoch':time.time()-60}]))
        proc=self.start('--engine','claude','--mode','unattended',FAKE_DONE='1')
        self.assertTrue(self.until(lambda:bool(self.calls())), 'unobservable job never wakes')
        self.assertTrue(self.until(lambda:proc.poll() is not None))
        receipts=[json.loads(p.read_text()) for p in (self.p/'.lemvo-receipts').glob('*.json')]
        self.assertTrue(receipts)
        self.assertTrue(all(r['status']!='succeeded' for r in receipts))

    def test_unrecognized_terminal_does_not_consume_steer(self):
        self.state()
        msg=self.root/'message.md'; msg.write_text('Keep this instruction until delivered.')
        result=self.call('advance',msg,FAKE_KIND='garbage')
        self.assertEqual(result.returncode,9,result.stdout)
        self.assertEqual(len(list((self.p/'.lemvo-steer-queue').glob('q-*.md'))),1)

    def test_missing_usage_does_not_disable_stall_gate(self):
        self.state()
        results=[self.adapter(FAKE_KIND='no-usage',LEMVO_STALL_ARTIFACT_N='2') for _ in range(2)]
        self.assertEqual([r.returncode for r in results],[0,2])
        ledger=(self.p/'.autoloop-prep-ledger.jsonl').read_text()
        self.assertIn('stall-no-artifact',ledger)

    def test_metadata_only_rewrite_does_not_reset_stall(self):
        self.state()
        results=[self.adapter(FAKE_KIND='blocked-text',LEMVO_STALL_ARTIFACT_N='2') for _ in range(2)]
        self.assertEqual([r.returncode for r in results],[0,2])

    def test_explicit_stop_still_blocks_resource_reassessment(self):
        self.state('WAITING_RESOURCE'); (self.p/'STOP').touch()
        result=self.adapter(LEMVO_RESOURCE_WAKE='resource-recheck')
        self.assertEqual(result.returncode,6,result.stdout)
        self.assertFalse(self.calls())

    def test_purge_advances_session_epoch_and_preserves_research(self):
        self.state()
        (self.p/'.autoloop-session-id').write_text(SID)
        (self.p/'findings.md').write_text('Keep observed positive and negative evidence.')
        before=(self.p/'research-state.yaml').read_bytes()
        result=self.call('purge')
        self.assertEqual(result.returncode,0,result.stdout)
        self.assertTrue((self.p/'.lemvo-session-epoch').exists())
        self.assertFalse((self.p/'.autoloop-session-id').exists())
        self.assertEqual((self.p/'research-state.yaml').read_bytes(),before)
        self.assertEqual((self.p/'findings.md').read_text(),'Keep observed positive and negative evidence.')

    def test_inline_selection_reaches_every_prompt(self):
        self.state()
        self.assertEqual(self.call('delegation','inline').returncode,0)
        msg=self.root/'steer.md'; msg.write_text('Use this existing implementation.')
        result=self.adapter(msg)
        self.assertEqual(result.returncode,0,result.stdout)
        prompt=self.calls()[0]['message']
        self.assertIn('Do not spawn subagents',prompt)
        self.assertIn('Use this existing implementation.',prompt)
        self.assertEqual(self.call('delegation','invalid').returncode,64)
        self.assertEqual((self.p/'.lemvo-delegation').read_text().strip(),'inline')

    def test_preselected_mode_does_not_ask_again(self):
        self.state(); (self.p/'.lemvo-mode').write_text('unattended')
        self.assertEqual(self.adapter().returncode,0)
        self.assertIn('mode=unattended',self.calls()[0]['message'])
        self.assertIn('do not ask again',self.calls()[0]['message'])

    def test_code_construction_keeps_liveness_even_without_metrics(self):
        self.state()
        for _ in range(3):
            result=self.adapter(FAKE_KIND='construct',LEMVO_STALL_ARTIFACT_N='2')
            self.assertEqual(result.returncode,0,result.stdout)
        self.assertEqual((self.p/'.autoloop-stall-count').read_text().strip(),'0')

    def test_pretty_json_terminal_and_usage(self):
        self.state()
        result=self.adapter(FAKE_KIND='pretty')
        self.assertEqual(result.returncode,0,result.stdout)
        events=[json.loads(s) for s in (self.p/'.autoloop-prep-ledger.jsonl').read_text().splitlines() if s.startswith('{')]
        last=[e for e in events if e.get('type')=='prep-turn'][-1]
        self.assertEqual(last['tokens_in'],10)

    def test_wrapped_codex_events_parse_without_modelusage(self):
        self.state(); self.assertEqual(self.adapter().returncode,0)
        output=self.p/'.codex-fixture.jsonl'
        output.write_text(json.dumps({'type':'event_msg','payload':{'type':'token_count',
            'info':{'total_token_usage':{'input_tokens':50,'output_tokens':3,'cached_input_tokens':20}}}})+'\n'+
            json.dumps({'type':'turn.completed','usage':{'input_tokens':50,'output_tokens':3,'cached_input_tokens':20}})+'\n')
        r=subprocess.run(['python3',str(self.p/'.autoloop-helper.py'),'usage-from',str(output),'none','codex'],
            cwd=str(self.p),capture_output=True,text=True,check=True)
        self.assertIn('TOKENS_IN=50',r.stdout)
        self.assertIn('TOKENS_OUT=3',r.stdout)
        self.assertIn('MODELUSAGE=absent',r.stdout)

    def test_dead_pid_wakes_with_non_success_receipt(self):
        self.state()
        (self.p/'.lemvo-jobs.json').write_text(json.dumps([{'name':'dead-worker','run_id':'r1',
            'pid':99999999,'done_file':'missing.json'}]))
        proc=self.start('--engine','claude','--mode','unattended',FAKE_DONE='1')
        self.assertTrue(self.until(lambda:proc.poll() is not None))
        receipts=[json.loads(p.read_text()) for p in (self.p/'.lemvo-receipts').glob('*.json')]
        self.assertEqual(receipts[0]['status'],'ended')
        self.assertEqual(len(self.calls()),1)

    def test_pid_reuse_does_not_wait_for_unrelated_process(self):
        self.state()
        (self.p/'.lemvo-jobs.json').write_text(json.dumps([{'name':'wrong-identity','run_id':'r1',
            'pid':os.getpid(),'pid_start':'impossible','done_file':'missing.json'}]))
        proc=self.start('--engine','claude','--mode','unattended',FAKE_DONE='1')
        self.assertTrue(self.until(lambda:proc.poll() is not None))
        self.assertEqual(len(self.calls()),1)

    def test_long_turn_steer_retained_then_delivered_next_turn(self):
        self.state()
        proc=self.start('--engine','claude','--mode','unattended','--max-turns','2',FAKE_DELAY='1')
        self.assertTrue(self.until(lambda:len(self.calls())==1))
        msg=self.root/'message.md'; msg.write_text('UNIQUE-MID-TURN-GUIDANCE')
        self.assertEqual(self.call('steer',msg).returncode,0)
        self.assertTrue(list((self.p/'.lemvo-steer-queue').glob('q-*.md')))
        self.assertTrue(self.until(lambda:proc.poll() is not None))
        self.assertEqual(len(self.calls()),2)
        self.assertNotIn('UNIQUE-MID-TURN-GUIDANCE',self.calls()[0]['message'])
        self.assertIn('UNIQUE-MID-TURN-GUIDANCE',self.calls()[1]['message'])
        delivery=[json.loads(s) for s in (self.p/'.lemvo-delivery.jsonl').read_text().splitlines()]
        self.assertEqual([e['event'] for e in delivery],['queued','submitted','delivered'])
        self.assertTrue(msg.exists())

    def test_steer_wakes_waiting_resource_before_deadline(self):
        self.state('WAITING_RESOURCE')
        proc=self.start('--engine','claude','--mode','unattended',FAKE_DONE='1',LEMVO_WAIT_RECHECK_SEC='9999')
        self.assertTrue(self.until(lambda:(self.p/'.lemvo-resource-wait.json').exists()))
        self.assertFalse(self.calls())
        msg=self.root/'wake.md'; msg.write_text('The resource is available; inspect it.')
        self.assertEqual(self.call('steer',msg).returncode,0)
        self.assertTrue(self.until(lambda:proc.poll() is not None))
        self.assertEqual(len(self.calls()),1)
        self.assertIn('user-steer',self.calls()[0]['message'])

    def test_guided_stays_idle_until_advance_and_switch_continues(self):
        self.state()
        proc=self.start('--engine','claude','--mode','guided',FAKE_DONE='1')
        self.assertTrue(self.until(lambda:(self.p/'.lemvo-status').exists()))
        time.sleep(.2); self.assertFalse(self.calls())
        self.assertEqual(self.call('switch','unattended').returncode,0)
        self.assertTrue(self.until(lambda:proc.poll() is not None))
        self.assertEqual(len(self.calls()),1)

    def test_duplicate_start_cannot_change_configuration(self):
        self.state()
        proc=self.start('--engine','claude','--mode','guided')
        self.assertTrue(self.until(lambda:(self.p/'.lemvo-mode').exists() and (self.p/'.lemvo-mode').read_text().strip()=='guided'))
        r=self.call('start','--engine','codex','--mode','unattended','--delegation','inline')
        self.assertEqual(r.returncode,0,r.stdout)
        self.assertEqual((self.p/'.lemvo-mode').read_text().strip(),'guided')
        self.assertEqual((self.p/'.lemvo-engine').read_text().strip(),'claude')
        self.assertFalse((self.p/'.lemvo-delegation').exists())
        self.assertIsNone(proc.poll())

    def test_purge_stops_only_registered_runtime_not_training(self):
        self.state()
        training=subprocess.Popen(['sleep','30'],cwd=str(self.p))
        self.addCleanup(lambda:(training.terminate(),training.wait()))
        proc=self.start('--engine','claude','--mode','unattended',FAKE_DELAY='30')
        self.assertTrue(self.until(lambda:bool(self.calls())))
        result=self.call('purge','--stop-runtime')
        self.assertEqual(result.returncode,0,result.stdout)
        self.assertTrue(self.until(lambda:proc.poll() is not None))
        self.assertIsNone(training.poll())
        self.assertFalse((self.p/'.autoloop-session-id').exists())
        self.assertTrue(list(self.p.glob('poisoned-archive-*')))

    def test_old_epoch_recovery_cannot_restore_session(self):
        self.state(); self.assertEqual(self.adapter().returncode,0)
        old=sorted((self.p/'turns').iterdir())[0]
        (old/'result.json').unlink()
        self.assertEqual(self.call('purge').returncode,0)
        arc=next(self.p.glob('poisoned-archive-*'))
        td=arc/'turns'/old.name
        r=subprocess.run(['bash',str(AUTO),'--recover',str(self.p),str(td)],
            env=self.env,capture_output=True,text=True,timeout=10)
        self.assertEqual(r.returncode,9,r.stdout+r.stderr)
        self.assertFalse((self.p/'.autoloop-session-id').exists())

    def test_doctor_inventory_does_not_kill_legacy_loop(self):
        self.state()
        script=self.p/'legacy-retry-loop.sh'; script.write_text('sleep 30\n')
        proc=subprocess.Popen(['bash',str(script)],cwd=str(self.p),start_new_session=True)
        self.addCleanup(lambda:(os.killpg(proc.pid,signal.SIGTERM),proc.wait()))
        report=self.call('doctor')
        self.assertEqual(report.returncode,0,report.stdout)
        data=json.loads(report.stdout)
        self.assertIn(proc.pid,[c['pid'] for c in data['legacy_candidates']])
        self.assertIsNone(proc.poll())

    def test_missing_option_values_fail_promptly(self):
        for option in ('--mode','--engine','--max-turns','--delegation'):
            self.assertEqual(self.call('start',option).returncode,64)

    def test_guided_advance_can_reassess_resource_wait(self):
        self.state('WAITING_RESOURCE')
        proc=self.start('--engine','claude','--mode','guided',FAKE_DONE='1',LEMVO_WAIT_RECHECK_SEC='9999')
        self.assertTrue(self.until(lambda:(self.p/'.lemvo-status').exists()))
        self.assertEqual(self.call('advance').returncode,0)
        self.assertTrue(self.until(lambda:proc.poll() is not None))
        self.assertEqual(len(self.calls()),1)

    def test_purge_fences_late_adapter_writes(self):
        self.state()
        out=open(self.root/'adapter.log','w'); self.addCleanup(out.close)
        proc=subprocess.Popen(['bash',str(AUTO),str(self.p)],env=dict(self.env,FAKE_DELAY='1'),stdout=out,stderr=out)
        self.addCleanup(lambda:proc.wait(timeout=10))
        self.assertTrue(self.until(lambda:bool(self.calls())))
        subprocess.run(['python3',str(TEMPLATES/'runtime-support.py'),'new-epoch'],cwd=str(self.p),check=True)
        self.assertEqual(proc.wait(timeout=10),9)
        self.assertFalse((self.p/'.autoloop-session-id').exists())

    def test_truncated_result_waits_for_live_producer(self):
        self.state()
        worker=subprocess.Popen(['sleep','30'])
        self.addCleanup(lambda:(worker.terminate(),worker.wait()))
        start=Path('/proc/%d/stat'%worker.pid).read_text().rsplit(')',1)[1].split()[19]
        result=self.p/'result.json'; result.write_text('{"status": "succ')
        (self.p/'.lemvo-jobs.json').write_text(json.dumps([{'name':'worker','run_id':'r1',
            'pid':worker.pid,'pid_start':start,'done_file':'result.json'}]))
        proc=self.start('--engine','claude','--mode','unattended',FAKE_DONE='1')
        self.assertTrue(self.until(lambda:(self.p/'.lemvo-status').exists()))
        time.sleep(.3); self.assertFalse(self.calls())
        tmp=self.p/'result.tmp'; tmp.write_text(json.dumps({'run_id':'r1','status':'succeeded','exit_code':0}))
        tmp.replace(result)
        self.assertTrue(self.until(lambda:proc.poll() is not None))
        self.assertEqual(len(self.calls()),1)

    def test_resource_wait_deadline_survives_restart(self):
        self.state('WAITING_RESOURCE')
        support=TEMPLATES/'runtime-support.py'
        env=dict(self.env,LEMVO_WAIT_RECHECK_SEC='9999')
        def check(): return subprocess.check_output(['python3',str(support),'wait-check','WAITING_RESOURCE'],cwd=str(self.p),env=env,text=True).strip()
        self.assertEqual(check(),'wait')
        path=self.p/'.lemvo-resource-wait.json'; saved=json.loads(path.read_text())
        self.assertEqual(check(),'wait')
        self.assertEqual(json.loads(path.read_text())['since'],saved['since'])
        saved['checked']=time.time()-10000; path.write_text(json.dumps(saved))
        self.assertEqual(check(),'resource-recheck')


if __name__=='__main__':
    unittest.main(verbosity=2)
