"""Actual adapter/controller dispatch, CPU CLI fixtures, no network/model/GPU."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
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

    def test_recover_older_turn_does_not_overwrite_newer_sid(self):
        r = self.adapter(FAKE_CHANGE_STAGE='1'); self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r = self.adapter(); self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((self.p / '.autoloop-session-id').read_text().strip(), SID_B)
        turns = sorted((self.p / 'turns').glob('t*'))
        t_old = turns[0]
        # Force older turn to look unsettled so --recover runs full path on older dispatch
        for name in ('result.json', 'work-settlement.json', 'work-result.json'):
            f = t_old / name
            if f.exists():
                f.unlink()
        rec = subprocess.run(
            ['bash', str(T / 'autoloop-reference.sh'), '--recover', str(self.p), str(t_old)],
            env=self.env, cwd=str(self.p),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=45,
        )
        self.assertEqual(rec.returncode, 0, rec.stdout + rec.stderr)
        self.assertEqual((self.p / '.autoloop-session-id').read_text().strip(), SID_B)
        self.assertFalse(json.loads((t_old / 'work-settlement.json').read_text())['promoted'])

    def test_crash_mid_settlement_then_recover_is_accounting_idempotent(self):
        # Run one real turn so .autoloop-helper.py & session-epoch are initialized cleanly
        r0 = self.adapter(); self.assertEqual(r0.returncode, 0, r0.stdout + r0.stderr)
        # Clear prior turn records so orphan is the authoritative single turn under recovery
        for f in ('.lemvo-context.json', '.lemvo-last-work-result.json', '.autoloop-prep-ledger.jsonl', 'campaign_log.txt'):
            if (self.p / f).exists():
                (self.p / f).unlink()
        epoch = (self.p / '.lemvo-session-epoch').read_text().strip() if (self.p / '.lemvo-session-epoch').exists() else 'legacy'
        (self.p / 'research-state.yaml').write_text(
            'campaign_status: ACTIVE\nproject:\n  status: ACTIVE\n  objective: Improve estimator\n'
            'prep:\n  prep_turns: 0\ntotal:\n  total_turns: 0\n  total_wall_time_min: 0.0\n'
            '  total_tokens_in: 0\n  total_tokens_out: 0\n'
            'budget:\n  unit: usd\n  used: 0\n  total_cap: 10\n  cap_authorized: yes\n  cap_source: user-test\n'
        )
        td = self.p / 'turns/t20991124T000001Z-999-solo'
        td.mkdir(parents=True)
        (td / 'manifest.json').write_text(json.dumps({
            'turn_id': td.name, 'session_epoch': epoch, 'engine': 'claude',
            'started_utc': '2099-11-24T00:00:01Z',
        }))
        (td / 'context-plan.json').write_text(json.dumps({
            'turn_id': td.name, 'session_action': 'fresh', 'created_epoch': 4099200001.0,
        }))
        (td / 'progress.json').write_text(json.dumps({
            'kind': 'prep', 'progress': False, 'neutral': False, 'changes': [], 'wait': {'kind': 'none'},
        }))
        (td / 'progress-explicit-before.json').write_text('{}')
        (td / 'engine-stdout.jsonl').write_text(
            json.dumps({'type': 'stream_event', 'event': {'type': 'ping'}}) + '\n' +
            json.dumps({
                'type': 'result', 'session_id': SID_A, 'is_error': False,
                'result': 'ok', 'total_cost_usd': 1.25,
                'usage': {'input_tokens': 150, 'output_tokens': 30},
            }) + '\n'
        )
        h_env = dict(self.env, LEMVO_TURN_DIR=str(td), LEMVO_TURN_ID=td.name, LEMVO_PROGRESS_RECORD=str(td / 'progress.json'))
        subprocess.run(['python3', str(self.p / '.autoloop-helper.py'), 'settle'], cwd=str(self.p), env=h_env, check=True)
        subprocess.run(['python3', str(self.p / '.autoloop-helper.py'), 'account', '60', '150', '30'], cwd=str(self.p), env=h_env, check=True)
        subprocess.run(['python3', str(self.p / '.autoloop-helper.py'), 'budget-add', '1.25'], cwd=str(self.p), env=h_env, check=True)
        subprocess.run(['python3', str(self.p / '.autoloop-helper.py'), 'ledger-append'],
                       input=json.dumps({'type': 'work-turn', 'turn_id': td.name, 'sid': SID_A, 'cost_usd_reported': 1.25}),
                       cwd=str(self.p), env=h_env, universal_newlines=True, check=True)
        rec = subprocess.run(
            ['bash', str(T / 'autoloop-reference.sh'), '--recover', str(self.p), str(td)],
            env=self.env, cwd=str(self.p),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=45,
        )
        self.assertEqual(rec.returncode, 0, rec.stdout + rec.stderr)
        state = (self.p / 'research-state.yaml').read_text()
        self.assertIn('total_turns: 1', state)
        self.assertIn('total_tokens_in: 150', state)
        self.assertIn('used: 1.25', state)
        ledger = [json.loads(l) for l in (self.p / '.autoloop-prep-ledger.jsonl').read_text().splitlines() if l.strip()]
        self.assertEqual([e['turn_id'] for e in ledger].count(td.name), 1)
        clog = (self.p / 'campaign_log.txt').read_text()
        self.assertNotIn('"stream_event"', clog)
        self.assertIn('"stream_event"', (td / 'engine-stdout.jsonl').read_text())

    def test_service_error_503_accumulates_usd_budget_and_enforces_cap_pre_dispatch(self):
        (self.p / 'research-state.yaml').write_text(
            'campaign_status: ACTIVE\nproject:\n  status: ACTIVE\n  objective: Improve estimator\n'
            'budget:\n  unit: usd\n  used: 0\n  total_cap: 1.0\n  cap_authorized: yes\n  cap_source: user-test\n'
        )
        fake503 = '''#!/usr/bin/env python3
import json, sys
from pathlib import Path
with open('calls.jsonl', 'a') as f:
    f.write('1\\n')
print(json.dumps({
    'type': 'result', 'session_id': '11111111-2222-4333-8444-555555555555',
    'is_error': True, 'result': 'service overloaded 503',
    'total_cost_usd': 0.6 * len(Path('calls.jsonl').read_text().splitlines()),
    'usage': {'input_tokens': 10, 'output_tokens': 2},
}))
sys.exit(1)
'''
        (self.bin / 'claude').write_text(fake503)
        (self.bin / 'claude').chmod(0o700)
        r1 = self.adapter(); self.assertEqual(r1.returncode, 8, r1.stdout + r1.stderr)
        r2 = self.adapter(); self.assertEqual(r2.returncode, 8, r2.stdout + r2.stderr)
        state = (self.p / 'research-state.yaml').read_text()
        self.assertIn('used: 1.2', state)
        r3 = self.adapter()
        self.assertEqual(r3.returncode, 2, r3.stdout + r3.stderr)
        self.assertEqual(len((self.p / 'calls.jsonl').read_text().splitlines()), 2)

    def test_account_settle_budget_atomic_crash_before_save_applied_no_double_count(self):
        sh = (T / 'autoloop-reference.sh').read_text(encoding='utf-8').replace('\r\n', '\n')
        m = re.search(r"cat >> \"\$HELPER\" <<'PYEOF'\n(.*?)\nPYEOF\n", sh, re.S)
        self.assertIsNotNone(m)
        helper = self.p / '.autoloop-helper.py'
        helper.write_text(
            '#!/usr/bin/env python3\nHELPER_VERSION = "test"\n'
            f'CONTRACT_PATH = {json.dumps(str(T / "runtime-contract.py"))}\n' + m.group(1),
            encoding='utf-8',
        )
        turn1 = self.p / 'turns' / '0001'
        turn2 = self.p / 'turns' / '0002'
        turn1.mkdir(parents=True)
        turn2.mkdir(parents=True)

        crash_runner = self.p / 'crash_once.py'
        crash_runner.write_text(
            'import os, runpy, sys\n'
            'helper, cmd, *rest = sys.argv[1:]\n'
            'def tr(frame, event, arg):\n'
            '    if event == "call" and frame.f_code.co_name == "_save_applied":\n'
            '        os._exit(73)\n'
            '    return tr\n'
            'sys.settrace(tr)\n'
            'sys.argv = [helper, cmd] + rest\n'
            'runpy.run_path(helper, run_name="__main__")\n',
            encoding='utf-8',
        )

        env1 = dict(self.env, LEMVO_TURN_DIR=str(turn1))
        applied_path = turn1 / '.accounting-applied.json'
        for key, cmd_args in (
            ('account', ['account', '60', '100', '20']),
            ('settle', ['settle']),
            ('budget_add', ['budget-add', '1.5']),
        ):
            c = subprocess.run([sys.executable, str(crash_runner), str(helper)] + cmd_args,
                               cwd=str(self.p), env=env1, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               universal_newlines=True)
            self.assertEqual(c.returncode, 73, c.stdout + c.stderr)
            cur_applied = json.loads(applied_path.read_text(encoding='utf-8')) if applied_path.exists() else {}
            self.assertNotIn(key, cur_applied)
            r = subprocess.run([sys.executable, str(helper)] + cmd_args,
                               cwd=str(self.p), env=env1, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               universal_newlines=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn('IDEMPOTENT_SKIP=yes', r.stdout)
            healed = json.loads(applied_path.read_text(encoding='utf-8'))
            self.assertIn(key, healed)

        state1 = (self.p / 'research-state.yaml').read_text(encoding='utf-8')
        self.assertIn('total_wall_time_min: 1.0', state1)
        self.assertIn('total_tokens_in: 100', state1)
        self.assertIn('total_tokens_out: 20', state1)
        self.assertIn('prep_turns: 1', state1)
        self.assertIn('total_turns: 1', state1)
        self.assertIn('used: 1.5', state1)
        applied = json.loads((turn1 / '.accounting-applied.json').read_text(encoding='utf-8'))
        self.assertIn('account', applied)
        self.assertIn('settle', applied)
        self.assertIn('budget_add', applied)

        env2 = dict(self.env, LEMVO_TURN_DIR=str(turn2))
        for cmd_args in (['account', '60', '100', '20'], ['settle'], ['budget-add', '1.5']):
            r = subprocess.run([sys.executable, str(helper)] + cmd_args,
                               cwd=str(self.p), env=env2, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               universal_newlines=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertNotIn('IDEMPOTENT_SKIP=yes', r.stdout)

        state2 = (self.p / 'research-state.yaml').read_text(encoding='utf-8')
        self.assertIn('total_wall_time_min: 2.0', state2)
        self.assertIn('total_tokens_in: 200', state2)
        self.assertIn('total_tokens_out: 40', state2)
        self.assertIn('prep_turns: 2', state2)
        self.assertIn('total_turns: 2', state2)
        self.assertIn('used: 3.0', state2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
