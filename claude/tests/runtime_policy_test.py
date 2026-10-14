"""CPU-only runtime policy regression; never invokes a real model."""
import json
import os
import re
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUTO = ROOT / 'skills/research-orchestrator/templates/autoloop-reference.sh'
SID = '11111111-2222-4333-8444-555555555555'


class RuntimePolicy(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='lemvo-policy-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.fake = self.root / 'fake-claude'
        self.fake.write_text('#!/usr/bin/env python3\nimport json,os\nfrom pathlib import Path\n'
            'Path("called").touch()\nprint(json.dumps({"type":"result","session_id":"'+SID+'",'
            '"is_error":False,"total_cost_usd":float(os.environ.get("STUB_COST","1")),'
            '"usage":{"input_tokens":10,"output_tokens":2}}))\n')
        self.fake.chmod(0o700)
        self.env = dict(os.environ, ENGINE='claude', CLAUDE_BIN=str(self.fake), LEMVO_RC_MODE='structured')

    def project(self, name, budget='  unit: usd\n  used: 0\n  total_cap: null\n'):
        p = self.root / name
        p.mkdir()
        (p/'research-state.yaml').write_text('project:\n  status: ACTIVE\nbudget:\n'+budget)
        return p

    def call(self, p, cost=1, recover=None):
        args = ['bash',str(AUTO)] + (['--recover',str(p),str(recover)] if recover else [str(p)])
        return subprocess.run(args, env=dict(self.env,STUB_COST=str(cost)),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=30)

    def used(self, p):
        state = (p/'research-state.yaml').read_text().split('budget:\n',1)[1]
        return float(re.search(r'^  used: ([^\n#]+)',state,re.M).group(1).strip(' "'))

    def test_caps_require_source(self):
        for index, source in enumerate([None, '""', 'unknown', 'null', 'user-message:test-cap']):
            with self.subTest(source=source):
                budget = '  unit: usd\n  used: 99999\n  total_cap: 5\n  cap_authorized: yes\n'
                if source is not None: budget += '  cap_source: '+source+'\n'
                p = self.project(str(index),budget)
                result = self.call(p)
                capped = source == 'user-message:test-cap'
                self.assertEqual(result.returncode,2 if capped else 0,result.stdout+result.stderr)
                self.assertEqual((p/'called').exists(),not capped)

    def test_old_helper_migrates_once(self):
        p = self.project('upgrade')
        helper = p/'.autoloop-helper.py'
        helper.write_text('HELPER_VERSION = "4.8"\nraise RuntimeError("old helper must migrate")\n')
        self.assertEqual(self.call(p).returncode,0)
        version = re.search(r'^HELPER_VERSION="([^"]+)"',AUTO.read_text(),re.M).group(1)
        self.assertIn('HELPER_VERSION = "'+version+'"',helper.read_text())
        backups = list(p.glob('.autoloop-helper.py.legacy-*'))
        self.assertEqual(len(backups),1)
        stamp = helper.stat().st_mtime_ns
        self.assertEqual(self.call(p,2).returncode,0)
        self.assertEqual(helper.stat().st_mtime_ns,stamp)
        self.assertEqual(list(p.glob('.autoloop-helper.py.legacy-*')),backups)

    def test_real_recover_then_resume_and_legacy_string(self):
        p = self.project('recovery')
        self.assertEqual(self.call(p,1).returncode,0)
        td = p/'turns'/'orphan'
        td.mkdir(parents=True)
        (td/'manifest.json').write_text(json.dumps({'turn_id':'orphan','engine':'claude',
            'started_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())}))
        (td/'engine.json').write_text('{"engine":"claude"}')
        (td/'engine-stdout.jsonl').write_text(json.dumps({'type':'result','session_id':SID,
            'is_error':False,'total_cost_usd':3,'usage':{'input_tokens':10,'output_tokens':2}})+'\n')
        (td/'engine-stderr.log').write_text('')
        self.assertEqual(self.call(p,recover=td).returncode,0)
        self.assertEqual(self.used(p),3)
        ledger = p/'.autoloop-prep-ledger.jsonl'
        events = [json.loads(line) for line in ledger.read_text().splitlines() if line.startswith('{')]
        recovered = [e for e in events if e.get('type') == 'recovered-turn'][-1]
        self.assertIsInstance(recovered['cost_usd_reported'],(int,float))
        before = (p/'research-state.yaml').read_text()
        self.assertEqual(self.call(p,recover=td).returncode,0)
        self.assertEqual((p/'research-state.yaml').read_text(),before)
        # Existing project ledgers can contain the previous string writer.
        lines=[]
        for line in ledger.read_text().splitlines():
            if line.startswith('{'):
                event=json.loads(line)
                if event.get('type')=='recovered-turn':
                    event['cost_usd_reported']='3'
                    line=json.dumps(event)
            lines.append(line)
        ledger.write_text('\n'.join(lines)+'\n')
        self.assertEqual(self.call(p,4).returncode,0)
        self.assertEqual(self.used(p),4)


if __name__ == '__main__':
    unittest.main(verbosity=2)
