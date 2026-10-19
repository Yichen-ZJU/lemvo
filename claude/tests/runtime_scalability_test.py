"""Deterministic CPU regressions: no real models, jobs or authorization changes."""
import contextlib, importlib.util, io, json, os, subprocess, tempfile, time, unittest
from pathlib import Path
from unittest.mock import patch
import runtime_liveness_test as previous

ROOT=Path(os.environ.get('LEMVO_TEMPLATE_ROOT',str(Path(__file__).resolve().parents[1]/'skills/research-orchestrator/templates')))
spec=importlib.util.spec_from_file_location('support',str(ROOT/'runtime-support.py'))
support=importlib.util.module_from_spec(spec); spec.loader.exec_module(support)

class Scalability(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='lemvo-scale-'); self.addCleanup(self.tmp.cleanup)
        old=os.getcwd(); os.chdir(self.tmp.name); self.addCleanup(os.chdir,old)
    def invoke(self,func,*args):
        stream=io.StringIO()
        with contextlib.redirect_stdout(stream): func(*args)
        return stream.getvalue().strip()
    def state(self): Path('research-state.yaml').write_text('campaign_status: WAITING_RESOURCE\n')
    def turn(self,name,**extra):
        p=Path('turns')/name; p.mkdir(parents=True)
        (p/'manifest.json').write_text(json.dumps(dict(status='dispatched',session_epoch='legacy',**extra)))
        return p
    def test_periodic_wait_already_exists_and_no_turns(self):
        self.state()
        with patch.object(support.time,'time',return_value=100): self.assertEqual(self.invoke(support.wait_check,'WAITING_RESOURCE'),'wait')
        with patch.object(support.time,'time',return_value=1001): self.assertEqual(self.invoke(support.wait_check,'WAITING_RESOURCE'),'resource-recheck')
        self.assertFalse(Path('turns').exists())
    def test_wait_backoff_and_source_death(self):
        self.state(); polls=[]
        for _ in range(4):
            self.invoke(support.wait_check,'WAITING_RESOURCE'); polls.append(support.load('.lemvo-resource-wait.json')['poll_sec'])
        self.assertEqual(polls[:3],[15,60,240]); self.assertGreater(polls[3],800)
        Path('.lemvo-wait.json').write_text(json.dumps({'conditions':[{'kind':'process','pid':99999999,'pid_start':'1'}]}))
        self.assertEqual(self.invoke(support.wait_check,'WAITING_RESOURCE'),'wait-source-ended')
        self.assertIsNone(support.load('.lemvo-wait-alert.json')['scientific_verdict'])
    def test_deadline_and_malformed_contract_are_observable(self):
        self.state(); Path('.lemvo-wait.json').write_text('{"deadline_epoch":1}')
        self.assertEqual(self.invoke(support.wait_check,'WAITING_RESOURCE'),'wait-deadline')
        Path('.lemvo-wait.json').write_text('{')
        self.assertEqual(self.invoke(support.wait_check,'WAITING_RESOURCE'),'wait-contract-invalid')
    def test_rc7_wait_is_sticky_without_yaml_flag(self):
        Path('research-state.yaml').write_text('campaign_status: ACTIVE\n')
        support.register_wait()
        self.assertEqual(self.invoke(support.wait_check,'WAITING_RESOURCE'),'wait')
    def test_snapshot_entry_budget(self):
        for n in range(100): Path('f%04d.py'%n).write_text('x')
        with patch.dict(os.environ,{'LEMVO_SNAPSHOT_MAX_ENTRIES':'20'}): support.progress_snapshot('.before.json')
        result=support.load('.before.json')['inspection']
        self.assertLessEqual(result['entries'],20); self.assertFalse(result['complete'])
    def test_snapshot_byte_budget_cache_and_exclusion(self):
        Path('assets').mkdir(); (Path('assets')/'x').write_bytes(b'xx'*100000)
        Path('method.py').write_text('return 1'); Path('.lemvo-snapshot-exclude').write_text('assets/\n')
        with patch.dict(os.environ,{'LEMVO_SNAPSHOT_MAX_BYTES':'100'}):
            support.progress_snapshot('.before.json'); support.progress_snapshot('.after.json')
        before=support.load('.before.json'); after=support.load('.after.json')
        self.assertTrue(before['inspection']['complete']); self.assertEqual(after['inspection']['bytes_read'],0)
        self.assertGreater(after['inspection']['cache_hits'],0)
        self.assertNotIn('assets/x',before['files'])
    def test_snapshot_changed_content_and_unknown_coverage(self):
        Path('method.py').write_text('x'); support.progress_snapshot('.before.json')
        Path('method.py').write_text('y')
        self.assertEqual(self.invoke(support.progress_changed,'.before.json'),'method.py')
        support.atomic('.before.json',{'files':{},'inspection':{'complete':False}})
        self.assertEqual(self.invoke(support.progress_changed,'.before.json'),'')
        self.assertEqual(self.invoke(support.snapshot_incomplete,'.before.json'),'yes')
    def test_snapshot_hash_resists_same_size_and_restored_mtime(self):
        p=Path('method.py'); p.write_text('one'); old=p.stat(); support.progress_snapshot('.before.json')
        p.write_text('two'); os.utime(str(p),ns=(old.st_atime_ns,old.st_mtime_ns))
        self.assertEqual(self.invoke(support.progress_changed,'.before.json'),'method.py')
    def test_empty_turns_archived_without_consuming_steer(self):
        p=self.turn('t-empty'); (p/'input.md').write_text('private instruction')
        Path('.lemvo-steer-queue').mkdir(); Path('.lemvo-steer-queue/q-a.md').write_text('keep')
        self.assertEqual(self.invoke(support.recovery_plan),'')
        self.assertFalse(p.exists()); self.assertTrue(Path('.lemvo-steer-queue/q-a.md').exists())
        self.assertEqual(len(list(Path('.lemvo-turn-archive').glob('*/t-empty/input.md'))),1)
        self.invoke(support.recovery_plan)
        self.assertEqual(support.load('.lemvo-recovery-status.json')['inspected'],0)
        self.assertEqual(support.load('.lemvo-recovery-status.json')['discovered_names'],0)
    def test_recovery_failure_is_not_retried_until_requested(self):
        p=self.turn('t-bad'); (p/'engine.json').write_text('{}'); (p/'engine-stdout.jsonl').write_text('{"type":"result","is_error":false}')
        self.assertIn('recover|turns/t-bad',self.invoke(support.recovery_plan))
        support.recovery_mark(str(p),'needs-review','bad terminal')
        self.assertEqual(self.invoke(support.recovery_plan),'')
        support.recovery_mark(str(p),'pending','explicit retry')
        self.assertIn('recover|turns/t-bad',self.invoke(support.recovery_plan))
        support.recovery_mark(str(p),'settled','verified')
        self.assertEqual(support.load(p/'manifest.json')['status'],'settled')
    def test_invalid_terminal_is_marked_without_adapter_replay(self):
        p=self.turn('t-invalid'); (p/'engine.json').write_text('{}'); (p/'engine-stdout.jsonl').write_text('not JSON')
        self.assertEqual(self.invoke(support.recovery_plan),'')
        self.assertEqual(support.load(p/'recovery.json')['state'],'needs-review')
    def test_live_writer_not_archived_or_recovered(self):
        p=self.turn('t-live'); (p/'engine.json').write_text(json.dumps({'adapter_pid':os.getpid(),'adapter_start':support.process_identity(os.getpid())}))
        self.assertIn('wait|turns/t-live',self.invoke(support.recovery_plan)); self.assertTrue(p.exists())
    def test_old_epoch_never_adopted(self):
        p=self.turn('t-old'); Path('.lemvo-session-epoch').write_text('new')
        self.assertEqual(self.invoke(support.recovery_plan),''); self.assertTrue(p.exists())
    def test_repair_dryrun_apply_idempotent_and_totals_unchanged(self):
        archive=Path('poisoned-archive-test'); p=archive/'turns'/'bad'; p.mkdir(parents=True); (p/'manifest.json').write_text('{}')
        state='prep:\n  prep_turns: 3\ntotal:\n  total_turns: 5\n  cost_usd_total: 12\n'
        Path('research-state.yaml').write_text(state)
        Path('.autoloop-prep-ledger.jsonl').write_text(json.dumps({'type':'recovered-turn','turn_id':'bad','prep_turns':1})+'\n')
        r=json.loads(self.invoke(support.repair_prep,str(archive),'confirmed invalid recovery'))
        self.assertEqual((r['before'],r['after']),(3,2)); self.assertEqual(Path('research-state.yaml').read_text(),state)
        self.invoke(support.repair_prep,str(archive),'confirmed invalid recovery',True)
        updated=Path('research-state.yaml').read_text(); self.assertIn('prep_turns: 2',updated); self.assertIn('total_turns: 5',updated)
        r=json.loads(self.invoke(support.repair_prep,str(archive),'repeat',True)); self.assertEqual(r['before'],r['after'])
    def test_repair_preserves_real_output_even_empty_file(self):
        archive=Path('poisoned-archive-test'); p=archive/'turns'/'real'; p.mkdir(parents=True)
        (p/'manifest.json').write_text('{}'); (p/'engine-stdout.jsonl').touch()
        Path('research-state.yaml').write_text('prep:\n  prep_turns: 1\n')
        Path('.autoloop-prep-ledger.jsonl').write_text('{"type":"recovered-turn","turn_id":"real","prep_turns":1}\n')
        r=json.loads(self.invoke(support.repair_prep,str(archive),'audit')); self.assertEqual(r['invalid_turn_ids'],[])

