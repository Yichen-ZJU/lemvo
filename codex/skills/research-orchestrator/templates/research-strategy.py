#!/usr/bin/env python3
"""Research strategy telemetry, intervention-tier tracking, and macro-review gate.

This module provides deterministic multi-source experiment consolidation,
comparable metric-scoped anchor tracking, family saturation analysis,
config duplicate detection, portfolio incubation tracking, failure-taxonomy
persistence, and plateau/incrementalism detection for Lemvo v2.2.
Python 3.6+ stdlib only.
"""
import argparse
import datetime
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile


VERSION = 2
LEGACY_SNAPSHOT_SCHEMA_VERSION = 1
STRATEGY_STATE = '.lemvo-strategy-state.json'
STRATEGY_POLICY = '.lemvo-strategy-policy.json'
PORTFOLIO_FILE = '.lemvo-portfolio.json'
EXPERIMENTS_LOG = 'logs/experiments.jsonl'
RESULTS_TSV = 'results.tsv'
TAXONOMY_LOG = 'logs/failure-taxonomy.jsonl'
VALID_TIERS = ('T0', 'T1', 'T2', 'T3')
VALID_REVIEW_MODES = ('advisory', 'gate')
DEFAULT_POLICY = {
    'enabled': True,
    'macro_review_threshold': 5,
    'macro_review_mode': 'advisory',
    'incubation_budget': 3,
    'family_saturation_runs': 4,
    'noise_band': None,
    'direction': 'maximize',
}

_NON_COMPLETED_STATES = (
    'crash', 'timeout', 'metric-error', 'metric_error', 'eng_failure',
    'failed', 'dispatched', 'trained_unevaluated', 'no-op', 'blocked',
)
_LIFECYCLE_PENDING_STATES = frozenset((
    'running', 'pending', 'dispatched', 'queued', 'active', 'in_progress', 'loading', 'training',
))
_NULL_VERDICT_PATTERNS = (
    'discard', 'null', 'below gate', 'below_gate', 'rejected', 'saturating',
    'additive-neutral', 'not confirmed', 'acceptance gate missed',
    'within seed band', 'within +/-', 'null on primary', 'duplicate-of',
    'same-config replicate', 'does not exceed', 'missed by', 'below every component',
)
_SECONDARY_ENDPOINT_MARKERS = (
    'secondary', 'sub-metric', 'submetric', 'auxiliary',
    'invariant subset', 'unchanged subset', 'guard metric',
)
_DIAGNOSTIC_KEYS = frozenset((
    'elapsed_seconds', 'wall_sec', 'train_loss', 'logged_train_loss',
    'peak_allocated_gib', 'families', 'rows_judged', 'optimizer_steps',
    'optimizer_steps_expected', 'lora_r', 'lora_alpha', 'grad_accum', 'lr',
    'seed', 'max_steps', 'limit', 'train_steps', 'train_rows', 'infer_rows',
    'exposure_rows', 'exposure_sequences', 'adapter_witness_delta_norm',
    'conclusion_unchanged', 'cycle', 'iteration',
))
_PRIORITY_METRIC_KEYS = (
    'metric', 'primary_metric', 'four_grid', 'val_score', 'accuracy',
    'change_pair_accuracy', 'all_four_accuracy', 'invariant_pair_accuracy',
    'f1', 'auroc', 'val_bpb', 'loss', 'score', 'value',
)
_METRIC_SUFFIXES = (
    '_accuracy', '_score', '_f1', '_auc', '_auroc', '_bpb',
    '_rmse', '_mae', '_rate', '_pass_rate', '_acc',
)
_T0_KEYWORDS = (
    'baseline', 'seedvar', 'seed_var', 'seed2', 'seed_match', 'replicate',
    'budget2x', 'budget3x', 'backbone512', 'accept2x',
)
_T3_KEYWORDS = (
    'architecture', 'architectural', 'paradigm', 'steer_vec', 'activation',
    'representation', 'reformulat', 'multi_stage', 'leap',
)
_T3_BOUNDARY_RE = re.compile(r'(?:^|[^a-z0-9])arch(?:$|[^a-z0-9])')
_T1_KEYWORDS = (
    'lambda', 'sweep', 'doseup', 'weight', 'order', 'midonly', 'bottomfix',
    'intra_top', 'origemphasis', 'soup', 'ens3', 'ensemble', 'margin',
    'invariance_kl', 'suppression', 'antibleed',
)
_T1_BOUNDARY_RE = re.compile(
    r'(?:^|[^a-z0-9])(x2|x3|l00|l01|l03|l10)(?:$|[^a-z0-9])'
)
_T2_KEYWORDS = (
    'new_mechanism', 'counterfactual_gen', 'synthetic', 'verifier', 'search',
    'rerank', 'distill', 'dpo', 'grpo', 'escalate', 'incubate', 'bleedrank',
    'selective_logprob', 'selective_v2',
)


def now_utc():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def atomic_write(path, value):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.' + p.name + '-', dir=str(p.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            if isinstance(value, str):
                fh.write(value)
            else:
                json.dump(value, fh, ensure_ascii=False, indent=2)
                fh.write('\n')
        os.replace(tmp, str(p))
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def locked_append_jsonl(path, obj):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    lock_path = p.parent / ('.' + p.name + '.lock')
    line = json.dumps(obj, ensure_ascii=False) + '\n'
    with open(str(lock_path), 'a+', encoding='utf-8') as lfh:
        fcntl.flock(lfh.fileno(), fcntl.LOCK_EX)
        try:
            with open(str(p), 'a', encoding='utf-8') as fh:
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())
        finally:
            fcntl.flock(lfh.fileno(), fcntl.LOCK_UN)


def load_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError, UnicodeError):
        return default


def _to_float(val):
    if isinstance(val, bool) or val is None:
        return None
    if isinstance(val, (int, float)):
        f = float(val)
        return f if math.isfinite(f) else None
    if isinstance(val, str):
        s = val.strip()
        m = re.match(r'^(-?\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)$', s)
        if m:
            num, den = float(m.group(1)), float(m.group(2))
            if den > 0 and math.isfinite(num / den):
                return num / den
        try:
            f = float(s)
            return f if math.isfinite(f) else None
        except ValueError:
            return None
    return None


def _normalize_direction(val):
    s = str(val or '').strip().lower()
    if s in ('minimize', 'lower', 'min'):
        return 'minimize'
    if s in ('maximize', 'higher', 'max'):
        return 'maximize'
    return None


def _normalize_review_mode(val):
    s = str(val or '').strip().lower()
    if s in VALID_REVIEW_MODES:
        return s
    return None


def _task_md_hints(root):
    hints = {}
    for rel in ('TASK.md', 'task/TASK.md'):
        p = Path(root) / rel
        if not p.is_file():
            continue
        try:
            raw = p.read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        lines = raw.splitlines()
        if not lines or lines[0].strip() != '---':
            continue
        for line in lines[1:60]:
            if line.strip() == '---':
                break
            m = re.match(r'^\s*([A-Za-z_][\w-]*)\s*:\s*([^#\n]+)', line)
            if not m:
                continue
            k, v = m.group(1), m.group(2).strip().strip('"\'')
            if k == 'direction':
                d = _normalize_direction(v)
                if d and 'direction' not in hints:
                    hints['direction'] = d
            elif k in ('metric_name', 'primary_metric') and v and 'primary_metric' not in hints:
                hints['primary_metric'] = v
        if hints:
            break
    return hints


