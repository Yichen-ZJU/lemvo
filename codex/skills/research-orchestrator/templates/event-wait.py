"""Operational Claude hooks: wait on identified jobs using CPU, not model polls.

No research selection, quality gates, provider calls, process signals or restarts.
Unrecognized work fails open. Parent runtime owns CLI recovery and liveness lease.
"""
from pathlib import Path
from contextlib import contextmanager
import argparse
import hashlib
import json
import os
import re
import shlex
import sys
import time

try:
    import fcntl
except ImportError:  # Allows deterministic, local Windows tests.
    fcntl = None


def read(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".%s.tmp" % os.getpid())
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    os.replace(tmp, path)


@contextmanager
def locked(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if fcntl:
            fcntl.flock(handle, fcntl.LOCK_EX)
        else:
            import msvcrt
            if handle.seek(0, 2) == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            if fcntl:
                fcntl.flock(handle, fcntl.LOCK_UN)
            else:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


class Runtime:
    def __init__(self, root, proc_root="/proc", interval=2):
        self.root = Path(root).resolve()
        self.state = self.root / ".runtime-events"
        self.proc_root = Path(proc_root)
        self.interval = interval

    def emit(self, kind, **fields):
        self.state.mkdir(parents=True, exist_ok=True)
        with locked(self.state / "events.lock"):
            with (self.state / "events.jsonl").open("a") as out:
                out.write(json.dumps({"at": time.time(), "kind": kind, **fields}) + "\n")

    def identity(self, pid):
        try:
            pid = int(pid)
            if pid <= 0:
                return None
            directory = self.proc_root / str(pid)
            fields = (directory / "stat").read_text().rsplit(")", 1)[1].split()
            return {"pid": pid, "start": fields[19], "state": fields[0], "ppid": int(fields[1]),
                    "uid": directory.stat().st_uid}
        except (ValueError, OSError, IndexError, TypeError):
            return None

    def jobs(self):
        jobs = read(self.state / "jobs.json", [])
        if not isinstance(jobs, list):
            return []
        return [j for j in jobs if isinstance(j, dict) and j.get("job_id")
                and str(j.get("pid", "")).isdigit()
                and str(j.get("start", "")).isdigit()]

    def allowed_roots(self):
        values = read(self.state / "allowed-roots.json", [])
        if isinstance(values, dict):
            values = values.get("roots", [])
        if not isinstance(values, list):
            values = []
        return [self.root] + [Path(p).resolve() for p in values
                              if isinstance(p, str) and Path(p).is_absolute() and p != "/"]

    def ancestors(self):
        result, pid = set(), os.getpid()
        for _ in range(64):
            if pid in result or pid <= 1:
                break
            result.add(pid)
            item = self.identity(pid)
            if not item:
                break
            pid = item["ppid"]
        result.add(os.getppid())
        return result

    def marker_current(self, path, job):
        if job.get("accept_cached_markers"):
            return True
        try:
            boot = next(int(line.split()[1]) for line in (self.proc_root / "stat").read_text().splitlines() if line.startswith("btime "))
            started = boot + float(job["start"]) / os.sysconf("SC_CLK_TCK")
            return Path(path).stat().st_mtime >= started - 1
        except (OSError, ValueError, KeyError, StopIteration, AttributeError):
            return True

    def marker_ready(self, path, job):
        p = Path(path)
        if not p.is_file() or not self.marker_current(p, job):
            return False
        if job.get('done_format') == 'json' or (job.get('done_format') != 'file' and p.suffix.lower() == '.json'):
            try:
                content = json.loads(p.read_text(encoding='utf-8'))
                if isinstance(content, dict) and any(
                        str(content.get(key, '')).strip().lower().replace('-', '_') in
                        ('running', 'pending', 'queued', 'starting', 'started', 'in_progress',
                         'processing', 'training', 'initializing', 'loading', 'waiting',
                         'scheduled', 'submitted') for key in ('status', 'state')):
                    return False
            except (OSError, ValueError, UnicodeError):
                return False
        return True

    def event(self, job):
        failed = [p for p in job.get("failed_files", []) if self.marker_ready(p, job)]
        if failed:
            return {"job_id": job["job_id"], "state": "failed", "files": failed}
        done = job.get("done_files", [])
        if done and all(self.marker_ready(p, job) for p in done):
            return {"job_id": job["job_id"], "state": "completed", "files": done}
        current = self.identity(job["pid"])
        if not current or current["state"] in ("Z", "X") or current["start"] != str(job["start"]):
            return {"job_id": job["job_id"], "state": "producer_exited",
                    "note": "Producer exit/identity mismatch alone does not establish success."}
        return None

    @staticmethod
    def expand_vars(command):
        variables = {}
        for match in re.finditer(r"(?:^|[;\n])\s*([A-Za-z_]\w*)=(\"[^\"]*\"|'[^']*'|[^;\s]+)", command):
            value = match.group(2).strip("\"'")
            if value.startswith("/") and "$" not in value and "`" not in value:
                variables[match.group(1)] = value
        for key, value in variables.items():
            command = command.replace("${" + key + "}", value)
            command = re.sub(r"\$" + re.escape(key) + r"\b", lambda _: value, command)
        return command

    @staticmethod
    def status_poll(command):
        """Recognize only leading sleep + read-only status probes; never work."""
        match = re.match(r"\A\s*sleep\s+(?:\d+(?:\.\d+)?)\s*(?:;|&&)\s*(.+)\Z", command, re.S)
        if not match:
            return None
        rest = match.group(1)
        clean = re.sub(r"(?:2|1)?>\s*/dev/null", "", rest)
        if re.search(r"[<>`]", clean):
            return None
        if "$(" in clean:
            clean, count = re.subn(r"\$\(\s*cat\s+[^()]+\)", " PID_FROM_FILE ", clean)
            if "$(" in clean:
                return None
        allowed = {"tail", "head", "ls", "stat", "test", "[", "ps", "cat", "echo",
                   "printf", "wc", "du", "date", "grep", "sed", "tr", "cut",
                   "true", "false", "nvidia-smi", "fuser", "kill"}
        for segment in re.split(r"[;|&\n]+", clean):
            segment = segment.strip()
            if not segment:
                continue
            # Only literal assignments, not command substitutions/evaluation.
            if re.fullmatch(r"[A-Za-z_]\w*=(?:\"[^\"]*\"|'[^']*'|[^\s]+)", segment):
                continue
            try:
                words = shlex.split(segment)
            except ValueError:
                return None
            if not words or words[0] not in allowed:
                return None
            if words[0] == "kill" and (len(words) < 2 or words[1] != "-0"):
                return None
            if words[0] == "fuser" and any("k" in w[1:] for w in words[1:] if w.startswith("-")):
                return None
            if words[0] == "nvidia-smi":
                if any(w.startswith('-') and not (
                        w in ('-q', '-L', '-i', '--id') or
                        w.startswith(('--query-', '--format=', '--id='))) for w in words[1:]):
                    return None
            if words[0] == 'tail' and any(w in ('-f', '-F', '--follow') or w.startswith('--follow=') for w in words[1:]):
                return None
            if words[0] in {"sed", "awk"} and any(w == "-i" or w.startswith("--in-place") for w in words[1:]):
                return None
            if words[0] == "sed" and not (len(words) >= 3 and words[1] == "-n"
                                              and re.fullmatch(r"[0-9,$]+p", words[2])):
                return None
        return rest

    def command_paths(self, command):
        expanded = self.expand_vars(command)
        paths = []
        for match in re.finditer(r"(?:^|[\s=\"'])((?:/[^\s;|&\"')]+))", expanded):
            path = Path(match.group(1)).resolve()
            if str(path) != "/dev/null":
                paths.append(path)
        return list(dict.fromkeys(paths))

    @staticmethod
    def path_score(query, marker):
        if query == marker:
            return 100
        # Prefix of a producer/arm stem: armW_train/log <-> armW_STAGE_DONE.
        qparts, mparts = query.parts, marker.parts
        i = 0
        while i < min(len(qparts), len(mparts)) and qparts[i] == mparts[i]:
            i += 1
        if i < 4:
            return 0
        if i < min(len(qparts), len(mparts)):
            q, m = qparts[i], mparts[i]
            qs, ms = re.split(r"[_.]", q)[0], re.split(r"[_.]", m)[0]
            if len(qs) >= 4 and qs == ms:
                return 90
        if query.parent == marker.parent:
            return 65
        if marker.parent in query.parents or query in marker.parents:
            return 55
        return 0

    def explicit_pids(self, command):
        expanded = self.expand_vars(command)
        pids = set()
        for match in re.finditer(r"(?:\bps\s+(?:-\w+\s+)*-p\s+|\bkill\s+-0\s+)([0-9,]+)", expanded):
            pids.update(int(p) for p in match.group(1).split(",") if p.isdigit())
        for match in re.finditer(r"\$\(\s*cat\s+([\"']?)(/[^\s()]+?)\1\s*\)", expanded):
            path = Path(match.group(2).strip("\"'"))
            if not path.name.endswith((".pid", "_pid", ".pid.txt")):
                continue
            try:
                value = path.read_text().strip()
            except OSError:
                continue
            if value.isdigit():
                pids.add(int(value))
        return pids

    def adopt_visible_pid(self, pid):
        now = self.identity(pid)
        if not now or now["state"] in ("Z", "X") or pid in self.ancestors():
            return None
        if now["uid"] != getattr(os, "getuid", lambda: now["uid"])():
            return None
        proc = self.proc_root / str(pid)
        try:
            command = (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            return None
        roots = self.allowed_roots()
        related = any(re.search(re.escape(str(root)) + r"(?=[/\\]|\s|$)", command) for root in roots)
        if not related:
            try:
                cwd = (proc / "cwd").resolve(strict=True)
            except OSError:
                return None
            related = any(cwd == root or root in cwd.parents for root in roots)
        if not related:
            return None  # Not demonstrably this project's producer.
        job = {"job_id": "visible-pid-%s-%s" % (pid, now["start"]),
               "pid": pid, "start": now["start"], "done_files": [], "failed_files": [],
               "label": "Exact project-owned producer referenced by status command"}
        with locked(self.state / "jobs.lock"):
            jobs = self.jobs()
            same = next((j for j in jobs if j["pid"] == pid and str(j["start"]) == now["start"]), None)
            if same:
                return same
            jobs.append(job)
            atomic(self.state / "jobs.json", jobs)
        self.emit("visible_producer_adopted", job_id=job["job_id"], pid=pid, start=now["start"])
        return job

    def resolve(self, event):
        inp = event.get("tool_input", {})
        jobs = self.jobs()
        if event.get("tool_name") == "TaskOutput":
            task_id = inp.get("task_id") or inp.get("taskId")
            return [j for j in jobs if task_id and (j["job_id"] == task_id or task_id in j.get("task_ids", []))]
        command = inp.get("command", "")
        pids = self.explicit_pids(command)
        selected = [j for j in jobs if int(j["pid"]) in pids]
        for pid in pids - {int(j["pid"]) for j in selected}:
            now = self.identity(pid)
            if not now or now["state"] in ("Z", "X"):
                # Nothing is waited on or killed: report the explicit status
                # query's dead producer immediately and remove its needless sleep.
                selected.append({"job_id": "observed-dead-pid-" + str(pid), "pid": pid,
                                 "start": now["start"] if now else "0",
                                 "done_files": [], "failed_files": []})
                continue
            job = self.adopt_visible_pid(pid)
            if job:
                selected.append(job)
        if selected:
            return selected
        if pids:
            return []  # Explicit unrelated live PID is not an excuse to wait on another job.
        scores = {}
        for job in jobs:
            score = max((self.path_score(q, Path(m).resolve())
                         for q in self.command_paths(command)
                         for m in job.get("done_files", []) + job.get("failed_files", [])), default=0)
            if score:
                scores[job["job_id"]] = score
        if not scores:
            return []
        best = max(scores.values())
        winners = [j for j in jobs if scores.get(j["job_id"]) == best]
        return winners if len(winners) == 1 else []  # No all-fleet/unrelated fallback.

    def wait(self, hook_event, jobs, kind):
        token = str(hook_event.get("tool_use_id") or hook_event.get("toolUseID") or
                    hashlib.sha256(json.dumps({"session": hook_event.get("session_id"),
                                              "agent": hook_event.get("agent_id"),
                                              "kind": kind, "jobs": [[j["job_id"], j["pid"], j["start"]]
                                                                       for j in jobs]}, sort_keys=True).encode()).hexdigest()[:24])
        token = re.sub(r"[^A-Za-z0-9_.-]", "_", token)
        path = self.state / "waits" / (token + ".json")
        previous = read(path, {})
        has_tool_id = hook_event.get('tool_use_id') or hook_event.get('toolUseID')
        if has_tool_id and previous.get("state") == "terminal":
            return previous["terminal"]
        process = self.identity(os.getpid())
        initial_terminal = list(filter(None, (self.event(job) for job in jobs)))
        fresh_terminal = [value for value in initial_terminal
                          if not self.delivered_path(hook_event, jobs, value).exists()]
        active = [job for job in jobs if self.event(job) is None]
        spec = {"pid": os.getpid(), "start": process["start"] if process else None,
                "session_id": hook_event.get("session_id"), "agent_id": hook_event.get("agent_id"),
                "job_ids": [j["job_id"] for j in jobs], "created": time.time(),
                "state": "waiting", "hook": kind, "tool_use_id": token,
                "initial_terminal": initial_terminal}
        atomic(path, spec)
        self.emit("wait_started", **spec)
        while True:
            if any((self.root / name).exists() for name in ("PAUSE", "STOP", "CAMPAIGN-DONE")) or (self.state / "PAUSE").exists():
                terminal = {"state": "user_paused"}
                break
            # A fresh completed result must reach the model even while another
            # producer is running. Only a previously delivered result is old.
            if fresh_terminal:
                terminal = fresh_terminal[0]
                break
            terminal = next(filter(None, (self.event(j) for j in active)), None)
            if not active:
                terminal = {"state": "already_delivered" if initial_terminal else "no_jobs",
                            "job_ids": [job['job_id'] for job in jobs]}
            if terminal:
                break
            time.sleep(self.interval)
        spec.update(state="terminal", terminal=terminal, ended=time.time())
        atomic(path, spec)
        if terminal.get('job_id'):
            atomic(self.delivered_path(hook_event, jobs, terminal), terminal)
        self.emit("wait_terminal", wait_id=token, session_id=spec["session_id"],
                  agent_id=spec["agent_id"], **terminal)
        return terminal

    def delivered_path(self, hook_event, jobs, event):
        job = next((j for j in jobs if j['job_id'] == event.get('job_id')), {})
        markers = []
        for name in event.get('files', []):
            try:
                value = Path(name).stat()
                markers.append([name, value.st_mtime_ns, value.st_size])
            except OSError:
                markers.append([name, None, None])
        scope = [hook_event.get('session_id'), hook_event.get('agent_id')]
        payload = [scope, job.get('job_id'), job.get('pid'), job.get('start'), event.get('state'), markers]
        signature = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        return self.state / 'delivered' / (signature + '.json')

    def register(self, pid, start=None, job_id=None, done=None, failed=None,
                 task_ids=None, done_format='auto'):
        now = self.identity(pid)
        if not now or now['state'] in ('Z', 'X') or now['uid'] != os.getuid():
            raise ValueError('A live producer owned by the current user is required')
        if start is not None and str(start) != now['start']:
            raise ValueError('Producer identity changed')
        if int(pid) in self.ancestors():
            raise ValueError('Cannot register an ancestor as its own wait dependency')
        # Explicit registration binds an exact identity chosen by the caller.
        # It never sends signals or starts a producer. Markers stay inside this
        # project or an explicitly configured allowed root.
        def paths(values):
            result = []
            for value in values or []:
                p = Path(value)
                p = p.resolve() if p.is_absolute() else (self.root / p).resolve()
                if not any(p == root or root in p.parents for root in self.allowed_roots()):
                    raise ValueError('Marker outside the configured project roots')
                result.append(str(p))
            return result
        job = dict(job_id=job_id or 'registered-%s-%s' % (pid, now['start']),
                   pid=int(pid), start=now['start'], done_files=paths(done),
                   failed_files=paths(failed), task_ids=task_ids or [], done_format=done_format,
                   registered_at=time.time())
        with locked(self.state / 'jobs.lock'):
            jobs = self.jobs()
            old = next((j for j in jobs if j['job_id'] == job['job_id']), None)
            if old and (old['pid'] != job['pid'] or str(old['start']) != job['start']):
                raise ValueError('job_id already belongs to a different producer; use a new id')
            jobs = [j for j in jobs if j['job_id'] != job['job_id']] + [job]
            atomic(self.state / 'jobs.json', jobs)
        self.emit('producer_registered', job_id=job['job_id'], pid=job['pid'], start=job['start'])
        return job

    def hook(self, event):
        name = event.get("hook_event_name")
        if name == "PreToolUse":
            tool = event.get("tool_name")
            rest = None
            if tool == "Bash":
                rest = self.status_poll(event.get("tool_input", {}).get("command", ""))
                if rest is None:
                    return {}
            elif tool != "TaskOutput":
                return {}
            jobs = self.resolve(event)
            if not jobs:
                return {}
            terminal = self.wait(event, jobs, "pretool")
            if terminal["state"] == "user_paused":
                return {"continue": False, "stopReason": "Explicit user PAUSE ended the CPU event wait."}
            out = {"hookEventName": "PreToolUse",
                   "additionalContext": "CPU event wait ended: " + json.dumps(terminal) +
                   ". Inspect this real terminal event once; process exit is not success."}
            if rest is not None:
                updated = dict(event.get("tool_input", {}))
                updated["command"] = rest  # Original harmless inspection runs once, without another timer.
                out["updatedInput"] = updated
            return {"hookSpecificOutput": out}
        if name in ("Stop", "SubagentStop"):
            message = event.get("last_assistant_message", "")
            matches = re.findall(r"<runtime_wait>\s*(.*?)\s*</runtime_wait>", str(message), re.S)
            if len(matches) != 1:
                return {}
            spec = read_json_string(matches[0])
            if not isinstance(spec, dict) or spec.get("independent_work_pending") is not False:
                return {}
            ids = spec.get("job_ids")
            if not isinstance(ids, list) or not ids:
                return {}
            jobs = [j for j in self.jobs() if j["job_id"] in ids]
            if len(jobs) != len(set(ids)):
                return {}
            terminal = self.wait(event, jobs, "stop")
            if terminal['state'] in ('already_delivered', 'no_jobs'):
                return {}
            key = self.delivered_path(event, jobs, terminal).stem
            receipt = self.state / "stop_delivered" / (key + ".json")
            atomic(receipt, terminal)
            if terminal["state"] == "user_paused":
                return {}
            return {"decision": "block", "reason": "A real registered CPU-watched job became terminal: " +
                    json.dumps(terminal) + ". Continue the SAME original scientific session now; inspect once and preserve completed work."}
        return {}


def read_json_string(value):
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["hook", "register", "status", "wait"], nargs="?", default="hook")
    parser.add_argument("--root", default=os.environ.get("RUNTIME_EVENTS_ROOT", str(Path(__file__).resolve().parents[1])))
    parser.add_argument("--interval", type=float, default=2)
    parser.add_argument('--pid', type=int)
    parser.add_argument('--start', help='Optional expected /proc start ticks')
    parser.add_argument('--job-id', action='append', default=[])
    parser.add_argument('--done', action='append', default=[])
    parser.add_argument('--failed', action='append', default=[])
    parser.add_argument('--task-id', action='append', default=[])
    parser.add_argument('--done-format', choices=['auto', 'json', 'file'], default='auto')
    args = parser.parse_args()
    runtime = Runtime(args.root, interval=args.interval)
    if args.mode != 'hook':
        try:
            if args.mode == 'register':
                if args.pid is None or len(args.job_id) > 1:
                    parser.error('register requires --pid and at most one --job-id')
                output = runtime.register(args.pid, args.start, args.job_id[0] if args.job_id else None,
                                          args.done, args.failed, args.task_id, args.done_format)
            else:
                jobs = runtime.jobs()
                if args.job_id:
                    jobs = [j for j in jobs if j['job_id'] in args.job_id]
                    if len(jobs) != len(set(args.job_id)):
                        raise ValueError('Unknown registered job_id')
                if args.mode == 'wait':
                    if not jobs:
                        raise ValueError('No identified jobs to wait on')
                    output = runtime.wait({}, jobs, 'explicit')
                else:
                    output = [dict(job=j, terminal=runtime.event(j)) for j in jobs]
        except ValueError as error:
            parser.error(str(error))
        print(json.dumps(output, ensure_ascii=False), flush=True)
        return
    try:
        output = runtime.hook(json.load(sys.stdin))
    except Exception as error:
        runtime.emit("hook_error", error=type(error).__name__)
        output = {}  # No scientific admission/denial and no retry-forcing hook failure.
    print(json.dumps(output, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
