"""Observe CLI records. No tools are run and no transcript is ever modified."""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path

from .store import redact, tail, read_text


def stamp(value):
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError):
        return None


def records(text):
    for line in text.splitlines():
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                yield obj
        except (ValueError, TypeError):
            continue


def metadata(path):
    for o in records(read_text(path, 32768)):
        if o.get('type') == 'session_meta':
            p = o.get('payload', {})
            return {'session_id': p.get('id', p.get('session_id')), 'project': p.get('cwd'), 'engine': 'codex'}
        if o.get('sessionId'):
            return {'session_id': o['sessionId'], 'project': o.get('cwd'), 'engine': 'claude'}
    return {}


def summarize(path, engine):
    result = {'phase': 'unknown', 'pending_tools': [], 'messages': [], 'last_event_at': None,
              'phase_since': None, 'parse_warning': None}
    pending = {}
    recognized = False

    def phase(name, ts):
        if result['phase'] != name:
            result['phase_since'] = ts
        result['phase'] = name
        if ts:
            result['last_event_at'] = ts

    def message(role, text):
        if text:
            result['messages'].append({'role': role, 'text': redact(text)[:2200]})
            result['messages'] = result['messages'][-8:]

    for o in records(tail(path)):
        ts = stamp(o.get('timestamp'))
        typ = o.get('type')
        if engine == 'claude':
            m = o.get('message', {})
            if not isinstance(m, dict):
                continue
            content = m.get('content', [])
            content = [{'type': 'text', 'text': content}] if isinstance(content, str) else content
            if not isinstance(content, list):
                continue
            if typ == 'result':
                recognized = True
                phase('waiting_user', ts)
            for c in content:
                if not isinstance(c, dict):
                    continue
                ct = c.get('type')
                if ct == 'tool_use':
                    recognized = True
                    args = c.get('input', {})
                    pending[c.get('id', '')] = {'id': c.get('id'), 'name': c.get('name'),
                        'command': redact(args.get('command', ''))[:800] if isinstance(args, dict) else '', 'since': ts}
                    phase('tool_pending', ts)  # A tool request does not prove the process started.
                elif ct == 'tool_result':
                    recognized = True
                    pending.pop(c.get('tool_use_id'), None)
                    phase('model', ts)
                elif ct == 'text' and typ in ('user', 'assistant'):
                    recognized = True
                    message(typ, c.get('text', ''))
                    if typ == 'user':
                        phase('model', ts)
            if typ == 'assistant' and m.get('stop_reason') == 'end_turn':
                phase('waiting_user', ts)
        else:
            p = o.get('payload', {})
            if not isinstance(p, dict):
                continue
            pt = p.get('type')
            if typ == 'event_msg':
                if pt == 'task_started':
                    recognized = True
                    pending.clear()
                    phase('model', ts)
                elif pt in ('task_complete', 'turn_aborted'):
                    recognized = True
                    pending.clear()
                    phase('waiting_user', ts)
            if typ == 'response_item':
                if pt in ('function_call', 'custom_tool_call'):
                    recognized = True
                    args = p.get('arguments', p.get('input', ''))
                    pending[p.get('call_id', '')] = {'id': p.get('call_id'), 'name': p.get('name'),
                        'command': redact(args if isinstance(args, str) else json.dumps(args))[:800], 'since': ts}
                    phase('tool_pending', ts)
                elif pt in ('function_call_output', 'custom_tool_call_output'):
                    recognized = True
                    pending.pop(p.get('call_id'), None)
                    phase('model', ts)
                elif pt == 'message' and p.get('role') in ('user', 'assistant'):
                    recognized = True
                    content = p.get('content', [])
                    if isinstance(content, list):
                        message(p['role'], '\n'.join(c.get('text', '') for c in content if isinstance(c, dict)))
                    if p.get('role') == 'user':
                        phase('model', ts)
                    elif p.get('phase') == 'final':
                        phase('waiting_user', ts)
    if result['phase'] not in ('waiting_user', 'completed') and pending:
        result['phase'] = 'tool_pending'
        result['phase_since'] = min((t['since'] for t in pending.values() if t['since']), default=None)
    result['pending_tools'] = list(pending.values())[-8:]
    if not recognized:
        result['parse_warning'] = 'No supported activity event; state is unknown, not healthy or failed.'
    return result


class TranscriptCache:
    def __init__(self):
        self.values = {}

    def get(self, path, engine):
        try:
            st = Path(path).stat()
            fingerprint = (st.st_ino, st.st_size, st.st_mtime_ns)
        except OSError:
            return {'phase': 'unknown', 'pending_tools': [], 'messages': [],
                    'last_event_at': None, 'phase_since': None, 'parse_warning': 'Transcript unavailable'}
        cached = self.values.get(str(path))
        if not cached or cached[0] != fingerprint:
            cached = (fingerprint, summarize(path, engine))
            self.values[str(path)] = cached
        return dict(cached[1])
