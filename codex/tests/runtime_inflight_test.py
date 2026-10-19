"""Real adapter/controller + fake streaming CLIs; no paid models or GPU jobs."""
import importlib.util, json, os, signal, subprocess, sys, time, unittest
from pathlib import Path
from unittest.mock import patch
import runtime_liveness_test as previous

ROOT = previous.TEMPLATES
spec = importlib.util.spec_from_file_location('support', str(ROOT/'runtime-support.py'))
support = importlib.util.module_from_spec(spec); spec.loader.exec_module(support)

FAKE = r'''#!/usr/bin/env python3
import json, os, sys, time, signal
from pathlib import Path
args=sys.argv[1:]; msg=args[args.index('-p')+1] if '-p' in args else args[-1]
with open('.test-calls.jsonl','a') as f: f.write(json.dumps({'message':msg,'args':args})+'\n')
print(json.dumps({'type':'system','session_id':'11111111-2222-4333-8444-555555555555'}),flush=True)
kind=os.environ.get('FAKE_KIND','hang')
if kind=='hang-once':
 count=len(Path('.test-calls.jsonl').read_text().splitlines())
 if count==1: kind='hang'
 else:
  kind='ok'; Path('CAMPAIGN-DONE').write_text('isolated test finished')
if kind=='ignore-term': signal.signal(signal.SIGTERM,signal.SIG_IGN)
if kind=='interrupt': os.kill(os.getpid(),signal.SIGTERM)
if kind in ('hang','ignore-term'): time.sleep(10800)
if kind=='active':
 for i in range(12):
  print(json.dumps({'type':'stream_event','event':{'type':'content_block_delta','delta':{'text':'thinking'}}}),flush=True)
  time.sleep(.1)
if kind=='lease': time.sleep(1.5)
print(json.dumps({'type':'assistant','message':{'usage':{'input_tokens':999,'output_tokens':888}}}),flush=True)
print(json.dumps({'type':'result','session_id':'11111111-2222-4333-8444-555555555555',
 'is_error':False,'usage':{'input_tokens':10,'output_tokens':2},'total_cost_usd':1}),flush=True)
'''

