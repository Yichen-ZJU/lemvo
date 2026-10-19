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
import fnmatch
import sqlite3
import math
import signal


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
    inspection=load('.lemvo-progress-inspection.json',{})
    if inspection and not inspection.get('complete',True):
        parts.append('Automatic progress inspection was incomplete due to its I/O bounds, not a negative research result. '
                     'Point prep.last_informative_artifact to the actual construction/evidence file and exclude unrelated '
                     'asset trees using .lemvo-snapshot-exclude. Do not create filler output to satisfy a counter.')
    wait_alert=load('.lemvo-wait-alert.json')
    if wait_alert:
        parts.append('Latest wait observation: '+json.dumps(wait_alert)+'. Reassess the actual launch dependency; '
                     'if waiting continues, record the concrete time/process/heartbeat/memory condition in .lemvo-wait.json. '
                     'A dead launcher is an execution problem, not a failed scientific hypothesis.')
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
    previous = load(path, {})
    if not waiting and not previous.get('adapter_wait'):
        if Path(path).exists(): Path(path).unlink()
        print('ready'); return
    now = time.time()
    interval = max(.05, float(os.environ.get('LEMVO_WAIT_RECHECK_SEC', '900')))
    first = previous.get('since', now) if previous.get('reason') == reason else now
    last = previous.get('checked', first) if previous.get('reason') == reason else first
    pending = any(Path('.lemvo-steer-queue').glob('q-*.md'))
    receipts = any(not load(p, {}).get('consumed', True) for p in Path('.lemvo-receipts').glob('*.json'))
    condition, identity = wait_condition(now)
    changed = identity != previous.get('condition_identity') or condition != previous.get('condition_state')
    wake = 'user-steer' if pending else ('job-receipt' if receipts else
            (condition if condition and changed else ('resource-recheck' if now-last >= interval else '')))
    ticks = 0 if wake else int(previous.get('ticks', 0)) + 1
    poll = min(float(os.environ.get('LEMVO_WAIT_MAX_POLL_SEC', '900')),
               float(os.environ.get('LEMVO_POLL_SEC', '15')) * (4 ** min(ticks-1, 3)))
    poll = max(.01, min(poll, interval - (0 if wake else now-last)))
    atomic(path, {'reason': reason, 'since': first, 'checked': now if wake else last,
                  'next_check_epoch': (now if wake else last) + interval,
                  'ticks':ticks, 'poll_sec':poll, 'condition_state':condition,
                  'condition_identity':identity, 'adapter_wait':previous.get('adapter_wait', False) and not bool(wake)})
    if wake:
        alert={'reason':wake,'since':first,'assessed_epoch':now,'condition_identity':identity,
               'action':'reassess-existing-permissions','scientific_verdict':None}
        atomic('.lemvo-wait-alert.json',alert)
        with open('.lemvo-wait-events.jsonl','a') as f: f.write(json.dumps(alert)+'\n')
    print(wake or 'wait')


