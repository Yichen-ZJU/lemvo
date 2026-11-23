#!/usr/bin/env python3
"""Verify Lemvo runtime template parity, shared skill contract alignment, and syntax."""
import argparse
import hashlib
from pathlib import Path
import py_compile
import subprocess
import sys

CANONICAL_FILES = (
    'autoloop-reference.sh',
    'configure-event-runtime.py',
    'event-wait.py',
    'job-events.py',
    'lemvo-run.sh',
    'native-event-owner.py',
    'research-strategy.py',
    'runtime-contract.py',
    'runtime-heartbeat.py',
    'runtime-idle.py',
    'runtime-support.py',
    'work-unit.py',
)

SHARED_SKILL_FILES = (
    'skills/research-orchestrator/references/macro-strategy.md',
    'skills/research-orchestrator/references/verdict-and-gates.md',
    'skills/autoresearch/SKILL.md',
    'skills/experiment-forge/SKILL.md',
    'skills/experiment-forge/references/contract.md',
)


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def discover_trees(repo):
    if (repo / 'claude/skills/research-orchestrator/templates').is_dir():
        return [repo / 'claude', repo / 'codex']
    return [repo]


def check_tree(tree, errors):
    tpl = tree / 'skills/research-orchestrator/templates'
    hashes = {}
    for name in CANONICAL_FILES:
        p = tpl / name
        if not p.is_file():
            errors.append('Missing runtime template: %s' % p)
            continue
        hashes[name] = sha256(p)
        if name.endswith('.py'):
            try:
                py_compile.compile(str(p), doraise=True)
            except py_compile.PyCompileError as exc:
                errors.append('py_compile failed for %s: %s' % (p, exc))
        elif name.endswith('.sh'):
            res = subprocess.run(['bash', '-n', str(p)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
            if res.returncode != 0:
                errors.append('bash -n failed for %s: %s' % (p, res.stderr.strip()))
    for rel in SHARED_SKILL_FILES:
        p = tree / rel
        if not p.is_file():
            errors.append('Missing shared skill/contract file: %s' % p)
        else:
            hashes['skill:' + rel] = sha256(p)
    skill = tree / 'skills/research-orchestrator/SKILL.md'
    if skill.is_file():
        txt = skill.read_text(encoding='utf-8', errors='replace')
        if 'direction（higher|lower）' in txt or 'direction (higher|lower)' in txt:
            errors.append('Deprecated direction(higher|lower) found in %s' % skill)
    else:
        errors.append('Missing orchestrator SKILL.md: %s' % skill)
    return hashes


def check_companion_dir(repo_a, repo_b, sub, errors):
    da, db = repo_a / sub, repo_b / sub
    if not da.is_dir() or not db.is_dir():
        return
    fa = {p.relative_to(da).as_posix(): sha256(p) for p in da.rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc'}
    fb = {p.relative_to(db).as_posix(): sha256(p) for p in db.rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc'}
    if fa != fb:
        only_a = sorted(set(fa) - set(fb))
        only_b = sorted(set(fb) - set(fa))
        diff_c = sorted(k for k in set(fa) & set(fb) if fa[k] != fb[k])
        errors.append('Companion drift in %s (%s vs %s): only_a=%s only_b=%s diff=%s' % (sub, repo_a, repo_b, only_a, only_b, diff_c))


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--repo', default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument('--compare', action='append', default=[])
    args = parser.parse_args(argv)

    root = Path(args.repo).resolve()
    errors = []
    trees = discover_trees(root)
    baseline = None
    for tree in trees:
        h = check_tree(tree, errors)
        if baseline is None:
            baseline = (tree, h)
        elif h != baseline[1]:
            diffs = [k for k in sorted(set(h) | set(baseline[1])) if h.get(k) != baseline[1].get(k)]
            errors.append('Template/skill drift between %s and %s: %s' % (baseline[0], tree, diffs))

    for other_str in args.compare:
        other = Path(other_str).resolve()
        for otree in discover_trees(other):
            oh = check_tree(otree, errors)
            if baseline and oh != baseline[1]:
                diffs = [k for k in sorted(set(oh) | set(baseline[1])) if oh.get(k) != baseline[1].get(k)]
                errors.append('Cross-repo template/skill drift %s vs %s: %s' % (baseline[0], otree, diffs))
        for sub in ('console', 'desktop'):
            check_companion_dir(root, other, sub, errors)

    if errors:
        for e in errors:
            print('DRIFT_ERROR:', e, file=sys.stderr)
        return 1
    print('RUNTIME_DRIFT_OK trees=%d' % (len(trees) + sum(len(discover_trees(Path(x))) for x in args.compare)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
