#!/usr/bin/env python3
"""Small deterministic runtime operations shared by both CLI adapters (stdlib)."""
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
import uuid


def atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name('.' + path.name + '.' + uuid.uuid4().hex)
    with open(str(tmp), 'w', encoding='utf-8') as f:
        f.write(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2))
        f.flush()
        os.fsync(f.fileno())
    os.replace(str(tmp), str(path))


def load(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return default


def epoch():
    try:
        return Path('.lemvo-session-epoch').read_text().strip() or 'legacy'
    except OSError:
        return 'legacy'


def event(kind, **fields):
    record = dict(event=kind, ts=time.time(), **fields)
    # One append per record. Contains IDs/paths, never copies user messages.
    with open('.lemvo-delivery.jsonl', 'a', encoding='utf-8') as f:
        f.write(json.dumps(record, ensure_ascii=False) + '\n')


def bootstrap():
    p = Path('research-state.yaml')
    if p.exists():
        return
    sources = [s for s in ('CHARTER.md', 'STATE.md', 'prompt.txt', 'findings.md') if Path(s).is_file()]
    # Machine accounting is initialized independently of the model or usage API.
    # No scientific hypothesis, authorization, metric or resource cap is invented.
    atomic(p, 'campaign_status: ACTIVE\nproject:\n  status: ACTIVE\n'
           '  objective: null\nbudget:\n  unit: usd\n  used: 0\n'
           '  total_cap: null\n  cap_authorized: no\n  cap_source: none\n'
           'prep:\n  prep_turns: 0\n  last_informative_artifact: none\n'
           '  last_artifact_sha: none\ntotal:\n  total_turns: 0\n'
           '  total_wall_time_min: 0.0\n  total_tokens_in: 0\n  total_tokens_out: 0\n')
    atomic('.lemvo-bootstrap.json', {'sources': sources, 'created_epoch': time.time()})


def prompt(source, target):
    mode = Path('.lemvo-mode').read_text().strip() if Path('.lemvo-mode').exists() else 'unspecified'
    delegation = Path('.lemvo-delegation').read_text().strip() if Path('.lemvo-delegation').exists() else 'auto'
    parts = ['Runtime context: mode=%s; delegation=%s.' % (mode, delegation)]
    if mode in ('guided', 'unattended'):
        parts.append('The project mode is already selected; do not ask again. Preserve existing authorization and constraints.')
    if delegation == 'inline':
        parts.append('Execute research/verification/writing steps yourself, sequentially. Do not spawn subagents. '
                     'Save each useful result before moving to the next step; retain the same evidence standards.')
    parts.append('At safe phase boundaries check .lemvo-steer-queue/q-*.md. If new guidance is waiting, '
                 'save your current checkpoint and finish this turn so the controller can deliver it. '
                 'Do not delete/archive/acknowledge queued files yourself. Background training can continue.')
    marker = load('.lemvo-bootstrap.json')
    if marker:
        parts.append('BOOTSTRAP: research-state.yaml was created as an accounting scaffold, not a completed research plan. '
                     'Read CHARTER.md, STATE.md, prompt.txt and findings.md when present, plus the user input below. '
                     'Extract the real question and existing permissions, update the research state, then produce the first useful '
                     'literature result, runnable implementation or experiment. Missing metadata alone is not a reason to keep '
                     'writing ready_blocker. If no actual research task is available, write BLOCKED-USER once with the missing input. '
                     'Remove .lemvo-bootstrap.json after the real objective and next concrete task are recorded.')
    wake = os.environ.get('LEMVO_RESOURCE_WAKE')
    if wake:
        parts.append('Wake reason: %s. Reconcile existing jobs/resources before submitting anything. '
                     'This wake grants a status assessment, not more resources or permission to duplicate a running experiment. '
                     'Update WAITING_RESOURCE if conditions changed; if still waiting, record a concrete wake condition.' % wake)
    parts.append(Path(source).read_text(encoding='utf-8'))
    atomic(target, '\n\n'.join(parts))


def wait_check(reason):
    """Persist a recheck deadline across controller restarts, without an LLM poll."""
    path = '.lemvo-resource-wait.json'
    state = Path('research-state.yaml')
    content = state.read_text(encoding='utf-8') if state.exists() else ''
    waiting = any(re.match(r'^\s*(?:campaign_status|measurement_state|root_status|status)\s*:\s*[\x22\x27]?WAITING_RESOURCE[\x22\x27]?\s*(?:#.*)?$', line, re.I)
                  for line in content.splitlines())
    if not waiting:
        if Path(path).exists(): Path(path).unlink()
        print('ready'); return
    now = time.time()
    previous = load(path, {})
    interval = max(.05, float(os.environ.get('LEMVO_WAIT_RECHECK_SEC', '900')))
    first = previous.get('since', now) if previous.get('reason') == reason else now
    last = previous.get('checked', first) if previous.get('reason') == reason else first
    pending = any(Path('.lemvo-steer-queue').glob('q-*.md'))
    receipts = any(not load(p, {}).get('consumed', True) for p in Path('.lemvo-receipts').glob('*.json'))
    wake = 'user-steer' if pending else ('job-receipt' if receipts else ('resource-recheck' if now-last >= interval else ''))
    atomic(path, {'reason': reason, 'since': first, 'checked': now if wake else last,
                  'next_check_epoch': (now if wake else last) + interval})
    print(wake or 'wait')


def process_identity(pid):
    try:
        p = int(pid)
        if p <= 0:
            return None
        fields = Path('/proc/%d/stat' % p).read_text().rsplit(')', 1)[1].split()
        return None if fields[0] == 'Z' else fields[19]
    except (OSError, ValueError, IndexError):
        return None


def progress_snapshot(target):
    """Liveness only: content changes, not rewritten state/counters/mtimes."""
    skip = {'.git', 'turns', 'models', 'data', 'datasets', 'checkpoints', 'node_modules',
            '__pycache__', 'venv', '.venv', 'poisoned-archive', 'backups'}
    metadata = {'research-state.yaml', 'STATE.md', 'campaign_log.txt', 'research-log.md',
                'CHANGELOG.md', 'CHARTER.md'}
    result = {}
    for root, dirs, files in os.walk('.'):
        dirs[:] = [d for d in dirs if not d.startswith('.') and d not in skip
                   and not d.startswith('poisoned-archive-')]
        if len(Path(root).parts) > 4:
            dirs[:] = []
        for name in files:
            path = Path(root) / name
            if name.startswith('.') or name in metadata or path.is_symlink():
                continue
            try:
                if path.stat().st_size > 16*1024*1024:
                    continue  # large results can still use prep.last_informative_artifact
                result[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                pass
    atomic(target, result)


def progress_changed(before):
    after = str(before) + '.after'
    progress_snapshot(after)
    old, new = load(before, {}), load(after, {})
    print(next((p for p in sorted(new) if old.get(p) != new[p]), ''))


def delivery(kind, list_file, turn_id=''):
    path = Path(list_file)
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip():
                if kind == 'delivered' and not Path(line.strip()).exists():
                    continue  # successful recovery already archived this message
                event(kind, message_id=Path(line.strip()).name, turn_id=turn_id)


def inventory():
    """Read-only takeover inventory, no automatic termination of unknown processes."""
    project = Path.cwd().resolve()
    ancestors = set()
    pid = os.getpid()
    while pid > 1:
        ancestors.add(pid)
        try: pid = int(Path('/proc/%d/stat' % pid).read_text().rsplit(')',1)[1].split()[1])
        except (OSError, ValueError, IndexError): break
    candidates = []
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit() or int(proc.name) in ancestors:
            continue
        try:
            if proc.stat().st_uid != os.getuid() or (proc/'cwd').resolve() != project:
                continue
            argv = (proc/'cmdline').read_bytes().split(b'\0')
            scripts = [a.decode('utf-8','replace') for a in argv if a.endswith((b'.sh', b'.py'))]
            relevant = [s for s in scripts if any(t in Path(s).name.lower() for t in ('loop','watch','retry','guard','lemvo-run'))]
            if relevant:
                candidates.append({'pid':int(proc.name),'starttime':process_identity(proc.name),
                                   'scripts':relevant})
        except (OSError, ValueError):
            continue
    return {'project':str(project), 'legacy_candidates':candidates,
            'note':'Candidates require disposition; unrelated training is never killed automatically.'}


def doctor(controller):
    report = inventory()
    current = Path(controller).resolve()
    report['invoked_controller'] = str(current)
    report['sha256'] = hashlib.sha256(current.read_bytes()).hexdigest()
    report['installed_candidates'] = []
    for root in (Path.home()/'.claude/skills', Path.home()/'.codex/skills'):
        installed = root/'research-orchestrator/templates/lemvo-run.sh'
        if installed.exists():
            digest = hashlib.sha256(installed.read_bytes()).hexdigest()
            report['installed_candidates'].append({'path':str(installed), 'sha256':digest,
                'matches_invoked':digest == report['sha256']})
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    cmd = sys.argv[1]
    if cmd == 'bootstrap': bootstrap()
    elif cmd == 'prompt': prompt(*sys.argv[2:4])
    elif cmd == 'wait-check': wait_check(sys.argv[2])
    elif cmd == 'epoch': print(epoch())
    elif cmd == 'new-epoch': atomic('.lemvo-session-epoch', uuid.uuid4().hex+'\n')
    elif cmd == 'progress-snapshot': progress_snapshot(sys.argv[2])
    elif cmd == 'progress-changed': progress_changed(sys.argv[2])
    elif cmd == 'delivery': delivery(*sys.argv[2:])
    elif cmd == 'doctor': doctor(sys.argv[2])
    else: raise SystemExit(64)
