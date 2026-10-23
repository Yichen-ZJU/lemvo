#!/usr/bin/env python3
"""Real CPU processes + HTTP acceptance, with native-shaped transcript fixtures.

No model or GPU calls. All process signals target only children created here.
"""
import argparse
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from lemvo_console.cli import register_process
from lemvo_console.collector import Processes
from lemvo_console.store import Store, atomic, load, key


def write_transcript(path, phase, engine):
    from datetime import datetime, timezone
    ts=datetime.now(timezone.utc).isoformat()
    events=[]
    if engine=='claude':
        if phase=='tool':
            events=[{'type':'assistant','timestamp':ts,'message':{'content':[{'type':'tool_use','id':'fixture-tool','name':'Bash','input':{'command':'fixture: long-running read-only search'}}]}}]
        else:
            events=[{'type':'assistant','timestamp':ts,'message':{'stop_reason':'end_turn','content':[{'type':'text','text':'Isolated acceptance: waiting for your direction.'}]}}]
    else:
        events=[{'type':'event_msg','timestamp':ts,'payload':{'type':'task_started'}},
                {'type':'response_item','timestamp':ts,'payload':{'type':'function_call','call_id':'fixture-fit','name':'exec_command','arguments':'{"cmd":"fixture training"}'}}]
    atomic(path,''.join(json.dumps(e)+'\n' for e in events))


def child_main(root):
    root=Path(root)
    proc=Processes();proc.scan();start=proc.items[os.getpid()]['start']
    for step in range(1,101):
        # A tiny real CPU calculation, not a model or GPU experiment.
        sum(i*i for i in range(1000))
        atomic(root/'a' / '.lemvo-progress' / 'fit-a.json',{'run_id':'fit-a','pid':os.getpid(),'pid_start':start,
            'phase':'loading_model' if step<10 else 'training','current':step,'total':100,'unit':'steps','at':time.time()})
        time.sleep(.15)
    atomic(root/'a/result.json',{'run_id':'fit-a','status':'succeeded','exit_code':0})


def run(output):
    root=Path(tempfile.mkdtemp(prefix='lemvo-console-acceptance-'))
    state=root/'state';store=Store(state)
    a,b,c=(root/x for x in ('a','b','c'))
    for p in (a,b,c):p.mkdir()
    children=[];server=None;checks=[]
    def check(name,condition):
        checks.append({'name':name,'pass':bool(condition)})
        if not condition:raise AssertionError(name)
    try:
        train=subprocess.Popen([sys.executable,__file__,'--child',str(root)]);children.append(train)
        stuck=subprocess.Popen([sys.executable,'-c','import time;time.sleep(90)']);children.append(stuck)
        waiting=subprocess.Popen([sys.executable,'-c','import time;time.sleep(90)']);children.append(waiting)
        for p,child,engine,phase in [(a,train,'codex','tool'),(b,stuck,'claude','tool'),(c,waiting,'claude','waiting')]:
            log=p/'session.jsonl';write_transcript(log,phase,engine)
            register_process(store,child.pid,engine,str(p),str(log))
        proc=Processes();proc.scan()
        atomic(a/'.lemvo-jobs.json',[{'run_id':'fit-a','pid':train.pid,'pid_start':proc.items[train.pid]['start'],'done_file':'result.json'}])
        entry=Path(__file__).resolve().parents[1]/'lemvo-console.py'
        import socket
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        log=(root/'server.log').open('w')
        server=subprocess.Popen([sys.executable,str(entry),'--state-dir',str(state),'serve','--port',str(port),'--poll-seconds','1','--warn-seconds','4'],stdout=log,stderr=log)
        for _ in range(80):
            if (state/'token').exists() and (state/'server.json').exists():break
            if server.poll() is not None:raise RuntimeError((root/'server.log').read_text())
            time.sleep(.1)
        token=(state/'token').read_text().strip()
        def view():
            conn=http.client.HTTPConnection('127.0.0.1',port,timeout=5)
            conn.request('GET','/api/snapshot',headers={'Authorization':'Bearer '+token})
            r=conn.getresponse();d=json.loads(r.read());conn.close();return d
        def wait_for(fn,seconds=12):
            end=time.time()+seconds
            while time.time()<end:
                s=view()
                if fn(s):return s
                time.sleep(.25)
            raise AssertionError('Timed out waiting for expected snapshot')
        s=wait_for(lambda s:any(p['path']==str(a) and p['jobs'] for p in s['projects']))
        check('three isolated conversations independently listed',sum(x['project'] in map(str,(a,b,c)) for x in s['sessions'])==3)
        s=wait_for(lambda s:any(x['project']==str(a) and x['phase']=='training' for x in s['sessions']))
        sa=next(x for x in s['sessions'] if x['project']==str(a))
        check('training progress associated only with A',sa['health']=='progressing')
        s=wait_for(lambda s:any(x['project']==str(b) and x['health']=='attention' for x in s['sessions']))
        check('B long tool wait raises attention without interrupt',stuck.poll() is None)
        sc=next(x for x in s['sessions'] if x['project']==str(c))
        check('C waiting for user never treated as stuck',sc['health']=='waiting')
        check('B does not inherit A training progress',next(x for x in s['sessions'] if x['project']==str(b))['phase']=='tool_pending')
        # The viewer/collector may stop; experiments are independent processes.
        server.terminate();server.wait(timeout=5);server=None
        check('console exit leaves every experiment alive',all(p.poll() is None for p in children))
        train.wait(timeout=25)
        check('CPU job finishes while console is stopped',load(a/'result.json',{}).get('status')=='succeeded')
        server=subprocess.Popen([sys.executable,str(entry),'--state-dir',str(state),'serve','--port',str(port),'--poll-seconds','1','--warn-seconds','4'],stdout=log,stderr=log)
        time.sleep(1)
        s=wait_for(lambda s:any(p['path']==str(a) and p['jobs'][0]['status']=='succeeded' for p in s['projects']))
        check('restart reconciles completed job without rerunning it',train.poll()==0)
        check('restart preserves waiting session',next(x for x in s['sessions'] if x['project']==str(c))['health']=='waiting')
        atomic(output,{'passed':len(checks),'failed':0,'checks':checks,'kind':'real CPU processes; native-shaped transcript fixtures; no models/GPU', 'fixture_root':str(root)})
        print(json.dumps({'passed':len(checks),'failed':0,'report':str(output)}))
    finally:
        if server and server.poll() is None:server.terminate();server.wait(timeout=5)
        for child in children:
            if child.poll() is None:child.terminate();child.wait(timeout=5)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--child');p.add_argument('--output',default='acceptance.json');a=p.parse_args()
    child_main(a.child) if a.child else run(Path(a.output).resolve())