def _state_strategy_hints(root):
    p = Path(root) / 'research-state.yaml'
    hints = {}
    if p.is_file():
        try:
            raw = p.read_text(encoding='utf-8', errors='replace')
        except OSError:
            raw = ''
        in_strategy = False
        in_eval_contract = False
        eval_indent = -1
        declared = []
        for line in raw.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith('#'):
                continue
            indent = len(line) - len(line.lstrip(' '))
            if indent == 0:
                in_strategy = bool(re.match(r'^strategy\s*:', line))
                in_eval_contract = bool(re.match(r'^eval_contract\s*:', line))
                eval_indent = 0 if in_eval_contract else -1
                if in_strategy or in_eval_contract:
                    continue
            elif re.match(r'^\s*eval_contract\s*:\s*$', line):
                in_eval_contract = True
                eval_indent = indent
                continue
            elif in_eval_contract and indent <= eval_indent:
                in_eval_contract = False

            if in_eval_contract:
                lm = re.match(r'^\s*-\s*([A-Za-z0-9_.-]+)\s*$', line)
                if lm:
                    declared.append(lm.group(1))
                    continue

            m = re.match(r'^\s*([A-Za-z_][\w-]*)\s*:\s*([^#\n]+)', line)
            if not m:
                continue
            k, v = m.group(1), m.group(2).strip().strip('"\'')
            if k == 'metrics' and in_eval_contract and v.startswith('[') and v.endswith(']'):
                items = [x.strip().strip('"\'') for x in v[1:-1].split(',') if x.strip()]
                if items:
                    declared.extend(x for x in items if x not in declared)
            elif k == 'primary_metric' and v:
                hints['primary_metric'] = v
            elif k == 'direction':
                d = _normalize_direction(v)
                if d:
                    hints['direction'] = d
            elif k == 'noise_band' and (in_strategy or 'noise_band' not in hints):
                f = _to_float(v)
                if f is not None and f >= 0:
                    hints['noise_band'] = f
            elif k == 'macro_review_threshold' and in_strategy:
                try:
                    hints['macro_review_threshold'] = int(v)
                except ValueError:
                    pass
            elif k == 'macro_review_mode' and in_strategy:
                rm = _normalize_review_mode(v)
                if rm:
                    hints['macro_review_mode'] = rm
            elif k == 'incubation_budget' and in_strategy:
                try:
                    hints['incubation_budget'] = max(1, int(v))
                except ValueError:
                    pass
            elif k == 'family_saturation_runs' and in_strategy:
                try:
                    hints['family_saturation_runs'] = max(1, int(v))
                except ValueError:
                    pass
        if declared:
            hints['declared_metrics'] = declared
            hints.setdefault('primary_metric', declared[0])

    task_hints = _task_md_hints(root)
    for k, v in task_hints.items():
        hints.setdefault(k, v)
    return hints


def load_policy(root=Path('.')):
    root = Path(root)
    pol = dict(DEFAULT_POLICY)
    hints = _state_strategy_hints(root)
    explicit_direction = 'direction' in hints
    pol.update(hints)
    file_pol = load_json(root / STRATEGY_POLICY, None)
    if isinstance(file_pol, dict):
        if isinstance(file_pol.get('enabled'), bool):
            pol['enabled'] = file_pol['enabled']
        for int_key, lo in (('macro_review_threshold', 0), ('incubation_budget', 1), ('family_saturation_runs', 1)):
            v = file_pol.get(int_key)
            if isinstance(v, int) and not isinstance(v, bool) and v >= lo:
                pol[int_key] = v
        nb = _to_float(file_pol.get('noise_band'))
        if nb is not None and nb >= 0:
            pol['noise_band'] = nb
        d = _normalize_direction(file_pol.get('direction'))
        if d:
            pol['direction'] = d
            explicit_direction = True
        rm = _normalize_review_mode(file_pol.get('macro_review_mode'))
        if rm:
            pol['macro_review_mode'] = rm
        pm = file_pol.get('primary_metric')
        if isinstance(pm, str) and pm.strip():
            pol['primary_metric'] = pm.strip()
    env_thr = os.environ.get('LEMVO_MACRO_REVIEW_THRESHOLD')
    if env_thr is not None and env_thr.strip() != '':
        try:
            n = int(env_thr.strip())
            if n >= 0:
                pol['macro_review_threshold'] = n
        except ValueError:
            pass
    env_mode = _normalize_review_mode(os.environ.get('LEMVO_MACRO_REVIEW_MODE'))
    if env_mode:
        pol['macro_review_mode'] = env_mode
    pol['_explicit_direction'] = explicit_direction
    return pol


def _parse_tsv_records(tp, source_label='results.tsv'):
    rows = []
    if not tp.is_file():
        return rows
    try:
        lines = tp.read_text(encoding='utf-8', errors='replace').splitlines()
    except OSError:
        return rows
    for idx, line in enumerate(lines):
        if not line.strip():
            continue
        cols = line.split('\t')
        if idx == 0 and cols[0].strip().lower() == 'iteration':
            continue
        if len(cols) < 7:
            continue
        metric = _to_float(cols[3]) if len(cols) > 3 else None
        status = cols[6].strip() if len(cols) > 6 else ''
        move = cols[7].strip() if len(cols) > 7 else ''
        desc = cols[8].strip() if len(cols) > 8 else ''
        rec = {
            'exp_id': 'iter_' + cols[0].strip(),
            'timestamp': cols[1].strip() if len(cols) > 1 else '',
            'commit': cols[2].strip() if len(cols) > 2 else '',
            'metric': metric,
            'delta': _to_float(cols[4]) if len(cols) > 4 else None,
            'guard': cols[5].strip() if len(cols) > 5 else '',
            'outcome': status,
            'move': move,
            'detail': desc,
            '_source': source_label,
        }
        if len(cols) > 9 and cols[9].strip().upper() in VALID_TIERS:
            rec['intervention_tier'] = cols[9].strip().upper()
        elif desc:
            tm = re.search(r'\[(T[0-3])\]', desc, re.I)
            if tm:
                rec['intervention_tier'] = tm.group(1).upper()
        if len(cols) > 10 and cols[10].strip():
            rec['family_id'] = cols[10].strip()
        elif desc:
            fm = re.search(r'\[family=([^\]]+)\]', desc, re.I)
            if fm and fm.group(1).strip():
                rec['family_id'] = fm.group(1).strip()
        rows.append(rec)
    return rows


def _parse_jsonl_records(jp, source_label='experiments.jsonl'):
    rows = []
    if not jp.is_file():
        return rows
    try:
        with jp.open('r', encoding='utf-8', errors='replace') as fh:
            for line in fh:
                s = line.strip()
                if not s or not s.startswith('{'):
                    continue
                try:
                    obj = json.loads(s)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    obj = dict(obj)
                    obj['_source'] = source_label
                    rows.append(obj)
    except OSError:
        pass
    return rows


def _parse_arbor_tree(tp, source_label='arbor_idea_tree'):
    rows = []
    data = load_json(tp, None)
    if not isinstance(data, dict):
        return rows
    nodes = data.get('nodes')
    if not isinstance(nodes, dict):
        return rows
    for nid in sorted(nodes.keys()):
        node = nodes.get(nid)
        if not isinstance(node, dict) or str(nid).upper() == 'ROOT':
            continue
        status = str(node.get('status') or '').lower()
        score = _to_float(node.get('score'))
        if status not in ('done', 'merged', 'pruned', 'needs_retry') and score is None:
            continue
        comp = node.get('comparison') if isinstance(node.get('comparison'), dict) else {}
        outcome = str(node.get('eval_status') or status or 'done')
        if status == 'pruned' and score is None:
            outcome = 'discard'
        rec = {
            'exp_id': 'arbor_' + str(nid),
            'family_id': str(node.get('family_id') or node.get('code_ref') or ('arbor_' + str(node.get('parent_id') or nid))),
            'metric': score,
            'split': node.get('score_split') or 'dev',
            'outcome': outcome,
            'detail': node.get('result') or node.get('insight') or node.get('hypothesis') or '',
            '_source': source_label,
        }
        if comp.get('comparator'):
            rec['comparator'] = comp.get('comparator')
        rows.append(rec)
    return rows


