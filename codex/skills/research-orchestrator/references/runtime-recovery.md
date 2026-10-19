# Runtime operation and recovery

Use the installed entry point directly. Do not copy the two shell files to a
separate directory: they ship with `runtime-support.py`, and a detached copy
will not receive later skill updates.

```bash
bash ~/.claude/skills/research-orchestrator/templates/lemvo-run.sh start /project --engine claude --mode unattended
bash ~/.codex/skills/research-orchestrator/templates/lemvo-run.sh start /project --engine codex --mode guided
```

An existing valid mode is preserved on restart. `switch /project guided|unattended`
changes the next handoff, not the running training job. `advance /project`
requests one guided turn. Research state, results and resource totals persist.

## Starting and taking over

The adapter creates missing `research-state.yaml` accounting fields itself.
The first model turn reads CHARTER.md, STATE.md, prompt.txt or the supplied
instruction, records the actual objective, and starts a useful research task.
The scaffold confers no new resource permissions and invents no research claims.
Existing Markdown STATE.md is context; the machine ledger is research-state.yaml.

`lemvo-run.sh doctor /project` reports the invoked runtime path/hash, installed
runtime candidates and project-associated legacy loop processes. Startup saves
the same inventory to `.lemvo-takeover-inventory.json`. Inspect and account for
each legacy controller/retry loop before switching ownership. Stop only identified
superseded automation when authorized; record or adopt real training jobs instead
of killing them. The owner lock coordinates Lemvo instances, not arbitrary scripts.
The inventory is advisory and cannot discover remote schedulers or every external
automation.

## Waiting without losing control

Register background jobs in `.lemvo-jobs.json` with name, run_id, done_file and,
for local jobs, pid plus `pid_start` (Linux /proc/PID/stat field 22). Use an
explicit timeout_min when the job has an expected deadline; this triggers
reassessment, not automatic process termination. Publish result JSON atomically.

A dead or identity-mismatched producer produces a non-success receipt. A result
file without a verifiable producer or deadline receives an `unknown` receipt
after `LEMVO_UNKNOWN_JOB_SEC` (default 900 seconds), so a missing file cannot
cause an infinite wait. Unknown does not mean the experiment failed.

`WAITING_RESOURCE` is periodically reassessed at
`LEMVO_WAIT_RECHECK_SEC` (default 900 seconds), with the timer saved across
controller restarts. Pending user guidance or a completed-job receipt can wake it
earlier. Normal waiting polls use no model calls. The assessment does not grant
new resources or permit duplicate jobs. Explicit STOP/HOLD/BLOCKED-USER still apply.
An actual resource reassessment that remains WAITING_RESOURCE records its model
usage and total call count, but does not count as a stagnant research/preparation
turn. Repeated waiting is an operational condition, not a negative research result.

## Guidance delivery and long turns

`steer /project /absolute/path/message.md` publishes an immutable message.
`.lemvo-delivery.jsonl` records queued, submitted and delivered with message_id
and turn_id. Delivered means a successful terminal event for the turn carrying
that message, not a guarantee the model performed every requested action.
Malformed/unknown terminal events and failed turns leave messages queued.
Messages arriving during a turn remain for the next turn. At a safe phase
boundary the agent should checkpoint and return when it sees pending guidance;
this CLI integration does not interrupt an in-progress model call.

## Inline dispatch

`start /project --delegation inline`, or `delegation /project inline`,
keeps research, checking and writing in the lead session. `auto` restores normal
task-dependent dispatch. This choice is independent of guided/unattended and
does not claim that a gateway's child-agent implementation has been fixed.
The next model call receives the preference even when it is a resume or a steer.

## Resetting a session

`purge /project` resets a stopped controller. For an explicit runtime stop and
reset, use `purge /project --stop-runtime`. The latter terminates only registered
controller/adapter/CLI process identities; it does not kill training or arbitrary
legacy automation. It waits for verified writers to stop before archiving.

Each reset advances `.lemvo-session-epoch`. Old turn outputs cannot restore an
old SID into the new session. Turns, receipts and SID caches are moved into a
timestamped archive. Findings, code, results, pending guidance, job registration
and cumulative resource ledgers are retained. If a verified process will not stop,
reset reports failure and preserves the evidence for inspection.

## Runtime health is not a research verdict

### Bounded inspection and historical recovery

