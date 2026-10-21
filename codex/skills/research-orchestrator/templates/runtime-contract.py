#!/usr/bin/env python3
"""Deterministic wait, progress and instruction contracts. No model/process control."""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import sys
import tempfile
import time
import uuid

HARD = {'STOP', 'STOPPED', 'PAUSED'}
HUMAN = {'WAITING_USER', 'HOLD', 'EVIDENCE_HOLD', 'STOPPED_INCONCLUSIVE',
         'BLOCKED', 'BLOCKED_USER', 'BLOCKED-USER'}
DONE = {'COMPLETED', 'CONCLUDED', 'CAMPAIGN_DONE', 'CAMPAIGN-DONE'}
META = {'research-state.yaml', 'STATE.md', 'campaign_log.txt', 'research-log.md',
        'CHANGELOG.md', 'CHARTER.md'}
PROTECTED_SCOPES = {'global', 'constraints', 'authorization', 'safety'}


def atomic(path, value):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.'+p.name+'-', dir=str(p.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, str(p))
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def load(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return default


def scalar(raw):
    quote, chars = None, []
    for c in raw.strip():
        if c in "'\"":
            if quote == c:
                quote = None
            elif quote is None:
                quote = c
        if c == '#' and quote is None:
            break
        chars.append(c)
    return ''.join(chars).strip().strip("'\"")


def fields():
    """Read supported flat fields, never infer control words from prose/logs."""
    out, section = {}, ''
    p = Path('research-state.yaml')
    for line in p.read_text().splitlines() if p.exists() else []:
        m = re.match(r'^(\s*)([\w-]+)\s*:\s*(.*)$', line)
        if not m:
            continue
        indent, key, value = m.groups()
        if not indent:
            section = key
            out[key] = scalar(value)
        elif len(indent) == 2:
            out[section+'.'+key] = scalar(value)
    return out


def state():
    f = fields()
    statuses = sorted({f.get(k, '').upper() for k in
                       ('campaign_status', 'measurement_state', 'root_status', 'status', 'project.status')}- {''})
    markers = [n for n in ('STOP', 'HOLD', 'BLOCKED-USER', 'CAMPAIGN-DONE') if Path(n).is_file()]
    values = set(statuses+markers)
    # Explicit stop always wins; a completed campaign cannot be restarted by steer.
    kind = ('hard' if values & HARD else 'done' if values & DONE else
            'human' if values & HUMAN else 'resource' if 'WAITING_RESOURCE' in values else 'ready')
    reason = '|'.join(sorted(values & (HARD if kind=='hard' else DONE if kind=='done' else HUMAN))) if kind in ('hard','done','human') else kind
    question = f.get('runtime_wait.question', '')
    identity = f.get('runtime_wait.question_id', '')
    fingerprint = hashlib.sha256(json.dumps([kind, reason, question, identity]).encode()).hexdigest()
    previous = load('.lemvo-wait-state.json', {})
    if kind in ('human', 'hard'):
        if previous.get('fingerprint') != fingerprint:
            previous = {'schema':1, 'kind':kind, 'reason':reason, 'fingerprint':fingerprint,
                        'question_id':identity or 'wait-'+uuid.uuid4().hex[:16],
                        'question':question, 'since':time.time()}
            atomic('.lemvo-wait-state.json', previous)
        return previous
    if previous and previous.get('kind') != kind:
        atomic('.lemvo-wait-state.json', {'schema':1, 'kind':kind, 'closed_question_id':previous.get('question_id')})
    return {'kind':kind, 'reason':reason, 'question_id':'', 'question':''}


def shell_state():
    st = state()
    blocking = st['reason'] if st['kind'] in ('human','hard') else 'none'
    for k, v in dict(GATE_KIND=st['kind'], GATE_REASON=st['reason'],
                     WAIT_QUESTION_ID=st['question_id'], BLOCKING=blocking,
                     WAITING='yes' if st['kind']=='resource' else 'no').items():
        print(k+'='+shlex.quote(v))


def read_message(path):
    text = Path(path).read_text(encoding='utf-8')
    lines = text.splitlines()
    # Only runtime-published header line 3 is metadata. Body text cannot impersonate it.
    if len(lines)>2 and lines[2].startswith('<!-- lemvo_instruction: ') and lines[2].endswith(' -->'):
        meta = json.loads(lines[2][len('<!-- lemvo_instruction: '):-len(' -->')])
        body = '\n'.join(lines[3:])
    else:
        meta = {'instruction_id':Path(path).stem, 'scope':'task', 'supersedes':[], 'persistent':False}
        body = text
    meta['file'] = str(path)
    meta['body'] = body
    return meta


def registry():
    p = Path('.lemvo-instructions.json')
    try:
        r = json.loads(p.read_text()) if p.exists() else {'schema':1, 'revision':0, 'entries':{}}
    except ValueError:
        raise ValueError('Instruction registry corrupt: preserve it and repair; no silent reset.')
    if not isinstance(r, dict) or not isinstance(r.get('entries'), dict):
        raise ValueError('Instruction registry corrupt: preserve it and repair; no silent reset.')
    return r


@contextlib.contextmanager
def instruction_lock():
    with open('.lemvo-instructions.lock', 'a') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def incorporate(r, item):
    entries = r['entries']
    key = item['instruction_id']
    if key in entries:
        if entries[key]['body'] != item['body'] or entries[key]['scope'] != item['scope']:
            raise ValueError('Instruction ID already used with different content: '+key)
        return
    replaced = item.get('supersedes', [])
    if replaced and item['scope'] in PROTECTED_SCOPES:
        raise ValueError('Global authorization/constraint replacement needs its existing explicit procedure.')
    for old in replaced:
        if old not in entries or entries[old]['scope'] != item['scope']:
            raise ValueError('supersedes must reference an existing instruction in the same scope: '+old)
        if entries[old].get('status') == 'superseded':
            raise ValueError('Instruction already superseded; replace its current successor: '+old)
    r['revision'] += 1
    entry = dict(item, status='active', revision=r['revision'])
    entries[key] = entry
    for old in replaced:
        entries[old].update(status='superseded', superseded_by=key)


def pending():
    items = [read_message(p) for p in Path('.lemvo-steer-queue').glob('q-*.md')]
    return sorted(items, key=lambda i: (i.get('publication_seq', Path(i['file']).stat().st_mtime_ns), i['file']))


def eligible(item, st):
    reply = item.get('reply_to', '')
    return not reply or (st['kind']=='human' and reply==st.get('question_id'))


def has_guidance():
    st = state()
    return any(eligible(i, st) for i in pending())


def enqueue(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument('source')
    parser.add_argument('tag')
    parser.add_argument('--id')
    parser.add_argument('--scope', default='task')
    parser.add_argument('--supersedes', default='')
    parser.add_argument('--reply-to', default='')
    a = parser.parse_args(argv)
    for v in (a.id, a.scope, a.reply_to):
        if v and not re.match(r'^[A-Za-z0-9_.:-]{1,128}$', v):
            raise ValueError('Invalid instruction identifier/scope.')
    body = Path(a.source).read_text(encoding='utf-8')
    with instruction_lock():
        r = registry()
        for old in pending():
            incorporate(r, old)
        # Generate publication order under the same lock. Random UUID ordering
        # inside one second could otherwise put a replacement before its parent.
        seq_path = Path('.lemvo-instruction-sequence')
        seq = max(int(time.time()*1000000000),
                  int(seq_path.read_text())+1 if seq_path.exists() else 0)
        atomic(seq_path, str(seq))
        key = 'q-'+time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())+'-%020d-'%seq+uuid.uuid4().hex
        item = dict(instruction_id=a.id or key, scope=a.scope,
                    supersedes=[s for s in a.supersedes.split(',') if s],
                    reply_to=a.reply_to, persistent=bool(a.id), publication_seq=seq,
                    body=body.rstrip('\n'))
        if item['instruction_id'] in r['entries']:
            raise ValueError('Duplicate instruction ID; delivery is already recorded/queued.')
        incorporate(r, item)  # Validate without activating queued guidance mid-turn.
        header = dict(item)
        header.pop('body')
        path = Path('.lemvo-steer-queue')/(key+'.md')
        atomic(path, '<!-- message_id: %s -->\n<!-- created: %s -->\n<!-- lemvo_instruction: %s -->\n%s\n' %
               (key, time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()), json.dumps(header), item['body']))
    print(path)