def _exp_identifier(record):
    for k in ('exp_id', 'experiment', 'arm', 'name', 'run_id'):
        v = record.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return str(record.get('cycle') or record.get('id') or 'unnamed')


def _is_lifecycle_pending(record):
    st = str(record.get('status') or record.get('outcome') or '').strip().lower()
    if st in _LIFECYCLE_PENDING_STATES and _extract_metric(record) is None:
        return True
    return False


def _merge_lifecycle_pair(older, newer):
    merged = dict(older)
    for k, v in newer.items():
        if k == 'config' and isinstance(older.get('config'), dict) and isinstance(v, dict):
            comb = dict(older['config'])
            comb.update(v)
            merged['config'] = comb
        elif v is not None:
            merged[k] = v
    return merged


def _consolidate_records(records):
    """Merge in-progress dispatch rows with their matching completion rows and dedup cross-source mirrors."""
    out = []
    pending_by_key = {}
    for rec in records:
        src = rec.get('_source', '')
        ident = _exp_identifier(rec)
        rid = str(rec.get('run_id') or '').strip()
        is_pend = _is_lifecycle_pending(rec)
        is_diag = _is_diagnostic_or_smoke(rec)
        key_id = (src, 'id', ident) if ident and ident != 'unnamed' else None
        key_run = (src, 'run', rid) if rid else None

        if is_pend:
            idx = len(out)
            out.append(rec)
            if key_id:
                pending_by_key[key_id] = idx
            if key_run:
                pending_by_key[key_run] = idx
            continue

        if is_diag:
            out.append(rec)
            continue

        matched_idx = None
        if key_run and key_run in pending_by_key:
            matched_idx = pending_by_key[key_run]
        elif key_id and key_id in pending_by_key:
            matched_idx = pending_by_key[key_id]

        if matched_idx is not None and out[matched_idx] is not None:
            out[matched_idx] = _merge_lifecycle_pair(out[matched_idx], rec)
            pending_by_key.pop(key_id, None)
            pending_by_key.pop(key_run, None)
            moved = out[matched_idx]
            out[matched_idx] = None
            out.append(moved)
        else:
            out.append(rec)

    consolidated = [r for r in out if r is not None]
    jsonl_ids = set()
    jsonl_rids = set()
    for r in consolidated:
        if 'experiments.jsonl' in str(r.get('_source', '')):
            eid = _exp_identifier(r)
            if eid != 'unnamed':
                jsonl_ids.add(eid)
            if r.get('run_id'):
                jsonl_rids.add(str(r['run_id']))
    final = []
    for r in consolidated:
        if r.get('_source') == 'results.tsv':
            eid = _exp_identifier(r)
            rid = str(r.get('run_id') or '')
            if (eid != 'unnamed' and eid in jsonl_ids) or (rid and rid in jsonl_rids):
                continue
        final.append(r)
    return final


def load_experiments(root=Path('.')):
    root = Path(root)
    records = []
    seen_paths = set()

    def _add_jsonl(p, label):
        rp = str(p.resolve()) if p.exists() else str(p)
        if rp in seen_paths:
            return
        seen_paths.add(rp)
        records.extend(_parse_jsonl_records(p, label))

    def _add_tsv(p, label):
        rp = str(p.resolve()) if p.exists() else str(p)
        if rp in seen_paths:
            return
        seen_paths.add(rp)
        records.extend(_parse_tsv_records(p, label))

    _add_jsonl(root / EXPERIMENTS_LOG, 'experiments.jsonl')
    _add_tsv(root / RESULTS_TSV, 'results.tsv')

    for p in sorted(root.glob('runs/*/logs/experiments.jsonl')):
        _add_jsonl(p, 'sub:' + str(p.relative_to(root)))
    for p in sorted(root.glob('runs/*/results.tsv')):
        _add_tsv(p, 'sub:' + str(p.relative_to(root)))
    for p in sorted(root.glob('arbor*/**/idea_tree.json')):
        rp = str(p.resolve())
        if rp not in seen_paths:
            seen_paths.add(rp)
            records.extend(_parse_arbor_tree(p, 'arbor:' + str(p.relative_to(root))))

    return _consolidate_records(records)


def _extract_from_mapping(d, declared_metrics=(), collected=None):
    if not isinstance(d, dict):
        return None, None
    ordered_keys = []
    for k in tuple(declared_metrics) + _PRIORITY_METRIC_KEYS:
        if k not in ordered_keys:
            ordered_keys.append(k)

    primary_val = None
    primary_name = None

    def _read_scalar_or_nested_val(v):
        f = _to_float(v)
        if f is not None:
            return f
        if isinstance(v, dict) and 'value' in v:
            return _to_float(v.get('value'))
        return None

    for k in ordered_keys:
        if k in d and k.lower() not in _DIAGNOSTIC_KEYS:
            val = _read_scalar_or_nested_val(d.get(k))
            if val is not None:
                if collected is not None and k not in collected:
                    collected[k] = val
                if primary_val is None:
                    primary_val = val
                    primary_name = k

    for k, v in d.items():
        kl = str(k).lower()
        if kl in _DIAGNOSTIC_KEYS:
            continue
        if kl in ('paired_vs_base', 'paired_vs_sft', 'bootstrap', 'paired_ci', 'ci_95', 'ci'):
            continue
        val = _read_scalar_or_nested_val(v)
        if val is not None and (k in ordered_keys or kl.endswith(_METRIC_SUFFIXES)):
            if collected is not None and k not in collected:
                collected[k] = val
            if primary_val is None:
                primary_val = val
                primary_name = k

    return primary_val, primary_name


def _extract_observations(record, declared_metrics=()):
    """Extract primary metric (value, name) plus all readable evaluation endpoints."""
    if not isinstance(record, dict):
        return None, None, {}
    collected = {}
    hint_name = str(record.get('metric_name') or '').strip()
    top_declared = tuple(declared_metrics)
    top_priority = ('metric', 'primary_metric', 'four_grid', 'val_score', 'score', 'value') + top_declared

    primary_val = None
    primary_name = None

    for k in top_priority:
        if k in record and k.lower() not in _DIAGNOSTIC_KEYS:
            v = _to_float(record.get(k))
            if v is not None:
                name = hint_name if (k in ('metric', 'val_score', 'score', 'value') and hint_name) else k
                collected[name] = v
                if primary_val is None:
                    primary_val = v
                    primary_name = name

    for container_key in ('result', 'metrics'):
        c = record.get(container_key)
        if isinstance(c, dict):
            c_val, c_name = _extract_from_mapping(c, top_declared, collected)
            if primary_val is None and c_val is not None:
                primary_val = c_val
                primary_name = hint_name or c_name

    if isinstance(record.get('result'), dict):
        _extract_from_mapping(record['result'], top_declared, collected)
    if isinstance(record.get('metrics'), dict):
        _extract_from_mapping(record['metrics'], top_declared, collected)

    if primary_name is None and primary_val is not None:
        primary_name = hint_name or 'metric'
    return primary_val, primary_name, collected


def _extract_metric(record, declared_metrics=()):
    val, _, _ = _extract_observations(record, declared_metrics=declared_metrics)
    return val