def wait_condition(now):
    """Optional declarative observations. Never execute commands or grant resources."""
    p=Path('.lemvo-wait.json')
    if not p.exists(): return '', 'unspecified'
    spec=load(p)
    if not isinstance(spec,dict): return 'wait-contract-invalid', 'invalid'
    identity=hashlib.sha256(json.dumps(spec,sort_keys=True).encode()).hexdigest()
    try:
        if spec.get('deadline_epoch') is not None and now>=float(spec['deadline_epoch']):
            return 'wait-deadline', identity
        conditions=spec.get('conditions',[])
        if not isinstance(conditions,list): raise ValueError('conditions')
        ready=[]
        for item in conditions:
            kind=item['kind']
            if kind=='time': ready.append(now>=float(item['not_before_epoch']))
            elif kind=='process':
                if not item.get('pid_start'): raise ValueError('process requires pid_start')
                if process_identity(item['pid'])!=str(item['pid_start']): return 'wait-source-ended', identity
            elif kind=='heartbeat':
                heartbeat=Path(item['path'])
                if not heartbeat.is_file() or now-heartbeat.stat().st_mtime>float(item['max_age_sec']):
                    return 'wait-source-stale', identity
            elif kind=='memory':
                available=next(int(line.split()[1])*1024 for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:'))
                ready.append(available>=int(item['min_available_bytes']))
            else: raise ValueError('unknown wait observation')
        return ('wait-condition-ready' if ready and all(ready) else ''), identity
    except (KeyError,ValueError,TypeError,OSError,StopIteration):
        return 'wait-contract-invalid',identity


def register_wait():
    p=load('.lemvo-resource-wait.json',{})
    p.update(reason='WAITING_RESOURCE',adapter_wait=True,checked=time.time(),ticks=0)
    p.setdefault('since',time.time())
    atomic('.lemvo-resource-wait.json',p)


def process_identity(pid):
    try:
        p = int(pid)
        if p <= 0:
            return None
        fields = Path('/proc/%d/stat' % p).read_text().rsplit(')', 1)[1].split()
        return None if fields[0] == 'Z' else fields[19]
    except (OSError, ValueError, TypeError, IndexError):
        return None


def progress_snapshot(target):
    """Bounded content inspection. Incomplete coverage is unknown, never no progress."""
    skip = {'.git', 'turns', 'models', 'data', 'datasets', 'checkpoints', 'node_modules',
            '__pycache__', 'venv', '.venv', 'poisoned-archive', 'backups'}
    metadata = {'research-state.yaml', 'STATE.md', 'campaign_log.txt', 'research-log.md',
                'CHANGELOG.md', 'CHARTER.md'}
    exclusions=Path('.lemvo-snapshot-exclude')
    patterns=[s.strip() for s in exclusions.read_text().splitlines() if s.strip() and not s.lstrip().startswith('#')] if exclusions.exists() else []
    max_entries=max(1,int(os.environ.get('LEMVO_SNAPSHOT_MAX_ENTRIES','5000')))
    max_bytes=max(1,int(os.environ.get('LEMVO_SNAPSHOT_MAX_BYTES',str(64*1024*1024))))
    seconds=max(.01,float(os.environ.get('LEMVO_SNAPSHOT_MAX_SEC','2')))
    started=time.monotonic(); deadline=started+seconds
    db=sqlite3.connect('.lemvo-snapshot-cache.sqlite3',timeout=2)
    db.execute('CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY,sig TEXT,digest TEXT)')
    result={}; stats={'entries':0,'bytes_read':0,'cache_hits':0,'complete':True,'started_epoch':time.time()}
    priority=[p for p in ('src','scripts','results','outputs','tests') if Path(p).is_dir()]
    stack=[('.',0)]+[(p,1) for p in reversed(priority)]
    visited=set()
    while stack:
        root,depth=stack.pop()
        if root in visited: continue
        visited.add(root)
        try:
            with os.scandir(root) as entries:
                for ent in entries:
                    if stats['entries']>=max_entries or time.monotonic()>=deadline:
                        stats['complete']=False; break
                    stats['entries']+=1
                    path=Path(ent.path); rel=path.as_posix().lstrip('./'); name=ent.name
                    if name.startswith('.') or name in skip or name in metadata or name.startswith('poisoned-archive-') or ent.is_symlink(): continue
                    if any(fnmatch.fnmatch(rel,p.rstrip('/')) or fnmatch.fnmatch(rel,p.rstrip('/')+'/*') for p in patterns): continue
                    if ent.is_dir(follow_symlinks=False):
                        if depth<4: stack.append((rel,depth+1))
                        continue
                    if not ent.is_file(follow_symlinks=False): continue
                    st=ent.stat(follow_symlinks=False)
                    if st.st_size>16*1024*1024: continue
                    sig=json.dumps([st.st_size,st.st_mtime_ns,st.st_ctime_ns,st.st_ino,st.st_dev])
                    cached=db.execute('SELECT sig,digest FROM files WHERE path=?',(rel,)).fetchone()
                    if cached and cached[0]==sig:
                        digest=cached[1]; stats['cache_hits']+=1
                    else:
                        if stats['bytes_read']+st.st_size>max_bytes: stats['complete']=False; break
                        digest=hashlib.sha256()
                        with path.open('rb') as f:
                            while True:
                                if time.monotonic()>=deadline: stats['complete']=False; break
                                chunk=f.read(65536)
                                if not chunk: break
                                digest.update(chunk); stats['bytes_read']+=len(chunk)
                        if not stats['complete']: break
                        after=path.stat()
                        if (after.st_size,after.st_mtime_ns,after.st_ctime_ns)!=(st.st_size,st.st_mtime_ns,st.st_ctime_ns):
                            stats['complete']=False; break
                        digest=digest.hexdigest()
                        db.execute('INSERT OR REPLACE INTO files VALUES(?,?,?)',(rel,sig,digest))
                    result[rel]=digest
        except OSError: stats['complete']=False
        if not stats['complete']: break
    db.commit(); db.close()
    stats['elapsed_sec']=time.monotonic()-started
    atomic(target,{'schema':2,'files':result,'inspection':stats})
    atomic('.lemvo-progress-inspection.json',stats)


def progress_changed(before):
    after = str(before) + '.after'
    progress_snapshot(after)
    old, new = load(before, {}), load(after, {})
    oldfiles=old.get('files',old); newfiles=new.get('files',new)
    complete=old.get('inspection',{}).get('complete',True)
    # A file absent from an incomplete old sample is not evidence that it is new.
    print(next((p for p in sorted(newfiles) if (p in oldfiles or complete) and oldfiles.get(p) != newfiles[p]), ''))


def snapshot_incomplete(before):
    print('yes' if any(not load(p,{}).get('inspection',{}).get('complete',True)
                       for p in (before,str(before)+'.after')) else 'no')


def recovery_db():
    db=sqlite3.connect('.lemvo-recovery.sqlite3',timeout=5)
    db.execute('CREATE TABLE IF NOT EXISTS turns(path TEXT PRIMARY KEY,state TEXT,reason TEXT,updated REAL)')
    db.execute('CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT)')
    return db


def turn_path(value):
    p=Path(value).resolve(); root=Path('turns').resolve()
    if p.parent!=root or p.is_symlink() or not p.is_dir(): raise ValueError('not a direct turn directory')
    return p, 'turns/'+p.name


def dir_stamp():
    if not Path('turns').exists(): return 'absent'
    st=Path('turns').stat(); return json.dumps([st.st_dev,st.st_ino,st.st_mtime_ns])


def turn_register(value):
    _,rel=turn_path(value)
    with recovery_db() as db:
        db.execute('INSERT OR REPLACE INTO turns VALUES(?,?,?,?)',(rel,'pending','registered',time.time()))
        # Do not mask undiscovered legacy turns. Discovery owns its directory stamp.


def recovery_mark(value,state,reason=''):
    p,rel=turn_path(value)
    atomic(p/'recovery.json',{'state':state,'reason':reason,'epoch':epoch(),'at':time.time()})
    if state == 'settled':
        manifest=load(p/'manifest.json',{})
        manifest['status']='settled'
        atomic(p/'manifest.json',manifest)
    with recovery_db() as db:
        db.execute('INSERT OR REPLACE INTO turns VALUES(?,?,?,?)',(rel,state,reason,time.time()))


def archive_prelaunch(p,why):
    """Recoverable rename, only when no CLI identity/output exists. Never ack guidance."""
    if (p/'engine.json').exists() or (p/'engine-stdout.jsonl').exists(): return False
    day=time.strftime('%Y-%m-%d',time.gmtime())
    parent=Path('.lemvo-turn-archive')/day; parent.mkdir(parents=True,exist_ok=True)
    dest=parent/p.name
    if dest.exists(): return False
    manifest=load(p/'manifest.json',{})
    atomic(p/'recovery.json',{'state':'not-dispatched','reason':why,'at':time.time()})
    os.rename(str(p),str(dest))
    with (parent/'summary.jsonl').open('a') as f:
        f.write(json.dumps({'turn_id':p.name,'archive':str(dest),'reason':why,
                            'started_utc':manifest.get('started_utc'),'engine_called':False})+'\n')
    return True


def preflight_close(value,reason):
    p,rel=turn_path(value)
    if archive_prelaunch(p,reason):
        with recovery_db() as db:
            db.execute('INSERT OR REPLACE INTO turns VALUES(?,?,?,?)',(rel,'archived',reason,time.time()))
    else:
        recovery_mark(value,'needs-review','rc7 after CLI identity/output; preserved')


def recoverable_output(path):
    """Bounded terminal check before launching a recovery adapter per historical row."""
    with path.open('rb') as f:
        f.seek(0,2); size=f.tell(); f.seek(max(0,size-8*1024*1024))
        text=f.read(8*1024*1024).decode('utf-8','replace')
    try:
        raw=json.loads(text); events=raw if isinstance(raw,list) else [raw]
    except ValueError:
        events=[]
        for line in text.splitlines():
            try: events.append(json.loads(line))
            except ValueError: pass
    success=False
    for ev in events:
        if not isinstance(ev,dict): continue
        if ev.get('type') in ('event','event_msg') and isinstance(ev.get('payload'),dict): ev=ev['payload']
        if ev.get('type')=='turn.completed': success=True
        elif ev.get('type') in ('turn.failed','error'): success=False
        elif ev.get('type')=='result': success=not ev.get('is_error',False) and not str(ev.get('subtype','')).startswith('error')
    return success


def recovery_plan():
    """Batch index discovery and classification; steady-state reads pending rows only."""
    Path('turns').mkdir(exist_ok=True)
    start=time.monotonic(); seconds=float(os.environ.get('LEMVO_RECOVERY_SCAN_SEC','2'))
    limit=int(os.environ.get('LEMVO_RECOVERY_BATCH','1000'))
    actions=[]; inspected=0; discovered=0; needs_more=False
    db=recovery_db()
    stored=db.execute('SELECT value FROM meta WHERE key="dir_stamp"').fetchone()
    stamp=dir_stamp()
    if not stored or stored[0]!=stamp:
        done=True
        with os.scandir('turns') as entries:
            for ent in entries:
                if time.monotonic()-start>seconds:
                    done=False; break
                if ent.is_dir(follow_symlinks=False):
                    db.execute('INSERT OR IGNORE INTO turns VALUES(?,?,?,?)',('turns/'+ent.name,'pending','discovered',time.time()))
                    discovered+=1
        if done: db.execute('INSERT OR REPLACE INTO meta VALUES("dir_stamp",?)',(stamp,))
        else: needs_more=True
    rows=db.execute('SELECT path FROM turns WHERE state="pending" ORDER BY updated,path LIMIT ?', (limit,)).fetchall()
    for (rel,) in rows:
        if time.monotonic()-start>seconds: needs_more=True; break
        inspected+=1; p=Path(rel)
        state='pending'; reason=''
        if not p.is_dir(): state='missing'
        else:
            marker=load(p/'recovery.json',{})
            manifest=load(p/'manifest.json',{})
            result=load(p/'result.json',{})
            ej=load(p/'engine.json',{})
            live=any(process_identity(ej.get(k))==str(ej.get(s)) and ej.get(s) is not None
                     for k,s in (('adapter_pid','adapter_start'),('pid_engine','pid_engine_start')))
            if live:
                actions.append(('wait',rel))
            elif marker.get('state') in ('settled','needs-review','not-dispatched'):
                state=marker['state']
            elif manifest.get('session_epoch','legacy')!=epoch(): state='old-epoch'
            elif (p/'timeout.json').exists():
                state='needs-review'; reason='timed-out turn; never acknowledge guidance as successful'
            elif result.get('status') in ('succeeded','recovered-orphan'):
                actions.append(('ack',rel))
            elif result.get('status') or manifest.get('status')=='settled': state='settled'
            elif not manifest:
                state='needs-review'; reason='missing/corrupt manifest'
            elif not (p/'engine.json').exists() and not (p/'engine-stdout.jsonl').exists():
                owner=manifest.get('controller_pid')
                if process_identity(owner) and str(owner)!=os.environ.get('LEMVO_RECOVERY_OWNER',''):
                    actions.append(('wait',rel))
                elif archive_prelaunch(p,'abandoned before CLI launch'):
                    state='archived'; reason='no CLI launched'
                else: state='needs-review'; reason='archive conflict'
            elif (p/'engine-stdout.jsonl').is_file() and (p/'engine-stdout.jsonl').stat().st_size:
                if recoverable_output(p/'engine-stdout.jsonl'): actions.append(('recover',rel))
                else: state='needs-review'; reason='no recognized successful terminal; no adapter replay'
            else:
                state='needs-review'; reason='no recoverable CLI output'
            if state=='needs-review':
                atomic(p/'recovery.json',{'state':state,'reason':reason,'at':time.time()})
        if state!='pending':
            db.execute('UPDATE turns SET state=?,reason=?,updated=? WHERE path=?',(state,reason,time.time(),rel))
    pending=db.execute('SELECT COUNT(*) FROM turns WHERE state="pending"').fetchone()[0]
    needs_more=needs_more or pending>len(actions)
    # Renames during this batch have been indexed; the next scan only needs new entries.
    if stored and stored[0]==stamp or not needs_more:
        db.execute('INSERT OR REPLACE INTO meta VALUES("dir_stamp",?)',(dir_stamp(),))
    db.commit(); db.close()
    atomic('.lemvo-recovery-status.json',{'inspected':inspected,'discovered_names':discovered,
        'pending':pending,'needs_more':needs_more,'elapsed_sec':time.monotonic()-start})
    # Any live writer takes precedence over recovery/dispatch.
    actions.sort(key=lambda a:0 if a[0]=='wait' else 1)
    for action,rel in actions[:int(os.environ.get('LEMVO_RECOVERY_MAX_ACTIONS','16'))]: print(action+'|'+rel)
    if needs_more or len(actions)>int(os.environ.get('LEMVO_RECOVERY_MAX_ACTIONS','16')): print('more|indexing')


def repair_prep(archive,reason,apply=False):
    """Conservative, explicit repair: subtract provably unlaunched recovered turns only."""
    if not reason.strip(): raise ValueError('repair reason required')
    root=Path(archive).resolve()
    if root.parent!=Path.cwd() or not root.name.startswith('poisoned-archive-'): raise ValueError('expected project-local incident archive')
    state=Path('research-state.yaml'); raw=state.read_text()
    pending=load('.lemvo-prep-repair-pending.json')
    if pending:
        if not apply:
            print(json.dumps({'pending_repair':pending,'action':'repeat --apply after writers stop'})); return
        finish_prep_repair(pending); return
    lines=raw.splitlines(); inside=False; index=None
    for n,line in enumerate(lines):
        if re.match(r'^prep:\s*(?:#.*)?$',line): inside=True; continue
        if line and not line[0].isspace(): inside=False
        if inside and re.match(r'^\s+prep_turns:',line):
            if index is not None: raise ValueError('ambiguous prep counter')
            index=n
    if index is None: raise ValueError('missing prep counter')
    before=int(lines[index].split(':',1)[1].split('#',1)[0].strip().strip('\"\''))
    ledger=Path('.autoloop-prep-ledger.jsonl'); events=[]
    for line in ledger.read_text().splitlines():
        try: events.append(json.loads(line))
        except ValueError: pass
    already={t for e in events if e.get('type')=='prep-counter-repair' for t in e.get('invalid_turn_ids',[])}
    invalid=set()
    for ev in reversed(events):
        if ev.get('type') in ('work-turn','prep-turn','recovered-turn') and ev.get('prep_turns')==0: break
        if ev.get('type')!='recovered-turn': continue
        tid=ev.get('turn_id','')
        if not tid or '/' in tid or tid in already: continue
        turn=root/'turns'/tid
        if (turn/'manifest.json').is_file() and not (turn/'engine.json').exists() and not (turn/'engine-stdout.jsonl').exists(): invalid.add(tid)
    if len(invalid)>before: raise ValueError('repair exceeds current prep counter; manual reconciliation needed')
    record={'type':'prep-counter-repair','before':before,'after':before-len(invalid),
            'invalid_turn_ids':sorted(invalid),'archive':str(root),'reason':reason,'ts':time.time(),
            'repair_id':uuid.uuid4().hex,
            'unchanged':'total turns, token/USD/GPU statistics, authorization caps, research evidence'}
    if apply and invalid:
        if state.read_text()!=raw: raise ValueError('state changed during repair')
        lines[index]=re.sub(r'(:).*',r'\1 '+str(record['after']),lines[index])
        after='\n'.join(lines)+'\n'
        backup=Path('.lemvo-prep-repairs')/record['repair_id']
        atomic(backup/'state-before.yaml',raw); atomic(backup/'state-after.yaml',after)
        record.update(before_sha=hashlib.sha256(raw.encode()).hexdigest(),after_sha=hashlib.sha256(after.encode()).hexdigest(),backup=str(backup))
        atomic('.lemvo-prep-repair-pending.json',record)
        finish_prep_repair(record); return
    print(json.dumps(record,indent=2))


def finish_prep_repair(record):
    pidfile=Path('.lemvo-owner.lock/pid')
    if pidfile.exists() and process_identity(pidfile.read_text().strip()): raise ValueError('stop controller at a safe boundary before repair')
    if Path('.autoloop-turn-active').exists(): raise ValueError('active turn marker; reconcile writers first')
    state=Path('research-state.yaml'); digest=hashlib.sha256(state.read_bytes()).hexdigest()
    if digest==record['before_sha']:
        atomic(state,(Path(record['backup'])/'state-after.yaml').read_text())
    elif digest!=record['after_sha']: raise ValueError('state changed since repair; inspect pending transaction')
    ledger=Path('.autoloop-prep-ledger.jsonl'); found=False
    for line in ledger.read_text().splitlines():
        try: found=found or json.loads(line).get('repair_id')==record['repair_id']
        except ValueError: pass
    if not found:
        with ledger.open('a') as f: f.write(json.dumps(record)+'\n'); f.flush(); os.fsync(f.fileno())
    Path('.lemvo-prep-repair-pending.json').unlink()
    print(json.dumps(record,indent=2))


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
    bundle=('lemvo-run.sh','autoloop-reference.sh','runtime-support.py')
    report['bundle']={name:hashlib.sha256((current.parent/name).read_bytes()).hexdigest()
        if (current.parent/name).is_file() else None for name in bundle}
    report['complete_bundle']=all(report['bundle'].values())
    report['installed_candidates'] = []
    for root in (Path.home()/'.claude/skills', Path.home()/'.codex/skills'):
        installed = root/'research-orchestrator/templates/lemvo-run.sh'
        if installed.exists():
            digest = hashlib.sha256(installed.read_bytes()).hexdigest()
            hashes={name:hashlib.sha256((installed.parent/name).read_bytes()).hexdigest()
                if (installed.parent/name).is_file() else None for name in bundle}
            report['installed_candidates'].append({'path':str(installed), 'sha256':digest,
                'bundle':hashes,'matches_invoked':hashes == report['bundle']})
    print(json.dumps(report, ensure_ascii=False, indent=2))


def watchdog_config():
    defaults = {'SOFT_IDLE_SEC': 1800, 'HARD_IDLE_SEC': 3600,
                'MAX_SEC': 0, 'POLL_SEC': 5, 'TERM_GRACE_SEC': 15}
    values = {}
    for key, default in defaults.items():
        value = float(os.environ.get('LEMVO_TURN_' + key, default))
        if not math.isfinite(value) or value < 0 or (key == 'POLL_SEC' and value == 0):
            raise ValueError('invalid LEMVO_TURN_' + key)
        values[key] = value
    if values['SOFT_IDLE_SEC'] and values['HARD_IDLE_SEC'] and values['SOFT_IDLE_SEC'] > values['HARD_IDLE_SEC']:
        raise ValueError('soft idle timeout must not exceed hard idle timeout')
    return values


def attached_jobs(engine_pid):
    """Do not let CLI signal handlers cascade into a registered live experiment."""
    path = Path('.lemvo-jobs.json')
    if not path.exists(): return []
    jobs = load(path)
    if not isinstance(jobs, list): return ['unreadable-job-registry']
    protected = []
    for job in jobs:
        if not isinstance(job, dict): return ['invalid-job-registry']
        try: pid = int(job.get('pid', 0))
        except (ValueError, TypeError): continue
        if pid <= 0: continue
        identity = process_identity(pid)
        if not identity: continue
        if job.get('pid_start') and str(job['pid_start']) != identity: continue
        seen = set()
        while pid > 1 and pid not in seen:
            if pid == engine_pid:
                protected.append(str(job.get('run_id', job.get('pid')))); break
            seen.add(pid)
            try: pid = int(Path('/proc/%d/stat' % pid).read_text().rsplit(')', 1)[1].split()[1])
            except (OSError, ValueError, IndexError): break
    return protected


def renew_turn(seconds):
    seconds = float(seconds)
    if not math.isfinite(seconds) or seconds <= 0: raise ValueError('renew requires positive seconds')
    hb = load('.lemvo-inflight.json', {})
    pid, identity = hb.get('engine_pid'), hb.get('engine_start')
    if not pid or not identity or process_identity(pid) != identity:
        raise ValueError('no verified live turn to renew')
    # A lease is scoped to this turn and process, never to future turns or budgets.
    lease = dict(turn_id=hb['turn_id'], engine_pid=pid, engine_start=identity,
                 until_epoch=time.time() + seconds)
    atomic('.lemvo-turn-lease.json', lease)
    print(json.dumps(lease))


def watch_turn(td, tid, pid, identity):
    """Observe output, not research success. Signals only this verified CLI PID."""
    cfg = watchdog_config(); pid = int(pid); td = Path(td)
    started = last = time.monotonic(); last_epoch = time.time()
    sizes = {}; state = ''; sent = None; reason = None
    session_epoch = epoch()
    def current():
        try:
            return epoch() == session_epoch and Path('.autoloop-turn-active').read_text().split('|')[0] == tid
        except OSError: return False
    def publish(new_state, protected=None):
        nonlocal state
        now = time.time()
        hb = dict(schema='lemvo-inflight-v1', turn_id=tid, engine_pid=pid,
                  engine_start=identity, state=new_state, observed_epoch=now,
                  last_activity_epoch=last_epoch, idle_sec=round(time.monotonic()-last, 3),
                  elapsed_sec=round(time.monotonic()-started, 3), policy=cfg,
                  pending_steer=sum(1 for _ in Path('.lemvo-steer-queue').glob('q-*.md')),
                  protected_jobs=protected or [], reason=reason,
                  activity_basis='stdout/stderr byte growth; not proof of useful progress')
        atomic(td/'heartbeat.json', hb)
        if current():
            atomic('.lemvo-inflight.json', hb)
            if new_state not in ('ended', 'timed-out'):
                status = 'RUNNING'  # Preserve the public state enum; warning is in detail.
                atomic('.lemvo-status', '%s|%s|turn_id=%s idle=%.1fs elapsed=%.1fs pending_steer=%d|%s\n' %
                       (status, new_state, tid, hb['idle_sec'], hb['elapsed_sec'], hb['pending_steer'],
                        time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now))))
        if new_state != state:
            with (td/'watchdog-events.jsonl').open('a') as f:
                f.write(json.dumps(hb)+'\n')
            if new_state not in ('active', 'ended'):
                print('LEMVO_WATCHDOG %s turn=%s idle=%.1fs' % (new_state, tid, hb['idle_sec']), flush=True)
            state = new_state
    while True:
        now = time.monotonic()
        # Only new CLI output is observable activity. Polling, state-file writes,
        # CPU usage, unrelated training output and queued steer do not reset it.
        for name in ('engine-stdout.jsonl', 'engine-stderr.log'):
            try: size = (td/name).stat().st_size
            except OSError: size = 0
            if size > sizes.get(name, 0): last = now; last_epoch = time.time()
            sizes[name] = size
        if process_identity(pid) != identity:
            publish('timed-out' if sent is not None else 'ended')
            return 124 if sent is not None else 0
        if epoch() != session_epoch:
            # An explicit reset fences the old watcher. Losing the adapter marker
            # alone must not abandon its orphan CLI without supervision.
            publish('superseded'); return 0
        idle = now-last; elapsed = now-started
        lease = load('.lemvo-turn-lease.json', {})
        try:
            leased = (lease.get('turn_id') == tid and lease.get('engine_pid') == pid
                      and lease.get('engine_start') == identity
                      and float(lease.get('until_epoch', 0)) > time.time())
        except (ValueError, TypeError, AttributeError): leased = False
        hard = ((cfg['HARD_IDLE_SEC'] and idle >= cfg['HARD_IDLE_SEC']) or
                (cfg['MAX_SEC'] and elapsed >= cfg['MAX_SEC']))
        if sent is not None:
            if now-sent >= cfg['TERM_GRACE_SEC'] and process_identity(pid) == identity:
                try: os.kill(pid, signal.SIGKILL)
                except ProcessLookupError: pass
            publish('terminating')
        elif hard and not leased:
            protected = attached_jobs(pid)
            if protected:
                # Claude's SIGTERM handler can terminate foreground Bash children.
                # Preserve known live jobs and make the exceptional hold visible.
                publish('timeout-protected-job', protected)
            else:
                reason = 'wall-timeout' if cfg['MAX_SEC'] and elapsed >= cfg['MAX_SEC'] else 'idle-timeout'
                marker = dict(turn_id=tid, engine_pid=pid, engine_start=identity,
                              reason=reason, observed_epoch=time.time(), signal='SIGTERM',
                              scientific_verdict=None)
                # Persist before signalling, so interrupted adapters can recover safely.
                atomic(td/'timeout.json', marker)
                if process_identity(pid) == identity:
                    try: os.kill(pid, signal.SIGTERM)
                    except ProcessLookupError: pass
                sent = now; publish('terminating')
        elif leased: publish('lease-active')
        elif cfg['SOFT_IDLE_SEC'] and idle >= cfg['SOFT_IDLE_SEC']: publish('idle-warning')
        else: publish('active')
        time.sleep(cfg['POLL_SEC'])


