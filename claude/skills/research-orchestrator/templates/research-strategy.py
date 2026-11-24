#!/usr/bin/env python3
"""Research strategy telemetry, intervention-tier tracking, and macro-review gate.

This module provides deterministic experiment family saturation analysis,
config duplicate detection, portfolio incubation tracking, failure-taxonomy
persistence, and plateau/incrementalism detection for Lemvo v2.1.
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


VERSION = 1
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
_NULL_VERDICT_PATTERNS = (
    'discard', 'null', 'below gate', 'below_gate', 'rejected', 'saturating',
    'additive-neutral', 'not confirmed', 'acceptance gate missed',
    'within seed band', 'within +/-', 'null on primary', 'duplicate-of',
    'same-config replicate', 'does not exceed', 'missed by', 'below every component',
)
_FAMILY_CLOSED_NEGATIONS = (
    'family remains open', 'family open', 'keep family', 'direction remains open',
    'continue family', 'remains open',
)
_FAMILY_CLOSED_RE = re.compile(
    r'\bfamil(?:y|ies)\s+(?:is\s+|are\s+)?closed\b|\bclose_family\b|\bfamily_status\s*[:=]\s*closed\b',
    re.I,
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


def _state_strategy_hints(root):
    p = Path(root) / 'research-state.yaml'
    if not p.is_file():
        return {}
    try:
        raw = p.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return {}
    hints = {}
    in_strategy = False
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        if re.match(r'^strategy\s*:', line):
            in_strategy = True
            continue
        if in_strategy and not line.startswith((' ', '\t')):
            in_strategy = False
        m = re.match(r'^\s*([A-Za-z_][\w-]*)\s*:\s*([^#\n]+)', line)
        if not m:
            continue
        k, v = m.group(1), m.group(2).strip().strip('"\'')
        if k == 'direction':
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
    return hints


def load_policy(root=Path('.')):
    root = Path(root)
    pol = dict(DEFAULT_POLICY)
    pol.update(_state_strategy_hints(root))
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
        rm = _normalize_review_mode(file_pol.get('macro_review_mode'))
        if rm:
            pol['macro_review_mode'] = rm
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
    return pol


def load_experiments(root=Path('.')):
    root = Path(root)
    records = []
    jp = root / EXPERIMENTS_LOG
    if jp.is_file():
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
                        obj['_source'] = 'experiments.jsonl'
                        records.append(obj)
        except OSError:
            pass
    if records:
        return records
    tp = root / RESULTS_TSV
    if tp.is_file():
        try:
            lines = tp.read_text(encoding='utf-8', errors='replace').splitlines()
        except OSError:
            return records
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
                '_source': 'results.tsv',
            }
            if len(cols) > 9 and cols[9].strip().upper() in VALID_TIERS:
                rec['intervention_tier'] = cols[9].strip().upper()
            if len(cols) > 10 and cols[10].strip():
                rec['family_id'] = cols[10].strip()
            records.append(rec)
    return records


def _extract_metric(record):
    for k in ('metric', 'four_grid', 'val_score', 'score', 'value'):
        v = _to_float(record.get(k))
        if v is not None:
            return v
    return None


def _exp_identifier(record):
    for k in ('exp_id', 'experiment', 'arm', 'name', 'run_id'):
        v = record.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return str(record.get('cycle') or record.get('id') or 'unnamed')


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
    if metric is None and (not status_text or 'pending' in status_text or 'running' in status_text):
        return False
    if metric is None and 'discard' in status_text:
        return False
    return True


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


def canonical_fingerprint(family, config_dict):
    if not isinstance(config_dict, dict) or not config_dict:
        return None
    blob = json.dumps(
        {'family': str(family), 'config': config_dict},
        sort_keys=True, separators=(',', ':'), ensure_ascii=False,
    )
    return hashlib.sha256(blob.encode('utf-8')).hexdigest()[:16]


def _extract_ci(record):
    for k in ('paired_ci', 'ci_95', 'bootstrap95ci_pp', 'ci'):
        v = record.get(k)
        if isinstance(v, (list, tuple)) and len(v) == 2:
            lo, hi = _to_float(v[0]), _to_float(v[1])
            if lo is not None and hi is not None:
                return min(lo, hi), max(lo, hi)
        if isinstance(v, str):
            m = re.search(r'\[\s*([+-]?\d+(?:\.\d+)?)\s*,\s*([+-]?\d+(?:\.\d+)?)\s*\]', v)
            if m:
                lo, hi = float(m.group(1)), float(m.group(2))
                return min(lo, hi), max(lo, hi)
    lo, hi = _to_float(record.get('ci_low')), _to_float(record.get('ci_high'))
    if lo is not None and hi is not None:
        return min(lo, hi), max(lo, hi)
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
        return min(lo, hi), max(lo, hi)
    return None


def _has_null_verdict(record):
    txt = ' '.join(
        str(record.get(k) or '')
        for k in ('outcome', 'verdict', 'detail', 'notes', 'description')
    ).lower()
    for pat in _NULL_VERDICT_PATTERNS:
        if pat in txt:
            if pat == 'null on primary' and 'excludes 0' in txt and 'significant' in txt:
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
    if record.get('close_family') is True:
        return True
    if str(record.get('family_status') or '').strip().lower() == 'closed':
        return True
    if str(record.get('family_decision') or '').strip().lower() in ('closed', 'close_family', 'retire_family'):
        return True
    txt = ' '.join(str(record.get(k) or '') for k in ('outcome', 'verdict', 'detail'))
    low = txt.lower()
    if any(neg in low for neg in _FAMILY_CLOSED_NEGATIONS):
        return False
    return bool(_FAMILY_CLOSED_RE.search(txt))


def _is_explicit_family_reopen(record):
    if record.get('reopen_family') is True:
        return True
    if str(record.get('family_status') or '').strip().lower() == 'active':
        return True
    return False


def is_breakthrough(record, best_so_far, noise_band, direction='maximize'):
    if not is_completed(record):
        return False
    if _has_duplicate_marker(record):
        return False
    if _has_null_verdict(record):
        return False

    metric = _extract_metric(record)
    delta = None
    if metric is not None and best_so_far is not None:
        delta = (best_so_far - metric) if direction == 'minimize' else (metric - best_so_far)
        if delta <= 0.0:
            return False

    sig = record.get('significant')
    if sig is False:
        return False

    ci = _extract_ci(record)
    if ci is not None:
        lo, hi = ci
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

    tier_counts = {t: 0 for t in VALID_TIERS}
    families = {}
    seen_fps = {}
    duplicate_warnings = []
    total_experiments = len(records)
    completed_experiments = 0
    incremental_streak = 0
    anchor_best = None
    direction = pol['direction']
    noise_band = pol['noise_band']

    for rec in records:
        exp_id = _exp_identifier(rec)
        tier, _ = infer_tier(rec)
        fam, _ = infer_family(rec)
        tier_counts[tier] += 1
        completed = is_completed(rec)
        metric = _extract_metric(rec)

        cfg = rec.get('config') if isinstance(rec.get('config'), dict) else rec.get('params')
        fp = canonical_fingerprint(fam, cfg) if isinstance(cfg, dict) and cfg else None
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
            'tiers': [],
            'best_metric': None,
            'breakthroughs': 0,
            'recent_outcomes': [],
            'explicit_family_closed': False,
            'status': 'ACTIVE',
            'incubation_remaining': 0,
        })
        finfo['runs'] += 1
        if tier not in finfo['tiers']:
            finfo['tiers'].append(tier)
        if _is_explicit_family_closure(rec):
            finfo['explicit_family_closed'] = True
        elif _is_explicit_family_reopen(rec):
            finfo['explicit_family_closed'] = False

        if completed:
            completed_experiments += 1
            finfo['completed_runs'] += 1
            if metric is not None:
                if finfo['best_metric'] is None:
                    finfo['best_metric'] = metric
                elif direction == 'minimize':
                    finfo['best_metric'] = min(finfo['best_metric'], metric)
                else:
                    finfo['best_metric'] = max(finfo['best_metric'], metric)

            bt = is_breakthrough(rec, anchor_best, noise_band, direction)
            finfo['recent_outcomes'].append({'bt': bt})
            if anchor_best is None and metric is not None:
                anchor_best = metric
                if tier in ('T2', 'T3') and bt:
                    finfo['breakthroughs'] += 1
            elif tier in ('T2', 'T3'):
                incremental_streak = 0
                if bt:
                    finfo['breakthroughs'] += 1
                    if metric is not None:
                        anchor_best = metric
            else:
                if bt:
                    finfo['breakthroughs'] += 1
                    incremental_streak = 0
                    if metric is not None:
                        anchor_best = metric
                else:
                    if not (tier == 'T0' and 'baseline' in exp_id.lower() and completed_experiments == 1):
                        incremental_streak += 1

    slots = portfolio.get('slots', {}) if isinstance(portfolio.get('slots'), dict) else {}
    explore_families = {}
    for slot_name, slot in slots.items():
        if not isinstance(slot, dict) or not slot.get('family'):
            continue
        sfam = str(slot['family'])
        families.setdefault(sfam, {
            'family_id': sfam,
            'runs': 0,
            'completed_runs': 0,
            'tiers': [slot.get('tier')] if slot.get('tier') in VALID_TIERS else [],
            'best_metric': None,
            'breakthroughs': 0,
            'recent_outcomes': [],
            'explicit_family_closed': False,
            'status': 'ACTIVE',
            'incubation_remaining': 0,
        })
        if slot_name == 'explore':
            explore_families[sfam] = slot

    sat_runs = pol['family_saturation_runs']
    for fam, finfo in families.items():
        slot = explore_families.get(fam)
        inc_budget = int(slot.get('incubation_budget') or pol['incubation_budget']) if slot else pol['incubation_budget']
        cruns = finfo['completed_runs']
        recent = finfo['recent_outcomes'][-sat_runs:]
        explicitly_closed = bool(
            (slot and str(slot.get('status') or '').lower() == 'closed')
            or finfo.get('explicit_family_closed')
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
            sfam = item.get('family')
            if sfam and sfam in families:
                fstatus = families[sfam]['status']
                item['completed_runs'] = families[sfam]['completed_runs']
                item['incubation_remaining'] = families[sfam]['incubation_remaining']
                item['incubation_protected'] = (sname == 'explore' and fstatus == 'INCUBATING')
            enriched_slots[sname] = item
        enriched_portfolio['slots'] = enriched_slots

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
        'schema_version': VERSION,
        'total_experiments': total_experiments,
        'completed_experiments': completed_experiments,
        'tier_counts': tier_counts,
        'incremental_streak': incremental_streak,
        'streak_since_last_review': streak_since_last_review,
        'macro_review_threshold': thr,
        'macro_review_mode': pol['macro_review_mode'],
        'noise_band_used': noise_band,
        'direction': direction,
        'anchor_best': anchor_best,
        'families': families,
        'portfolio': enriched_portfolio,
        'duplicate_warnings': duplicate_warnings,
        'latest_failure_taxonomy': taxonomies[-1] if taxonomies else None,
        'last_macro_review_exp_count': last_mr_count,
        'macro_review_armed': armed,
    }


def render_ledger_text(snapshot, max_lines=30):
    tc = snapshot.get('tier_counts', {})
    lines = [
        'RESEARCH STRATEGY LEDGER (anti-incrementalism telemetry; read-only summary):',
        '- Totals: experiments=%d completed=%d | tiers T0=%d T1=%d T2=%d T3=%d | incremental_streak=%d (since_review=%d/%d) | mode=%s | macro_review_armed=%s' % (
            snapshot.get('total_experiments', 0),
            snapshot.get('completed_experiments', 0),
            tc.get('T0', 0), tc.get('T1', 0), tc.get('T2', 0), tc.get('T3', 0),
            snapshot.get('incremental_streak', 0),
            snapshot.get('streak_since_last_review', snapshot.get('incremental_streak', 0)),
            snapshot.get('macro_review_threshold', 0),
            snapshot.get('macro_review_mode', 'advisory'),
            'YES' if snapshot.get('macro_review_armed') else 'no',
        ),
    ]
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
            if isinstance(s, dict) and s.get('family'):
                prot = ' [INCUBATION_PROTECTED]' if s.get('incubation_protected') else ''
                slot_strs.append('%s=%s(%s)%s' % (sname, s.get('family'), s.get('tier', '?'), prot))
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
        'schema_version': VERSION,
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
    p = root / PORTFOLIO_FILE
    doc = load_json(p, None)
    if not isinstance(doc, dict):
        doc = {'schema': 'lemvo-portfolio', 'schema_version': VERSION, 'slots': {}}
    slots = doc.setdefault('slots', {})
    slot = {
        'family': args.family,
        'tier': args.tier,
        'incubation_budget': args.incubation_budget,
        'hypothesis': args.hypothesis or '',
        'status': args.status,
        'updated_utc': now_utc(),
    }
    slots[args.slot] = slot
    doc['updated_utc'] = now_utc()
    atomic_write(p, doc)
    print(json.dumps(doc, ensure_ascii=False))
    return 0


def cmd_record_taxonomy(args):
    root = Path(args.root)
    modes = json.loads(args.modes_json)
    if not isinstance(modes, list):
        raise ValueError('--modes-json must be a JSON array')
    entry = {
        'schema': 'lemvo-failure-taxonomy',
        'schema_version': VERSION,
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
