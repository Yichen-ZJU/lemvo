"""Controller/adapter integration: temporary projects, fake CLI, CPU only."""
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time
import unittest
import runtime_liveness_test as base

EXTRA = r'''
path=os.environ.get('FAKE_ARTIFACT')
if path:
    p=Path(path);p.parent.mkdir(parents=True,exist_ok=True)
    p.write_text('real construction '+('same' if os.environ.get('FAKE_SAME') else str(time.time_ns())))
    import re,hashlib
    state=Path('research-state.yaml').read_text()
    state=re.sub(r'(?m)^  last_informative_artifact:.*$', '  last_informative_artifact: '+str(p),state)
    if os.environ.get('FAKE_SHA')=='yes':
        state=re.sub(r'(?m)^  last_artifact_sha:.*$', '  last_artifact_sha: '+hashlib.sha256(p.read_bytes()).hexdigest(),state)
    Path('research-state.yaml').write_text(state)
if os.environ.get('FAKE_STATUS'):
    import re
    s=Path('research-state.yaml').read_text()
    s=re.sub(r'(?m)^campaign_status:.*$', 'campaign_status: '+os.environ['FAKE_STATUS'],s)
    Path('research-state.yaml').write_text(s)
if os.environ.get('FAKE_JOB_PID'):
    pid=int(os.environ['FAKE_JOB_PID'])
    st=Path('/proc/%d/stat'%pid).read_text().rsplit(')',1)[1].split()[19]
    Path('.lemvo-jobs.json').write_text(json.dumps([{'run_id':'job-1','pid':pid,'pid_start':st}]))
'''


