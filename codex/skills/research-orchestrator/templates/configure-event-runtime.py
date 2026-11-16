#!/usr/bin/env python3
"""Install project-local Claude event hooks without changing permissions.

Installation failure is a runtime capability notice, never a research gate.
The caller inspects active/status; stdout is one JSON object. Stdlib/Python 3.6+.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import tempfile


MARKER = ' # lemvo-event-runtime:v1'
EVENTS = ('PreToolUse', 'Stop', 'SubagentStop')


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def contained(path, root):
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def read_object(path):
    if not path.exists():
        return {}, None, None
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode('utf-8-sig'))
    except (OSError, UnicodeError, ValueError) as exc:
        return None, None, 'Unreadable or malformed JSON: ' + str(exc)
    if not isinstance(value, dict):
        return None, raw, 'Expected a JSON object.'
    return value, raw, None


def atomic_json(path, value, expected=None, check_previous=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    if check_previous:
        present = path.read_bytes() if path.exists() else None
        if present != expected:
            raise ValueError('Configuration changed concurrently; preserved the newer file.')
    if path.exists() and path.read_bytes() == raw:
        return False
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if check_previous:
            present = path.read_bytes() if path.exists() else None
            if present != expected:
                raise ValueError('Configuration changed concurrently; preserved the newer file.')
        os.replace(name, str(path))
    finally:
        if os.path.exists(name):
            os.unlink(name)
    return True


def owned(hook):
    return (isinstance(hook, dict) and hook.get('type') == 'command' and
            isinstance(hook.get('command'), str) and hook['command'].endswith(MARKER))


def clean_managed(settings):
    """Remove only this installer's commands, preserving all foreign hook data."""
    hooks = settings.get('hooks', {})
    if not isinstance(hooks, dict):
        raise ValueError('hooks is not an object; settings preserved.')
    output = dict(settings)
    cleaned = dict(hooks)
    for event in EVENTS:
        groups = hooks.get(event, [])
        if not isinstance(groups, list):
            raise ValueError(event + ' hook groups are not a list; settings preserved.')
        remaining = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get('hooks'), list):
                raise ValueError(event + ' contains an unsupported group; settings preserved.')
            original = group['hooks']
            retained = [h for h in original if not owned(h)]
            if len(retained) == len(original):
                remaining.append(group)
            elif retained:
                replacement = dict(group)
                replacement['hooks'] = retained
                remaining.append(replacement)
        if event in hooks:
            cleaned[event] = remaining
    if 'hooks' in settings or cleaned:
        output['hooks'] = cleaned
    return output


