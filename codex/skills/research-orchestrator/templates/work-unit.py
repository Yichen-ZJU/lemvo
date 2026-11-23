#!/usr/bin/env python3
"""Concrete runtime work packets; no scientific admission or resource authority.

This module only reads existing research state. It never changes a SID, budget,
job, research verdict, user instruction, or authorization. Python 3.6+ stdlib.
"""
import argparse
import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import importlib.util
import tempfile
import time


_IDLE_MOD = None


def _load_idle_module():
    global _IDLE_MOD
    if _IDLE_MOD is None:
        idle = Path(__file__).with_name('runtime-idle.py')
        if idle.is_file():
            spec = importlib.util.spec_from_file_location('_lemvo_runtime_idle', str(idle))
            if spec is not None and spec.loader is not None:
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                _IDLE_MOD = mod
    return _IDLE_MOD


VERSION = 1
CONTEXT = Path('.lemvo-context.json')
POLICY = Path('.lemvo-context-policy.json')
LAST_RESULT = Path('.lemvo-last-work-result.json')
DEFAULT_POLICY = {'mode': 'auto', 'max_session_turns': 12,
                  'max_context_tokens': 120000, 'file_excerpt_chars': 5000}
VALID_STATUS = ('completed', 'partial', 'waiting', 'failed', 'unknown')
MISSING = object()


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def atomic(path, value):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + p.name + '-', dir=str(p.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            if isinstance(value, str):
                f.write(value)
            else:
                json.dump(value, f, ensure_ascii=False, indent=2)
                f.write('\n')
        os.replace(name, str(p))
    finally:
        if os.path.exists(name):
            os.unlink(name)


def load(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError, UnicodeError):
        return default


def text(path, default=''):
    try:
        return Path(path).read_text(encoding='utf-8', errors='replace')
    except OSError:
        return default


def scalar(raw):
    """YAML scalar subset: quotes/comments respected; never guess units."""
    s = raw.strip()
    quote, esc = None, False
    cut = len(s)
    for i, c in enumerate(s):
        if esc:
            esc = False
            continue
        if quote == '"' and c == '\\':
            esc = True
        elif quote:
            if c == quote:
                quote = None
        elif c in ('"', "'"):
            quote = c
        elif c == '#' and (i == 0 or s[i - 1].isspace()):
            cut = i
            break
    s = s[:cut].strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        if s[0] == '"':
            try:
                return json.loads(s)
            except ValueError:
                pass
        return s[1:-1].replace("''", "'")
    return None if s.lower() in ('null', 'none', '~', 'unknown', '') else s


def state_fields(path='research-state.yaml'):
    """Only simple scalar navigation, not a replacement YAML state writer."""
    fields, stack = {}, []
    for line in text(path).splitlines():
        m = re.match(r'^( *)([A-Za-z_][\w-]*):(?:\s*(.*))?$', line)
        if not m:
            continue
        depth, key, raw = len(m.group(1)), m.group(2), m.group(3) or ''
        while stack and stack[-1][0] >= depth:
            stack.pop()
        keypath = '.'.join([s[1] for s in stack] + [key])
        if raw.strip() and not raw.lstrip().startswith('#'):
            fields[keypath] = scalar(raw)
        else:
            stack.append((depth, key))
    return fields


def first(fields, keys, default=None):
    for key in keys:
        if fields.get(key) is not None:
            return fields[key]
    return default


def policy():
    p = dict(DEFAULT_POLICY)
    candidate = load(POLICY, {})
    if isinstance(candidate, dict):
        for key in p:
            value = candidate.get(key)
            if key == 'mode':
                if value in ('auto', 'resume', 'fresh'):
                    p[key] = value
            elif isinstance(value, int) and not isinstance(value, bool) and value > 0:
                p[key] = value
    return p


def excerpt(raw, limit):
    if len(raw) <= limit:
        return raw, False
    # Keep both original context and latest scoped outcomes. Do not paraphrase.
    n = max(1, (limit - 100) // 2)
    return raw[:n] + '\n[EXCERPT: middle omitted; original file remains authoritative]\n' + raw[-n:], True


def file_record(path, max_chars=5000):
    p = Path(path)
    try:
        st = p.stat()
        if not p.is_file():
            return None
        # Hash scientific memory/metadata only, not arbitrarily large raw logs.
        if st.st_size <= 2 * 1024 * 1024:
            raw = p.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            sample = raw.decode('utf-8', errors='replace')
            basis = 'full-file'
            if p.name == 'research-state.yaml':
                mod = _load_idle_module()
                if mod is not None:
                    # Ledger counters remain in the excerpt for honest budget
                    # decisions, but do not masquerade as a scientific change.
                    scientific = mod.scientific_state_text(sample)
                    digest = hashlib.sha256(scientific.encode('utf-8')).hexdigest()
                    basis = 'scientific-state-content'
        else:
            with p.open('rb') as f:
                head = f.read(32768)
                f.seek(max(0, st.st_size - 32768))
                tail = f.read(32768)
            # A sample alone misses equal-sized edits in the middle. Include
            # filesystem change times and label this as a stat/sample hint,
            # never a full content SHA guarantee.
            stat_stamp = '%s:%s:%s' % (st.st_size,
                getattr(st, 'st_mtime_ns', int(st.st_mtime * 1000000000)),
                getattr(st, 'st_ctime_ns', int(st.st_ctime * 1000000000)))
            digest = hashlib.sha256(head + tail + stat_stamp.encode()).hexdigest()
            sample = (head.decode('utf-8', errors='replace') +
                      '\n[FILE SAMPLE: middle not read]\n' + tail.decode('utf-8', errors='replace'))
            basis = 'stat-size-head-tail-sample'
        body, truncated = excerpt(sample, max_chars)
        return {'path': str(p), 'bytes': st.st_size, 'fingerprint': digest,
                'fingerprint_basis': basis, 'excerpt': body,
                'excerpt_truncated': truncated or basis not in ('full-file', 'scientific-state-content')}
    except OSError:
        return None


def jobs_context():
    obj = load('.lemvo-jobs.json', [])
    if isinstance(obj, dict):
        obj = obj.get('jobs', [obj])
    rows = []
    for r in obj if isinstance(obj, list) else []:
        if not isinstance(r, dict):
            continue
        rows.append({k: r[k] for k in ('run_id', 'name', 'status', 'pid', 'pid_start',
                                      'host', 'done_file', 'result_file') if k in r})
    return rows


def skill_paths(kind):
    name = str(kind or '').lower()
    if any(x in name for x in ('idea', 'ideat', 'brainstorm', '构思')):
        return ['brainstorming-research-ideas/SKILL.md', 'creative-thinking-for-research/SKILL.md',
                'shared-references/neighbor-to-method.md']
    if any(x in name for x in ('writ', 'paper', '写作')):
        return ['paper-writing/SKILL.md', 'paper-narrative/SKILL.md']
    if any(x in name for x in ('implement', 'experiment', 'develop', 'train', '实验', '实现')):
        return ['autoresearch/SKILL.md']
    if any(x in name for x in ('literature', 'research', '调研')):
        return ['research-orchestrator/SKILL.md']
    return []


def bounded_handoff(last):
    selection = {k: last[k] for k in ('turn_id', 'status', 'summary', 'artifact_refs', 'metrics',
                 'limitations', 'next_task', 'source', 'evidence_scope') if k in last}
    raw = json.dumps(selection, ensure_ascii=False)
    if len(raw) <= 10000:
        return raw
    # Keep the machine record intact on disk. Project only the changed task and
    # explicit scope, never turn a huge evidence table into an invented verdict.
    small = {k: selection[k] for k in ('turn_id', 'status', 'next_task', 'source') if k in selection}
    small['summary_excerpt'], _ = excerpt(str(selection.get('summary', '')), 3000)
    small['evidence_scope_excerpt'], _ = excerpt(str(selection.get('evidence_scope', '')), 1000)
    small['artifact_refs'] = selection.get('artifact_refs', [])[:20]
    small['projection_notice'] = 'Large handoff projected; full scoped metrics and limitations remain in .lemvo-last-work-result.json and its turn work-result.json. Read relevant originals before interpreting them.'
    return json.dumps(small, ensure_ascii=False)


def guidance_first_line(raw):
    """Index real guidance, never the queue's HTML metadata or receipt header.

    All original instructions remain preserved verbatim by prepare(). This is
    only a faithful task index, not a natural-language instruction resolver.
    Supersession is already resolved by the controller's frozen snapshot.
    """
    # In a legacy combined receipt+steer input, a queue marker identifies the
    # beginning of actual guidance. Do not read the preceding job receipt as a
    # user instruction. The controller normally supplies the steer-only file.
    markers = list(re.finditer(r'<!--\s*message_id\s*:', raw, re.I))
    if markers:
        # Frozen snapshot order is authoritative. Index the latest delivered
        # message, while prepare preserves all still-active guidance verbatim.
        raw = raw[markers[-1].start():]
    raw = re.sub(r'<!--.*?-->', '', raw, flags=re.S)
    raw = re.sub(r'<!--[^\n]*', '', raw)  # incomplete single-line metadata
    for line in raw.splitlines():
        line = line.strip()
        if not line or re.match(r'^[-=_*]{3,}\s*$', line):
            continue
        if line.startswith('#'):
            line = re.sub(r'^#{1,6}\s*', '', line).strip()
            if not line or re.match(r'^(?:steer|guidance|user guidance|instructions?|message|task|指令|指导|用户指导|当前指导|任务)\s*[:：]?$', line, re.I):
                continue
        if (line.startswith('以下后台作业已出结果') or
                line.lower().startswith(('background job receipts', 'runtime receipt'))):
            continue
        if re.match(r'^[-*]\s+.*\b(?:status|run_id|exit_code|done_file)\s*=', line):
            continue
        # Skip a bare queue envelope label, not arbitrary user sentences.
        if re.match(r'^(?:Current (?:user )?guidance|User (?:message|guidance)|用户指导|当前指导)\s*[:：]?$', line, re.I):
            continue
        line = re.sub(r'^[-*]\s+', '', line)
        return line[:1000]
    return ''


def request_context_tokens(context):
    # Total usage across a CLI invocation or session is NOT context length.
    observation = context.get('last_request_context')
    if isinstance(observation, dict) and observation.get('scope') == 'per_request':
        n = observation.get('input_tokens')
        if isinstance(n, int) and not isinstance(n, bool) and n > 0:
            return n
    return None


def observe_request_context(path):
    """Last actual request input; never CLI/session aggregate usage."""
    last = None
    try:
        with Path(path).open(encoding='utf-8', errors='replace') as stream:
            for index, line in enumerate(stream):
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(ev, dict):
                    continue
                if ev.get('type') in ('event_msg', 'event') and isinstance(ev.get('payload'), dict):
                    ev = ev['payload']
                if ev.get('type') == 'stream_event' and isinstance(ev.get('event'), dict):
                    ev = ev['event']
                if ev.get('type') == 'token_count':
                    info = ev.get('info', {})
                    usage = info.get('last_token_usage') if isinstance(info, dict) else None
                    n = usage.get('input_tokens') if isinstance(usage, dict) else None
                    if isinstance(n, int) and not isinstance(n, bool) and n > 0:
                        last = {'scope': 'per_request', 'input_tokens': n,
                                'basis': 'codex-last-token-usage', 'source': str(path),
                                'event_line': index + 1}
                elif ev.get('type') in ('assistant', 'message_start'):
                    message = ev.get('message', {})
                    usage = message.get('usage') if isinstance(message, dict) else None
                    if not isinstance(usage, dict):
                        continue
                    keys = ('input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens')
                    values = [usage.get(k) for k in keys]
                    if all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in values) and sum(values) > 0:
                        last = {'scope': 'per_request', 'input_tokens': sum(values),
                                'basis': 'claude-message-ordinary-plus-cache', 'source': str(path),
                                'event_line': index + 1}
    except OSError:
        pass
    return last


def choose_session(previous, current_sid, engine, phase, branch, p):
    if not current_sid:
        return 'fresh', 'no-session'
    if p['mode'] == 'fresh':
        return 'fresh', 'configured-fresh'
    if p['mode'] == 'resume':
        return 'resume', 'configured-resume'
    if not previous or previous.get('sid') != current_sid:
        return 'resume', 'adopt-existing-session'
    if previous.get('engine') and previous['engine'] != engine:
        return 'fresh', 'engine-changed'
    if phase and previous.get('phase') and phase != previous['phase']:
        return 'fresh', 'phase-changed'
    if branch and previous.get('branch') and branch != previous['branch']:
        return 'fresh', 'method-branch-changed'
    if previous.get('successful_turns', 0) >= p['max_session_turns']:
        return 'fresh', 'session-working-set-window'
    n = request_context_tokens(previous)
    if n is not None and n >= p['max_context_tokens']:
        return 'fresh', 'observed-request-context-size'
    return 'resume', 'same-working-unit'


def next_task(fields, source, last):
    if os.environ.get('LEMVO_MESSAGE_KIND') == 'user':
        original = os.environ.get('LEMVO_CURRENT_MESSAGE_PATH')
        raw = text(original).strip() if original else ''
        if raw:
            # A faithful first line is an index, not a paraphrase. Full new
            # instructions remain in the preserved input, including long steer.
            line = guidance_first_line(raw)
            if line:
                return line, 'current-user-message: ' + original, None
    was_waiting = isinstance(last, dict) and last.get('status') == 'waiting'
    if was_waiting and ((source and text(source).strip()) or os.environ.get('LEMVO_RESOURCE_WAKE')):
        # A genuine new event gets a new work packet. Do not copy the old
        # waiting worker's free-form "next_task" back as another idle task.
        return ('Process the newly delivered runtime event or changed scientific sources; '
                'read the actual new result once, preserve completed work, and select '
                'the next concrete authorized research action or register its real dependency.',
                'new-runtime-event-after-wait', 'analysis')
    requested = last.get('next_task') if isinstance(last, dict) and not was_waiting else None
    if isinstance(requested, dict) and requested.get('objective'):
        return str(requested['objective']), 'previous-work-result.next_task', requested.get('kind')
    objective = first(fields, ['prep.next_min_probe', 'next_min_probe', 'research_plan.next_action',
                               'research_plan.next_task', 'next_action'])
    if objective:
        return str(objective), 'research-state.yaml', None
    # Do not call a runtime boilerplate paragraph the user's concrete objective.
    objective = first(fields, ['project.objective', 'project.question', 'objective', 'question'])
    if objective:
        return str(objective), 'research-state.yaml project objective', None
    for name in ('prompt.txt', 'CHARTER.md', 'STATE.md'):
        if Path(name).is_file():
            raw = text(name).strip()
            if raw:
                head = next((line.strip('# \t') for line in raw.splitlines()
                             if line.strip('# \t')), '')
                return head[:1000], name + ' (read source to resolve task)', 'bootstrap'
    return 'Resolve the concrete research task from the preserved input below, then perform its next useful work.', 'input', 'bootstrap'


def prepare(td, source, target, engine='codex', session_file='.autoloop-session-id'):
    td = Path(td)
    td.mkdir(parents=True, exist_ok=True)
    current_sid = text(session_file).strip()
    previous = load(CONTEXT, {})
    previous = previous if isinstance(previous, dict) else {}
    fields, p = state_fields(), policy()
    phase = first(fields, ['research_plan.stage', 'project.phase', 'phase', 'stage'], '')
    branch = first(fields, ['research_plan.method_branch', 'research_plan.branch',
                            'project.method_branch', 'method_branch'], '')
    last = load(LAST_RESULT, {})
    objective, origin, kind = next_task(fields, source, last)
    phase = str(phase or '')
    branch = str(branch or '')
    kind = kind or ('research' if not phase else phase)
    action, reason = choose_session(previous, current_sid, engine, phase, branch, p)
    files = []
    for name in ('CHARTER.md', 'research-state.yaml', 'STATE.md', 'findings.md',
                 'research-log.md', 'prompt.txt'):
        f = file_record(name, p['file_excerpt_chars'])
        if f:
            files.append(f)
    refs = [{'path': f['path'], 'fingerprint': f['fingerprint'],
             'fingerprint_basis': f['fingerprint_basis'], 'bytes': f['bytes']} for f in files]
    task = {'schema': 'lemvo-work-unit', 'schema_version': VERSION,
            'turn_id': td.name, 'kind': str(kind), 'objective': objective,
            'objective_source': origin, 'phase': phase, 'method_branch': branch,
            'project_objective': first(fields, ['project.objective', 'project.question', 'objective', 'question']),
            'mode': text('.lemvo-mode').strip() or 'unspecified', 'engine': engine,
            'current_input_kind': os.environ.get('LEMVO_MESSAGE_KIND', 'runtime'),
            'jobs': jobs_context(), 'source_refs': refs,
            'relevant_skill_paths': skill_paths(kind),
            'previous_result': '.lemvo-last-work-result.json' if last else None,
            'result_path': str(td / 'work-result.json')}
    plan = {'schema': 'lemvo-context-plan', 'schema_version': VERSION,
            'turn_id': td.name, 'session_action': action, 'reason': reason,
            'previous_sid': current_sid or None, 'engine': engine,
            'phase': phase, 'branch': branch, 'policy': p,
            'adopted_session': reason == 'adopt-existing-session',
            'files': refs, 'created_utc': now(), 'created_epoch': time.time()}
    parts = [
        'WORK UNIT (runtime dispatch, not a scientific admission gate):\n' + json.dumps(task, ensure_ascii=False),
        'Perform the concrete work above, then hand back useful changes/results. Do not turn missing metadata into preparation or approval work. '
        'A new metric is not required for coding, implementation repair or useful analysis to count. '
        'Select and improve methods; a scoped negative result or missing confirmation does not reject a whole direction. '
        'Current user guidance overrides a suggested next task. Relevant skill paths are optional focused entrypoints, '
        'not a demand to read every listed skill or a restriction on other necessary skills.',
        'Context policy: %s (%s). The project, permissions, instruction revision, jobs and accumulated resource records continue unchanged. '
        'A fresh model session is NOT a fresh research campaign. Never restart an existing run_id. '
        'Read original files when needed; fingerprints identify unchanged sources, not a prohibition on reading. '
        'No excerpt or summary grants permission absent from its source.' % (action, reason),
    ]
    if action == 'fresh':
        for f in files:
            parts.append('PROJECT SOURCE %s [fingerprint %s; %s; %s]\n%s' %
                         (f['path'], f['fingerprint'], f['fingerprint_basis'],
                          'EXCERPT ONLY; omitted text remains authoritative, consult source for relevant permissions/evidence' if f['excerpt_truncated'] else 'complete',
                          f['excerpt']))
    else:
        changed = [f['path'] for f in refs if not any(old.get('path') == f['path'] and
                   old.get('fingerprint') == f['fingerprint'] for old in previous.get('files', []))]
        parts.append('Project source files changed since the last successful work: %s. '
                     'Use current relevant sections; avoid rereading unchanged whole papers, logs and role guides merely to resume.' %
                     (', '.join(changed) or '(none among tracked memory files)'))
    if isinstance(last, dict) and last:
        # This is a machine projection, never a model paraphrase of old verdicts.
        parts.append('Previous work handoff (retain conditions; not a scientific verdict):\n' + bounded_handoff(last))
    parts.append('At completion, optionally write %s as JSON with status, summary, artifact_refs, '
                 'metrics (only actually observed values with their conditions), and next_task {kind, objective}. '
                 'Partial/waiting work is valid. Missing JSON is compatible and does not stop the campaign. '
                 'Do not copy raw training logs into chat; keep full logs on disk and return changed metrics or a necessary error excerpt plus paths.' % (td/'work-result.json'))
    if os.environ.get('LEMVO_MESSAGE_KIND') == 'user':
        parts.append('NEW CURRENT GUIDANCE: the preserved input below takes priority over the suggested objective/previous next_task. '
                     'Apply it to current work without reviving superseded tasks or inventing extra resource permission.')
    parts.append('PRESERVED CURRENT INPUT AND RUNTIME INSTRUCTIONS (verbatim; do not discard or replace with the packet):\n' + text(source))
    atomic(td/'work-unit.json', task)
    atomic(td/'context-plan.json', plan)
    atomic(target, '\n\n'.join(parts))
    return plan


def last_answer(path, limit=6000):
    """Only terminal assistant prose, never tool output or model reasoning."""
    answer = ''
    try:
        with Path(path).open(encoding='utf-8', errors='replace') as stream:
            for line in stream:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(e, dict):
                    continue
                if e.get('type') == 'result' and isinstance(e.get('result'), str):
                    answer = e['result']
                item = e.get('item', {})
                if e.get('type') == 'item.completed' and isinstance(item, dict) and item.get('type') == 'agent_message':
                    if isinstance(item.get('text'), str):
                        answer = item['text']
    except OSError:
        pass
    return excerpt(answer, limit)[0] if answer else ''


def clean_result(obj, td, rc):
    # Preserve scoped statements verbatim rather than manufacturing a verdict.
    status = obj.get('status')
    if status not in VALID_STATUS:
        status = 'unknown'
    refs = obj.get('artifact_refs', [])
    refs = refs if isinstance(refs, list) else [refs]
    metrics = obj.get('metrics', [])
    if not isinstance(metrics, (list, dict)):
        metrics = []
    r = {'schema': 'lemvo-work-result', 'schema_version': VERSION,
         'turn_id': td.name, 'status': status, 'summary': str(obj.get('summary') or ''),
         'artifact_refs': refs, 'metrics': metrics,
         'engine_rc': int(rc), 'recorded_utc': now(),
         'source': 'explicit-work-result', 'evidence_scope': obj.get('evidence_scope',
              'Reported values and conclusions apply only to their recorded implementation and conditions.'),
         'limitations': obj.get('limitations', [])}
    nxt = obj.get('next_task')
    if isinstance(nxt, dict) and nxt.get('objective'):
        r['next_task'] = {k: nxt[k] for k in ('kind', 'objective', 'method_branch') if k in nxt}
    return r


def numeric_epoch(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    return None


def parse_utc(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    if value.endswith('Z'):
        value = value[:-1] + '+0000'
    value = re.sub(r'([+-]\d\d):(\d\d)$', r'\1\2', value)
    for fmt in ('%Y-%m-%dT%H:%M:%S.%f%z', '%Y-%m-%dT%H:%M:%S%z',
                '%Y-%m-%dT%H:%M:%S.%f', '%Y-%m-%dT%H:%M:%S'):
        try:
            dt = datetime.datetime.strptime(value, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            return dt.timestamp()
        except ValueError:
            continue
    return None


def dispatch_order(td, plan=None, manifest=None):
    """Order dispatches, not recovery time. Legacy sources have clear basis."""
    td = Path(td)
    plan = plan if isinstance(plan, dict) else (load(td/'context-plan.json', {}) or {})
    manifest = manifest if isinstance(manifest, dict) else (load(td/'manifest.json', {}) or {})
    stamp, basis = parse_utc(manifest.get('started_utc')), 'manifest.started_utc'
    tie = numeric_epoch(plan.get('created_epoch'))
    if stamp is None:
        stamp = tie
        basis = 'context-plan.created_epoch'
    if stamp is None:
        stamp = parse_utc(plan.get('created_utc'))
        basis = 'context-plan.created_utc'
    if stamp is None:
        m = re.search(r'(\d{8}T\d{6}Z)', td.name)
        if m:
            try:
                stamp = datetime.datetime.strptime(m.group(1), '%Y%m%dT%H%M%SZ').replace(
                    tzinfo=datetime.timezone.utc).timestamp()
                basis = 'timestamp-in-turn-id'
            except ValueError:
                pass
    if stamp is None:
        for name in ('.turn-start', 'manifest.json', 'context-plan.json', 'engine-stdout.jsonl',
                     'result.json', 'work-result.json'):
            try:
                stamp = (td/name).stat().st_mtime
                basis = 'legacy-file-mtime:' + name
                break
            except OSError:
                pass
    return {'started_epoch': stamp, 'basis': basis if stamp is not None else 'unsequenced-legacy',
            'tie_break_epoch': tie, 'turn_id': td.name}


def order_key(order):
    if not isinstance(order, dict):
        return None
    stamp = numeric_epoch(order.get('started_epoch'))
    if stamp is None:
        return None
    tie = numeric_epoch(order.get('tie_break_epoch'))
    return (stamp, tie if tie is not None else stamp, str(order.get('turn_id', '')))


def existing_order(record, context=False):
    if not isinstance(record, dict):
        return None
    order = record.get('dispatch_order')
    if order_key(order) is not None:
        return order
    tid = record.get('last_settled_turn' if context else 'turn_id')
    if tid and Path('turns', str(tid)).is_dir():
        return dispatch_order(Path('turns', str(tid)))
    stamp = parse_utc(record.get('recorded_utc'))
    return {'started_epoch': stamp, 'basis': 'legacy-result.recorded_utc', 'turn_id': tid or ''} if stamp else None


def settle(td, rc=0, session_file='.autoloop-session-id'):
    td = Path(td)
    runtime = load(td/'result.json', {}) or {}
    manifest = load(td/'manifest.json', {}) or {}
    epoch = text('.lemvo-session-epoch').strip() or 'legacy'
    wrong_epoch = manifest.get('session_epoch', epoch) != epoch
    runtime_status = runtime.get('status')
    runtime_bad = runtime_status is not None and runtime_status not in (
        'succeeded', 'success', 'ok', 'completed', 'recovered-orphan')
    if (runtime_bad or wrong_epoch) and not rc:
        rc = runtime.get('engine_rc') or 1
    marker = load(td/'work-settlement.json', {}) or {}
    if marker.get('successful'):
        return load(td/'work-result.json', {}) or {}
    plan = load(td/'context-plan.json', {}) or {}
    context = load(CONTEXT, {}) or {}
    order = dispatch_order(td, plan, manifest)
    previous_orders = [existing_order(load(LAST_RESULT, {})), existing_order(context, context=True)]
    latest_key = max((order_key(o) for o in previous_orders if order_key(o) is not None), default=None)
    current_key = order_key(order)
    stale = (latest_key is not None and current_key is not None and current_key < latest_key)
    old = load(td/'work-result.json')
    # A module-produced record may already exist after interruption/recovery.
    result = clean_result(old, td, rc) if isinstance(old, dict) else None
    if result is None:
        progress = load(td/'progress.json', {}) or {}
        summary = last_answer(td/'engine-stdout.jsonl')
        changed = [c.get('path') for c in progress.get('changes', [])
                   if isinstance(c, dict) and c.get('path')]
        status = 'failed' if rc else ('waiting' if runtime.get('next_action') in
                                     ('waiting_jobs', 'needs_user', 'waiting_resource') else 'partial')
        result = clean_result({'status': status, 'summary': summary or
                              ('Runtime completed; inspect referenced artifacts for research conclusions.' if not rc else
                               'Execution did not complete; this is not evidence against a scientific hypothesis.'),
                               'artifact_refs': changed + [str(td/'result.json'), str(td/'progress.json')],
                               'metrics': [], 'limitations': ['No explicit structured research result; metrics remain unknown.']}, td, rc)
        result['source'] = 'runtime-compatible-fallback'
        result['summary_source'] = str(td/'engine-stdout.jsonl') if summary else 'runtime-facts'
    if rc:
        result['status'] = 'failed'
        result['scientific_interpretation'] = 'Execution failure does not establish a scientific negative.'
    if wrong_epoch:
        result['runtime_notice'] = 'Archived session epoch; retained per-turn only, not adopted into current research context.'
    result['dispatch_order'] = order
    if stale:
        result['runtime_notice'] = 'Older dispatched work recovered after a newer handoff; retained per-turn, not promoted over current work.'
    atomic(td/'work-result.json', result)
    if rc == 0 and stale:
        atomic(td/'work-settlement.json', {'successful': True, 'turn_id': td.name,
                                        'promoted': False, 'reason': 'older-dispatch',
                                        'dispatch_order': order, 'recorded_utc': now()})
        return result
    # Failed work is retained per-turn but must not replace successful handoff.
    if rc == 0:
        atomic(LAST_RESULT, result)
        if context.get('last_settled_turn') != td.name:
            sid = text(session_file).strip()
            confirmed = bool(sid) and (plan.get('session_action') != 'fresh' or sid != plan.get('previous_sid'))
            if confirmed:
                same = sid == context.get('sid') and plan.get('session_action') != 'fresh'
                new = {'schema': 'lemvo-context-runtime', 'schema_version': VERSION,
                       'sid': sid, 'engine': plan.get('engine'),
                       'phase': plan.get('phase'), 'branch': plan.get('branch'),
                       'successful_turns': (int(context.get('successful_turns', 0)) + 1) if same else 1,
                       'last_settled_turn': td.name, 'files': plan.get('files', []),
                       'dispatch_order': order,
                       'updated_utc': now()}
                observation = observe_request_context(td/'engine-stdout.jsonl')
                if observation:
                    atomic(td/'request-context.json', observation)
                else:
                    observation = load(td/'request-context.json', {}) or {}
                if observation.get('scope') == 'per_request' and isinstance(observation.get('input_tokens'), int):
                    new['last_request_context'] = observation
                atomic(CONTEXT, new)
            else:
                result['session_notice'] = 'Useful work retained; no new confirmed session identity, context counters unchanged.'
                atomic(td/'work-result.json', result)
                atomic(LAST_RESULT, result)
        atomic(td/'work-settlement.json', {'successful': True, 'turn_id': td.name,
                                        'recorded_utc': now()})
    return result


def report(td, status, summary, next_objective=None, next_kind=None, artifacts=None,
           metrics_file=None, evidence_scope=None):
    td = Path(td)
    obj = {'status': status, 'summary': summary, 'artifact_refs': artifacts or [],
           'metrics': load(metrics_file, []) if metrics_file else []}
    if evidence_scope:
        obj['evidence_scope'] = evidence_scope
    if next_objective:
        obj['next_task'] = {'kind': next_kind or 'research', 'objective': next_objective}
    result = clean_result(obj, td, 0)
    atomic(td/'work-result.json', result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command')
    p = sub.add_parser('prepare')
    p.add_argument('turn_dir'); p.add_argument('input'); p.add_argument('output')
    p.add_argument('--engine', default='codex', choices=('codex', 'claude'))
    p.add_argument('--session-file', default='.autoloop-session-id')
    p = sub.add_parser('settle')
    p.add_argument('turn_dir'); p.add_argument('--rc', type=int, default=0)
    p.add_argument('--session-file', default='.autoloop-session-id')
    p = sub.add_parser('report')
    p.add_argument('turn_dir'); p.add_argument('--status', choices=VALID_STATUS, default='partial')
    p.add_argument('--summary', required=True); p.add_argument('--next', dest='next_objective')
    p.add_argument('--next-kind'); p.add_argument('--artifact', action='append', default=[])
    p.add_argument('--metrics-file'); p.add_argument('--evidence-scope')
    a = parser.parse_args(argv)
    if a.command == 'prepare':
        prepare(a.turn_dir, a.input, a.output, a.engine, a.session_file)
        print('WORK_UNIT_PLAN=' + str(Path(a.turn_dir)/'context-plan.json'))
    elif a.command == 'settle':
        result = settle(a.turn_dir, a.rc, a.session_file)
        print('WORK_RESULT=%s status=%s' % (Path(a.turn_dir)/'work-result.json', result['status']))
    elif a.command == 'report':
        report(a.turn_dir, a.status, a.summary, a.next_objective, a.next_kind, a.artifact,
               a.metrics_file, a.evidence_scope)
        print('WORK_RESULT=' + str(Path(a.turn_dir)/'work-result.json'))
    else:
        parser.print_help()
        return 64
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