def is_completed(record):
    status_text = ' '.join(
        str(record.get(k) or '') for k in ('status', 'outcome', 'verdict')
    ).strip().lower()
    metric = _extract_metric(record)
    for tag in _NON_COMPLETED_STATES:
        if (
            status_text == tag
            or status_text.startswith(tag + ' ')
            or status_text.startswith(tag + ':')
            or (' ' + tag + ':') in status_text
            or ('status=' + tag) in status_text
        ):
            return False
    if 'dispatched' in status_text and metric is None:
        return False
    if 'failed' in status_text and metric is None:
        return False
    if 'trained_unevaluated' in status_text:
        return False
    if metric is None and (
        not status_text
        or 'pending' in status_text
        or 'running' in status_text
        or 'queued' in status_text
    ):
        return False
    if metric is None and 'discard' in status_text:
        return False
    return True


def _is_diagnostic_or_smoke(record):
    ident = _exp_identifier(record).lower()
    status = str(record.get('status') or '').strip().lower()
    purpose = str(record.get('purpose') or '').strip().lower()
    if status in ('health-verified', 'diagnostic', 'pipeline-smoke', 'erratum'):
        return True
    if ident in ('smoke', 'ga-loss-normalization-bug') or ident.endswith(('-smoke', '_smoke', '-erratum', '_erratum')):
        return True
    if ('smoke' in ident or 'diagnostic' in ident or 'erratum' in ident) and _extract_metric(record) is None:
        return True
    if purpose and ('validation' in purpose or 'root-cause' in purpose or 'correct ' in purpose) and _extract_metric(record) is None:
        return True
    return False


def _is_retracted(record):
    if record.get('retracted') is True or record.get('invalidated') is True or record.get('implementation_invalid') is True:
        return True
    st = str(record.get('status') or '').strip().lower()
    if st in ('retracted', 'invalidated', 'implementation_invalid'):
        return True
    verdicts = [str(record.get('verdict') or ''), str(record.get('outcome') or '')]
    if isinstance(record.get('result'), dict):
        verdicts.append(str(record['result'].get('verdict') or ''))
    txt = ' '.join(verdicts).lower()
    return any(tok in txt for tok in (
        'optimization-crippled', 're-scoped', 'implementation_invalid', 'retracted',
    ))


def _is_rejected_or_invalid_baseline(record):
    if _is_retracted(record):
        return True
    for k in ('status', 'outcome', 'verdict'):
        v = str(record.get(k) or '').strip().lower()
        if v == 'rejected' or v.startswith('rejected ') or v.startswith('rejected:') or 'eng_failure' in v or 'invalid' in v:
            return True
    return False


def infer_tier(record):
    for k in ('intervention_tier', 'tier'):
        v = record.get(k)
        if isinstance(v, str) and v.strip().upper() in VALID_TIERS:
            return v.strip().upper(), 'explicit'

    ident = _exp_identifier(record).lower()
    move = str(record.get('move') or '').strip().lower()
    detail = ' '.join(
        str(record.get(k) or '')
        for k in ('outcome', 'verdict', 'detail', 'hypothesis', 'notes', 'description', 'summary')
    ).lower()
    combined = ident + ' ' + move + ' ' + detail

    if not is_completed(record):
        return 'T0', 'inferred'
    if any(kw in ident or kw == move for kw in _T0_KEYWORDS) or move == 'baseline':
        return 'T0', 'inferred'

    if (
        move in ('leap', 'arch')
        or _T3_BOUNDARY_RE.search(ident)
        or any(kw in ident for kw in _T3_KEYWORDS)
        or 'paradigm shift' in detail
        or 'architectural' in detail
    ):
        if not _T1_BOUNDARY_RE.search(ident):
            return 'T3', 'inferred'

    if move in ('follow', 'reverse', 'simplify'):
        return 'T1', 'inferred'
    if any(kw in ident for kw in _T1_KEYWORDS) or _T1_BOUNDARY_RE.search(ident):
        return 'T1', 'inferred'
    if any(w in detail for w in ('lambda_eff', 'margin_lambda', 'antibleed_lambda', 'weight average', 'majority vote', 'seed-matched control')):
        return 'T1', 'inferred'

    if move in ('escalate', 'incubate') or any(kw in combined for kw in _T2_KEYWORDS):
        return 'T2', 'inferred'

    return 'T1', 'inferred'


def infer_family(record):
    for k in ('family_id', 'method_family', 'family'):
        v = record.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip(), 'explicit'
    ident = _exp_identifier(record).lower()
    stripped = re.sub(r'(?:_v\d+[a-z]?|_e\d+|_arm[a-z0-9_-]*|_s\d+)(?:_.*|$)', '', ident)
    stripped = stripped.strip('_-')
    if not stripped:
        stripped = ident.strip('_-') or 'default'
    return stripped, 'inferred'


def canonical_fingerprint(family, config_dict, eval_contract=None):
    if not isinstance(config_dict, dict) or not config_dict:
        return None
    payload = {'family': str(family), 'config': config_dict}
    if eval_contract:
        payload['eval_contract'] = str(eval_contract)
    blob = json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return hashlib.sha256(blob.encode('utf-8')).hexdigest()[:16]


def _parse_ci_value(val):
    if isinstance(val, (list, tuple)):
        if len(val) == 2:
            lo, hi = _to_float(val[0]), _to_float(val[1])
            if lo is not None and hi is not None:
                return min(lo, hi), max(lo, hi)
        elif len(val) == 3:
            pt, lo, hi = _to_float(val[0]), _to_float(val[1]), _to_float(val[2])
            if pt is not None and lo is not None and hi is not None:
                lo_n, hi_n = min(lo, hi), max(lo, hi)
                if lo_n <= pt <= hi_n:
                    return lo_n, hi_n
    if isinstance(val, str):
        m = re.search(r'\[\s*([+-]?\d+(?:\.\d+)?)\s*,\s*([+-]?\d+(?:\.\d+)?)\s*\]', val)
        if m:
            lo, hi = float(m.group(1)), float(m.group(2))
            return min(lo, hi), max(lo, hi)
    return None


def _extract_ci_info(record):
    """Return (lo, hi, comparator_name_or_None) if a paired CI is present."""
    for k in ('paired_ci', 'ci_95', 'bootstrap95ci_pp', 'ci'):
        ci = _parse_ci_value(record.get(k))
        if ci is not None:
            comp = record.get('comparator') or record.get('baseline')
            return ci[0], ci[1], (str(comp).strip() if comp else None)
    lo, hi = _to_float(record.get('ci_low')), _to_float(record.get('ci_high'))
    if lo is not None and hi is not None:
        comp = record.get('comparator') or record.get('baseline')
        return min(lo, hi), max(lo, hi), (str(comp).strip() if comp else None)

    for cname in ('result', 'metrics'):
        cont = record.get(cname)
        if not isinstance(cont, dict):
            continue
        for k in ('paired_ci', 'ci_95', 'bootstrap95ci_pp', 'ci'):
            ci = _parse_ci_value(cont.get(k))
            if ci is not None:
                comp = record.get('comparator') or cont.get('comparator')
                return ci[0], ci[1], (str(comp).strip() if comp else None)
        vs_keys = sorted([k for k in cont.keys() if k.startswith('paired_vs_')])
        vs_keys.sort(key=lambda x: (x == 'paired_vs_base', x))
        for vk in vs_keys:
            comp_name = vk[len('paired_vs_'):]
            vv = cont.get(vk)
            if isinstance(vv, dict):
                for sk, sv in vv.items():
                    if 'invariant' in sk:
                        continue
                    ci = _parse_ci_value(sv)
                    if ci is not None:
                        return ci[0], ci[1], comp_name
            else:
                ci = _parse_ci_value(vv)
                if ci is not None:
                    base_comp = comp_name.split('_')[0]
                    return ci[0], ci[1], base_comp

    txt = ' '.join(
        str(record.get(k) or '')
        for k in ('detail', 'outcome', 'verdict', 'notes', 'description')
    )
    m = re.search(
        r'\bci(?:\s*95)?\s*[:=]?\s*\[\s*([+-]?\d+(?:\.\d+)?)\s*,\s*([+-]?\d+(?:\.\d+)?)\s*\]',
        txt, re.I,
    )
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        return min(lo, hi), max(lo, hi), None
    return None