class Inflight(previous.Liveness):
    def setUp(self):
        super().setUp(); self.fake.write_text(FAKE)
        self.env.update(LEMVO_TURN_SOFT_IDLE_SEC='0.2', LEMVO_TURN_HARD_IDLE_SEC='0.65',
                        LEMVO_TURN_POLL_SEC='0.03', LEMVO_TURN_TERM_GRACE_SEC='0.1')
        self.state()
    def result(self):
        files=list((self.p/'turns').glob('*/result.json')); self.assertTrue(files)
        return json.loads(sorted(files)[-1].read_text())
    def hb(self):
        try: return json.loads((self.p/'.lemvo-inflight.json').read_text())
        except (ValueError,OSError): return {}
    def spawn_adapter(self, **env):
        out=open(self.root/'adapter.log','w'); self.addCleanup(out.close)
        proc=subprocess.Popen(['bash',str(previous.AUTO),str(self.p)],env=dict(self.env,**env),
                              stdout=out,stderr=out,start_new_session=True)
        def cleanup():
            try: os.killpg(proc.pid,signal.SIGKILL)
            except ProcessLookupError: pass
            proc.wait(timeout=3)
        self.addCleanup(cleanup); return proc
    def test_hard_timeout_soft_observable_and_counters_preserved(self):
        (self.p/'.autoloop-stall-count').write_text('2')
        (self.p/'.autoloop-poison-count').write_text('1')
        (self.p/'research-state.yaml').write_text((self.p/'research-state.yaml').read_text()+'prep:\n  prep_turns: 2\n')
        proc=self.spawn_adapter()
        self.assertTrue(self.until(lambda:self.hb().get('state')=='idle-warning'))
        # Two separately atomic files are not one transaction; await both views.
        self.assertTrue(self.until(lambda:'RUNNING|idle-warning' in (self.p/'.lemvo-status').read_text()))
        self.assertEqual(proc.wait(timeout=8),124)
        self.assertEqual(self.result()['next_action'],'runtime_error')
        self.assertEqual(self.result()['runtime_reason'],'turn-timeout')
        self.assertEqual((self.p/'.autoloop-session-id').read_text().strip(),previous.SID)
        self.assertIn('prep_turns: 2',(self.p/'research-state.yaml').read_text())
        self.assertEqual((self.p/'.autoloop-stall-count').read_text(),'2')
        self.assertEqual((self.p/'.autoloop-poison-count').read_text(),'1')
        ledger=[json.loads(x) for x in (self.p/'.autoloop-prep-ledger.jsonl').read_text().splitlines() if x.startswith('{')]
        row=[x for x in ledger if x.get('type')=='runtime-interrupted'][0]
        self.assertEqual(row['total_turns'],1); self.assertEqual(row['tokens_in'],'unknown')
    def test_steer_retained_then_delivered_on_resume(self):
        msg=self.root/'instruction.md'; msg.write_text('urgent repair instruction')
        first=self.call('advance',msg)
        self.assertEqual(first.returncode,124,first.stdout)
        self.assertEqual(len(list((self.p/'.lemvo-steer-queue').glob('q-*.md'))),1)
        second=self.call('advance',FAKE_KIND='ok')
        self.assertEqual(second.returncode,0,second.stdout)
        self.assertIn('--resume',self.calls()[1]['args'])
        self.assertIn('urgent repair instruction',self.calls()[1]['message'])
        self.assertEqual(len(list((self.p/'.lemvo-steer-queue').glob('q-*.md'))),0)
    def test_streaming_activity_outlives_idle_limit_without_wall_cap(self):
        result=self.adapter(FAKE_KIND='active')
        self.assertEqual(result.returncode,0,result.stdout)
        args=self.calls()[0]['args']
        self.assertIn('stream-json',args); self.assertIn('--verbose',args); self.assertIn('--include-partial-messages',args)
        rows=[json.loads(x) for x in (self.p/'.autoloop-prep-ledger.jsonl').read_text().splitlines() if x.startswith('{')]
        self.assertEqual(rows[-1]['tokens_in'],10); self.assertEqual(rows[-1]['tokens_out'],2)
    def test_optional_wall_cap_is_not_idle_cap(self):
        result=self.adapter(FAKE_KIND='active',LEMVO_TURN_MAX_SEC='.35')
        self.assertEqual(result.returncode,124,result.stdout)
        marker=list((self.p/'turns').glob('*/timeout.json'))[0]
        self.assertEqual(json.loads(marker.read_text())['reason'],'wall-timeout')
    def test_renew_is_turn_scoped_and_no_fake_heartbeat(self):
        proc=self.spawn_adapter(FAKE_KIND='lease')
        self.assertTrue(self.until(lambda:self.hb().get('state')=='idle-warning'))
        original=self.hb()['last_activity_epoch']
        renewal=self.call('renew','2'); self.assertEqual(renewal.returncode,0,renewal.stdout)
        self.assertTrue(self.until(lambda:self.hb().get('state')=='lease-active'))
        self.assertEqual(self.hb()['last_activity_epoch'],original)
        self.assertEqual(proc.wait(timeout=8),0)
        self.assertEqual(self.adapter().returncode,124)  # prior lease does not leak
    def test_ignoring_sigterm_gets_bounded_kill(self):
        result=self.adapter(FAKE_KIND='ignore-term')
        self.assertEqual(result.returncode,124,result.stdout)
    def test_manual_cli_interruption_is_not_research_failure(self):
        result=self.adapter(FAKE_KIND='interrupt')
        self.assertEqual(result.returncode,143,result.stdout)
        self.assertIn('prep_turns: 0',(self.p/'research-state.yaml').read_text())
        self.assertFalse((self.p/'.autoloop-stall-count').exists())
    def test_independent_registered_training_survives(self):
        job=subprocess.Popen(['sleep','30']); self.addCleanup(job.wait); self.addCleanup(job.terminate)
        (self.p/'.lemvo-jobs.json').write_text(json.dumps([{'run_id':'training','pid':job.pid,'pid_start':support.process_identity(job.pid)}]))
        self.assertEqual(self.adapter().returncode,124)
        self.assertIsNone(job.poll())
    def test_attached_job_is_protected_and_visible(self):
        # The fake CLI itself is the registered producer: conservative ancestry match.
        proc=self.spawn_adapter()
        self.assertTrue(self.until(lambda:bool(self.hb().get('engine_pid'))))
        hb=self.hb()
        (self.p/'.lemvo-jobs.json').write_text(json.dumps([{'run_id':'attached','pid':hb['engine_pid'],'pid_start':hb['engine_start']}]))
        self.assertTrue(self.until(lambda:self.hb().get('state')=='timeout-protected-job'))
        self.assertIsNone(proc.poll())
        self.assertEqual(self.hb()['protected_jobs'],['attached'])
        (self.p/'.lemvo-jobs.json').write_text('[]')
        self.assertEqual(proc.wait(timeout=8),124)
    def test_invalid_config_stops_before_model_call(self):
        for value in ('nan','-1','bad'):
            result=self.adapter(LEMVO_TURN_HARD_IDLE_SEC=value)
            self.assertEqual(result.returncode,64,result.stdout)
        self.assertFalse(self.calls())
    def test_no_unrequested_default_absolute_deadline(self):
        with patch.dict(os.environ,{},clear=True): self.assertEqual(support.watchdog_config()['MAX_SEC'],0)
    def test_pid_identity_mismatch_never_signals(self):
        old=os.getcwd(); os.chdir(str(self.p))
        try:
            Path('turns/test').mkdir(parents=True); Path('.autoloop-turn-active').write_text('test|1|1')
            with patch.object(support.os,'kill') as kill:
                self.assertEqual(support.watch_turn('turns/test','test',str(os.getpid()),'wrong-start'),0)
                kill.assert_not_called()
        finally: os.chdir(old)
    def test_timeout_orphan_never_acknowledges_success(self):
        self.assertEqual(self.adapter().returncode,124)
        turn=list((self.p/'turns').iterdir())[0]
        (turn/'result.json').unlink()
        (turn/'engine-stdout.jsonl').write_text('{"type":"result","is_error":false}')
        result=subprocess.run(['bash',str(previous.AUTO),'--recover',str(self.p),str(turn)],
                              env=self.env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,universal_newlines=True,timeout=15)
        self.assertEqual(result.returncode,124,result.stdout)
        old=os.getcwd(); os.chdir(str(self.p))
        try: support.recovery_plan()
        finally: os.chdir(old)
        self.assertEqual(json.loads((turn/'recovery.json').read_text())['state'],'needs-review')
    def test_codex_adapter_also_supervised(self):
        bindir=self.root/'bin'; bindir.mkdir(); exe=bindir/'codex'; exe.write_text(FAKE); exe.chmod(0o700)
        result=self.adapter(ENGINE='codex',PATH=str(bindir)+os.pathsep+os.environ['PATH'])
        self.assertEqual(result.returncode,124,result.stdout)
        self.assertIn('--json',self.calls()[0]['args'])
    def test_steer_arriving_during_hang_does_not_renew_idle_timer(self):
        proc=self.spawn_adapter()
        self.assertTrue(self.until(lambda:self.hb().get('state')=='idle-warning'))
        msg=self.root/'late.md'; msg.write_text('late urgent instruction')
        self.assertEqual(self.call('steer',msg).returncode,0)
        self.assertEqual(proc.wait(timeout=8),124)
        self.assertEqual(len(list((self.p/'.lemvo-steer-queue').glob('q-*.md'))),1)
        self.assertEqual(self.hb()['pending_steer'],1)
    def test_lost_adapter_marker_does_not_disable_watchdog(self):
        proc=self.spawn_adapter()
        self.assertTrue(self.until(lambda:self.hb().get('state')=='idle-warning'))
        (self.p/'.autoloop-turn-active').unlink()
        self.assertEqual(proc.wait(timeout=8),124)
        self.assertEqual(self.result()['next_action'],'runtime_error')
        self.assertEqual(self.result()['runtime_reason'],'turn-timeout')
    def test_unattended_controller_retries_without_human_continue(self):
        bindir=self.root/'fast-clock'; bindir.mkdir()
        # Only accelerate the existing 60-second runtime retry, not model/idle time.
        sleeper=bindir/'sleep'
        sleeper.write_text('#!/bin/sh\nif [ "$1" = 60 ]; then exec /bin/sleep 0.03; else exec /bin/sleep "$@"; fi\n')
        sleeper.chmod(0o700)
        msg=self.root/'initial.md'; msg.write_text('retain me across interrupted attempt')
        self.assertEqual(self.call('steer',msg).returncode,0)
        proc=self.start('--engine','claude','--mode','unattended',FAKE_KIND='hang-once',
                        PATH=str(bindir)+os.pathsep+os.environ['PATH'])
        self.assertTrue(self.until(lambda:proc.poll() is not None,timeout=12))
        self.assertEqual(proc.returncode,0)
        calls=self.calls(); self.assertEqual(len(calls),2)
        self.assertIn('--resume',calls[1]['args'])
        self.assertIn('retain me across interrupted attempt',calls[1]['message'])
        self.assertEqual(len(list((self.p/'.lemvo-steer-queue').glob('q-*.md'))),0)

if __name__=='__main__':
    # Do not silently rerun/count inherited old tests as new watchdog assertions.
    suite=unittest.TestSuite(Inflight(n) for n in Inflight.__dict__ if n.startswith('test_'))
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(not result.wasSuccessful())