class Integration(previous.Liveness):
    def test_wait_reassessment_is_not_research_stagnation(self):
        self.state(); state=self.p/'research-state.yaml'
        state.write_text(state.read_text()+'status: WAITING_RESOURCE\n')
        (self.p/'.autoloop-stall-count').write_text('2')
        result=self.adapter(LEMVO_RESOURCE_WAKE='resource-recheck')
        self.assertEqual(result.returncode,0,result.stdout)
        self.assertEqual((self.p/'.autoloop-stall-count').read_text(),'2')
        self.assertIn('prep_turns: 0',state.read_text())
        records=[json.loads(s) for s in (self.p/'.autoloop-prep-ledger.jsonl').read_text().splitlines()]
        self.assertTrue(any(r.get('waiting_assessment') is True for r in records))
    def test_incomplete_scan_does_not_increment_or_reset_stall(self):
        self.state(); (self.p/'.autoloop-stall-count').write_text('2')
        for n in range(30): (self.p/('asset%d.bin'%n)).write_text('data')
        result=self.adapter(LEMVO_SNAPSHOT_MAX_ENTRIES='1')
        self.assertEqual(result.returncode,0,result.stdout)
        self.assertEqual((self.p/'.autoloop-stall-count').read_text(),'2')
        self.assertIn('prep_turns: 0',(self.p/'research-state.yaml').read_text())
    def test_persistent_authorization_is_consumed_once(self):
        self.state(); state=self.p/'research-state.yaml'
        state.write_text(state.read_text()+'prep:\n  prep_turns: 2522\n')
        result=self.adapter(AUTHORIZE_JUSTIFICATION='user decision',LEMVO_STALL_ARTIFACT_N='99')
        self.assertEqual(result.returncode,0,result.stdout)
        records=[json.loads(s) for s in (self.p/'.autoloop-prep-ledger.jsonl').read_text().splitlines() if s.startswith('{')]
        auth=[r for r in records if r.get('type')=='authorize']; self.assertEqual(len(auth),1); self.assertEqual(auth[0]['new_cap'],2532)
        state.write_text(state.read_text().replace('prep_turns: 2523','prep_turns: 2532'))
        result=self.adapter(AUTHORIZE_JUSTIFICATION='user decision',LEMVO_STALL_ARTIFACT_N='99')
        self.assertEqual(result.returncode,2,result.stdout)
    def test_rc7_race_no_repeated_turn_creation(self):
        self.state(); adapter=self.root/'rc7.sh'
        adapter.write_text('#!/bin/bash\necho LEMVO_RC_REASON=WAITING_RESOURCE\nexit 7\n'); adapter.chmod(0o700)
        proc=self.start('--mode','unattended',AUTOLOOP_SCRIPT=str(adapter),LEMVO_WAIT_RECHECK_SEC='30')
        time.sleep(1.5)
        self.assertEqual(len(list((self.p/'turns').glob('*/manifest.json'))),0)
        self.assertEqual(len(list((self.p/'.lemvo-turn-archive').glob('*/*/manifest.json'))),1)
        self.assertIsNone(proc.poll())

def load_tests(loader,tests,pattern):
    suite=unittest.TestSuite(loader.loadTestsFromTestCase(Scalability))
    for name in Integration.__dict__:
        if name.startswith('test_'): suite.addTest(Integration(name))
    return suite

if __name__=='__main__': unittest.main(verbosity=2)
