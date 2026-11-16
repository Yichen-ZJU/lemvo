"""Runtime work packets: CPU/std-lib only, no model or active campaign."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time
import unittest


HERE = Path(__file__).resolve().parent
MODULE = HERE.parent/'skills/research-orchestrator/templates/work-unit.py'
if not MODULE.is_file():
    MODULE = HERE/'work-unit.py'
spec = importlib.util.spec_from_file_location('work_unit', str(MODULE))
work = importlib.util.module_from_spec(spec)
spec.loader.exec_module(work)


class WorkUnits(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = Path.cwd()
        os.chdir(self.tmp.name)
        self.td = Path('turns/t1')
        self.td.mkdir(parents=True)
        self.input = Path('input.md')
        self.input.write_text('Use the existing data; do not download weights. 当前用户指导必须保留。', encoding='utf-8')
        Path('research-state.yaml').write_text(
            'project:\n  objective: Improve grounded decisions # comment\n'
            'research_plan:\n  stage: "experiment" # phase\n  method_branch: parent-A\n'
            'prep:\n  next_min_probe: "Implement A # keep this token" # ignored\n', encoding='utf-8')
        self.output = self.td/'context-prompt.md'
        self.saved_env = {k: os.environ.get(k) for k in ('LEMVO_MESSAGE_KIND', 'LEMVO_CURRENT_MESSAGE_PATH')}
        for k in self.saved_env:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self.saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        os.chdir(str(self.old))
        self.tmp.cleanup()

    def write(self, path, data):
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')

    def prepare(self, td=None):
        return work.prepare(td or self.td, self.input, self.output, 'codex', '.autoloop-session-id')

    def previous(self, **kwargs):
        p = {'sid': 'old-sid', 'engine': 'codex', 'phase': 'experiment',
             'branch': 'parent-A', 'successful_turns': 2, 'files': []}
        p.update(kwargs)
        self.write('.lemvo-context.json', p)
        Path('.autoloop-session-id').write_text('old-sid')

    def test_prepare_concrete_task_quoted_comment_and_sources(self):
        plan = self.prepare()
        unit = work.load(self.td/'work-unit.json')
        self.assertEqual(unit['objective'], 'Implement A # keep this token')
        self.assertEqual(unit['phase'], 'experiment')
        self.assertEqual(unit['method_branch'], 'parent-A')
        self.assertEqual(plan['session_action'], 'fresh')
        self.assertIn('autoresearch/SKILL.md', unit['relevant_skill_paths'])

    def test_preserved_user_input_is_verbatim(self):
        self.prepare()
        self.assertIn(self.input.read_text(encoding='utf-8'), self.output.read_text(encoding='utf-8'))
        self.assertIn('Current user guidance overrides', self.output.read_text(encoding='utf-8'))

    def test_new_user_message_overrides_previous_next_task_without_paraphrase(self):
        self.write('.lemvo-last-work-result.json', {'next_task': {'objective': 'Old suggested action'}})
        Path('steer.md').write_text('Implement the alternate selector now\nKeep previous permissions.', encoding='utf-8')
        os.environ['LEMVO_MESSAGE_KIND'] = 'user'
        os.environ['LEMVO_CURRENT_MESSAGE_PATH'] = 'steer.md'
        self.prepare()
        unit = work.load(self.td/'work-unit.json')
        self.assertEqual(unit['objective'], 'Implement the alternate selector now')
        self.assertEqual(unit['current_input_kind'], 'user')
        self.assertIn('NEW CURRENT GUIDANCE', self.output.read_text(encoding='utf-8'))

    def test_runtime_summary_does_not_override_previous_next_task(self):
        self.write('.lemvo-last-work-result.json', {'next_task': {'objective': 'Build current candidate'}})
        os.environ['LEMVO_MESSAGE_KIND'] = 'runtime'
        self.prepare()
        self.assertEqual(work.load(self.td/'work-unit.json')['objective'], 'Build current candidate')

    def test_current_guidance_skips_queue_metadata_and_empty_headers(self):
        body = ('<!-- message_id: q-1 -->\n<!-- created: now -->\n'
                '<!-- lemvo_instruction: {"supersedes": ["old"]} -->\n## Guidance\n#\n'
                'Implement the new estimator, not the old selector.\nKeep current resource limits.')
        Path('steer.md').write_text(body, encoding='utf-8')
        self.input.write_text('Runtime rules\n' + body, encoding='utf-8')
        os.environ['LEMVO_MESSAGE_KIND'] = 'user'
        os.environ['LEMVO_CURRENT_MESSAGE_PATH'] = 'steer.md'
        self.prepare()
        self.assertEqual(work.load(self.td/'work-unit.json')['objective'],
                         'Implement the new estimator, not the old selector.')
        self.assertIn(body, self.output.read_text(encoding='utf-8'))

    def test_combined_legacy_receipt_and_guidance_indexes_guidance(self):
        body = ('以下后台作业已出结果（回执，未消费）：\n- old: status=succeeded\n'
                '<!-- message_id: q-2 -->\nImplement the new estimator, not the old selector.\n')
        Path('steer.md').write_text(body, encoding='utf-8')
        os.environ['LEMVO_MESSAGE_KIND'] = 'user'
        os.environ['LEMVO_CURRENT_MESSAGE_PATH'] = 'steer.md'
        self.prepare()
        self.assertEqual(work.load(self.td/'work-unit.json')['objective'],
                         'Implement the new estimator, not the old selector.')

    def test_markdown_heading_with_real_instruction_is_not_discarded(self):
        self.assertEqual(work.guidance_first_line('# Implement the alternate estimator now'),
                         'Implement the alternate estimator now')

    def test_task_index_uses_latest_frozen_message_without_dropping_prior_authority(self):
        body = ('<!-- message_id: q-1 -->\nNever expand the authorized GPU list.\n'
                '<!-- message_id: q-2 -->\n<!-- lemvo_instruction: {"supersedes": ["old-method"]} -->\n'
                'Implement the revised estimator.\n')
        Path('steer.md').write_text(body, encoding='utf-8')
        self.input.write_text(body, encoding='utf-8')
        os.environ['LEMVO_MESSAGE_KIND'] = 'user'
        os.environ['LEMVO_CURRENT_MESSAGE_PATH'] = 'steer.md'
        self.prepare()
        self.assertEqual(work.load(self.td/'work-unit.json')['objective'], 'Implement the revised estimator.')
        self.assertIn('Never expand the authorized GPU list.', self.output.read_text(encoding='utf-8'))

    def test_adopt_existing_sid_does_not_delete_it(self):
        Path('.autoloop-session-id').write_text('legacy-session')
        plan = self.prepare()
        self.assertEqual(plan['session_action'], 'resume')
        self.assertEqual(plan['reason'], 'adopt-existing-session')
        self.assertEqual(Path('.autoloop-session-id').read_text(), 'legacy-session')

    def test_same_working_unit_resumes(self):
        self.previous()
        self.assertEqual(self.prepare()['session_action'], 'resume')

    def test_phase_boundary_fresh(self):
        self.previous(phase='ideation')
        self.assertEqual(self.prepare()['reason'], 'phase-changed')
        self.assertEqual(Path('.autoloop-session-id').read_text(), 'old-sid')

    def test_branch_boundary_fresh(self):
        self.previous(branch='other-method')
        self.assertEqual(self.prepare()['reason'], 'method-branch-changed')

    def test_session_window_fresh_and_configurable(self):
        self.previous(successful_turns=12)
        self.assertEqual(self.prepare()['reason'], 'session-working-set-window')
        self.write('.lemvo-context-policy.json', {'max_session_turns': 20})
        self.assertEqual(self.prepare()['session_action'], 'resume')

    def test_explicit_resume_and_fresh_modes(self):
        self.previous(phase='old', successful_turns=100)
        self.write('.lemvo-context-policy.json', {'mode': 'resume'})
        self.assertEqual(self.prepare()['reason'], 'configured-resume')
        self.write('.lemvo-context-policy.json', {'mode': 'fresh'})
        self.assertEqual(self.prepare()['reason'], 'configured-fresh')

    def test_cumulative_usage_cannot_trigger_context_rotation(self):
        self.previous(cumulative_input_tokens=10000000,
                      last_request_context={'scope': 'session', 'input_tokens': 9999999})
        self.assertEqual(self.prepare()['session_action'], 'resume')
        self.previous(last_request_context={'scope': 'per_request', 'input_tokens': 140000})
        self.assertEqual(self.prepare()['reason'], 'observed-request-context-size')

    def test_invalid_optional_policy_is_compatible(self):
        Path('.lemvo-context-policy.json').write_text('half JSON {')
        self.assertEqual(self.prepare()['session_action'], 'fresh')
        self.write('.lemvo-context-policy.json', {'mode': 'nonsense', 'max_session_turns': -1})
        self.assertEqual(self.prepare()['policy']['max_session_turns'], 12)

    def test_missing_research_metadata_does_not_block(self):
        Path('research-state.yaml').unlink()
        self.prepare()
        self.assertEqual(work.load(self.td/'work-unit.json')['kind'], 'bootstrap')
        self.assertFalse(Path('BLOCKED-USER').exists())

    def test_fresh_packet_preserves_evidence_scope_and_authority_sources(self):
        Path('CHARTER.md').write_text('Do not use GPU0. No external downloads.', encoding='utf-8')
        Path('findings.md').write_text('EXPLORATORY_POSITIVE: macro +3.97 under config A.\n'
                                       'CONFIRMATION_NOT_ESTABLISHED: seed43, not a family verdict.', encoding='utf-8')
        self.prepare()
        prompt = self.output.read_text(encoding='utf-8')
        self.assertIn('Do not use GPU0', prompt)
        self.assertIn('macro +3.97 under config A', prompt)
        self.assertIn('seed43, not a family verdict', prompt)

    def test_long_source_is_marked_excerpt_and_original_is_not_modified(self):
        raw = 'AUTHORITY-BEGIN\n' + 'x' * 20000 + '\nLATEST POSITIVE config A'
        Path('CHARTER.md').write_text(raw, encoding='utf-8')
        self.prepare()
        prompt = self.output.read_text(encoding='utf-8')
        self.assertIn('EXCERPT ONLY', prompt)
        self.assertIn('AUTHORITY-BEGIN', prompt)
        self.assertIn('LATEST POSITIVE config A', prompt)
        self.assertNotIn('x' * 10000, prompt)
        self.assertEqual(Path('CHARTER.md').read_text(encoding='utf-8'), raw)

    def test_large_file_equal_size_middle_update_changes_stat_sample_fingerprint(self):
        p = Path('findings.md')
        p.write_bytes(b'A' * (2 * 1024 * 1024 + 1000))
        before = work.file_record(p)
        previous = p.stat()
        with p.open('r+b') as f:
            f.seek(1024 * 1024)
            f.write(b'B' * 100)
        os.utime(str(p), (previous.st_atime, previous.st_mtime + 1))
        after = work.file_record(p)
        self.assertEqual(before['bytes'], after['bytes'])
        self.assertNotEqual(before['fingerprint'], after['fingerprint'])
        self.assertEqual(after['fingerprint_basis'], 'stat-size-head-tail-sample')
        self.assertTrue(after['excerpt_truncated'])

    def test_resume_only_tracks_unchanged_file_not_repeat_body(self):
        Path('findings.md').write_text('Known scoped positive ABC123')
        self.previous()
        self.prepare()
        Path('.autoloop-session-id').write_text('old-sid')
        work.settle(self.td, 0)
        self.prepare(Path('turns/t2'))
        prompt = self.output.read_text(encoding='utf-8')
        self.assertIn('(none among tracked memory files)', prompt)
        self.assertNotIn('Known scoped positive ABC123', prompt)

    def test_jobs_identity_is_read_only(self):
        jobs = [{'run_id': 'do-not-repeat', 'pid': 1122, 'pid_start': 'x', 'status': 'running'}]
        self.write('.lemvo-jobs.json', jobs)
        self.prepare()
        self.assertEqual(work.load('.lemvo-jobs.json'), jobs)
        self.assertEqual(work.load(self.td/'work-unit.json')['jobs'][0]['run_id'], 'do-not-repeat')

    def test_explicit_result_metrics_and_next_task_retained(self):
        self.prepare()
        self.write('metrics.json', {'accuracy': {'value': .91, 'unit': 'fraction', 'split': 'dev'}})
        work.report(self.td, 'completed', 'Built candidate', 'Improve sampler', 'implement',
                    ['method.py'], 'metrics.json', 'Only config A, dev split')
        r = work.settle(self.td, 0)
        self.assertEqual(r['metrics']['accuracy']['value'], .91)
        self.assertEqual(r['evidence_scope'], 'Only config A, dev split')
        self.assertEqual(r['next_task']['objective'], 'Improve sampler')
        plan = self.prepare(Path('turns/t2'))
        self.assertEqual(work.load(Path('turns/t2/work-unit.json'))['objective'], 'Improve sampler')

    def test_legacy_no_report_uses_actual_terminal_message(self):
        self.prepare()
        self.write(self.td/'progress.json', {'changes': [{'path': 'method.py'}], 'progress': True})
        (self.td/'engine-stdout.jsonl').write_text(json.dumps({'type': 'item.completed',
            'item': {'type': 'agent_message', 'text': 'Built sampler; metric not measured yet.'}}) + '\n')
        r = work.settle(self.td, 0)
        self.assertEqual(r['status'], 'partial')
        self.assertEqual(r['metrics'], [])
        self.assertIn('method.py', r['artifact_refs'])
        self.assertIn('not measured yet', r['summary'])

    def test_claude_fallback_reads_result_not_reasoning(self):
        self.prepare()
        f = self.td/'engine-stdout.jsonl'
        f.write_text(json.dumps({'type': 'assistant', 'text': 'private reasoning'})+'\n'+
                     json.dumps({'type': 'result', 'result': 'Implementation is ready'})+'\n')
        r = work.settle(self.td, 0)
        self.assertEqual(r['summary'], 'Implementation is ready')

    def test_waiting_fallback_is_not_scientific_failure(self):
        self.prepare()
        self.write(self.td/'result.json', {'next_action': 'waiting_jobs'})
        r = work.settle(self.td, 0)
        self.assertEqual(r['status'], 'waiting')
        self.assertEqual(r['metrics'], [])

    def test_settle_idempotent_even_after_other_turn(self):
        self.previous()
        self.prepare()
        work.settle(self.td, 0)
        second = Path('turns/t2')
        self.prepare(second)
        work.report(second, 'partial', 'Latest valid work')
        work.settle(second, 0)
        before = work.load('.lemvo-context.json')
        work.settle(self.td, 0)
        self.assertEqual(work.load('.lemvo-context.json'), before)
        self.assertEqual(work.load('.lemvo-last-work-result.json')['summary'], 'Latest valid work')

    def test_failure_retains_old_sid_and_successful_handoff(self):
        self.previous()
        self.write('.lemvo-last-work-result.json', {'status': 'partial', 'summary': 'prior useful positive'})
        self.prepare()
        self.write(self.td/'work-result.json', {'status': 'completed', 'summary': 'unconfirmed output'})
        old = work.load('.lemvo-context.json')
        r = work.settle(self.td, 143)
        self.assertEqual(r['status'], 'failed')
        self.assertEqual(work.load('.lemvo-context.json'), old)
        self.assertEqual(Path('.autoloop-session-id').read_text(), 'old-sid')
        self.assertEqual(work.load('.lemvo-last-work-result.json')['summary'], 'prior useful positive')

    def test_failed_runtime_cannot_increment_even_if_rc_omitted(self):
        self.previous()
        self.prepare()
        self.write(self.td/'result.json', {'status': 'failed', 'engine_rc': 9})
        old = work.load('.lemvo-context.json')
        self.assertEqual(work.settle(self.td)['status'], 'failed')
        self.assertEqual(work.load('.lemvo-context.json'), old)

    def test_old_epoch_not_adopted_on_recovery(self):
        self.previous()
        self.prepare()
        Path('.lemvo-session-epoch').write_text('current')
        self.write(self.td/'manifest.json', {'session_epoch': 'purged-old'})
        old = work.load('.lemvo-context.json')
        self.assertEqual(work.settle(self.td)['status'], 'failed')
        self.assertEqual(work.load('.lemvo-context.json'), old)

    def test_older_dispatch_recovered_after_newer_cannot_replace_handoff(self):
        self.previous()
        for tid, goal, started in [('older', 'OLD NEXT TASK', '2026-11-16T01:00:00Z'),
                                   ('newer', 'NEW NEXT TASK', '2026-11-16T02:00:00Z')]:
            td = Path('turns')/tid
            self.prepare(td)
            self.write(td/'manifest.json', {'session_epoch': 'legacy', 'started_utc': started})
            self.write(td/'result.json', {'status': 'succeeded', 'engine_rc': 0})
            work.report(td, 'completed', 'finished '+tid, next_objective=goal)
        work.settle('turns/newer', 0)
        context = work.load('.lemvo-context.json')
        recovered = work.settle('turns/older', 0)
        self.assertEqual(work.load('.lemvo-last-work-result.json')['turn_id'], 'newer')
        self.assertEqual(work.load('.lemvo-last-work-result.json')['next_task']['objective'], 'NEW NEXT TASK')
        self.assertEqual(work.load('.lemvo-context.json'), context)
        self.assertIn('Older dispatched', recovered['runtime_notice'])
        self.assertFalse(work.load('turns/older/work-settlement.json')['promoted'])

    def test_order_guard_works_without_sid_or_context_file(self):
        for tid, started in [('a', '2026-11-16T01:00:00Z'), ('b', '2026-11-16T02:00:00Z')]:
            td = Path('turns')/tid
            td.mkdir(parents=True)
            self.write(td/'manifest.json', {'session_epoch': 'legacy', 'started_utc': started})
            work.report(td, 'completed', tid, next_objective=tid+' next')
        work.settle('turns/b')
        work.settle('turns/a')
        self.assertEqual(work.load('.lemvo-last-work-result.json')['summary'], 'b')
        self.assertFalse(Path('.lemvo-context.json').exists())

    def test_legacy_no_manifest_uses_existing_file_time_not_recovery_time(self):
        for tid, modified in [('legacy-old', 1000), ('legacy-new', 2000)]:
            td = Path('turns')/tid
            td.mkdir(parents=True)
            self.write(td/'result.json', {'status': 'succeeded', 'engine_rc': 0})
            os.utime(str(td/'result.json'), (modified, modified))
            work.report(td, 'completed', tid)
        work.settle('turns/legacy-new')
        work.settle('turns/legacy-old')
        self.assertEqual(work.load('.lemvo-last-work-result.json')['summary'], 'legacy-new')
        self.assertEqual(work.load('turns/legacy-old/work-result.json')['dispatch_order']['basis'],
                         'legacy-file-mtime:result.json')

    def test_first_legacy_turn_without_manifest_is_still_accepted(self):
        td = Path('turns/legacy-first')
        td.mkdir(parents=True)
        work.report(td, 'partial', 'Useful existing engineering work')
        work.settle(td)
        self.assertEqual(work.load('.lemvo-last-work-result.json')['summary'], 'Useful existing engineering work')

    def test_manifest_order_has_priority_over_recovery_written_plan(self):
        old = Path('turns/old-with-new-plan')
        new = Path('turns/current')
        self.prepare(new)
        self.write(new/'manifest.json', {'session_epoch': 'legacy', 'started_utc': '2026-11-16T02:00:00Z'})
        work.report(new, 'completed', 'current work')
        work.settle(new)
        self.prepare(old)  # a late import must not outrank the original dispatch
        self.write(old/'manifest.json', {'session_epoch': 'legacy', 'started_utc': '2026-11-16T01:00:00Z'})
        work.report(old, 'completed', 'older work')
        work.settle(old)
        self.assertEqual(work.load('.lemvo-last-work-result.json')['summary'], 'current work')

    def test_success_count_starts_new_session_without_touching_budget(self):
        self.previous(successful_turns=12)
        self.prepare()
        Path('.autoloop-session-id').write_text('new-sid')
        state = Path('research-state.yaml').read_bytes()
        work.settle(self.td, 0)
        self.assertEqual(work.load('.lemvo-context.json')['successful_turns'], 1)
        self.assertEqual(work.load('.lemvo-context.json')['sid'], 'new-sid')
        self.assertEqual(Path('research-state.yaml').read_bytes(), state)

    def test_only_explicit_per_request_observation_is_recorded(self):
        self.prepare()
        Path('.autoloop-session-id').write_text('sid-real')
        self.write(self.td/'request-context.json', {'scope': 'session', 'input_tokens': 999999})
        work.settle(self.td)
        self.assertNotIn('last_request_context', work.load('.lemvo-context.json'))
        td2 = Path('turns/t2')
        self.prepare(td2)
        self.write(td2/'request-context.json', {'scope': 'per_request', 'input_tokens': 150000})
        work.settle(td2)
        self.assertEqual(work.load('.lemvo-context.json')['last_request_context']['input_tokens'], 150000)

    def test_service_and_protocol_errors_never_promote_handoff_on_recovery(self):
        for status in ('service-error', 'protocol-error', 'config-error', 'failed', 'unknown-error'):
            td = Path('turns/error-' + status)
            self.previous()
            self.prepare(td)
            self.write('.lemvo-last-work-result.json', {'summary': 'prior useful result'})
            self.write(td/'result.json', {'status': status, 'engine_rc': 0})
            before = work.load('.lemvo-context.json')
            r = work.settle(td, 0)
            self.assertEqual(r['status'], 'failed')
            self.assertEqual(work.load('.lemvo-context.json'), before)
            self.assertEqual(work.load('.lemvo-last-work-result.json')['summary'], 'prior useful result')

    def test_fresh_without_new_sid_does_not_adopt_old_sid_as_new_context(self):
        self.previous(successful_turns=12)
        self.prepare()
        before = work.load('.lemvo-context.json')
        work.report(self.td, 'partial', 'Useful code; identity not reported')
        r = work.settle(self.td, 0)
        self.assertEqual(work.load('.lemvo-context.json'), before)
        self.assertIn('identity', r['session_notice'])
        self.assertEqual(work.load('.lemvo-last-work-result.json')['summary'], 'Useful code; identity not reported')

    def test_codex_observation_uses_last_request_not_total_or_turn_aggregate(self):
        p = self.td/'engine-stdout.jsonl'
        events = [
            {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {
                'total_token_usage': {'input_tokens': 9999999},
                'last_token_usage': {'input_tokens': 140000}}}},
            {'type': 'turn.completed', 'usage': {'input_tokens': 99999999}}
        ]
        p.write_text('\n'.join(json.dumps(e) for e in events))
        observation = work.observe_request_context(p)
        self.assertEqual(observation['input_tokens'], 140000)
        self.assertEqual(observation['scope'], 'per_request')

    def test_claude_observation_uses_per_message_all_three_input_classes(self):
        p = self.td/'engine-stdout.jsonl'
        events = [
            {'type': 'assistant', 'message': {'usage': {'input_tokens': 100,
                'cache_read_input_tokens': 20000, 'cache_creation_input_tokens': 300}}},
            {'type': 'stream_event', 'event': {'type': 'message_start', 'message': {'usage': {
                'input_tokens': 200, 'cache_read_input_tokens': 30000, 'cache_creation_input_tokens': 400}}}},
            {'type': 'result', 'usage': {'input_tokens': 9999999, 'cache_read_input_tokens': 999999999}}
        ]
        p.write_text('\n'.join(json.dumps(e) for e in events))
        self.assertEqual(work.observe_request_context(p)['input_tokens'], 30600)

    def test_aggregate_only_usage_is_unknown_context(self):
        p = self.td/'engine-stdout.jsonl'
        p.write_text(json.dumps({'type': 'result', 'usage': {'input_tokens': 99999999}}))
        self.assertIsNone(work.observe_request_context(p))
        p.write_text(json.dumps({'type': 'assistant', 'message': {'usage': {'input_tokens': 100}}}))
        self.assertIsNone(work.observe_request_context(p))

    def test_settle_automatically_records_actual_request_context(self):
        self.previous()
        self.prepare()
        p = self.td/'engine-stdout.jsonl'
        p.write_text(json.dumps({'type': 'assistant', 'message': {'usage': {'input_tokens': 1000,
            'cache_read_input_tokens': 130000, 'cache_creation_input_tokens': 0}}}))
        work.settle(self.td, 0)
        self.assertEqual(work.load('.lemvo-context.json')['last_request_context']['input_tokens'], 131000)
        self.assertEqual(self.prepare(Path('turns/t2'))['reason'], 'observed-request-context-size')

    def test_huge_handoff_remains_on_disk_but_prompt_projection_is_bounded(self):
        raw = {'status': 'partial', 'summary': 'positive only under A',
               'metrics': [{'setting': 'a'*10000, 'value': .91}] * 100}
        self.write('.lemvo-last-work-result.json', raw)
        self.prepare()
        self.assertLess(len(self.output.read_text(encoding='utf-8')), 25000)
        self.assertIn('Large handoff projected', self.output.read_text(encoding='utf-8'))
        self.assertEqual(work.load('.lemvo-last-work-result.json'), raw)


if __name__ == '__main__':
    unittest.main()