if __name__ == '__main__':
    cmd = sys.argv[1]
    if cmd == 'bootstrap': bootstrap()
    elif cmd == 'prompt': prompt(*sys.argv[2:4])
    elif cmd == 'wait-check': wait_check(sys.argv[2])
    elif cmd == 'wait-register': register_wait()
    elif cmd == 'epoch': print(epoch())
    elif cmd == 'new-epoch': atomic('.lemvo-session-epoch', uuid.uuid4().hex+'\n')
    elif cmd == 'progress-snapshot': progress_snapshot(sys.argv[2])
    elif cmd == 'progress-changed': progress_changed(sys.argv[2])
    elif cmd == 'snapshot-incomplete': snapshot_incomplete(sys.argv[2])
    elif cmd == 'recovery-plan': recovery_plan()
    elif cmd == 'recovery-mark': recovery_mark(*sys.argv[2:])
    elif cmd == 'turn-register': turn_register(sys.argv[2])
    elif cmd == 'preflight-close': preflight_close(*sys.argv[2:4])
    elif cmd == 'repair-prep': repair_prep(sys.argv[2],sys.argv[3],'--apply' in sys.argv[4:])
    elif cmd == 'delivery': delivery(*sys.argv[2:])
    elif cmd == 'doctor': doctor(sys.argv[2])
    elif cmd == 'watchdog-config': watchdog_config()
    elif cmd == 'watch-turn': raise SystemExit(watch_turn(*sys.argv[2:6]))
    elif cmd == 'renew-turn': renew_turn(sys.argv[2])
    else: raise SystemExit(64)
