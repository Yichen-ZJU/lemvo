#!/usr/bin/env python3
"""Install Lemvo's Windows decision client and portable skills; preserve user files."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time
import uuid


def winpath(path):
    text = str(Path(path).absolute())
    return Path('\\\\?\\' + text) if os.name == 'nt' and not text.startswith('\\\\?\\') else Path(text)


def hashes(root):
    root = winpath(root)
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob('*')) if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--skills-source', type=Path)
    parser.add_argument('--home', type=Path, default=Path.home() / '.lemvo-desktop')
    parser.add_argument('--skills-dest', type=Path, default=Path.home() / '.codex/skills')
    parser.add_argument('--check', action='store_true')
    a = parser.parse_args()
    src = Path(__file__).resolve().parent
    repo = src.parent
    source = a.skills_source or (repo / 'codex/skills' if (repo / 'codex/skills').exists() else repo / 'skills')
    source = source.resolve(strict=True)
    dest, home = a.skills_dest.resolve(), a.home.resolve()
    manifest_file = home / 'installation.json'
    previous = json.loads(manifest_file.read_text(encoding='utf-8')) if manifest_file.exists() else {'skills': {}}
    actions, skipped = [], []
    # Existing skills in either discovery root win over unmanaged incoming ones.
    alternate = Path.home() / '.agents/skills'
    for directory in sorted(source.iterdir()):
        if not directory.is_dir() or directory.name.startswith('.'):
            continue
        target = dest / directory.name
        old = previous.get('skills', {}).get(directory.name)
        if (alternate / directory.name).exists() and alternate.resolve() != dest:
            skipped.append({'name': directory.name, 'reason': 'existing alternate-root skill preserved'})
            continue
        if target.exists() and (not old or hashes(target) != old['files']):
            skipped.append({'name': directory.name, 'reason': 'existing unmanaged or locally modified files preserved'})
            continue
        actions.append((directory, target, hashes(directory)))
    summary = {'source': str(source), 'destination': str(dest), 'install_directories': len(actions), 'skipped': skipped}
    if a.check:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return
    home.mkdir(parents=True, exist_ok=True)
    dest.mkdir(parents=True, exist_ok=True)
    tag = time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6]
    backup = home / 'backups' / tag
    backup.mkdir(parents=True)
    current_app_files = {n: (src / n).read_bytes() for n in ('lemvo_desktop.py', 'remote_codex_bridge.py', 'README.md')}
    app_hash = hashlib.sha256(b''.join(current_app_files[n] for n in sorted(current_app_files))).hexdigest()[:16]
    app = home / 'app' / app_hash
    app.mkdir(parents=True, exist_ok=True)
    for name, content in current_app_files.items():
        (app / name).write_bytes(content)
    installed = dict(previous.get('skills', {}))
    applied = []
    try:
        for directory, target, expected in actions:
            stage = dest / ('.' + directory.name + '.install-' + tag)
            shutil.copytree(winpath(directory), winpath(stage), ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            if hashes(stage) != expected:
                raise RuntimeError('Staged hash mismatch: ' + directory.name)
            saved = backup / directory.name
            had_old = target.exists()
            if had_old:
                os.replace(target, saved)
            applied.append((target, saved, had_old))
            os.replace(stage, target)
            installed[directory.name] = {'files': expected}
        manifest = dict(summary, skills=installed, app=str(app), python=sys.executable, backup=str(backup), installed_at=time.time())
        if manifest_file.exists():
            shutil.copy2(manifest_file, backup / 'installation.json')
        temp = home / ('installation-' + tag + '.json')
        temp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(temp, manifest_file)
        # This launcher uses the current app manifest; running workers remain on their pinned version.
        launcher = "$m = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'installation.json') -Raw | ConvertFrom-Json\n& $m.python (Join-Path $m.app 'lemvo_desktop.py') @args\nexit $LASTEXITCODE\n"
        (home / 'lemvo.ps1').write_text(launcher, encoding='utf-8')
    except Exception:
        for target, saved, had_old in reversed(applied):
            # Targets are explicitly recorded children of the selected skills destination.
            if target.parent.resolve() != dest:
                raise RuntimeError('Rollback target escaped skills destination')
            if target.exists():
                quarantined = backup / (target.name + '.failed-new')
                os.replace(target, quarantined)
            if had_old and saved.exists():
                os.replace(saved, target)
        raise
    print(json.dumps(dict(summary, app=str(app), launcher=str(home / 'lemvo.ps1'), backup=str(backup)), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