class Contracts(base.Liveness):
    def setUp(self):
        super().setUp()
        fake=base.FAKE.replace("event={'type':'result'", EXTRA+"\nevent={'type':'result'")
        fake=fake.replace("if kind!='no-usage':", "if os.environ.get('FAKE_COST'):event['total_cost_usd']=float(os.environ['FAKE_COST'])\nif kind!='no-usage':")
        fake+="\nif os.environ.get('FAKE_EXIT'):sys.exit(int(os.environ['FAKE_EXIT']))\n"
        self.fake.write_text(fake)
        self.env.update(LEMVO_TURN_POLL_SEC='.02')
        self.contract=base.TEMPLATES/'runtime-contract.py'

    def prep(self):
        s=(self.p/'research-state.yaml').read_text()
        return int(re.search(r'(?m)^  prep_turns: (\d+)',s).group(1))

    def contract_call(self, *args):
        return subprocess.run(['python3',str(self.contract),*map(str,args)],cwd=str(self.p),
                              capture_output=True,text=True,check=True)

    def test_waiting_user_adapter_no_calls(self):
        self.state('WAITING_USER')
        self.assertEqual(self.adapter().returncode,6)
        self.assertFalse(self.calls())

    def test_waiting_user_one_reply_then_silent(self):
        self.state('WAITING_USER')
        proc=self.start('--engine','claude')
        self.assertTrue(self.until(lambda:(self.p/'.lemvo-wait-state.json').exists()))
        time.sleep(.25);self.assertFalse(self.calls())
        msg=self.root/'reply.md';msg.write_text('Please consider this reply; it supplies no new resource permission.')
        self.assertEqual(self.call('steer',msg).returncode,0)
        self.assertTrue(self.until(lambda:len(self.calls())==1))
        self.assertTrue(self.until(lambda:len(list((self.p/'.lemvo-steer-archive').glob('*.md')))==1))
        time.sleep(.4)
        self.assertEqual(len(self.calls()),1)
        self.assertEqual(self.prep(),0)
        self.assertIsNone(proc.poll())
        self.assertEqual(len(list((self.p/'turns').iterdir())),1)
        r=json.loads(next((self.p/'turns').glob('*/result.json')).read_text())
        self.assertEqual(r['next_action'],'needs_user')

    def test_hold_advance_reaches_reply_without_erasing_wait(self):
        self.state('HOLD')
        result=self.call('advance')
        self.assertEqual(result.returncode,0,result.stdout)
        self.assertEqual(len(self.calls()),1)
        self.assertEqual(self.prep(),0)
        self.assertIn('campaign_status: HOLD',(self.p/'research-state.yaml').read_text())

    def test_hard_pause_cannot_be_cleared_by_steer(self):
        for status in ('PAUSED','STOPPED'):
            self.state(status)
            msg=self.root/'reply.md';msg.write_text('continue')
            r=self.call('advance',msg)
            self.assertEqual(r.returncode,6,r.stdout)
            self.assertFalse(self.calls())

    def test_explicit_stop_marker_overrides_human_reply(self):
        self.state('WAITING_USER'); (self.p/'STOP').touch()
        msg=self.root/'reply.md';msg.write_text('continue')
        r=self.call('advance',msg)
        self.assertEqual(r.returncode,6)
        self.assertFalse(self.calls())

    def test_reply_to_other_question_does_not_wake(self):
        self.state('WAITING_USER')
        proc=self.start('--engine','claude')
        self.assertTrue(self.until(lambda:(self.p/'.lemvo-wait-state.json').exists()))
        msg=self.root/'reply.md';msg.write_text('answer for another work item')
        self.assertEqual(self.call('steer',msg,'--reply-to','another-question').returncode,0)
        time.sleep(.5);self.assertFalse(self.calls())
        self.assertIsNone(proc.poll())

    def test_reply_matching_question_wakes_once(self):
        self.state('WAITING_USER')
        st=json.loads(self.contract_call('state').stdout)
        msg=self.root/'reply.md';msg.write_text('answer')
        self.assertEqual(self.call('steer',msg,'--reply-to',st['question_id']).returncode,0)
        proc=self.start('--engine','claude')
        self.assertTrue(self.until(lambda:len(self.calls())==1))
        self.assertTrue(self.until(lambda:len(list((self.p/'.lemvo-steer-archive').glob('*.md')))==1))
        time.sleep(.3);self.assertEqual(len(self.calls()),1)

    def test_wait_resolved_by_reply_resumes_without_resetting_total(self):
        self.state('HOLD')
        first=self.call('advance',FAKE_STATUS='ACTIVE',FAKE_ARTIFACT='src/first.py')
        self.assertEqual(first.returncode,0,first.stdout)
        second=self.adapter(FAKE_ARTIFACT='src/second.py')
        self.assertEqual(second.returncode,0,second.stdout)
        self.assertIn('total_turns: 2',(self.p/'research-state.yaml').read_text())

    def test_engineering_changes_share_prep_and_stall(self):
        self.state()
        for path in ('notes/plan.md','scripts/launch.sh','config/train.yaml',
                     'src/model.py','notes/plan.md','src/model.py'):
            r=self.adapter(FAKE_ARTIFACT=path,PREP_CAP_TURNS='2',LEMVO_STALL_ARTIFACT_N='2')
            self.assertEqual(r.returncode,0,r.stdout)
            self.assertEqual(self.prep(),0)
            self.assertEqual((self.p/'.autoloop-stall-count').read_text().strip(),'0')

    def test_model_written_sha_cannot_cancel_real_progress(self):
        self.state()
        for _ in range(3):
            r=self.adapter(FAKE_ARTIFACT='results/metrics.json',FAKE_SHA='yes',PREP_CAP_TURNS='2')
            self.assertEqual(r.returncode,0,r.stdout)
            self.assertEqual(self.prep(),0)

    def test_same_content_not_progress(self):
        self.state()
        rs=[self.adapter(FAKE_ARTIFACT='src/code.py',FAKE_SAME='yes',LEMVO_STALL_ARTIFACT_N='2') for _ in range(3)]
        self.assertEqual([r.returncode for r in rs],[0,0,2])

    def test_registered_external_file_counts(self):
        self.state()
        outside=self.root/'external'/'fork.py'
        self.assertEqual(self.call('track',outside).returncode,0)
        for _ in range(3):
            r=self.adapter(FAKE_ARTIFACT=str(outside),FAKE_SHA='yes',PREP_CAP_TURNS='2')
            self.assertEqual(r.returncode,0,r.stdout)
            self.assertEqual(self.prep(),0)

    def test_registered_deep_excluded_file_counts(self):
        self.state(); (self.p/'.lemvo-snapshot-exclude').write_text('vendor/\n')
        path=self.p/'vendor/a/b/c/d/model.py'
        self.assertEqual(self.call('track',path).returncode,0)
        for _ in range(3):
            r=self.adapter(FAKE_ARTIFACT=str(path),PREP_CAP_TURNS='2')
            self.assertEqual(r.returncode,0,r.stdout)
            self.assertEqual(self.prep(),0)

    def test_live_job_registration_counts_once(self):
        self.state()
        child=subprocess.Popen(['sleep','30'])
        self.addCleanup(lambda:child.poll() is None and child.terminate())
        self.addCleanup(lambda:None)
        first=self.adapter(FAKE_JOB_PID=str(child.pid))
        self.assertEqual(first.returncode,0,first.stdout);self.assertEqual(self.prep(),0)
        second=self.adapter(FAKE_JOB_PID=str(child.pid))
        self.assertEqual(second.returncode,0,second.stdout);self.assertEqual(self.prep(),1)
        child.terminate();child.wait()

    def test_wait_does_not_reset_existing_stall(self):
        self.state();self.assertEqual(self.adapter().returncode,0)
        count=(self.p/'.autoloop-stall-count').read_text()
        s=(self.p/'research-state.yaml').read_text().replace('campaign_status: ACTIVE','campaign_status: WAITING_USER')
        (self.p/'research-state.yaml').write_text(s)
        msg=self.root/'reply.md';msg.write_text('more context only')
        self.assertEqual(self.call('advance',msg).returncode,0)
        self.assertEqual((self.p/'.autoloop-stall-count').read_text(),count)
        self.assertEqual(self.prep(),1)

    def queue(self, text, ident, *options):
        msg=self.root/(ident+'.md');msg.write_text(text)
        r=self.call('steer',msg,'--id',ident,*options)
        self.assertEqual(r.returncode,0,r.stdout)

    def test_scoped_supersession_active_and_archive(self):
        self.state()
        self.queue('OLD_ASSESS_ONLY','A')
        self.queue('NEW_IMPLEMENT','B','--supersedes','A')
        r=self.call('advance',FAKE_ARTIFACT='src/code.py')
        self.assertEqual(r.returncode,0,r.stdout)
        self.assertNotIn('OLD_ASSESS_ONLY',self.calls()[0]['message'])
        self.assertIn('NEW_IMPLEMENT',self.calls()[0]['message'])
        frozen=json.loads(next((self.p/'turns').glob('*/instructions.json')).read_text())
        self.assertEqual(list(frozen['active']),['B'])
        self.assertEqual(len(list((self.p/'.lemvo-steer-archive').glob('*.md'))),2)
        events=[json.loads(x) for x in (self.p/'.lemvo-delivery.jsonl').read_text().splitlines()]
        self.assertEqual(sum(x['event']=='superseded' for x in events),1)
        self.assertEqual(sum(x['event']=='delivered' for x in events),1)

    def test_delivered_instruction_replaced_after_restart(self):
        self.state();self.queue('OLD_TASK','A')
        self.assertEqual(self.call('advance',FAKE_ARTIFACT='src/one.py').returncode,0)
        self.queue('CURRENT_TASK','B','--supersedes','A')
        self.assertEqual(self.call('advance',FAKE_ARTIFACT='src/two.py').returncode,0)
        self.assertNotIn('OLD_TASK',self.calls()[1]['message'])
        self.assertIn('CURRENT_TASK',self.calls()[1]['message'])
        self.assertEqual(self.call('advance',FAKE_ARTIFACT='src/three.py').returncode,0)
        self.assertIn('CURRENT_TASK',self.calls()[2]['message'])

    def test_instruction_cross_scope_and_duplicate_rejected(self):
        self.state();self.queue('original','A','--scope','work-one')
        msg=self.root/'new.md';msg.write_text('new')
        self.assertNotEqual(self.call('steer',msg,'--id','B','--scope','work-two','--supersedes','A').returncode,0)
        self.assertNotEqual(self.call('steer',msg,'--id','A','--scope','work-one').returncode,0)
        self.assertEqual(len(list((self.p/'.lemvo-steer-queue').glob('*.md'))),1)

    def test_global_constraint_not_superseded(self):
        self.state();self.queue('no downloads','G','--scope','constraints')
        msg=self.root/'new.md';msg.write_text('download anyway')
        self.assertNotEqual(self.call('steer',msg,'--id','B','--scope','constraints','--supersedes','G').returncode,0)

    def test_failure_preserves_versioned_messages(self):
        self.state();self.queue('old','A');self.queue('new','B','--supersedes','A')
        r=self.call('advance',FAKE_KIND='garbage')
        self.assertEqual(r.returncode,9,r.stdout)
        self.assertEqual(len(list((self.p/'.lemvo-steer-queue').glob('*.md'))),2)
        self.assertEqual(self.call('advance',FAKE_ARTIFACT='src/code.py').returncode,0)
        self.assertEqual(len(list((self.p/'.lemvo-steer-queue').glob('*.md'))),0)

    def test_three_line_receipt_and_post_turn_wait(self):
        self.state()
        r=self.call('advance',FAKE_STATUS='WAITING_USER')
        self.assertEqual(r.returncode,0,r.stdout)
        td=next((self.p/'turns').iterdir())
        self.assertEqual(len((td/'runtime-summary.md').read_text().splitlines()),3)
        result=json.loads((td/'result.json').read_text())
        self.assertEqual(result['next_action'],'needs_user')
        self.assertEqual(result['scientific_verdict']['campaign_status'],'WAITING_USER')
        self.assertEqual(self.prep(),0)

    def test_midturn_replacement_freezes_current_revision(self):
        self.state();self.queue('FIRST_ACTIVE','A')
        proc=self.start('--engine','claude','--mode','guided',FAKE_DELAY='.8',FAKE_KIND='construct')
        self.assertTrue(self.until(lambda:len(self.calls())==1))
        first_td=next((self.p/'turns').iterdir())
        self.queue('SECOND_ACTIVE','B','--supersedes','A')
        frozen=json.loads((first_td/'instructions.json').read_text())
        self.assertEqual(list(frozen['active']),['A'])
        self.assertTrue(self.until(lambda:len(self.calls())==2))
        self.assertNotIn('FIRST_ACTIVE',self.calls()[1]['message'])
        self.assertIn('SECOND_ACTIVE',self.calls()[1]['message'])
        self.assertTrue(self.until(lambda:len(list((self.p/'.lemvo-steer-archive').glob('*.md')))==2))
        time.sleep(.2);self.assertEqual(len(self.calls()),2)
        self.assertIsNone(proc.poll())

    def test_wait_survives_controller_restart_without_calls(self):
        self.state('WAITING_USER')
        proc=self.start('--engine','claude')
        self.assertTrue(self.until(lambda:(self.p/'.lemvo-wait-state.json').exists()))
        first=json.loads((self.p/'.lemvo-wait-state.json').read_text())['question_id']
        proc.terminate();proc.wait(timeout=3)
        second=self.start('--engine','claude')
        time.sleep(.5)
        self.assertFalse(self.calls())
        self.assertEqual(json.loads((self.p/'.lemvo-wait-state.json').read_text())['question_id'],first)
        self.assertIsNone(second.poll())

    def test_corrupt_instruction_registry_is_not_reset(self):
        self.state()
        p=self.p/'.lemvo-instructions.json';p.write_text('{"entries":')
        msg=self.root/'message.md';msg.write_text('new')
        self.assertNotEqual(self.call('steer',msg,'--id','B').returncode,0)
        self.assertEqual(p.read_text(),'{"entries":')
        self.assertFalse(list((self.p/'.lemvo-steer-queue').glob('*.md')))

    def test_same_clock_publications_preserve_causal_order(self):
        self.state()
        source=self.root/'rapid.md';source.write_text('rapid')
        code="""import importlib.util,os
spec=importlib.util.spec_from_file_location('contract',%r)
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
m.time.time=lambda:1000000000.0
m.time.strftime=lambda *a:'constant'
for n in range(8):
    args=[%r,'user','--id','v'+str(n)]
    if n: args+=['--supersedes','v'+str(n-1)]
    m.enqueue(args)
m.snapshot()
"""%(str(self.contract),str(source))
        r=subprocess.run(['python3','-c',code],cwd=str(self.p),capture_output=True,text=True)
        self.assertEqual(r.returncode,0,r.stderr)
        registry=json.loads((self.p/'.lemvo-instructions.json').read_text())
        self.assertEqual([k for k,v in registry['entries'].items() if v['status']=='active'],['v7'])

    def test_orphan_recovery_keeps_engineering_progress_and_wait(self):
        self.state()
        td=self.p/'turns'/'orphan';td.mkdir(parents=True)
        # First init creates the helper/epoch without calling any CLI.
        init=subprocess.run(['bash',str(base.AUTO),'--init',str(self.p)],
                            env=self.env,capture_output=True,text=True)
        self.assertEqual(init.returncode,0,init.stdout+init.stderr)
        epoch=(self.p/'.lemvo-session-epoch').read_text().strip() if (self.p/'.lemvo-session-epoch').exists() else 'legacy'
        (td/'manifest.json').write_text(json.dumps({'turn_id':'orphan','session_epoch':epoch,
                                                   'started_utc':'2026-10-20T00:00:00Z'}))
        subprocess.run(['python3',str(base.TEMPLATES/'runtime-support.py'),'progress-snapshot',
                        str(td/'progress-before.json')],cwd=str(self.p),check=True,capture_output=True)
        self.contract_call('progress-begin',td)
        (self.p/'src').mkdir();(self.p/'src/code.py').write_text('print(42)')
        self.state('WAITING_USER')
        (td/'engine-stdout.jsonl').write_text(json.dumps({'type':'result','subtype':'success','is_error':False,
                                                       'session_id':'11111111-1111-1111-1111-111111111111',
                                                       'usage':{'input_tokens':1,'output_tokens':1}})+'\n')
        r=subprocess.run(['bash',str(base.AUTO),'--recover',str(self.p),str(td)],
                         env=dict(self.env,ENGINE='claude'),capture_output=True,text=True)
        self.assertEqual(r.returncode,0,r.stdout+r.stderr)
        self.assertEqual(self.prep(),0)
        self.assertEqual(json.loads((td/'result.json').read_text())['next_action'],'needs_user')
        self.assertEqual(json.loads((td/'progress.json').read_text())['kind'],'construction')

    def test_cli_nonzero_exit_preserves_counters_guidance_and_receipt(self):
        self.state()
        self.assertEqual(self.adapter().returncode,0)
        (self.p/'.autoloop-poison-count').write_text('1')
        before=(self.p/'.lemvo-progress-accepted.json').read_text()
        self.queue('implement pending work','crash-task')
        r=self.call('advance',FAKE_EXIT='1',FAKE_ARTIFACT='src/partial.py')
        self.assertEqual(r.returncode,1,r.stdout)
        self.assertEqual(self.prep(),1)
        self.assertEqual((self.p/'.autoloop-stall-count').read_text().strip(),'1')
        self.assertEqual((self.p/'.autoloop-poison-count').read_text().strip(),'1')
        self.assertEqual((self.p/'.lemvo-progress-accepted.json').read_text(),before)
        self.assertTrue((self.p/'src/partial.py').is_file())
        self.assertEqual(len(list((self.p/'.lemvo-steer-queue').glob('*.md'))),1)
        failed=[p.parent for p in (self.p/'turns').glob('*/result.json')
                if json.loads(p.read_text())['status']=='failed'][0]
        self.assertEqual(len((failed/'runtime-summary.md').read_text().splitlines()),3)
        result=json.loads((failed/'result.json').read_text())
        self.assertEqual(result['runtime_reason'],'engine-error')
        self.assertEqual(result['next_action'],'runtime_error')
        rows=[json.loads(x) for x in (self.p/'.autoloop-prep-ledger.jsonl').read_text().splitlines()]
        self.assertEqual(sum(x.get('type')=='runtime-error' for x in rows),1)

    def test_wait_assessment_cost_baseline_is_continuous(self):
        self.state('WAITING_USER')
        for cost in ('1','3','4'):
            self.assertEqual(self.call('advance',FAKE_COST=cost).returncode,0)
        rows=[json.loads(x) for x in (self.p/'.autoloop-prep-ledger.jsonl').read_text().splitlines()]
        deltas=[x['cost_usd_delta'] for x in rows if x.get('type')=='wait-assessment']
        self.assertEqual(deltas,[1,2,1])
        self.assertEqual(self.prep(),0)

    def test_runtime_error_cost_baseline_is_continuous(self):
        self.state()
        self.assertEqual(self.adapter(FAKE_COST='1',FAKE_ARTIFACT='src/a.py').returncode,0)
        self.assertEqual(self.adapter(FAKE_COST='3',FAKE_EXIT='1').returncode,1)
        self.assertEqual(self.adapter(FAKE_COST='4',FAKE_ARTIFACT='src/b.py').returncode,0)
        rows=[json.loads(x) for x in (self.p/'.autoloop-prep-ledger.jsonl').read_text().splitlines()]
        deltas=[x['cost_usd_delta'] for x in rows if x.get('type') in ('work-turn','runtime-error')]
        self.assertEqual(deltas,[1,2,1])
        self.assertEqual(self.prep(),0)

    def test_missing_budget_fields_stay_in_currency_section(self):
        self.state('WAITING_USER')
        state=self.p/'research-state.yaml'
        state.write_text(state.read_text().replace('total_cap: null','total_cap: 20')+
                         'gpu_usage:\n  used_hours: 7\n  basis: measured\n')
        for amount in ('1','3','4'):
            self.assertEqual(self.call('advance',FAKE_COST=amount).returncode,0)
        text=state.read_text()
        budget=re.search(r'(?ms)^budget:\n(.*?)(?=^\S|\Z)',text).group(1)
        self.assertRegex(budget,r'(?m)^  used: 4\.0$')
        self.assertRegex(budget,r'(?m)^  remaining: 16\.0$')
        gpu=re.search(r'(?ms)^gpu_usage:\n(.*?)(?=^\S|\Z)',text).group(1)
        self.assertIn('  used_hours: 7\n',gpu)
        self.assertNotRegex(gpu,r'(?m)^  (used|remaining):')
        self.assertEqual(len(re.findall(r'(?m)^  used:',text)),1)


if __name__=='__main__':
    unittest.main(verbosity=2)
