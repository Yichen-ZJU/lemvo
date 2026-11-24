"""CPU-only tests for Lemvo v2.1 strategy engine, plateau advisory/gate, and anti-incrementalism."""
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest


HERE = Path(__file__).resolve().parent
TPL_DIR = HERE.parent / 'skills/research-orchestrator/templates'
if not TPL_DIR.is_dir():
    TPL_DIR = HERE
STRATEGY_PATH = TPL_DIR / 'research-strategy.py'
WORK_UNIT_PATH = TPL_DIR / 'work-unit.py'
JOB_EVENTS_PATH = TPL_DIR / 'job-events.py'


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


strategy = _load(STRATEGY_PATH, 'research_strategy_test_mod')
work = _load(WORK_UNIT_PATH, 'work_unit_strategy_test_mod')


class StrategyEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cwd = Path.cwd()
        self.root = Path(self.tmp.name)
        os.chdir(self.root)
        self.saved_env = {
            k: os.environ.get(k)
            for k in (
                'LEMVO_MESSAGE_KIND', 'LEMVO_CURRENT_MESSAGE_PATH',
                'LEMVO_MACRO_REVIEW_THRESHOLD', 'LEMVO_MACRO_REVIEW_MODE',
                'LEMVO_RESOURCE_WAKE',
            )
        }
        for k in self.saved_env:
            os.environ.pop(k, None)

    def tearDown(self):
        os.chdir(self.old_cwd)
        for k, v in self.saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def test_legacy_lemvo138_style_log_inference_saturation_and_dup(self):
        (self.root / 'logs').mkdir()
        rows = [
            {'exp_id': 'pairmargin_matched_arms', 'cycle': 1, 'outcome': 'DISCARD crashed traceback', 'metric': None},
            {'exp_id': 'steer_vec_v4', 'cycle': 4, 'outcome': 'DISCARD', 'metric': 0.325},
            {'exp_id': 'selective_v2_bleedrank_armselv2', 'cycle': 6, 'outcome': 'NULL', 'metric': 0.6111},
            {'exp_id': 'selective_v7b_budget2x_armselv7b', 'cycle': 12, 'outcome': 'PROMISING', 'metric': 0.6278},
            {'exp_id': 'selective_v7c_seedvar_armselv7c', 'cycle': 13, 'outcome': 'BUDGET LEVER CONFIRMED', 'metric': 0.6361, 'detail': 'paired vs armC +4.17pp CI [+0.28,+8.01] EXCLUDES 0'},
            {'exp_id': 'selective_v7d_budget3x_armselv7d', 'cycle': 14, 'outcome': 'SATURATING', 'metric': 0.6333},
            {'exp_id': 'selective_v9b_antibleed_all_armselv9b', 'cycle': 16, 'outcome': 'STRONG NEGATIVE', 'metric': 0.1472},
            {'exp_id': 'selective_v9d_antibleed_lex_l03_armselv9d', 'cycle': 17, 'outcome': 'NULL', 'metric': 0.5833},
            {'exp_id': 'selective_v9c_antibleed_lex_l01_armselv9c', 'cycle': 17, 'outcome': 'NULL on primary', 'metric': 0.6194},
            {'exp_id': 'selective_v10e_soup3_l00_armselv10e', 'cycle': 20, 'outcome': 'REJECTED', 'metric': 0.5778},
            {'exp_id': 'selective_v11b_budget2x_s17_armselv11b', 'cycle': 21, 'outcome': 'RECORD but same-config replicate of v7c_seedvar', 'metric': 0.6389, 'family_status': 'closed', 'detail': 'duplicate-of-v7c; Loss-level mechanism families CLOSED'},
        ]
        (self.root / 'logs/experiments.jsonl').write_text('\n'.join(json.dumps(r) for r in rows) + '\n', encoding='utf-8')
        snap = strategy.analyze(self.root)
        self.assertEqual(snap['tier_counts']['T3'], 1)
        self.assertEqual(snap['tier_counts']['T2'], 1)
        self.assertEqual(snap['families']['selective']['status'], 'CLOSED')
        self.assertEqual(snap['families']['steer_vec']['status'], 'INCUBATING')
        self.assertEqual(snap['incremental_streak'], 6)
        self.assertTrue(snap['macro_review_armed'])
        self.assertTrue(any(d['exp_id'] == 'selective_v11b_budget2x_s17_armselv11b' for d in snap['duplicate_warnings']))

    def test_structured_check_dup_blocks_identical_config_and_allow_override(self):
        r1 = subprocess.run(
            [sys.executable, str(STRATEGY_PATH), 'log-experiment', '--root', str(self.root),
             '--exp-id', 'run_a', '--family', 'focal', '--tier', 'T1',
             '--config-json', '{"lr": 1e-4, "gamma": 2.0}', '--metric', '0.70', '--outcome', 'KEEP'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
        )
        self.assertEqual(r1.returncode, 0, r1.stderr)
        dup = subprocess.run(
            [sys.executable, str(STRATEGY_PATH), 'check-dup', '--root', str(self.root),
             '--family', 'focal', '--config-json', '{"gamma": 2.0, "lr": 1e-4}'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
        )
        self.assertEqual(dup.returncode, 2)
        self.assertEqual(json.loads(dup.stdout)['matched_exp_id'], 'run_a')
        blocked = subprocess.run(
            [sys.executable, str(STRATEGY_PATH), 'log-experiment', '--root', str(self.root),
             '--exp-id', 'run_dup', '--family', 'focal', '--tier', 'T1',
             '--config-json', '{"gamma": 2.0, "lr": 1e-4}', '--metric', '0.71'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
        )
        self.assertEqual(blocked.returncode, 2)
        forced = subprocess.run(
            [sys.executable, str(STRATEGY_PATH), 'log-experiment', '--root', str(self.root),
             '--exp-id', 'run_dup_ok', '--family', 'focal', '--tier', 'T1',
             '--config-json', '{"gamma": 2.0, "lr": 1e-4}', '--metric', '0.71', '--allow-duplicate'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
        )
        self.assertEqual(forced.returncode, 0)
        snap = strategy.analyze(self.root)
        self.assertEqual(len(snap['duplicate_warnings']), 1)

    def test_variant_rejected_preserves_family_incubation_until_explicit_close(self):
        subprocess.run(
            [sys.executable, str(STRATEGY_PATH), 'portfolio-set', '--root', str(self.root),
             '--slot', 'explore', '--family', 'new_branch', '--tier', 'T2',
             '--incubation-budget', '3', '--hypothesis', 'Orthogonal objective'],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
        )
        (self.root / 'logs').mkdir(exist_ok=True)
        rows = [
            {'exp_id': 'nb_1', 'family_id': 'new_branch', 'intervention_tier': 'T2', 'metric': 0.50, 'outcome': 'complete', 'detail': 'implementation works'},
            {'exp_id': 'nb_2', 'family_id': 'new_branch', 'intervention_tier': 'T2', 'metric': 0.49, 'outcome': 'rejected', 'detail': 'this variant rejected; family remains open'},
        ]
        (self.root / 'logs/experiments.jsonl').write_text('\n'.join(json.dumps(r) for r in rows) + '\n', encoding='utf-8')
        snap = strategy.analyze(self.root)
        self.assertEqual(snap['families']['new_branch']['status'], 'INCUBATING')
        self.assertEqual(snap['families']['new_branch']['incubation_remaining'], 1)
        self.assertTrue(snap['portfolio']['slots']['explore']['incubation_protected'])

        # Explicit family-level closure closes the family and drops incubation protection
        r_close = subprocess.run(
            [sys.executable, str(STRATEGY_PATH), 'log-experiment', '--root', str(self.root),
             '--exp-id', 'nb_3', '--family', 'new_branch', '--tier', 'T2',
             '--metric', '0.48', '--outcome', 'REJECTED', '--family-status', 'closed',
             '--detail', 'Formal family retirement after incubation diagnostic'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
        )
        self.assertEqual(r_close.returncode, 0, r_close.stderr)
        snap_closed = strategy.analyze(self.root)
        self.assertEqual(snap_closed['families']['new_branch']['status'], 'CLOSED')
        self.assertFalse(snap_closed['portfolio']['slots']['explore']['incubation_protected'])

    def test_breakthrough_direction_and_uncalibrated_noise_guard(self):
        # Negative delta + adverse CI + rejected must NEVER be breakthrough even if significant=True
        self.assertFalse(strategy.is_breakthrough(
            {'metric': 0.45, 'paired_ci': [-0.06, -0.04], 'outcome': 'rejected', 'significant': True},
            0.50, 0.01, direction='maximize',
        ))
        self.assertFalse(strategy.is_breakthrough(
            {'metric': 0.45, 'significant': True},
            0.50, 0.01, direction='maximize',
        ))
        # Tiny positive improvement without noise_band or CI/significance is NOT breakthrough
        self.assertFalse(strategy.is_breakthrough(
            {'metric': 0.5000001, 'outcome': 'KEEP'},
            0.50, None, direction='maximize',
        ))
        # Minimize direction: lower metric with negative CI is breakthrough; higher metric is not
        self.assertTrue(strategy.is_breakthrough(
            {'metric': 0.38, 'paired_ci': [-0.14, -0.09], 'outcome': 'KEEP', 'significant': True},
            0.50, 0.02, direction='minimize',
        ))
        self.assertFalse(strategy.is_breakthrough(
            {'metric': 0.55, 'paired_ci': [0.02, 0.08], 'outcome': 'rejected', 'significant': True},
            0.50, 0.02, direction='minimize',
        ))

    def test_noise_band_in_band_gain_does_not_reset_streak_but_breakthrough_does(self):
        (self.root / '.lemvo-strategy-policy.json').write_text(
            json.dumps({'macro_review_threshold': 4, 'noise_band': 0.03, 'direction': 'maximize'}),
            encoding='utf-8',
        )
        (self.root / 'logs').mkdir()
        in_band = [
            {'exp_id': 'base_baseline', 'family_id': 'ce', 'intervention_tier': 'T0', 'metric': 0.80, 'outcome': 'baseline'},
            {'exp_id': 'ce_t1_1', 'family_id': 'ce', 'intervention_tier': 'T1', 'metric': 0.805, 'outcome': 'KEEP minor'},
            {'exp_id': 'ce_t1_2', 'family_id': 'ce', 'intervention_tier': 'T1', 'metric': 0.810, 'outcome': 'KEEP minor'},
            {'exp_id': 'ce_t1_3', 'family_id': 'ce', 'intervention_tier': 'T1', 'metric': 0.812, 'outcome': 'KEEP minor'},
            {'exp_id': 'ce_t1_4', 'family_id': 'ce', 'intervention_tier': 'T1', 'metric': 0.815, 'outcome': 'KEEP minor'},
        ]
        (self.root / 'logs/experiments.jsonl').write_text('\n'.join(json.dumps(r) for r in in_band) + '\n', encoding='utf-8')
        snap = strategy.analyze(self.root)
        self.assertEqual(snap['incremental_streak'], 4)
        self.assertEqual(snap['streak_since_last_review'], 4)
        self.assertTrue(snap['macro_review_armed'])

        # Adding a CI-excluding-0 breakthrough resets the streak
        with (self.root / 'logs/experiments.jsonl').open('a', encoding='utf-8') as fh:
            fh.write(json.dumps({'exp_id': 'ce_t1_sig', 'family_id': 'ce', 'intervention_tier': 'T1', 'metric': 0.819, 'ci_low': 0.01, 'ci_high': 0.04, 'outcome': 'KEEP'}) + '\n')
        snap2 = strategy.analyze(self.root)
        self.assertEqual(snap2['incremental_streak'], 0)
        self.assertFalse(snap2['macro_review_armed'])

    def test_default_advisory_preserves_planned_confirmations_and_full_cooldown(self):
        (self.root / '.lemvo-strategy-policy.json').write_text(
            json.dumps({'macro_review_threshold': 3, 'noise_band': 0.05}), encoding='utf-8'
        )
        Path('research-state.yaml').write_text(
            'project:\n  objective: Estimate fixed protocol variability\n'
            'prep:\n  next_min_probe: Complete the planned seed study\n',
            encoding='utf-8',
        )
        Path('inp.md').write_text('runtime tick', encoding='utf-8')
        (self.root / 'logs').mkdir()
        rows = [
            {'exp_id': 'seed0', 'family_id': 'base', 'intervention_tier': 'T0', 'metric': 0.50, 'outcome': 'completed'},
            {'exp_id': 'seed1', 'family_id': 'base', 'intervention_tier': 'T0', 'metric': 0.50, 'outcome': 'completed'},
            {'exp_id': 'seed2', 'family_id': 'base', 'intervention_tier': 'T0', 'metric': 0.50, 'outcome': 'completed'},
            {'exp_id': 'seed3', 'family_id': 'base', 'intervention_tier': 'T0', 'metric': 0.50, 'outcome': 'completed'},
        ]
        (self.root / 'logs/experiments.jsonl').write_text('\n'.join(json.dumps(r) for r in rows) + '\n', encoding='utf-8')

        td1 = Path('turns/t_adv_1')
        td1.mkdir(parents=True)
        plan1 = work.prepare(td1, 'inp.md', td1 / 'out.md', 'claude')
        self.assertFalse(plan1['macro_review_triggered'])
        self.assertTrue(plan1['macro_review_advisory'])
        unit1 = work.load(td1 / 'work-unit.json')
        self.assertEqual(unit1['kind'], 'research')
        self.assertEqual(unit1['objective'], 'Complete the planned seed study')
        self.assertIn('research-orchestrator/references/macro-strategy.md', unit1['relevant_skill_paths'])
        prompt1 = (td1 / 'out.md').read_text(encoding='utf-8')
        self.assertIn('PLATEAU ADVISORY', prompt1)
        self.assertIn('RESEARCH STRATEGY LEDGER', prompt1)

        (td1 / 'result.json').write_text(json.dumps({'status': 'succeeded', 'engine_rc': 0}), encoding='utf-8')
        work.report(td1, 'completed', 'Finished planned seed 3')
        work.settle(td1, 0)

        # Adding 1 flat run must NOT immediately re-arm (cooldown = full threshold 3 new flat runs)
        with (self.root / 'logs/experiments.jsonl').open('a', encoding='utf-8') as fh:
            fh.write(json.dumps({'exp_id': 'seed4', 'family_id': 'base', 'intervention_tier': 'T0', 'metric': 0.50, 'outcome': 'completed'}) + '\n')
        td2 = Path('turns/t_adv_2')
        td2.mkdir(parents=True)
        plan2 = work.prepare(td2, 'inp.md', td2 / 'out.md', 'claude')
        self.assertFalse(plan2['macro_review_triggered'])
        self.assertFalse(plan2['macro_review_advisory'])

        # Adding 2 more flat runs (total 3 since last review) re-arms cleanly
        with (self.root / 'logs/experiments.jsonl').open('a', encoding='utf-8') as fh:
            fh.write(json.dumps({'exp_id': 'seed5', 'family_id': 'base', 'intervention_tier': 'T0', 'metric': 0.50, 'outcome': 'completed'}) + '\n')
            fh.write(json.dumps({'exp_id': 'seed6', 'family_id': 'base', 'intervention_tier': 'T0', 'metric': 0.50, 'outcome': 'completed'}) + '\n')
        td3 = Path('turns/t_adv_3')
        td3.mkdir(parents=True)
        plan3 = work.prepare(td3, 'inp.md', td3 / 'out.md', 'claude')
        self.assertTrue(plan3['macro_review_advisory'])

    def test_opt_in_macro_review_gate_in_work_unit_guards_and_cooldown(self):
        (self.root / '.lemvo-strategy-policy.json').write_text(
            json.dumps({'macro_review_threshold': 3, 'macro_review_mode': 'gate', 'noise_band': 0.05}), encoding='utf-8'
        )
        (self.root / '.lemvo-context.json').write_text(
            json.dumps({'schema': 'lemvo-context-runtime', 'schema_version': 1, 'sid': 'sid-1', 'engine': 'claude', 'successful_turns': 1, 'files': []}),
            encoding='utf-8',
        )
        Path('.autoloop-session-id').write_text('sid-1', encoding='utf-8')
        Path('research-state.yaml').write_text('project:\n  objective: Improve generalization\nprep:\n  next_min_probe: tweak weight 0.12\n', encoding='utf-8')
        (self.root / 'logs').mkdir()
        rows = [
            {'exp_id': 'exp_0_baseline', 'family_id': 'sft', 'intervention_tier': 'T0', 'metric': 0.50, 'outcome': 'baseline'},
            {'exp_id': 'sft_1', 'family_id': 'sft', 'intervention_tier': 'T1', 'metric': 0.51, 'outcome': 'KEEP'},
            {'exp_id': 'sft_2', 'family_id': 'sft', 'intervention_tier': 'T1', 'metric': 0.515, 'outcome': 'KEEP'},
            {'exp_id': 'sft_3', 'family_id': 'sft', 'intervention_tier': 'T1', 'metric': 0.512, 'outcome': 'NULL'},
        ]
        (self.root / 'logs/experiments.jsonl').write_text('\n'.join(json.dumps(r) for r in rows) + '\n', encoding='utf-8')

        # 1. Suppressed when LEMVO_MESSAGE_KIND=user
        os.environ['LEMVO_MESSAGE_KIND'] = 'user'
        Path('u.md').write_text('Run specific user test', encoding='utf-8')
        os.environ['LEMVO_CURRENT_MESSAGE_PATH'] = 'u.md'
        td_u = Path('turns/t_user')
        td_u.mkdir(parents=True)
        plan_u = work.prepare(td_u, 'u.md', td_u / 'out.md', 'claude')
        self.assertFalse(plan_u['macro_review_triggered'])
        os.environ.pop('LEMVO_MESSAGE_KIND', None)
        os.environ.pop('LEMVO_CURRENT_MESSAGE_PATH', None)

        # 2. Suppressed when waiting on active job
        Path('.lemvo-jobs.json').write_text(json.dumps([{'run_id': 'j1', 'status': 'running', 'pid': 999}]), encoding='utf-8')
        td_j = Path('turns/t_job')
        td_j.mkdir(parents=True)
        Path('inp.md').write_text('runtime tick', encoding='utf-8')
        plan_j = work.prepare(td_j, 'inp.md', td_j / 'out.md', 'claude')
        self.assertFalse(plan_j['macro_review_triggered'])
        Path('.lemvo-jobs.json').unlink()

        # 3. Armed & triggers fresh macro_review turn
        td_m = Path('turns/t_macro')
        td_m.mkdir(parents=True)
        plan_m = work.prepare(td_m, 'inp.md', td_m / 'out.md', 'claude')
        self.assertTrue(plan_m['macro_review_triggered'])
        self.assertEqual(plan_m['session_action'], 'fresh')
        self.assertEqual(plan_m['reason'], 'macro-review-plateau')
        unit_m = work.load(td_m / 'work-unit.json')
        self.assertEqual(unit_m['kind'], 'macro_review')
        self.assertEqual(unit_m['objective_source'], 'runtime-macro-review-gate')
        self.assertEqual(unit_m['strategy']['deferred_tactical_next_task'], 'tweak weight 0.12')
        self.assertIn('research-orchestrator/references/macro-strategy.md', unit_m['relevant_skill_paths'])
        prompt_m = (td_m / 'out.md').read_text(encoding='utf-8')
        self.assertIn('RESEARCH STRATEGY LEDGER', prompt_m)
        self.assertIn('OUTER_MACRO_REVIEW', prompt_m)
        self.assertTrue((td_m / 'strategy-snapshot.json').is_file())

        # 4. Settling with rc=0 marks macro review handled & respects full cooldown (+1 run stays unarmed)
        (td_m / 'result.json').write_text(json.dumps({'status': 'succeeded', 'engine_rc': 0}), encoding='utf-8')
        work.report(td_m, 'completed', 'Completed taxonomy and proposed T3 jump', next_objective='Run T3 pilot', next_kind='experiment', tier='T3', family='latent')
        work.settle(td_m, 0)
        self.assertEqual(work.load(td_m / 'work-result.json')['intervention_tier'], 'T3')

        td_next = Path('turns/t_after')
        td_next.mkdir(parents=True)
        plan_after = work.prepare(td_next, 'inp.md', td_next / 'out.md', 'claude')
        self.assertFalse(plan_after['macro_review_triggered'])

        with (self.root / 'logs/experiments.jsonl').open('a', encoding='utf-8') as fh:
            fh.write(json.dumps({'exp_id': 'sft_4', 'family_id': 'sft', 'intervention_tier': 'T1', 'metric': 0.511, 'outcome': 'NULL'}) + '\n')
        td_plus1 = Path('turns/t_plus1')
        td_plus1.mkdir(parents=True)
        plan_plus1 = work.prepare(td_plus1, 'inp.md', td_plus1 / 'out.md', 'claude')
        self.assertFalse(plan_plus1['macro_review_triggered'])

    def test_portfolio_incubation_and_failure_taxonomy_surfacing(self):
        subprocess.run(
            [sys.executable, str(STRATEGY_PATH), 'portfolio-set', '--root', str(self.root),
             '--slot', 'explore', '--family', 'latent_ctrl', '--tier', 'T3',
             '--incubation-budget', '3', '--hypothesis', 'Activation space routing'],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
        )
        subprocess.run(
            [sys.executable, str(STRATEGY_PATH), 'record-failure-taxonomy', '--root', str(self.root),
             '--turn-id', 't_tax_1', '--summary', 'Bleed errors dominate 60%',
             '--modes-json', json.dumps([{'mode': 'edit_vocab_bleed', 'count': 18}])],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
        )
        snap = strategy.analyze(self.root)
        self.assertTrue(snap['portfolio']['slots']['explore']['incubation_protected'])
        self.assertEqual(snap['latest_failure_taxonomy']['turn_id'], 't_tax_1')
        text = strategy.render_ledger_text(snap)
        self.assertIn('INCUBATION_PROTECTED', text)
        self.assertIn('Bleed errors dominate 60%', text)

    def test_zero_regression_without_history_files(self):
        td = Path('turns/t_empty')
        td.mkdir(parents=True)
        Path('input.md').write_text('Bootstrap check', encoding='utf-8')
        Path('research-state.yaml').write_text('prep:\n  next_min_probe: Run initial task\n', encoding='utf-8')
        plan = work.prepare(td, 'input.md', td / 'prompt.md', 'codex')
        self.assertFalse(plan['macro_review_triggered'])
        unit = work.load(td / 'work-unit.json')
        self.assertNotIn('strategy', unit)
        self.assertNotIn('RESEARCH STRATEGY LEDGER', (td / 'prompt.md').read_text(encoding='utf-8'))

    def test_python36_compatibility_static_guard(self):
        forbidden = [
            r'from __future__ import annotations',
            r'\bcapture_output\b',
            r'\btext\s*=',
            r'\bfromisoformat\b',
        ]
        for path in (STRATEGY_PATH, WORK_UNIT_PATH, JOB_EVENTS_PATH):
            src = path.read_text(encoding='utf-8')
            for pat in forbidden:
                self.assertIsNone(re.search(pat, src), '%s matched forbidden py3.6 pattern %s' % (path.name, pat))

    def test_v22_cross_metric_conditional_close_t2_escalation_and_tsv_consolidation(self):
        (self.root / 'logs').mkdir(parents=True, exist_ok=True)
        # 1) Secondary minimize metric must not flip primary accuracy direction
        cross_rows = [
            {'exp_id': 'b', 'metric': 0.80, 'intervention_tier': 'T0', 'family_id': 'cand', 'metric_name': 'accuracy'},
            {'exp_id': 'l', 'metric': 60, 'intervention_tier': 'T1', 'family_id': 'cand', 'metric_name': 'latency_ms', 'direction': 'minimize', 'significant': True},
            {'exp_id': 'c', 'metric': 0.90, 'intervention_tier': 'T1', 'family_id': 'cand', 'metric_name': 'accuracy', 'paired_ci': [0.08, 0.12]},
        ]
        (self.root / 'logs/experiments.jsonl').write_text('\n'.join(json.dumps(r) for r in cross_rows) + '\n', encoding='utf-8')
        snap = strategy.analyze(self.root)
        self.assertAlmostEqual(snap['anchor_best'], 0.90)
        self.assertEqual(snap['families']['cand']['breakthroughs'], 1)
        self.assertEqual(snap['incremental_streak'], 0)

        # 2) Generic child metric inherits primary metric_name & secondary NULL does not veto primary positive CI
        pos_rows = [
            {'exp_id': 'base', 'metric': 0.50, 'intervention_tier': 'T0', 'family_id': 'cand', 'metric_name': 'accuracy'},
            {'exp_id': 'new', 'metric': 0.55, 'intervention_tier': 'T1', 'family_id': 'cand', 'paired_ci': [0.01, 0.08], 'outcome': 'primary improved vs matched comparator; secondary endpoint NULL'},
        ]
        (self.root / 'logs/experiments.jsonl').write_text('\n'.join(json.dumps(r) for r in pos_rows) + '\n', encoding='utf-8')
        snap = strategy.analyze(self.root)
        self.assertAlmostEqual(snap['anchor_best'], 0.55)
        self.assertEqual(snap['families']['cand']['breakthroughs'], 1)

        # 3) Conditional blocked prose does not close family or count completed run
        cond_rows = [
            {'exp_id': 'worker', 'family_id': 'cand', 'status': 'blocked', 'outcome': 'blocked', 'detail': 'Before declaring the family closed, repair the parser.'},
        ]
        (self.root / 'logs/experiments.jsonl').write_text('\n'.join(json.dumps(r) for r in cond_rows) + '\n', encoding='utf-8')
        snap = strategy.analyze(self.root)
        self.assertEqual(snap['completed_experiments'], 0)
        self.assertEqual(snap['families']['cand']['status'], 'ACTIVE')

        # 4) T2 series past incubation saturates and increments incremental_streak
        t2_rows = [{'exp_id': 'base', 'metric': 0.50, 'intervention_tier': 'T0', 'family_id': 'cand', 'metric_name': 'accuracy'}]
        t2_rows += [{'exp_id': 'w_%d' % i, 'metric': 0.48, 'intervention_tier': 'T2', 'family_id': 'cand', 'outcome': 'NULL'} for i in range(12)]
        (self.root / 'logs/experiments.jsonl').write_text('\n'.join(json.dumps(r) for r in t2_rows) + '\n', encoding='utf-8')
        snap = strategy.analyze(self.root)
        self.assertEqual(snap['incremental_streak'], 10)
        self.assertTrue(snap['macro_review_armed'])
        self.assertEqual(snap['families']['cand']['status'], 'SATURATED')

        # 5) TSV merged with non-overlapping DISPATCHED jsonl keeps completed TSV trials
        (self.root / 'logs/experiments.jsonl').write_text(json.dumps({'exp_id': 'new', 'outcome': 'DISPATCHED'}) + '\n', encoding='utf-8')
        tsv = 'iteration\ttimestamp\tcommit\tmetric\tdelta\tguard\tstatus\tmove\tdescription\n' + ''.join(
            '%d\t2026-11-22\tabc\t0.5\t0\tok\tKEEP\tfollow\t[T2] [family=hello]\n' % i for i in range(1, 7)
        )
        (self.root / 'results.tsv').write_text(tsv, encoding='utf-8')
        snap = strategy.analyze(self.root)
        self.assertEqual(snap['completed_experiments'], 6)
        self.assertAlmostEqual(snap['anchor_best'], 0.5)
        self.assertEqual(snap['families']['hello']['status'], 'SATURATED')
        self.assertEqual(snap['families']['new']['status'], 'ACTIVE')

    def test_v22_nested_result_lifecycle_retraction_and_unreadable_warning(self):
        (self.root / 'logs').mkdir(parents=True, exist_ok=True)
        (self.root / 'research-state.yaml').write_text(
            'objective: >-\n  Improve counterfactual accuracy\n'
            'eval_contract:\n  metrics: [change_pair_accuracy, invariant_pair_accuracy]\n',
            encoding='utf-8',
        )
        rows = [
            {'arm': 'sft', 'status': 'running', 'family_id': 'sft', 'intervention_tier': 'T1', 'run_id': 'r-sft'},
            {'arm': 'sft', 'status': 'complete', 'family_id': 'sft', 'run_id': 'r-sft',
             'result': {'change_pair_accuracy': 0.625, 'invariant_pair_accuracy': 0.75}},
            {'arm': 'bad_run', 'status': 'complete', 'family_id': 'cand', 'intervention_tier': 'T2',
             'retracted': True, 'result': {'change_pair_accuracy': 0.30}},
            {'arm': 'odd_run', 'status': 'complete', 'family_id': 'cand', 'intervention_tier': 'T2',
             'result': {'note_only': 'hello'}},
        ]
        (self.root / 'logs/experiments.jsonl').write_text('\n'.join(json.dumps(r) for r in rows) + '\n', encoding='utf-8')
        snap = strategy.analyze(self.root)
        self.assertEqual(snap['total_experiments'], 3)
        self.assertEqual(snap['retracted_experiments'], 1)
        self.assertEqual(snap['retracted_ids'], ['bad_run'])
        self.assertEqual(snap['primary_metric'], 'change_pair_accuracy')
        self.assertAlmostEqual(snap['anchor_best'], 0.625)
        self.assertEqual([u['exp_id'] for u in snap['unreadable_result_warnings']], ['odd_run'])

    def test_v221_append_retraction_revokes_anchor_unreadable_neutral_and_dot_arbor(self):
        (self.root / 'logs').mkdir(parents=True, exist_ok=True)
        # 1) Appended retraction with matching exp_id revokes prior 0.90 anchor
        base = {'exp_id': 'base', 'family_id': 'baseline', 'metric_name': 'accuracy', 'metric': 0.5, 'status': 'complete', 'intervention_tier': 'T0'}
        cand = {'exp_id': 'foo', 'family_id': 'new', 'metric_name': 'accuracy', 'metric': 0.9, 'status': 'complete', 'intervention_tier': 'T1', 'paired_ci': [0.2, 0.5], 'comparator': 'base'}
        ret = {'exp_id': 'foo', 'family_id': 'new', 'status': 'retracted', 'retracted': True}
        (self.root / 'logs/experiments.jsonl').write_text('\n'.join(json.dumps(r) for r in (base, cand, ret)) + '\n', encoding='utf-8')
        snap = strategy.analyze(self.root)
        self.assertEqual(snap['completed_experiments'], 1)
        self.assertEqual(snap['retracted_experiments'], 1)
        self.assertEqual(snap['retracted_ids'], ['foo'])
        self.assertAlmostEqual(snap['anchor_best'], 0.5)
        self.assertEqual(snap['incremental_streak'], 0)
        self.assertFalse(snap['macro_review_armed'])

        # 1b) Explicit erratum entry targeting retracts=['foo'] also revokes
        err = {'exp_id': 'erratum-1', 'retracts': ['foo'], 'retracted': True}
        (self.root / 'logs/experiments.jsonl').write_text('\n'.join(json.dumps(r) for r in (base, cand, err)) + '\n', encoding='utf-8')
        snap_err = strategy.analyze(self.root)
        self.assertEqual(snap_err['completed_experiments'], 1)
        self.assertAlmostEqual(snap_err['anchor_best'], 0.5)
        self.assertEqual(snap_err['retracted_ids'], ['foo'])

        # 2) Unreadable completed rows warn without counting as scientific stagnation
        rows_unr = [base] + [{'exp_id': 'unr_%d' % i, 'family_id': 'f', 'status': 'complete', 'result': {'new_field': 0.6}} for i in range(6)]
        (self.root / 'logs/experiments.jsonl').write_text('\n'.join(json.dumps(r) for r in rows_unr) + '\n', encoding='utf-8')
        snap_unr = strategy.analyze(self.root)
        self.assertEqual(snap_unr['completed_experiments'], 1)
        self.assertEqual(snap_unr['unreadable_experiments'], 6)
        self.assertEqual(len(snap_unr['unreadable_result_warnings']), 6)
        self.assertEqual(snap_unr['incremental_streak'], 0)
        self.assertFalse(snap_unr['macro_review_armed'])
        self.assertEqual(snap_unr['families']['f']['status'], 'ACTIVE')

        # 3) Native .arbor/sessions/<name>/.coordinator/idea_tree.json discovery & export dedup
        (self.root / 'logs/experiments.jsonl').unlink()
        coord = self.root / '.arbor/sessions/demo/.coordinator'
        coord.mkdir(parents=True)
        tree_doc = {'nodes': {'ROOT': {'status': 'root'}, 'n1': {'status': 'done', 'score': 0.7, 'family_id': 'arbor_f'}}}
        (coord / 'idea_tree.json').write_text(json.dumps(tree_doc), encoding='utf-8')
        # Simulate Arbor export copy alongside .coordinator without double-counting
        (coord.parent / 'idea_tree.json').write_text(json.dumps(tree_doc), encoding='utf-8')
        snap_arbor = strategy.analyze(self.root)
        self.assertEqual(snap_arbor['completed_experiments'], 1)
        self.assertAlmostEqual(snap_arbor['anchor_best'], 0.7)


if __name__ == '__main__':
    unittest.main(verbosity=2)
