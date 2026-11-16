#!/usr/bin/env python3
"""Expose actual CPU hook waits to the parent watchdog, without model calls."""
from pathlib import Path
import argparse
import importlib.util
import json
import sys
import time


def identity(pid):
    try:
        fields = Path('/proc/%s/stat' % int(pid)).read_text().rsplit(')', 1)[1].split()
        return fields[19] if fields[0] not in ('Z', 'X') else None
    except (OSError, ValueError, IndexError, TypeError):
        return None


def active_waits(root):
    active = []
    for path in (Path(root) / '.runtime-events' / 'waits').glob('*.json'):
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(value, dict):
                continue
            pid = value.get('pid', value.get('hook_pid'))
            start = value.get('start', value.get('hook_start'))
            if value.get('state') == 'waiting' and pid and identity(pid) == str(start):
                active.append(path.stem)
        except (OSError, ValueError, UnicodeError):
            pass
    return active


def registry_adapter(root):
    """Adopt real native/.lemvo jobs for hooks even without a native owner."""
    source = Path(__file__).resolve().with_name('native-event-owner.py')
    if not source.exists():
        source = source.with_name('native_event_owner.py')
    try:
        spec = importlib.util.spec_from_file_location('runtime_cpu_job_registry', str(source))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        state_path = Path(root) / '.runtime-events/native_owner_state.json'
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        engine = state.get('engine', 'claude') if isinstance(state, dict) else 'claude'
        return module.Owner(root, engine=engine)
    except (OSError, ValueError, ImportError, AttributeError, TypeError):
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--parent', type=int, required=True)
    parser.add_argument('--interval', type=float, default=30)
    args = parser.parse_args()
    start = identity(args.parent)
    registry = registry_adapter(args.root)
    while start and identity(args.parent) == start:
        if registry is not None:
            try:
                registry.jobs()  # Pure CPU registry reconciliation, never .run().
            except Exception:
                pass
        waits = active_waits(args.root)
        if waits:
            sys.stderr.write('[runtime-events] CPU terminal-event wait active; no model polling; waits=' + ','.join(waits[:4]) + '\n')
            sys.stderr.flush()
        until = time.monotonic() + max(.05, args.interval)
        while time.monotonic() < until:
            if identity(args.parent) != start:
                return
            time.sleep(min(.2, max(.001, until - time.monotonic())))


if __name__ == '__main__':
    main()