def snapshot():
    """Activate at the turn boundary; failed delivery preserves immutable queue."""
    with instruction_lock():
        r, st = registry(), state()
        items = [i for i in pending() if eligible(i, st)]
        if not items:
            return
        for item in items:
            incorporate(r, item)
        atomic('.lemvo-instructions.json', r)
        selected = [i for i in items if r['entries'][i['instruction_id']]['status']=='active']
        obsolete = [i for i in items if r['entries'][i['instruction_id']]['status']=='superseded']
        fd, name = tempfile.mkstemp(prefix='lemvo-instructions-')
        os.close(fd)
        atomic(name, '\n\n'.join(Path(i['file']).read_text() for i in selected))
        atomic(name+'.list', ''.join(i['file']+'\n' for i in selected))
        atomic(name+'.superseded.list', ''.join(i['file']+'\n' for i in obsolete))
        print(name)


def context():
    r, st = registry(), state()
    active = {k:v for k,v in r['entries'].items() if v['status']=='active' and v.get('persistent')}
    text = ['Instruction revision %s. Active scoped instructions: %s.' % (r['revision'], ', '.join(active) or '(legacy appended guidance)'),
            'Explicit supersession replaces only its named task scope, never global user constraints or resource permissions. '
            'Do not revive superseded tasks from resumed conversation history.']
    for key, entry in active.items():
        text.append('[%s scope=%s]\n%s' % (key, entry['scope'], entry['body']))
    if st['kind']=='human':
        text.append('Waiting question %s: %s (%s). This call is for the new reply/advance, not automatic resource approval. '
                    'If it resolves this wait, update the corresponding research-state waiting fields and remove only the resolved HOLD/BLOCKED-USER marker. '
                    'If unresolved, retain the wait and state the specific missing decision once.' %
                    (st['question_id'], st['question'], st['reason']))
    text.append('Real code/config/launcher/evidence changes count as execution progress; a new metric is not required. '
                'Point prep.last_informative_artifact at useful output; do not write last_artifact_sha (runner-owned acknowledgement). '
                'For files outside the automatic scan, register paths before work with lemvo-run.sh track. '
                'Do not create filler notes or rewrite state just to satisfy a counter. '
                'If waiting for a human, set campaign_status: WAITING_USER with runtime_wait.question; do not keep returning empty continue turns.')
    latest = load('.lemvo-last-turn.json', {})
    if latest:
        text.append('Previous runtime receipt (not a scientific verdict): '+json.dumps(latest,ensure_ascii=False))
    print('\n\n'.join(text))


