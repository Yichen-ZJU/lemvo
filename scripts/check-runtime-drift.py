#!/usr/bin/env python3
"""Verify Lemvo runtime template parity, syntax compilation, and schema alignment."""
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
    'runtime-contract.py',
    'runtime-heartbeat.py',
    'runtime-idle.py',
    'runtime-support.py',
    'work-unit.py',
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def discover_trees(repo: Path):
    if (repo / 'claude/skills/research-orchestrator/templates').is_dir():
        return [repo / 'claude', repo / 'codex']
    return [repo]


def check_tree(tree: Path, errors: list):
    tpl = tree / 'skills/research-orchestrator/templates'
    hashes = {}
    for name in CANONICAL_FILES:
        p = tpl / name
        if not p.is_file():
            errors.append(f'Missing runtime template: {p}')
            continue
        hashes[name] = sha256(p)
        if name.endswith('.py'):
            try:
                py_compile.compile(str(p), doraise=True)
            except py_compile.PyCompileError as exc:
                errors.append(f'py_compile failed for {p}: {exc}')
        elif name.endswith('.sh'):
            res = subprocess.run(['bash', '-n', str(p)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if res.returncode != 0:
                errors.append(f'bash -n failed for {p}: {res.stderr.strip()}')
    skill = tree / 'skills/research-orchestrator/SKILL.md'
    if skill.is_file():
        txt = skill.read_text(encoding='utf-8', errors='replace')
        if 'direction（higher|lower）' in txt or 'direction (higher|lower)' in txt:
            errors.append(f'Deprecated direction(higher|lower) found in {skill}')
    return hashes


def check_companion_dir(repo_a: Path, repo_b: Path, sub: str, errors: list):
    da, db = repo_a / sub, repo_b / sub
    if not da.is_dir() or not db.is_dir():
        return
    fa = {p.relative_to(da).as_posix(): sha256(p) for p in da.rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc'}
    fb = {p.relative_to(db).as_posix(): sha256(p) for p in db.rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc'}
    if fa != fb:
        only_a = sorted(set(fa) - set(fb))
        only_b = sorted(set(fb) - set(fa))
        diff_c = sorted(k for k in set(fa) & set(fb) if fa[k] != fb[k])
        errors.append(f'Companion drift in {sub} ({repo_a} vs {repo_b}): only_a={only_a} only_b={only_b} diff={diff_c}')


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
            diffs = [k for k in CANONICAL_FILES if h.get(k) != baseline[1].get(k)]
            errors.append(f'Template drift between {baseline[0]} and {tree}: {diffs}')

    for other_str in args.compare:
        other = Path(other_str).resolve()
        for otree in discover_trees(other):
            oh = check_tree(otree, errors)
            if baseline and oh != baseline[1]:
                diffs = [k for k in CANONICAL_FILES if oh.get(k) != baseline[1].get(k)]
                errors.append(f'Cross-repo template drift {baseline[0]} vs {otree}: {diffs}')
        for sub in ('console', 'desktop'):
            check_companion_dir(root, other, sub, errors)

    if errors:
        for e in errors:
            print('DRIFT_ERROR:', e, file=sys.stderr)
        return 1
    print(f'RUNTIME_DRIFT_OK trees={len(trees) + sum(len(discover_trees(Path(x))) for x in args.compare)}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
