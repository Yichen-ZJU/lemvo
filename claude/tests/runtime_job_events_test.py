"""Actual per-job reconciler/controller, CPU-only producers; no model or GPU."""
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import runtime_liveness_test as base

SPEC = importlib.util.spec_from_file_location('job_events', str(base.TEMPLATES/'job-events.py'))
events = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(events)


class JobEvents(unittest.TestCase):
    setUp = base.Liveness.setUp
    state = base.Liveness.state
    call = base.Liveness.call
    start = base.Liveness.start
    until = base.Liveness.until
    calls = base.Liveness.calls

    def worker(self, name='worker', run_id='r1'):
        proc = subprocess.Popen(['sleep', '30'])
        def stop():
            if proc.poll() is None: proc.terminate()
            proc.wait()
        self.addCleanup(stop)
        job = {'name': name, 'run_id': run_id, 'pid': proc.pid,
               'pid_start': events.identity(proc.pid)['start'],
               'done_file': str(self.p/(name+'.json')), 'started_epoch': time.time()}
        return proc, job

    def journal(self, jobs):
        path = self.p/'.lemvo-jobs.json'
        path.write_text(json.dumps(jobs))
        return path

    def reconcile(self):
        return events.reconcile(self.p/'.lemvo-jobs.json', self.p/'.lemvo-jobstates.json',
                                self.p/'.lemvo-receipts')

    def receipts(self):
        return [(p, json.loads(p.read_text())) for p in (self.p/'.lemvo-receipts').glob('*.json')]

    def consume(self):
        for p, obj in self.receipts():
            obj['consumed'] = True
            events.atomic(p, obj)

    def terminal(self, name='done', run_id='r2', status='failed'):
        path = self.p/(name+'.json')
        events.atomic(path, {'run_id': run_id, 'status': status,
                             'exit_code': 0 if status=='succeeded' else 2,
                             'metrics': {'accuracy': .91}, 'split': 'dev',
                             'settings': {'seed': 4}})
        return {'name': name, 'run_id': run_id, 'pid': 999999999,
                'pid_start': '1', 'done_file': str(path), 'started_epoch': time.time()-1}

    def test_mixed_failure_wakes_before_live_sibling_and_preserves_journal(self):
        worker, live = self.worker()
        path = self.journal([live, self.terminal()]); original = path.read_bytes()
        self.assertEqual(self.reconcile(), 'EVENTS')
        self.assertEqual(path.read_bytes(), original)
        self.assertIsNone(worker.poll())
        receipts = self.receipts()
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0][1]['status'], 'failed')
        self.assertNotIn('scientific_verdict', receipts[0][1])
        self.assertIn('Execution observation only', receipts[0][1]['interpretation'])

    def test_mixed_success_keeps_original_units_scope_and_one_receipt(self):
        _, live = self.worker()
        self.journal([live, self.terminal(status='succeeded')])
        self.assertEqual(self.reconcile(), 'EVENTS')
        rec = self.receipts()[0][1]
        self.assertEqual(rec['metrics']['accuracy'], .91)
        self.assertEqual(rec['result']['split'], 'dev')
        self.assertEqual(rec['result']['settings'], {'seed': 4})
        self.assertEqual(self.reconcile(), 'EVENTS')
        self.assertEqual(len(self.receipts()), 1)

    def test_consumed_terminal_with_live_sibling_returns_to_wait(self):
        _, live = self.worker()
        self.journal([live, self.terminal()])
        self.assertEqual(self.reconcile(), 'EVENTS')
        self.consume()
        for _ in range(3): self.assertEqual(self.reconcile(), 'PENDING worker')
        self.assertEqual(len(self.receipts()), 1)
        self.assertTrue(self.receipts()[0][1]['consumed'])
        history=(self.p/'.lemvo-jobs-history.jsonl').read_text().splitlines()
        self.assertEqual(len(history), 1)

    def test_restart_republishes_no_duplicates_and_delivers_existing_receipt(self):
        _, live = self.worker()
        self.journal([live, self.terminal()])
        self.assertEqual(self.reconcile(), 'EVENTS')
        key = self.receipts()[0][1]['event_key']
        result = subprocess.run([sys.executable, str(base.TEMPLATES/'job-events.py'),
            'reconcile', str(self.p/'.lemvo-jobs.json'), str(self.p/'.lemvo-jobstates.json'),
            str(self.p/'.lemvo-receipts')], text=True, capture_output=True, check=True)
        self.assertEqual(result.stdout.strip(), 'EVENTS')
        self.assertEqual([r[1]['event_key'] for r in self.receipts()], [key])
        (self.p/'.lemvo-jobs.json').unlink()
        self.assertEqual(self.reconcile(), 'EVENTS')

    def test_same_run_id_new_process_identity_creates_new_attempt(self):
        _, live = self.worker()
        first = self.terminal(run_id='retry')
        self.journal([live, first]); self.assertEqual(self.reconcile(), 'EVENTS')
        self.consume()
        retry_proc, retry = self.worker('retry', 'retry')
        self.journal([live, retry])
        events.atomic(retry['done_file'], {'run_id': 'retry', 'status': 'failed', 'exit_code': 2})
        self.assertEqual(self.reconcile(), 'EVENTS')
        self.assertEqual(len(self.receipts()), 2)
        self.assertEqual(len({r[1]['event_key'] for r in self.receipts()}), 2)
        self.assertIsNone(retry_proc.poll())

    def test_new_attempt_ignores_old_done_marker_while_alive(self):
        proc, live = self.worker()
        path=Path(live['done_file'])
        events.atomic(path, {'run_id': live['run_id'], 'status': 'succeeded', 'exit_code': 0})
        os.utime(str(path), (time.time()-3600, time.time()-3600))
        self.journal([live])
        self.assertEqual(self.reconcile(), 'PENDING worker')
        self.assertFalse(self.receipts())
        self.assertIsNone(proc.poll())

    def test_run_id_mismatch_does_not_finish_live_producer(self):
        _, live = self.worker()
        events.atomic(live['done_file'], {'run_id': 'other-attempt', 'status': 'succeeded'})
        self.journal([live]); self.assertEqual(self.reconcile(), 'PENDING worker')
        self.assertFalse(self.receipts())

    def test_partial_json_waits_then_died_producer_is_execution_failure(self):
        proc, live = self.worker()
        Path(live['done_file']).write_text('{"run_id": "r1", "status": "succ')
        self.journal([live])
        self.assertEqual(self.reconcile(), 'PENDING worker')
        self.assertFalse(self.receipts())
        proc.terminate(); proc.wait()
        self.assertEqual(self.reconcile(), 'ENDED')
        self.assertEqual(self.receipts()[0][1]['status'], 'failed')

    def test_empty_or_nonterminal_json_does_not_advance_live_producer(self):
        _, live = self.worker()
        self.journal([live])
        for text in ('', '{}', '{"status": "running"}'):
            Path(live['done_file']).write_text(text)
            self.assertEqual(self.reconcile(), 'PENDING worker')
            self.assertFalse(self.receipts())

    def test_timeout_receipt_keeps_producer_identity(self):
        proc, live = self.worker()
        live.update(started_epoch=time.time()-10, timeout_min=.01)
        path=self.journal([live]); before=path.read_bytes()
        self.assertEqual(self.reconcile(), 'EVENTS')
        receipt=self.receipts()[0][1]
        self.assertEqual(receipt['status'], 'timed_out')
        self.assertEqual(receipt['pid'], proc.pid)
        self.assertEqual(receipt['pid_start'], live['pid_start'])
        self.assertIsNone(proc.poll())
        self.consume()
        self.assertEqual(self.reconcile(), 'PENDING worker')
        self.assertEqual(path.read_bytes(), before)

    def test_finished_marker_with_live_pid_delivers_once_and_keeps_identity(self):
        proc, live=self.worker()
        events.atomic(live['done_file'], {'run_id':'r1', 'status':'succeeded','exit_code':0})
        path=self.journal([live]); before=path.read_bytes()
        self.assertEqual(self.reconcile(), 'EVENTS')
        self.consume()
        self.assertEqual(self.reconcile(), 'PENDING worker')
        self.assertEqual(path.read_bytes(), before)
        self.assertIsNone(proc.poll())
        self.assertEqual(len(self.receipts()),1)

    def test_unknown_and_malformed_dead_events_do_not_repeat_each_poll(self):
        _, live=self.worker()
        malformed=self.p/'malformed.json'; malformed.write_text('{"status":')
        self.journal([live, {'name':'unobservable','run_id':'u',
                    'started_epoch':time.time()-10000,'done_file':str(self.p/'missing')},
                    {'name':'partial-dead','run_id':'bad','pid':99999999,
                     'done_file':str(malformed)}])
        self.assertEqual(self.reconcile(),'EVENTS')
        self.assertEqual({r[1]['status'] for r in self.receipts()},{'unknown','failed'})
        self.consume()
        for _ in range(3): self.assertEqual(self.reconcile(),'PENDING worker')
        self.assertEqual(len(self.receipts()),2)

    def test_all_terminal_controller_keeps_legacy_archive_contract(self):
        self.state(); self.journal([self.terminal()])
        result=self.call('advance', FAKE_KIND='construct')
        self.assertEqual(result.returncode,0,result.stdout)
        self.assertEqual(len(self.calls()),1)
        self.assertFalse((self.p/'.lemvo-jobs.json').exists())
        self.assertTrue(list(self.p.glob('.lemvo-jobs.json.pending-receipt-*')))
        self.assertEqual(len(self.receipts()),1)
        self.assertTrue(self.receipts()[0][1]['consumed'])

    def test_advance_mixed_event_delivers_once_without_detaching_live_job(self):
        self.state(); worker, live=self.worker()
        jobs=self.journal([live,self.terminal()]); before=jobs.read_bytes()
        result=self.call('advance', FAKE_KIND='construct')
        self.assertEqual(result.returncode,0,result.stdout)
        self.assertEqual(len(self.calls()),1)
        self.assertIn('status=failed',self.calls()[0]['message'])
        self.assertEqual(jobs.read_bytes(),before)
        self.assertIsNone(worker.poll())
        result=self.call('advance', FAKE_KIND='construct')
        self.assertEqual(result.returncode,0,result.stdout)
        self.assertEqual(len(self.calls()),1, 'Consumed failure must not re-wake a live batch')

    def test_real_controller_wait_is_zero_calls_then_event_wakes_once(self):
        self.state(); worker, live=self.worker()
        other, branch=self.worker('branch','r2')
        jobs=self.journal([live,branch]); before=jobs.read_bytes()
        controller=self.start('--engine','claude','--mode','unattended',FAKE_KIND='construct')
        self.assertTrue(self.until(lambda:(self.p/'.lemvo-status').exists()))
        time.sleep(.35)
        self.assertFalse(self.calls(), 'A live producer wait must not enter the model')
        events.atomic(branch['done_file'], {'run_id':'r2','status':'failed','exit_code':2})
        self.assertTrue(self.until(lambda:len(self.calls())==1))
        self.assertTrue(self.until(lambda:bool(self.receipts()) and self.receipts()[0][1]['consumed']))
        time.sleep(.4)
        self.assertEqual(len(self.calls()),1)
        self.assertEqual(jobs.read_bytes(),before)
        self.assertIsNone(worker.poll()); self.assertIsNone(other.poll())
        self.assertIsNone(controller.poll())


    def test_blank_harvest_summary_does_not_swallow_result_or_fabricate_success(self):
        done = self.p / 'blank_done.json'
        events.atomic(done, {'run_id': 'b1', 'status': 'succeeded', 'exit_code': 0})
        self.journal([
            {'name': 'blank-with-done', 'run_id': 'b1', 'status': 'running',
             'harvest_summary': '', 'done_path': str(done), 'started_epoch': time.time() - 1000},
            {'name': 'blank-no-done', 'run_id': 'b2', 'status': 'running',
             'harvest_summary': '', 'started_epoch': time.time() - 1000},
        ])
        res = self.reconcile()
        self.assertEqual(res, 'ENDED')
        by_run = {r['run_id']: r for _, r in self.receipts()}
        self.assertEqual(by_run['b1']['status'], 'succeeded')
        self.assertEqual(by_run['b2']['status'], 'unknown')

    def test_result_path_plain_progress_text_never_declares_succeeded(self):
        prog = self.p / 'progress.txt'
        prog.write_text('training step 3 / 100', encoding='utf-8')
        _, live = self.worker()
        live.pop('done_file', None)
        live['result_path'] = str(prog)
        self.journal([live])
        self.assertEqual(self.reconcile(), 'PENDING worker')
        self.assertFalse(self.receipts())

        # Stale PID-less progress text transitions to unknown, never succeeded
        self.journal([{'name': 'prog-stale', 'run_id': 'p2', 'result_path': str(prog),
                       'started_epoch': time.time() - 1000}])
        self.assertEqual(self.reconcile(), 'ENDED')
        rcpts = self.receipts()
        self.assertEqual(len(rcpts), 1)
        self.assertEqual(rcpts[0][1]['status'], 'unknown')
        self.assertNotEqual(rcpts[0][1]['status'], 'succeeded')

    def test_sentinel_path_not_preempted_by_intermediate_result_path(self):
        prog = self.p / 'progress.txt'
        prog.write_text('training step 3 / 100', encoding='utf-8')
        worker, live = self.worker()
        live['result_path'] = str(prog)
        self.journal([live])
        self.assertEqual(self.reconcile(), 'PENDING worker')
        self.assertFalse(self.receipts())

    def test_resumed_live_job_ignores_unattributed_prior_failed_marker(self):
        proc, _ = self.worker()
        rstart = events.identity(proc.pid)['start']
        ff = self.p / 'failed.json'
        events.atomic(ff, {'run_id': 'r-res', 'status': 'failed', 'exit_code': 1})
        self.journal([{'name': 'resumed', 'run_id': 'r-res', 'pid': 99999999, 'pid_start': '1',
                       'resume_pid': proc.pid, 'resume_pid_start': rstart,
                       'failed_file': str(ff), 'started_epoch': time.time() - 100}])
        self.assertEqual(self.reconcile(), 'PENDING resumed')
        self.assertFalse(self.receipts())
        states = json.loads((self.p / '.lemvo-jobstates.json').read_text(encoding='utf-8'))
        self.assertEqual(states[0]['pid'], proc.pid)
        self.assertTrue(states[0]['producer_alive'])

    def test_unbound_resume_pid_rejected_without_corroboration(self):
        proc = subprocess.Popen(['sleep', '30'])
        self.addCleanup(lambda: (proc.terminate() if proc.poll() is None else None, proc.wait()))
        self.journal([{'name': 'unbound', 'run_id': 'u1', 'pid': 99999999, 'pid_start': '1',
                       'resume_pid': proc.pid, 'started_epoch': time.time() - 100}])
        self.assertEqual(self.reconcile(), 'ENDED')
        states = json.loads((self.p / '.lemvo-jobstates.json').read_text(encoding='utf-8'))
        self.assertFalse(states[0]['producer_alive'])
        self.assertTrue(states[0].get('unbound_resume_pid'))

    def test_corroborated_unbound_resume_pid_accepted_when_cmdline_matches(self):
        proc = subprocess.Popen(['python3', '-c', 'import time; time.sleep(30)', 'unique_run_v10d_token'])
        self.addCleanup(lambda: (proc.terminate() if proc.poll() is None else None, proc.wait()))
        self.journal([{'name': 'corrob', 'run_id': 'unique_run_v10d_token',
                       'pid': 99999999, 'pid_start': '1',
                       'resume_pid': proc.pid, 'started_epoch': time.time() - 100}])
        self.assertEqual(self.reconcile(), 'PENDING corrob')
        states = json.loads((self.p / '.lemvo-jobstates.json').read_text(encoding='utf-8'))
        self.assertTrue(states[0]['producer_alive'])
        self.assertEqual(states[0]['pid'], proc.pid)

if __name__=='__main__':
    unittest.main(verbosity=2)