def _extract_ci(record):
    info = _extract_ci_info(record)
    return (info[0], info[1]) if info is not None else None


def _has_null_verdict(record):
    clauses = []
    for k in ('outcome', 'verdict', 'detail', 'notes', 'description'):
        v = record.get(k)
        if v:
            clauses.extend(re.split(r'[;|]|\.\s+', str(v)))
    res = record.get('result')
    if isinstance(res, dict) and res.get('verdict'):
        clauses.extend(re.split(r'[;|]|\.\s+', str(res.get('verdict'))))
    for clause in clauses:
        low = clause.strip().lower()
        if not low:
            continue
        if any(sec in low for sec in _SECONDARY_ENDPOINT_MARKERS):
            continue
        for pat in _NULL_VERDICT_PATTERNS:
            if pat in low:
                if pat == 'null on primary' and 'excludes 0' in low and 'significant' in low:
                    continue
                return True
    return False


def _has_duplicate_marker(record):
    if record.get('duplicate_of'):
        return str(record['duplicate_of'])
    txt = ' '.join(
        str(record.get(k) or '')
        for k in ('outcome', 'verdict', 'detail', 'notes', 'description')
    )
    m = re.search(
        r'(?:same-config replicate of|duplicate[-_ ]of[:\s-]+)([A-Za-z0-9_.-]+)',
        txt, re.I
    )
    return m.group(1) if m else None


def _is_explicit_family_closure(record):
    """Only explicit structured decisions close a family, never prose/blocked status."""
    if record.get('close_family') is True:
        return True
    if str(record.get('family_status') or '').strip().lower() == 'closed':
        return True
    if str(record.get('family_decision') or '').strip().lower() in ('closed', 'close_family', 'retire_family'):
        return True
    if not is_completed(record):
        return False
    txt = ' '.join(str(record.get(k) or '') for k in ('outcome', 'verdict'))
    return bool(re.search(r'\[(?:close_family|family_closed)\]', txt, re.I))


def _is_explicit_family_reopen(record):
    if record.get('reopen_family') is True:
        return True
    if str(record.get('family_status') or '').strip().lower() == 'active':
        return True
    if str(record.get('family_decision') or '').strip().lower() in ('reopen', 'reopen_family', 'active'):
        return True
    return False


def _comparator_matches_anchor(ci_comparator, anchor_exp_id, anchor_family):
    if not ci_comparator:
        return True
    comp = str(ci_comparator).strip().lower()
    if comp in ('anchor', 'best', 'champion', 'matched_comparator'):
        return True
    ah_exp = str(anchor_exp_id or '').strip().lower()
    ah_fam = str(anchor_family or '').strip().lower()
    if not ah_exp and not ah_fam:
        return True
    if comp in ah_exp or comp in ah_fam or ah_exp in comp or (ah_fam and ah_fam in comp):
        return True
    return False


def is_breakthrough(record, best_so_far, noise_band, direction='maximize',
                    declared_metrics=(), anchor_exp_id=None, anchor_family=None):
    if not is_completed(record) or _is_diagnostic_or_smoke(record) or _is_retracted(record):
        return False
    if _has_duplicate_marker(record):
        return False
    if _has_null_verdict(record):
        return False

    metric = _extract_metric(record, declared_metrics=declared_metrics)
    delta = None
    if metric is not None and best_so_far is not None:
        delta = (best_so_far - metric) if direction == 'minimize' else (metric - best_so_far)
        if delta <= 0.0:
            return False

    sig = record.get('significant')
    if sig is False:
        return False

    ci_info = _extract_ci_info(record)
    if ci_info is not None:
        lo, hi, ci_comp = ci_info
        if not _comparator_matches_anchor(ci_comp, anchor_exp_id, anchor_family):
            return False
        if direction == 'minimize':
            return hi < 0.0
        return lo > 0.0

    if sig is True:
        return (delta is not None and delta > 0.0) or (metric is None and best_so_far is None)

    if delta is None or delta <= 0.0:
        return False
    if noise_band is not None and noise_band > 0:
        return delta > float(noise_band)
    return False


def load_failure_taxonomies(root=Path('.')):
    p = Path(root) / TAXONOMY_LOG
    rows = []
    if not p.is_file():
        return rows
    try:
        for line in p.read_text(encoding='utf-8', errors='replace').splitlines():
            s = line.strip()
            if not s or not s.startswith('{'):
                continue
            try:
                obj = json.loads(s)
                if isinstance(obj, dict):
                    rows.append(obj)
            except ValueError:
                continue
    except OSError:
        pass
    return rows