def freeze(td):
    td = Path(td)
    r = registry()
    frozen = {'revision':r['revision'], 'active':{k:v for k,v in r['entries'].items()
              if v['status']=='active' and v.get('persistent')}, 'wait':state()}
    atomic(td/'instructions.json', frozen)
    manifest = load(td/'manifest.json', {})
    manifest['instruction_revision'] = r['revision']
    manifest['active_instruction_ids'] = list(frozen['active'])
    manifest['wait_question_id'] = frozen['wait'].get('question_id')
    atomic(td/'manifest.json', manifest)


def track(paths):
    p = Path('.lemvo-progress-paths.json')
    old = load(p, [])
    if not isinstance(old, list):
        raise ValueError('Expected explicit file path list.')
    for name in paths:
        target = str(Path(name).absolute())
        if Path(target).name in META or Path(target).name.startswith('.'):
            raise ValueError('Bookkeeping cannot be registered as construction.')
        if Path(target).is_dir():
            raise ValueError('Register concrete files, not recursive trees.')
        if target not in old:
            old.append(target)
    atomic(p, old)
    print(json.dumps(old))


def digest(path):
    p = Path(path)
    if not p.exists():
        return {'state':'absent'}
    if not p.is_file() or p.name in META or p.name.startswith('.'):
        return {'state':'excluded'}
    deadline = time.monotonic()+float(os.environ.get('LEMVO_EXPLICIT_HASH_SEC','2'))
    limit = int(os.environ.get('LEMVO_EXPLICIT_HASH_BYTES', str(64*1024*1024)))
    before = p.stat()
    if before.st_size>limit:
        return {'state':'unknown', 'reason':'size-bound'}
    h = hashlib.sha256()
    try:
        with p.open('rb') as f:
            while True:
                if time.monotonic()>deadline:
                    return {'state':'unknown', 'reason':'time-bound'}
                block = f.read(65536)
                if not block:
                    break
                h.update(block)
        after = p.stat()
        if (before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_size,after.st_mtime_ns,after.st_ctime_ns):
            return {'state':'unknown', 'reason':'concurrent-write'}
        return {'state':'known', 'sha256':h.hexdigest()}
    except OSError:
        return {'state':'unknown', 'reason':'unreadable'}


def explicit_files():
    paths = load('.lemvo-progress-paths.json', [])
    if not isinstance(paths, list):
        raise ValueError('Progress registration must be a list of files.')
    ref = fields().get('prep.last_informative_artifact', '')
    if ref and ref.lower() not in ('none','null'):
        paths = paths+[ref]
    return {str(Path(p).absolute()):digest(p) for p in paths}


def pid_identity(pid):
    try:
        f=Path('/proc/%d/stat'%int(pid)).read_text().rsplit(')',1)[1].split()
        return f[19] if f[0]!='Z' else None
    except (OSError,ValueError,TypeError,IndexError):
        return None


def job_evidence():
    result = {}
    jobs = load('.lemvo-jobs.json', [])
    if not isinstance(jobs,list):
        return result
    for j in jobs:
        if not isinstance(j,dict) or not j.get('run_id'):
            continue
        run_id = str(j['run_id'])
        identity = pid_identity(j.get('pid'))
        if identity and str(j.get('pid_start'))==identity:
            result[run_id]={'state':'running','pid':j['pid'],'pid_start':identity}
        if j.get('done_file'):
            done = load(j['done_file'])
            if (isinstance(done,dict) and str(done.get('run_id'))==run_id
                and done.get('status') in ('succeeded','failed') and type(done.get('exit_code')) is int
                and (done['status']!='succeeded' or done['exit_code']==0)):
                result[run_id]={'state':done['status'],'exit_code':done['exit_code'],
                                'path':j['done_file']}
    return result


