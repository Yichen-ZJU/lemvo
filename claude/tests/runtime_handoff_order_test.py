"""Independent regressions for concrete handoffs; CPU fixtures, no model or GPU."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import runtime_dispatch_test as base

SPEC = importlib.util.spec_from_file_location('workunit_order', str(base.T/'work-unit.py'))
work = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(work)


class HandoffOrder(unittest.TestCase):
    setUp = base.Dispatch.setUp
    calls = base.Dispatch.calls
    def _project(self):
        old = Path.cwd()
        os.chdir(str(self.p))
        self.addCleanup(os.chdir, str(old))

    def test_queue_metadata_is_not_the_concrete_objective(self):
        self._project()
        body = ('<!-- message_id: q-1 -->\n<!-- created: now -->\n'
                '<!-- lemvo_instruction: {} -->\n'
                'Implement the new estimator, not the old selector.\n')
        Path('current.md').write_text(body)
        Path('input.md').write_text('Runtime instructions\n'+body)
        old = {k: os.environ.get(k) for k in ('LEMVO_MESSAGE_KIND','LEMVO_CURRENT_MESSAGE_PATH')}
        self.addCleanup(self._restore_env, old)
        os.environ['LEMVO_MESSAGE_KIND']='user'
        os.environ['LEMVO_CURRENT_MESSAGE_PATH']='current.md'
        work.prepare('turns/current','input.md','prompt.md')
        unit=work.load('turns/current/work-unit.json')
        self.assertEqual(unit['objective'],'Implement the new estimator, not the old selector.')
        self.assertIn(body,Path('prompt.md').read_text())

    @staticmethod
    def _restore_env(values):
        for k,v in values.items():
            if v is None: os.environ.pop(k,None)
            else: os.environ[k]=v

    def test_real_controller_receipt_cannot_replace_steer_task(self):
        guidance=self.p/'guidance.md'
        body='Implement the new estimator, not the old selector.'
        guidance.write_text(body+'\nKeep existing evidence and authorization.\n')
        receipts=self.p/'.lemvo-receipts';receipts.mkdir()
        receipt={'schema_version':2,'schema':'lemvo-job-receipt','name':'old-job',
                 'run_id':'existing-previous-run','status':'succeeded','exit_code':0,
                 'consumed':False,'done_file':'results/old.json'}
        (receipts/'old.json').write_text(json.dumps(receipt))
        published=subprocess.run(['bash',str(base.T/'lemvo-run.sh'),'steer',str(self.p),str(guidance)],
            cwd=str(self.p),env=self.env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
            universal_newlines=True,timeout=30)
        self.assertEqual(published.returncode,0,published.stdout+published.stderr)
        run=subprocess.run(['bash',str(base.T/'lemvo-run.sh'),'advance',str(self.p)],
            cwd=str(self.p),env=self.env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
            universal_newlines=True,timeout=45)
        self.assertEqual(run.returncode,0,run.stdout+run.stderr)
        self.assertEqual(len(self.calls()),1)
        turns=list((self.p/'turns').glob('*/work-unit.json'))
        self.assertEqual(len(turns),1)
        self.assertEqual(json.loads(turns[0].read_text())['objective'],body)
        message=self.calls()[0]['message']
        self.assertIn(body,message)
        self.assertIn('existing-previous-run',message)
        self.assertEqual(guidance.read_text(),body+'\nKeep existing evidence and authorization.\n')

    def test_recovered_older_turn_does_not_rewind_current_handoff(self):
        self._project()
        for tid,goal,started in [('t20261116T010000Z-old','OLD NEXT TASK','2026-11-16T01:00:00Z'),
                                 ('t20261116T020000Z-new','NEW NEXT TASK','2026-11-16T02:00:00Z')]:
            td=Path('turns')/tid;td.mkdir(parents=True)
            (td/'manifest.json').write_text(json.dumps({'session_epoch':'legacy','started_utc':started}))
            (td/'result.json').write_text(json.dumps({'status':'succeeded','engine_rc':0}))
            work.report(td,'completed','finished '+tid,next_objective=goal)
        work.settle('turns/t20261116T020000Z-new',0)
        work.settle('turns/t20261116T010000Z-old',0)
        latest=work.load('.lemvo-last-work-result.json')
        self.assertEqual(latest['turn_id'],'t20261116T020000Z-new')
        self.assertEqual(latest['next_task']['objective'],'NEW NEXT TASK')
        self.assertTrue(Path('turns/t20261116T010000Z-old/work-result.json').is_file())

    def test_purge_archives_runtime_handoff_without_reopening_old_task(self):
        self._project()
        Path('.lemvo-last-work-result.json').write_text(json.dumps(
            {'status':'partial','summary':'old scoped record','next_task':{'objective':'PURGED OLD NEXT TASK'}}))
        Path('.lemvo-context.json').write_text(json.dumps({'sid':base.SID_A,'successful_turns':8}))
        Path('.lemvo-context-policy.json').write_text(json.dumps({'mode':'auto','max_session_turns':3}))
        before=Path('research-state.yaml').read_text()
        purged=subprocess.run(['bash',str(base.T/'lemvo-run.sh'),'purge',str(self.p)],
            cwd=str(self.p),env=self.env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
            universal_newlines=True,timeout=30)
        self.assertEqual(purged.returncode,0,purged.stdout+purged.stderr)
        self.assertFalse(Path('.lemvo-last-work-result.json').exists())
        self.assertFalse(Path('.lemvo-context.json').exists())
        self.assertTrue(Path('.lemvo-context-policy.json').exists())
        self.assertEqual(Path('research-state.yaml').read_text(),before)
        Path('input.md').write_text('Continue current project from retained state.')
        old={k:os.environ.get(k) for k in ('LEMVO_MESSAGE_KIND','LEMVO_CURRENT_MESSAGE_PATH')}
        self.addCleanup(self._restore_env,old)
        os.environ['LEMVO_MESSAGE_KIND']='runtime';os.environ.pop('LEMVO_CURRENT_MESSAGE_PATH',None)
        work.prepare('turns/after-purge','input.md','prompt.md')
        self.assertNotIn('PURGED OLD NEXT TASK',work.load('turns/after-purge/work-unit.json')['objective'])
        archives=list(Path('.').glob('poisoned-archive-*/.lemvo-last-work-result.json'))
        # Archive naming is not an execution contract; evidence must remain in
        # whichever purge archive the existing controller reports.
        if not archives:
            archives=[p for p in Path('.').rglob('.lemvo-last-work-result.json') if p.parent!=Path('.')]
        self.assertTrue(archives,'Purged runtime handoff should be archived, not destroyed')


if __name__=='__main__':
    unittest.main(verbosity=2)