def analyze(root=Path('.')):
    root = Path(root)
    pol = load_policy(root)
    records = load_experiments(root)
    portfolio = load_json(root / PORTFOLIO_FILE, None)
    if not isinstance(portfolio, dict):
        portfolio = {}
    state = load_json(root / STRATEGY_STATE, {})
    if not isinstance(state, dict):
        state = {}
    taxonomies = load_failure_taxonomies(root)

    declared_metrics = tuple(pol.get('declared_metrics') or ())
    dirs_by_metric_scope = {}
    for rec in records:
        if _is_diagnostic_or_smoke(rec):
            continue
        rdir = _normalize_direction(rec.get('direction'))
        if rdir is None and isinstance(rec.get('higher_is_better'), bool):
            rdir = 'maximize' if rec.get('higher_is_better') else 'minimize'
        if not rdir:
            continue
        res_d = rec.get('result') if isinstance(rec.get('result'), dict) else {}
        ec = str(rec.get('eval_contract') or res_d.get('eval_contract') or 'default')
        sp = str(rec.get('split') or res_d.get('split') or 'default')
        _, mk, _ = _extract_observations(rec, declared_metrics=declared_metrics)
        if mk:
            dirs_by_metric_scope.setdefault((ec, sp, mk), set()).add(rdir)

    slots = portfolio.get('slots', {}) if isinstance(portfolio.get('slots'), dict) else {}
    explore_families = {}
    for slot_name, slot in slots.items():
        if not isinstance(slot, dict):
            continue
        sfam = slot.get('family') or slot.get('family_id')
        if sfam and slot_name == 'explore':
            explore_families[str(sfam)] = slot

    tier_counts = {t: 0 for t in VALID_TIERS}
    families = {}
    seen_fps = {}
    duplicate_warnings = []
    unreadable_result_warnings = []
    retracted_ids = []
    total_experiments = len(records)
    completed_experiments = 0
    retracted_experiments = 0
    incremental_streak = 0
    direction = pol['direction']
    noise_band = pol['noise_band']

    anchors_by_scope = {}
    primary_scope_key = None

    for rec in records:
        exp_id = _exp_identifier(rec)
        tier, _ = infer_tier(rec)
        fam, _ = infer_family(rec)
        diag_smoke = _is_diagnostic_or_smoke(rec)
        retracted = _is_retracted(rec)
        completed = is_completed(rec) and not diag_smoke

        if not diag_smoke:
            tier_counts[tier] += 1

        res_dict = rec.get('result') if isinstance(rec.get('result'), dict) else {}
        eval_contract = str(rec.get('eval_contract') or res_dict.get('eval_contract') or 'default')
        split = str(rec.get('split') or res_dict.get('split') or 'default')

        metric, metric_name, _ = _extract_observations(rec, declared_metrics=declared_metrics)
        rec_explicit_dir = _normalize_direction(rec.get('direction'))
        if rec_explicit_dir is None and isinstance(rec.get('higher_is_better'), bool):
            rec_explicit_dir = 'maximize' if rec.get('higher_is_better') else 'minimize'
        generic_name = (
            not str(rec.get('metric_name') or '').strip()
            and metric_name in ('metric', 'val_score', 'score', 'value', 'primary_metric')
        )
        m_key = metric_name or 'metric'
        if generic_name:
            if pol.get('primary_metric'):
                m_key = pol['primary_metric']
            elif primary_scope_key and (primary_scope_key[0], primary_scope_key[1]) == (eval_contract, split):
                m_key = primary_scope_key[2]
        scope_dirs = dirs_by_metric_scope.get((eval_contract, split, m_key), set())
        if rec_explicit_dir:
            rec_dir = rec_explicit_dir
        elif not pol.get('_explicit_direction') and len(scope_dirs) == 1:
            rec_dir = next(iter(scope_dirs))
        else:
            rec_dir = direction

        if not diag_smoke:
            cfg = rec.get('config') if isinstance(rec.get('config'), dict) else rec.get('params')
            fp = canonical_fingerprint(fam, cfg, eval_contract=eval_contract if eval_contract != 'default' else None) if isinstance(cfg, dict) and cfg else None
            if fp:
                if fp in seen_fps:
                    duplicate_warnings.append({
                        'exp_id': exp_id,
                        'matched_exp_id': seen_fps[fp],
                        'fingerprint': fp,
                        'basis': 'config_fingerprint',
                    })
                else:
                    seen_fps[fp] = exp_id
            else:
                dup_ref = _has_duplicate_marker(rec)
                if dup_ref:
                    duplicate_warnings.append({
                        'exp_id': exp_id,
                        'matched_exp_id': dup_ref,
                        'fingerprint': None,
                        'basis': 'explicit_or_text_duplicate_marker',
                    })

        finfo = families.setdefault(fam, {
            'family_id': fam,
            'runs': 0,
            'completed_runs': 0,
            'retracted_runs': 0,
            'tiers': [],
            'best_metric': None,
            'best_metrics_by_name': {},
            'breakthroughs': 0,
            'recent_outcomes': [],
            'explicit_family_closed': False,
            'status': 'ACTIVE',
            'incubation_remaining': 0,
        })
        if not diag_smoke:
            finfo['runs'] += 1
            if tier not in finfo['tiers']:
                finfo['tiers'].append(tier)

        if _is_explicit_family_closure(rec):
            finfo['explicit_family_closed'] = True
        elif _is_explicit_family_reopen(rec):
            finfo['explicit_family_closed'] = False

        if retracted:
            retracted_experiments += 1
            finfo['retracted_runs'] += 1
            retracted_ids.append(exp_id)
            continue

        if completed:
            completed_experiments += 1
            finfo['completed_runs'] += 1
            if metric is None:
                unreadable_result_warnings.append({
                    'exp_id': exp_id,
                    'family_id': fam,
                    'status': rec.get('status') or rec.get('outcome'),
                    'top_keys': sorted(list(rec.keys())),
                    'result_keys': sorted(list(res_dict.keys())) if res_dict else [],
                    'metrics_keys': sorted(list(rec['metrics'].keys())) if isinstance(rec.get('metrics'), dict) else [],
                })
                incremental_streak += 1
                finfo['recent_outcomes'].append({'bt': False})
                continue

            prev_fbest = finfo['best_metrics_by_name'].get(m_key)
            if prev_fbest is None:
                finfo['best_metrics_by_name'][m_key] = metric
            elif rec_dir == 'minimize':
                finfo['best_metrics_by_name'][m_key] = min(prev_fbest, metric)
            else:
                finfo['best_metrics_by_name'][m_key] = max(prev_fbest, metric)

            if primary_scope_key is None or m_key == pol.get('primary_metric') or m_key == primary_scope_key[2]:
                if finfo['best_metric'] is None:
                    finfo['best_metric'] = metric
                elif rec_dir == 'minimize':
                    finfo['best_metric'] = min(finfo['best_metric'], metric)
                else:
                    finfo['best_metric'] = max(finfo['best_metric'], metric)

            scope_key = (eval_contract, split, m_key, rec_dir)
            if primary_scope_key is None:
                primary_scope_key = scope_key
            elif pol.get('primary_metric') and m_key == pol['primary_metric'] and primary_scope_key[2] != pol['primary_metric']:
                primary_scope_key = scope_key

            cur_anchor = anchors_by_scope.get(scope_key)
            cur_val = cur_anchor['value'] if cur_anchor else None
            cur_exp = cur_anchor['exp_id'] if cur_anchor else None
            cur_fam = cur_anchor['family_id'] if cur_anchor else None

            bt = is_breakthrough(
                rec, cur_val, noise_band, rec_dir,
                declared_metrics=declared_metrics,
                anchor_exp_id=cur_exp,
                anchor_family=cur_fam,
            )
            finfo['recent_outcomes'].append({'bt': bt})

            if cur_anchor is None:
                if not _is_rejected_or_invalid_baseline(rec):
                    anchors_by_scope[scope_key] = {'value': metric, 'exp_id': exp_id, 'family_id': fam}
                if tier in ('T2', 'T3') and bt:
                    finfo['breakthroughs'] += 1
                    incremental_streak = 0
                elif tier in ('T2', 'T3'):
                    incremental_streak = 0
                elif not (tier == 'T0' and ('baseline' in exp_id.lower() or exp_id == 'base') and completed_experiments <= 2):
                    if _is_rejected_or_invalid_baseline(rec):
                        incremental_streak += 1
            elif bt:
                finfo['breakthroughs'] += 1
                incremental_streak = 0
                anchors_by_scope[scope_key] = {'value': metric, 'exp_id': exp_id, 'family_id': fam}
            else:
                if tier in ('T2', 'T3'):
                    slot = explore_families.get(fam)
                    fam_inc_budget = int(slot.get('incubation_budget') or pol['incubation_budget']) if slot else pol['incubation_budget']
                    if finfo['completed_runs'] == 1:
                        incremental_streak = 0
                    elif finfo['completed_runs'] <= fam_inc_budget:
                        pass
                    else:
                        incremental_streak += 1
                else:
                    if not (tier == 'T0' and ('baseline' in exp_id.lower() or exp_id == 'base') and completed_experiments <= 2):
                        incremental_streak += 1

    for slot_name, slot in slots.items():
        if not isinstance(slot, dict):
            continue
        sfam = slot.get('family') or slot.get('family_id')
        if not sfam:
            continue
        sfam = str(sfam)
        families.setdefault(sfam, {
            'family_id': sfam,
            'runs': 0,
            'completed_runs': 0,
            'retracted_runs': 0,
            'tiers': [slot.get('tier')] if slot.get('tier') in VALID_TIERS else [],
            'best_metric': None,
            'best_metrics_by_name': {},
            'breakthroughs': 0,
            'recent_outcomes': [],
            'explicit_family_closed': False,
            'status': 'ACTIVE',
            'incubation_remaining': 0,
        })

    retired_set = {str(x) for x in (portfolio.get('retired_families') or []) if isinstance(x, str) and x.strip()}
    sat_runs = pol['family_saturation_runs']
    for fam, finfo in families.items():
        slot = explore_families.get(fam)
        inc_budget = int(slot.get('incubation_budget') or pol['incubation_budget']) if slot else pol['incubation_budget']
        cruns = finfo['completed_runs']
        recent = finfo['recent_outcomes'][-sat_runs:]
        explicitly_closed = bool(
            (slot and str(slot.get('status') or '').lower() == 'closed')
            or finfo.get('explicit_family_closed')
            or fam in retired_set
        )
        has_t2_t3 = any(t in ('T2', 'T3') for t in finfo['tiers'])
        if explicitly_closed:
            finfo['status'] = 'CLOSED'
            finfo['incubation_remaining'] = 0
        elif slot and str(slot.get('status') or '').lower() != 'closed' and cruns < inc_budget:
            finfo['status'] = 'INCUBATING'
            finfo['incubation_remaining'] = max(0, inc_budget - cruns)
        elif has_t2_t3 and 0 < cruns < inc_budget:
            finfo['status'] = 'INCUBATING'
            finfo['incubation_remaining'] = max(0, inc_budget - cruns)
        elif cruns >= sat_runs and not any(x['bt'] for x in recent):
            finfo['status'] = 'SATURATED'
            finfo['incubation_remaining'] = 0
        else:
            finfo['status'] = 'ACTIVE'
            finfo['incubation_remaining'] = 0
        finfo.pop('recent_outcomes', None)
        finfo.pop('explicit_family_closed', None)

    enriched_portfolio = dict(portfolio)
    if slots:
        enriched_slots = {}
        for sname, sdata in slots.items():
            if not isinstance(sdata, dict):
                continue
            item = dict(sdata)
            sfam = item.get('family') or item.get('family_id')
            if sfam and sfam in families:
                fstatus = families[sfam]['status']
                item['completed_runs'] = families[sfam]['completed_runs']
                item['incubation_remaining'] = families[sfam]['incubation_remaining']
                item['incubation_protected'] = (sname == 'explore' and fstatus == 'INCUBATING')
            enriched_slots[sname] = item
        enriched_portfolio['slots'] = enriched_slots

    anchor_best = anchors_by_scope[primary_scope_key]['value'] if (primary_scope_key and primary_scope_key in anchors_by_scope) else None
    anchors_summary = {
        '%s|%s|%s|%s' % k: v for k, v in anchors_by_scope.items()
    }

    last_mr_count = int(state.get('last_macro_review_exp_count') or 0)
    thr = pol['macro_review_threshold']
    since_last_review = completed_experiments if last_mr_count <= 0 else max(0, completed_experiments - last_mr_count)
    streak_since_last_review = min(incremental_streak, since_last_review)
    armed = bool(
        pol['enabled']
        and thr > 0
        and streak_since_last_review >= thr
    )

    return {
        'schema': 'lemvo-strategy-snapshot',
        'schema_version': LEGACY_SNAPSHOT_SCHEMA_VERSION,
        'strategy_version': VERSION,
        'total_experiments': total_experiments,
        'completed_experiments': completed_experiments,
        'retracted_experiments': retracted_experiments,
        'retracted_ids': retracted_ids,
        'tier_counts': tier_counts,
        'incremental_streak': incremental_streak,
        'streak_since_last_review': streak_since_last_review,
        'macro_review_threshold': thr,
        'macro_review_mode': pol['macro_review_mode'],
        'noise_band_used': noise_band,
        'direction': primary_scope_key[3] if (not pol.get('_explicit_direction') and primary_scope_key) else direction,
        'primary_metric': primary_scope_key[2] if primary_scope_key else pol.get('primary_metric'),
        'anchor_best': anchor_best,
        'anchors_by_scope': anchors_summary,
        'families': families,
        'portfolio': enriched_portfolio,
        'duplicate_warnings': duplicate_warnings,
        'unreadable_result_warnings': unreadable_result_warnings,
        'latest_failure_taxonomy': taxonomies[-1] if taxonomies else None,
        'last_macro_review_exp_count': last_mr_count,
        'macro_review_armed': armed,
    }


