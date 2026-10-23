#!/usr/bin/env python3
"""Install an immutable console release; never restart a research process."""
import argparse
import hashlib
import os
from pathlib import Path
import shlex
import shutil
import sys
import tempfile


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--prefix',default=str(Path.home()/'.local/share/lemvo-console'))
    p.add_argument('--bin-dir',default=str(Path.home()/'.local/bin'))
    a=p.parse_args()
    if sys.platform!='linux' or sys.version_info<(3,9):p.error('Use Python 3.9+ on the Linux server')
    source=Path(__file__).resolve().parent
    files=sorted(x for x in source.rglob('*') if x.is_file() and '__pycache__' not in x.parts and x.suffix not in ('.pyc','.pyo') and '.git' not in x.parts)
    digest=hashlib.sha256()
    for f in files:digest.update(str(f.relative_to(source)).encode());digest.update(f.read_bytes())
    version='0.1.0-'+digest.hexdigest()[:12]
    prefix=Path(a.prefix).expanduser().resolve();bindir=Path(a.bin_dir).expanduser().resolve()
    wrapper=bindir/'lemvo-console'
    if wrapper.exists() and 'LEMVO_CONSOLE_LAUNCHER' not in wrapper.read_text():
        p.error('An unrelated lemvo-console command already exists; choose --bin-dir')
    current=prefix/'current'
    if current.exists() and not current.is_symlink():p.error('current exists but is not a release symlink')
    destination=prefix/'releases'/version
    destination.mkdir(parents=True,exist_ok=True)
    for f in files:
        target=destination/f.relative_to(source)
        target.parent.mkdir(parents=True,exist_ok=True)
        if target.exists():
            if target.read_bytes()!=f.read_bytes():p.error('Existing immutable release differs: '+str(target))
        else:shutil.copy2(f,target)
    temp=prefix/('.current-'+str(os.getpid()))
    temp.symlink_to(destination,target_is_directory=True)
    os.replace(temp,current)
    bindir.mkdir(parents=True,exist_ok=True)
    text='#!/bin/sh\n# LEMVO_CONSOLE_LAUNCHER\nexec %s %s "$@"\n'%(shlex.quote(sys.executable),shlex.quote(str(current/'lemvo-console.py')))
    fd,tmp=tempfile.mkstemp(prefix='.lemvo-console-',dir=bindir)
    with os.fdopen(fd,'w') as f:f.write(text)
    os.chmod(tmp,0o755);os.replace(tmp,wrapper)
    print('Installed '+version+'\nCommand: '+str(wrapper)+'\nNo service/session was restarted. Start with: '+str(wrapper)+' start')


if __name__=='__main__':main()
