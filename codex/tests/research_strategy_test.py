"""CPU-only tests for Lemvo v2 strategy engine, plateau gate, and anti-incrementalism."""
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
            for k in ('LEMVO_MESSAGE_KIND', 'LEMVO_CURRENT_MESSAGE_PATH', 'LEMVO_MACRO_REVIEW_THRESHOLD', 'LEMVO_RESOURCE_WAKE')
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

    def _run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(STRATEGY_PATH), '--root', str(self.root)] if False else [sys.executable, str(STRATEGY_PATH)] + list(args),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
        )

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
            {'exp_id': 'selective_v11b_budget2x_s17_armselv11b', 'cycle': 21, 'outcome': 'RECORD but same-config replicate of v7c_seedvar', 'metric': 0.6389, 'detail': 'duplicate-of-v7c; Loss-level mechanism families CLOSED'},
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
        self.assertTrue(snap['macro_review_armed'])

        # Adding a CI-excluding-0 breakthrough resets the streak
        with (self.root / 'logs/experiments.jsonl').open('a', encoding='utf-8') as fh:
            fh.write(json.dumps({'exp_id': 'ce_t1_sig', 'family_id': 'ce', 'intervention_tier': 'T1', 'metric': 0.819, 'ci_low': 0.01, 'ci_high': 0.04, 'outcome': 'KEEP'}) + '\n')
        snap2 = strategy.analyze(self.root)
        self.assertEqual(snap2['incremental_streak'], 0)
        self.assertFalse(snap2['macro_review_armed'])

    def test_macro_review_gate_in_work_unit_guards_and_one_shot_settlement(self):
        (self.root / '.lemvo-strategy-policy.json').write_text(
            json.dumps({'macro_review_threshold': 3, 'noise_band': 0.05}), encoding='utf-8'
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

        # 4. Settling with rc=0 marks macro review handled (one-shot; next turn won't loop)
        (td_m / 'result.json').write_text(json.dumps({'status': 'succeeded', 'engine_rc': 0}), encoding='utf-8')
        work.report(td_m, 'completed', 'Completed taxonomy and proposed T3 jump', next_objective='Run T3 pilot', next_kind='experiment', tier='T3', family='latent')
        work.settle(td_m, 0)
        self.assertEqual(work.load(td_m / 'work-result.json')['intervention_tier'], 'T3')

        td_next = Path('turns/t_after')
        td_next.mkdir(parents=True)
        plan_after = work.prepare(td_next, 'inp.md', td_next / 'out.md', 'claude')
        self.assertFalse(plan_after['macro_review_triggered'])

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
        for path in (STRATEGY_PATH, WORK_UNIT_PATH):
            src = path.read_text(encoding='utf-8')
            for pat in forbidden:
                self.assertIsNone(re.search(pat, src), '%s matched forbidden py3.6 pattern %s' % (path.name, pat))


if __name__ == '__main__':
    unittest.main(verbosity=2)