Progress inspection has independent I/O limits, not research/GPU limits: defaults
are 5,000 directory entries, 64 MiB of new reads and a cooperative 2-second scan.
These are configurable with `LEMVO_SNAPSHOT_MAX_ENTRIES`,
`LEMVO_SNAPSHOT_MAX_BYTES`, `LEMVO_SNAPSHOT_MAX_SEC`. A single blocked filesystem
operation is not preempted by this time limit. Content hashes are cached by file
identity/size/mtime/ctime; unchanged files are not reread. Unrelated assets can be
excluded with root-relative patterns, one per line in `.lemvo-snapshot-exclude`.
Large results can use the explicit `prep.last_informative_artifact` path.
An incomplete scan is recorded as unknown, not no progress: it cannot increment
preparation/stall/poison counters on its own. Explicit evidence remains usable.

Recovery indexes pending turns in `.lemvo-recovery.sqlite3`. Initial legacy
discovery is batched; settled and failed recovery rows are not reread at every
startup. A failed recovery becomes `needs-review`, retaining inputs/output;
after investigation, `retry-recovery /project turns/<turn_id>` explicitly retries
that turn while the controller is stopped. No-CLI preflight turns are renamed
into `.lemvo-turn-archive/<UTC-day>/` with a summary; nothing is deleted and
guidance is not acknowledged by this archival. Active writers always take priority.

Historical prep pollution can be audited with:

```bash
bash /installed/templates/lemvo-run.sh repair-prep /project /project/poisoned-archive-<id> "incident reason"
```

This is dry-run by default. With `--apply` and a stopped controller it subtracts
only individually evidenced recovered turns whose archived directories contain
neither CLI identity nor output. It preserves original ledger entries, backs up
state, appends a repair record, and leaves total/cost/GPU statistics and caps alone.
It does not equate every archived directory with an invalid turn or reset all history.
A continuation authorization is consumed once; use a fresh `AUTHORIZE_REQUEST_ID`
only for a genuinely new authorization, not a persistent environment bypass.

### Resource-wait observations

Normal waits back off from 15 to 60 to 240 to 900 seconds (maximum configurable via
`LEMVO_WAIT_MAX_POLL_SEC`). Lightweight sleep slices still check human guidance and
mode changes. The existing default 900-second reassessment remains independent.
The rc=7 fallback registers the same wait state and closes a preflight-only turn;
it cannot create a fresh directory on every polling tick.

Optional `.lemvo-wait.json` describes concrete wake observations, not permission:

```json
{"deadline_epoch": 1800000000, "conditions": [
  {"kind": "time", "not_before_epoch": 1799990000},
  {"kind": "process", "pid": 1234, "pid_start": "123456"},
  {"kind": "heartbeat", "path": "launcher.heartbeat", "max_age_sec": 180},
  {"kind": "memory", "min_available_bytes": 118111600640}
]}
```

Use only relevant observations, with actual times/identities. Dead/stale sources,
deadlines and invalid contracts create a persistent alert and wake a reassessment;
they do not automatically declare the research failed or require a new human gate.
Read `.lemvo-wait-alert.json` and `.lemvo-wait-events.jsonl` for observations.
Multiple time/memory conditions must all be ready; process/heartbeat conditions
monitor producer health. Existing launch constraints remain in the research plan.

### Detached operation and deployment identity

`start` intentionally runs in the foreground. For an SSH-independent process use
a tmux session or `nohup bash /installed/templates/lemvo-run.sh start /project
--engine claude --mode unattended > /project/lemvo-controller.log 2>&1 < /dev/null &`.
Do not treat a terminal's session lifetime as the campaign lifetime.
`doctor` checks all three runtime files, including the helper; startup saves the
invoked bundle hashes in `.lemvo-runtime-at-start.json`. During deployment compare
that record, the actual process invocation and installed hashes. Updating files
does not prove an existing process switched versions. Migrate only at a safe
turn boundary, retaining background training and the same project ledger.

Missing usage remains unknown with a visible warning; it does not disable the
independent turn, wall-time and liveness counters. Unrecognized terminal protocols
are runtime errors, not successful turns. Rewriting STATE.md or bookkeeping alone
does not reset liveness. New code, analysis and evidence count as construction;
an observed negative result is still evidence. A liveness pause requests a concrete
next action or runtime repair, never a declaration that the idea is false.
GPU hours and model dollars retain the existing statistics-only default.