def progress_begin(td):
    atomic(Path(td)/'progress-explicit-before.json',
           {'files':explicit_files(),'jobs':job_evidence(),
            'accepted':load('.lemvo-progress-accepted.json', {'files':{},'jobs':{}})})


def progress_end(td, interrupted=False, failed=False):
    td = Path(td)
    before = load(td/'progress-before.json', {})
    after = load(td/'progress-before.json.after', {})
    broad_before, broad_after = before.get('files',{}), after.get('files',{})
    valid_before = before.get('inspection',{}).get('complete',False)
    changes = [{'path':k,'before':broad_before.get(k),'after':v,'basis':'snapshot'}
               for k,v in broad_after.items() if (k in broad_before or valid_before) and broad_before.get(k)!=v]
    explicit = load(td/'progress-explicit-before.json', {'files':{},'jobs':{},'accepted':{'files':{},'jobs':{}}})
    accepted = explicit.get('accepted', {'files':{},'jobs':{}})
    current = explicit_files()
    unknown = any(not s.get('inspection',{}).get('complete',False) for s in (before,after))
    for path, now in current.items():
        old = explicit.get('files',{}).get(path, accepted.get('files',{}).get(path))
        if now['state']=='known' and old and old.get('state') in ('known','absent') and old.get('sha256')!=now['sha256']:
            changes.append({'path':path,'before':old.get('sha256'),'after':now['sha256'],'basis':'registered'})
        elif now['state']=='unknown' or (now['state']=='known' and old is None):
            unknown = True  # first late registration is a baseline, not invented progress
    jobs = job_evidence()
    job_changes = [{'run_id':k,'evidence':v} for k,v in jobs.items()
                   if explicit.get('jobs',{}).get(k)!=v and accepted.get('jobs',{}).get(k)!=v]
    wait = state()
    progress = bool(changes or job_changes)
    incomplete_turn = interrupted or failed
    neutral = incomplete_turn or wait['kind'] in ('human','hard','resource','done') or unknown
    kind = ('interrupted' if interrupted else 'runtime-error' if failed else 'construction' if changes else 'job-transition' if job_changes
            else 'waiting' if wait['kind'] in ('human','hard','resource') else 'unknown' if unknown else 'none')
    record = {'schema':1,'turn_id':td.name,'kind':kind,'progress':progress and not incomplete_turn,
              'neutral':neutral,'changes':changes,'job_changes':job_changes,'wait':wait,
              'scientific_verdict':None}
    atomic(td/'progress.json', record)
    if not incomplete_turn:
        accepted.setdefault('files',{}).update(current)
        accepted.setdefault('jobs',{}).update(jobs)
        atomic('.lemvo-progress-accepted.json', accepted)
    print(str(td/'progress.json'))


def receipt(td):
    td = Path(td)
    r = load(td/'result.json', {})
    p = load(td/'progress.json', {})
    obj = {'turn_id':td.name,'status':r.get('status'), 'next_action':r.get('next_action'),
           'runtime_reason':r.get('runtime_reason'), 'progress_kind':p.get('kind'),
           'evidence':str(td/'result.json'), 'error_log':str(td/'engine-stderr.log'),
           'wait':p.get('wait')}
    atomic('.lemvo-last-turn.json', obj)
    atomic(td/'runtime-summary.md', 'Turn: %s — %s (rc=%s)\nEvidence: %s; stderr: %s\nNext: %s; reason: %s\n' %
           (td.name,r.get('status'),r.get('engine_rc'),td/'result.json',td/'engine-stderr.log',
            r.get('next_action'),r.get('runtime_reason') or p.get('wait',{}).get('reason','')))


def main():
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd=='state-shell': shell_state()
    elif cmd=='state': print(json.dumps(state(),ensure_ascii=False))
    elif cmd=='has-guidance': print('yes' if has_guidance() else 'no')
    elif cmd=='enqueue': enqueue(args)
    elif cmd=='snapshot': snapshot()
    elif cmd=='context': context()
    elif cmd=='freeze': freeze(args[0])
    elif cmd=='track': track(args)
    elif cmd=='progress-begin': progress_begin(args[0])
    elif cmd=='progress-end': progress_end(args[0], '--interrupted' in args, '--failed' in args)
    elif cmd=='receipt': receipt(args[0])
    else: raise ValueError('Unknown contract operation')


if __name__=='__main__':
    try:
        main()
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print('Runtime contract error: '+str(exc), file=sys.stderr)
        raise SystemExit(4)
