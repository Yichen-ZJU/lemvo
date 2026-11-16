"""Actual adapter/controller dispatch, CPU CLI fixtures, no network/model/GPU."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
T = ROOT / 'skills/research-orchestrator/templates'
SID_A = '11111111-2222-4333-8444-555555555555'
SID_B = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
FAKE = r'''#!/usr/bin/env python3
import json,os,sys,time
from pathlib import Path
args=sys.argv[1:]
is_claude='-p' in args
message=args[args.index('-p')+1] if is_claude else args[-1]
resume=('--resume' in args) if is_claude else ('resume' in args)
counter=Path('.fake-counter'); n=int(counter.read_text())+1 if counter.exists() else 1
counter.write_text(str(n))
with open('calls.jsonl','a') as f:f.write(json.dumps({'args':args,'message':message,'resume':resume})+'\n')
sid='11111111-2222-4333-8444-555555555555' if n==1 or resume else 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
if os.environ.get('FAKE_FAIL'):
 print(json.dumps({'type':'result','session_id':sid,'is_error':True,'result':'service overloaded 503','usage':{'input_tokens':10,'output_tokens':2}}))
 sys.exit(1)
Path('src').mkdir(exist_ok=True);Path('src/method.py').write_text('value = %d\n'%n)
if os.environ.get('FAKE_CHANGE_STAGE'):
 p=Path('research-state.yaml');p.write_text(p.read_text().replace('stage: experiment','stage: analysis'))
if n>=int(os.environ.get('FAKE_DONE_AFTER','999')):Path('CAMPAIGN-DONE').touch()
if is_claude:
 print(json.dumps({'type':'result','session_id':sid,'is_error':False,'result':'Implemented method change; real artifact src/method.py.','total_cost_usd':float(n),'usage':{'input_tokens':100,'cache_read_input_tokens':80,'output_tokens':20}}))
else:
 print(json.dumps({'type':'thread.started','thread_id':sid}))
 print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'Implemented method change; artifact src/method.py.'}}))
 print(json.dumps({'type':'turn.completed','usage':{'input_tokens':n*100,'cached_input_tokens':n*80,'output_tokens':n*20}}))
'''


class Dispatch(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='lemvo-dispatch-')
        self.addCleanup(self.temp.cleanup)
        self.p = Path(self.temp.name)
        self.bin = self.p / 'bin'; self.bin.mkdir()
        for name in ('claude', 'codex'):
            f = self.bin / name; f.write_text(FAKE); f.chmod(0o700)
        (self.p/'research-state.yaml').write_text(
            'campaign_status: ACTIVE\nproject:\n  status: ACTIVE\n'
            '  objective: Improve the CPU estimator\n'
            'research_plan:\n  stage: experiment\n  version: 1\n'
            'prep:\n  next_min_probe: Implement and compare the weighted estimator\n'
            'budget:\n  unit: usd\n  used: 0\n  total_cap: null\n  cap_authorized: no\n')
        (self.p/'CHARTER.md').write_text('Use the authorized CPU task only. Preserve all existing results.')
        self.env = dict(os.environ, PATH=str(self.bin)+os.pathsep+os.environ['PATH'],
                        ENGINE='claude', CLAUDE_BIN=str(self.bin/'claude'),
                        LEMVO_RC_MODE='structured', LEMVO_TURN_POLL_SEC='.02',
                        LEMVO_POLL_SEC='.05', TURN_GAP_SEC='.02')

    def adapter(self, engine='claude', **env):
        return subprocess.run(['bash',str(T/'autoloop-reference.sh'),str(self.p)],
            env=dict(self.env,ENGINE=engine,**env),cwd=str(self.p),
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,universal_newlines=True,timeout=45)

    def calls(self):
        return [json.loads(l) for l in (self.p/'calls.jsonl').read_text().splitlines()]

    def plans(self):
        return [json.loads(f.read_text()) for f in sorted((self.p/'turns').glob('*/context-plan.json'))]

    def test_claude_same_work_continues_and_receives_concrete_task(self):
        for _ in range(2):
            r=self.adapter(); self.assertEqual(r.returncode,0,r.stdout+r.stderr)
        self.assertEqual([r['resume'] for r in self.calls()],[False,True])
        self.assertIn('weighted estimator',self.calls()[0]['message'])
        self.assertEqual(len(list((self.p/'turns').glob('*/work-unit.json'))),2)
        self.assertEqual(len(list((self.p/'turns').glob('*/work-result.json'))),2)

    def test_codex_same_work_continues(self):
        for _ in range(2):
            r=self.adapter(engine='codex'); self.assertEqual(r.returncode,0,r.stdout+r.stderr)
        self.assertEqual([r['resume'] for r in self.calls()],[False,True])

    def test_phase_boundary_fresh_preserves_state_and_budget(self):
        r=self.adapter(FAKE_CHANGE_STAGE='1');self.assertEqual(r.returncode,0,r.stdout+r.stderr)
        r=self.adapter();self.assertEqual(r.returncode,0,r.stdout+r.stderr)
        self.assertEqual([r['resume'] for r in self.calls()],[False,False])
        self.assertEqual((self.p/'.autoloop-session-id').read_text().strip(),SID_B)
        state=(self.p/'research-state.yaml').read_text()
        self.assertIn('total_turns: 2',state)
        self.assertIn('cap_authorized: no',state)
        self.assertEqual((self.p/'src/method.py').read_text(),'value = 2\n')

    def test_configured_rotation_is_not_a_stop_gate(self):
        (self.p/'.lemvo-context-policy.json').write_text(json.dumps({'max_session_turns':1}))
        for _ in range(2):
            r=self.adapter();self.assertEqual(r.returncode,0,r.stdout+r.stderr)
        self.assertEqual([r['resume'] for r in self.calls()],[False,False])

    def test_failed_fresh_retains_previous_session_and_results(self):
        r=self.adapter(FAKE_CHANGE_STAGE='1');self.assertEqual(r.returncode,0,r.stdout+r.stderr)
        r=self.adapter(FAKE_FAIL='1');self.assertEqual(r.returncode,8,r.stdout+r.stderr)
        self.assertEqual([r['resume'] for r in self.calls()],[False,False])
        self.assertEqual((self.p/'.autoloop-session-id').read_text().strip(),SID_A)
        self.assertTrue((self.p/'src/method.py').exists())

    def test_controller_runs_work_handoffs_without_continue(self):
        r=subprocess.run(['bash',str(T/'lemvo-run.sh'),'start',str(self.p),
                          '--engine','claude','--mode','unattended','--max-turns','4'],
            env=dict(self.env,FAKE_CHANGE_STAGE='1',FAKE_DONE_AFTER='3'),cwd=str(self.p),
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,universal_newlines=True,timeout=60)
        self.assertEqual(r.returncode,0,r.stdout+r.stderr)
        self.assertEqual(len(self.calls()),3)
        self.assertEqual(len(list((self.p/'turns').glob('*/work-result.json'))),3)
        self.assertTrue((self.p/'CAMPAIGN-DONE').exists())

    def test_relative_controller_invocation_resolves_its_bundle(self):
        script=os.path.relpath(str(T/'lemvo-run.sh'),str(self.p))
        r=subprocess.run(['bash',script,'start',str(self.p),
                          '--engine','claude','--mode','unattended','--max-turns','1'],
            env=dict(self.env,FAKE_DONE_AFTER='1'),cwd=str(self.p),
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,universal_newlines=True,timeout=45)
        self.assertEqual(r.returncode,0,r.stdout+r.stderr)
        inventory=json.loads((self.p/'.lemvo-takeover-inventory.json').read_text())
        self.assertTrue(inventory['complete_bundle'])
        self.assertNotIn('FileNotFoundError',r.stderr)


if __name__ == '__main__':
    unittest.main(verbosity=2)