def render_ledger_text(snapshot, max_lines=32):
    tc = snapshot.get('tier_counts', {})
    lines = [
        'RESEARCH STRATEGY LEDGER (anti-incrementalism telemetry; read-only summary):',
        '- Totals: experiments=%d completed=%d retracted=%d | tiers T0=%d T1=%d T2=%d T3=%d | incremental_streak=%d (since_review=%d/%d) | mode=%s | macro_review_armed=%s' % (
            snapshot.get('total_experiments', 0),
            snapshot.get('completed_experiments', 0),
            snapshot.get('retracted_experiments', 0),
            tc.get('T0', 0), tc.get('T1', 0), tc.get('T2', 0), tc.get('T3', 0),
            snapshot.get('incremental_streak', 0),
            snapshot.get('streak_since_last_review', snapshot.get('incremental_streak', 0)),
            snapshot.get('macro_review_threshold', 0),
            snapshot.get('macro_review_mode', 'advisory'),
            'YES' if snapshot.get('macro_review_armed') else 'no',
        ),
    ]
    unreadable = snapshot.get('unreadable_result_warnings') or []
    if unreadable:
        lines.append('- UNREADABLE RESULTS WARNING (%d completed row(s) yielded metric=null): %s' % (
            len(unreadable),
            ', '.join('%s(result_keys=%s)' % (u['exp_id'], u.get('result_keys') or u.get('metrics_keys') or []) for u in unreadable[:3]),
        ))
    fams = list((snapshot.get('families') or {}).values())
    fams.sort(key=lambda f: (-f.get('runs', 0), f.get('family_id', '')))
    if fams:
        lines.append('- Family saturation (top %d):' % min(8, len(fams)))
        for f in fams[:8]:
            bm = '%.4g' % f['best_metric'] if isinstance(f.get('best_metric'), (int, float)) else 'none'
            extra = ' (incubating left=%d)' % f['incubation_remaining'] if f.get('status') == 'INCUBATING' else ''
            lines.append('  * %s: runs=%d/%d tiers=%s best=%s breakthroughs=%d status=%s%s' % (
                f['family_id'], f['completed_runs'], f['runs'],
                ','.join(f.get('tiers') or ['?']), bm, f.get('breakthroughs', 0), f.get('status'), extra
            ))
    slots = (snapshot.get('portfolio') or {}).get('slots') or {}
    if slots:
        slot_strs = []
        for sname in ('exploit', 'explore'):
            s = slots.get(sname)
            if isinstance(s, dict) and (s.get('family') or s.get('family_id')):
                fam_lbl = s.get('family') or s.get('family_id')
                prot = ' [INCUBATION_PROTECTED]' if s.get('incubation_protected') else ''
                slot_strs.append('%s=%s(%s)%s' % (sname, fam_lbl, s.get('tier', '?'), prot))
        if slot_strs:
            lines.append('- Portfolio slots: ' + '; '.join(slot_strs))
    dups = snapshot.get('duplicate_warnings') or []
    if dups:
        tail = dups[-3:]
        lines.append('- Duplicate config warnings (%d): %s' % (
            len(dups),
            ', '.join('%s==%s' % (d['exp_id'], d['matched_exp_id']) for d in tail),
        ))
    tax = snapshot.get('latest_failure_taxonomy')
    if isinstance(tax, dict) and tax:
        lines.append('- Latest failure taxonomy (%s): %s | modes=%s' % (
            tax.get('turn_id', '?'),
            str(tax.get('summary', ''))[:160],
            json.dumps(tax.get('modes', []), ensure_ascii=False)[:180],
        ))
    if len(lines) > max_lines:
        lines = lines[:max_lines - 1] + ['  ... (full strategy snapshot in turn strategy-snapshot.json)']
    return '\n'.join(lines)