def install_runtime(root, engine, templates, disabled=False, enable=False):
    root = Path(root).resolve()
    templates = Path(templates).resolve()
    runtime_root = root / '.runtime-events'
    record_path = runtime_root / 'installation.json'
    settings_path = root / '.claude' / 'settings.local.json'
    result = {'schema': 'lemvo-event-installation', 'schema_version': 1,
              'engine': engine, 'project_root': str(root), 'active': False,
              'installed_utc': utc_now(), 'settings_path': str(settings_path),
              'permissions_changed': False, 'scientific_admission_gate': False,
              'applies_on': 'next_cli_invocation_that_loads_project_local_settings'}

    def finish(status, error=None, write=True):
        result['status'] = status
        if error:
            result['notice'] = str(error)
        if write and contained(record_path, root):
            try:
                atomic_json(record_path, result)
            except (OSError, ValueError) as exc:
                result['record_write_notice'] = str(exc)
        return result

    if not root.is_dir():
        return finish('project_missing', 'Project directory does not exist.', write=False)
    if not contained(runtime_root, root) or not contained(settings_path, root):
        return finish('project_path_escape', 'Project-local configuration resolves outside project; untouched.', write=False)
    previous, _, previous_error = read_object(record_path)
    if previous_error:
        return finish('invalid_installation_record', previous_error, write=False)
    user_disabled = bool(disabled or (previous.get('disabled') and not enable))
    result['disabled'] = user_disabled
    if engine == 'codex':
        # Codex does not implement Claude settings hooks. Keep its existing
        # controller/job contracts; don't imply these hooks protect Codex workers.
        return finish('codex_controller_only', 'No Claude native hooks installed for Codex.')
    if os.name == 'nt':
        return finish('unsupported_hook_shell', 'This hook command installer targets POSIX server Claude; no Windows shell settings changed.')
    settings, settings_raw, settings_error = read_object(settings_path)
    if settings_error:
        return finish('invalid_settings', settings_error)
    if settings.get('disableAllHooks') is True:
        return finish('disabled_by_project', 'Existing disableAllHooks preference retained.')
    try:
        desired = clean_managed(settings)
    except ValueError as exc:
        return finish('invalid_hook_schema', exc)
    if user_disabled:
        try:
            if desired != settings:
                atomic_json(settings_path, desired, settings_raw, check_previous=True)
        except (OSError, ValueError) as exc:
            return finish('settings_write_failed', exc)
        return finish('disabled', 'User event-hook disable preference retained.')

    source = templates / 'event-wait.py'
    if not source.is_file():
        return finish('helper_missing', 'event-wait.py unavailable; settings were not changed.')
    try:
        payload = source.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        result['source_path'] = str(source)
        result['source_sha256'] = digest
        versions = runtime_root / 'runtime'
        versions.mkdir(parents=True, exist_ok=True)
        version = versions / digest
        script = version / 'event-wait.py'
        if version.exists():
            if not script.is_file() or hashlib.sha256(script.read_bytes()).hexdigest() != digest:
                return finish('version_mismatch', 'Existing immutable runtime version differs; not overwritten.')
        else:
            staging = Path(tempfile.mkdtemp(prefix='.stage-', dir=str(versions)))
            try:
                (staging / 'event-wait.py').write_bytes(payload)
                atomic_json(staging / 'version.json', {'source_path': str(source),
                            'sha256': digest, 'created_utc': utc_now()})
                try:
                    os.rename(str(staging), str(version))
                except OSError:
                    if not script.is_file() or hashlib.sha256(script.read_bytes()).hexdigest() != digest:
                        raise
            finally:
                if staging.exists():
                    shutil.rmtree(str(staging))
        command = ' '.join(shlex.quote(v) for v in
                           (sys.executable, str(script), 'hook', '--root', str(root))) + MARKER
        hooks = dict(desired.get('hooks', {}))
        for event in EVENTS:
            group = {'hooks': [{'type': 'command', 'command': command, 'timeout': 30}]}
            if event == 'PreToolUse':
                group['matcher'] = 'Bash|TaskOutput'
            hooks[event] = list(hooks.get(event, [])) + [group]
        desired['hooks'] = hooks
        # Compare semantic objects before writing to keep repeated install cheap.
        if desired != settings:
            atomic_json(settings_path, desired, settings_raw, check_previous=True)
        result.update(active=True, runtime_script=str(script), runtime_version=digest,
                      python_executable=sys.executable,
                      hook_events=list(EVENTS), managed_hook_count=3)
        return finish('active')
    except (OSError, ValueError) as exc:
        return finish('installation_failed', exc)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command')
    p = commands.add_parser('install')
    p.add_argument('--root', required=True)
    p.add_argument('--engine', choices=('claude', 'codex'), required=True)
    p.add_argument('--templates', required=True)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument('--disabled', action='store_true')
    mode.add_argument('--enable', action='store_true')
    args = parser.parse_args(argv)
    if args.command != 'install':
        parser.print_help()
        return 0
    try:
        result = install_runtime(args.root, args.engine, args.templates,
                                 disabled=args.disabled, enable=args.enable)
    except (OSError, ValueError) as exc:
        result = {'active': False, 'status': 'installation_failed', 'notice': str(exc),
                  'engine': args.engine, 'permissions_changed': False,
                  'scientific_admission_gate': False}
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