def mark_macro_review(root, turn_id, exp_count=None):
    root = Path(root)
    snap = analyze(root)
    count = int(exp_count) if exp_count is not None else snap['completed_experiments']
    doc = {
        'schema': 'lemvo-strategy-state',
        'schema_version': LEGACY_SNAPSHOT_SCHEMA_VERSION,
        'last_macro_review_exp_count': count,
        'last_macro_review_turn_id': str(turn_id),
        'incremental_streak': snap['incremental_streak'],
        'streak_since_last_review': max(0, snap['completed_experiments'] - count),
        'macro_review_armed': False,
        'macro_review_mode': snap.get('macro_review_mode', 'advisory'),
        'tier_counts': snap['tier_counts'],
        'updated_utc': now_utc(),
    }
    atomic_write(root / STRATEGY_STATE, doc)
    return doc


def _parse_bool(raw):
    if raw is None:
        return None
    s = str(raw).strip().lower()
    if s in ('true', 'yes', '1'):
        return True
    if s in ('false', 'no', '0'):
        return False
    raise ValueError('Expected boolean true/false, got: ' + str(raw))


def cmd_check_dup(root, family, config_json):
    cfg = json.loads(config_json)
    if not isinstance(cfg, dict) or not cfg:
        raise ValueError('--config-json must be a non-empty JSON object')
    fp = canonical_fingerprint(family, cfg)
    for rec in load_experiments(root):
        rfam, _ = infer_family(rec)
        rcfg = rec.get('config') if isinstance(rec.get('config'), dict) else rec.get('params')
        rfp = canonical_fingerprint(rfam, rcfg) if isinstance(rcfg, dict) and rcfg else None
        if rfp and rfp == fp:
            out = {
                'duplicate': True,
                'fingerprint': fp,
                'matched_exp_id': _exp_identifier(rec),
                'family': rfam,
            }
            print(json.dumps(out, ensure_ascii=False))
            return 2
    print(json.dumps({'duplicate': False, 'fingerprint': fp, 'family': family}, ensure_ascii=False))
    return 0


def cmd_log_experiment(args):
    root = Path(args.root)
    cfg = json.loads(args.config_json) if args.config_json else {}
    if not isinstance(cfg, dict):
        raise ValueError('--config-json must be a JSON object')
    fp = canonical_fingerprint(args.family, cfg) if cfg else None
    if fp and not args.allow_duplicate:
        for rec in load_experiments(root):
            if _is_lifecycle_pending(rec) and _exp_identifier(rec) == args.exp_id:
                continue
            rfam, _ = infer_family(rec)
            rcfg = rec.get('config') if isinstance(rec.get('config'), dict) else rec.get('params')
            rfp = canonical_fingerprint(rfam, rcfg) if isinstance(rcfg, dict) and rcfg else None
            if rfp == fp:
                print(json.dumps({
                    'error': 'duplicate_config',
                    'fingerprint': fp,
                    'matched_exp_id': _exp_identifier(rec),
                }, ensure_ascii=False), file=sys.stderr)
                return 2
    rec = {
        'exp_id': args.exp_id,
        'family_id': args.family,
        'intervention_tier': args.tier,
        'config': cfg,
        'recorded_utc': now_utc(),
    }
    if fp:
        rec['config_fingerprint'] = fp
    if args.metric is not None:
        rec['metric'] = args.metric
    if args.outcome is not None:
        rec['outcome'] = args.outcome
    if args.significant is not None:
        rec['significant'] = _parse_bool(args.significant)
    if args.ci_low is not None and args.ci_high is not None:
        rec['ci_low'] = args.ci_low
        rec['ci_high'] = args.ci_high
        rec['paired_ci'] = [args.ci_low, args.ci_high]
    if args.family_status is not None:
        rec['family_status'] = args.family_status
    if args.detail is not None:
        rec['detail'] = args.detail
    locked_append_jsonl(root / EXPERIMENTS_LOG, rec)
    print(json.dumps(rec, ensure_ascii=False))
    return 0


def cmd_portfolio_set(args):
    root = Path(args.root)
    pf = load_json(root / PORTFOLIO_FILE, None)
    if not isinstance(pf, dict):
        pf = {'schema': 'lemvo-portfolio', 'schema_version': LEGACY_SNAPSHOT_SCHEMA_VERSION, 'slots': {}}
    slots = pf.setdefault('slots', {})
    slots[args.slot] = {
        'family': args.family,
        'tier': args.tier,
        'status': args.status,
        'incubation_budget': max(1, int(args.incubation_budget)),
        'hypothesis': args.hypothesis or '',
        'updated_utc': now_utc(),
    }
    pf['updated_utc'] = now_utc()
    atomic_write(root / PORTFOLIO_FILE, pf)
    print(json.dumps(pf, ensure_ascii=False))
    return 0


def cmd_record_taxonomy(args):
    root = Path(args.root)
    modes = json.loads(args.modes_json)
    if not isinstance(modes, list):
        raise ValueError('--modes-json must be a JSON array')
    entry = {
        'schema': 'lemvo-failure-taxonomy',
        'schema_version': LEGACY_SNAPSHOT_SCHEMA_VERSION,
        'turn_id': args.turn_id,
        'summary': args.summary,
        'modes': modes,
        'recorded_utc': now_utc(),
    }
    locked_append_jsonl(root / TAXONOMY_LOG, entry)
    print(json.dumps(entry, ensure_ascii=False))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command')

    p = sub.add_parser('analyze')
    p.add_argument('--root', default='.')
    p.add_argument('--json', action='store_true')

    p = sub.add_parser('check-dup')
    p.add_argument('--root', default='.')
    p.add_argument('--family', required=True)
    p.add_argument('--config-json', required=True)

    p = sub.add_parser('log-experiment')
    p.add_argument('--root', default='.')
    p.add_argument('--exp-id', required=True)
    p.add_argument('--family', required=True)
    p.add_argument('--tier', required=True, choices=VALID_TIERS)
    p.add_argument('--config-json', default='{}')
    p.add_argument('--metric', type=float)
    p.add_argument('--outcome')
    p.add_argument('--significant')
    p.add_argument('--ci-low', type=float)
    p.add_argument('--ci-high', type=float)
    p.add_argument('--family-status', choices=('active', 'closed'))
    p.add_argument('--detail')
    p.add_argument('--allow-duplicate', action='store_true')

    p = sub.add_parser('portfolio-set')
    p.add_argument('--root', default='.')
    p.add_argument('--slot', required=True, choices=('exploit', 'explore'))
    p.add_argument('--family', required=True)
    p.add_argument('--tier', required=True, choices=VALID_TIERS)
    p.add_argument('--incubation-budget', type=int, default=3)
    p.add_argument('--hypothesis', default='')
    p.add_argument('--status', default='active', choices=('active', 'closed'))

    p = sub.add_parser('record-failure-taxonomy')
    p.add_argument('--root', default='.')
    p.add_argument('--turn-id', required=True)
    p.add_argument('--summary', required=True)
    p.add_argument('--modes-json', required=True)

    p = sub.add_parser('mark-macro-review')
    p.add_argument('--root', default='.')
    p.add_argument('--turn-id', required=True)
    p.add_argument('--exp-count', type=int)

    a = parser.parse_args(argv)
    if a.command == 'analyze':
        snap = analyze(a.root)
        if a.json:
            print(json.dumps(snap, ensure_ascii=False, indent=2))
        else:
            print(render_ledger_text(snap))
        return 0
    if a.command == 'check-dup':
        return cmd_check_dup(a.root, a.family, a.config_json)
    if a.command == 'log-experiment':
        return cmd_log_experiment(a)
    if a.command == 'portfolio-set':
        return cmd_portfolio_set(a)
    if a.command == 'record-failure-taxonomy':
        return cmd_record_taxonomy(a)
    if a.command == 'mark-macro-review':
        doc = mark_macro_review(a.root, a.turn_id, a.exp_count)
        print(json.dumps(doc, ensure_ascii=False))
        return 0
    parser.print_help()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
