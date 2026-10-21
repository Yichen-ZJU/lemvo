# Runtime operation and recovery

Use the installed entry point directly. Do not copy the two shell files to a
separate directory: they ship with `runtime-support.py` and `runtime-contract.py`, and a detached copy
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
new resources or permit duplicate jobs. Explicit STOP/PAUSED still prevent dispatch;
human HOLD/BLOCKED-USER follows the reply contract below.
An actual resource reassessment that remains WAITING_RESOURCE records its model
usage and total call count, but does not count as a stagnant research/preparation
turn. Repeated waiting is an operational condition, not a negative research result.

## Guidance delivery and long turns

### Human waiting and replies

Set `campaign_status: WAITING_USER` when a human decision is actually needed.
Optionally add `runtime_wait.question_id` and `runtime_wait.question` to identify
the question; otherwise the runner assigns a stable ID. HOLD, EVIDENCE_HOLD,
BLOCKED, BLOCKED_USER/BLOCKED-USER and STOPPED_INCONCLUSIVE use the same human-wait
contract. A higher-priority STOP/STOPPED/PAUSED remains a hard pause.

While waiting, the controller creates no model calls, research turns or prep/stall
increments. A new steer or explicit `advance` permits one reply-handling turn.
If resolved, update the waiting fields and remove only the resolved HOLD or
BLOCKED-USER marker; if unresolved, retain the wait and state the missing decision
once. Receiving a reply does not itself authorize GPU work, spending, downloads
or changes to global constraints. In guided mode a new steer also grants one
guided turn, after which the controller waits again.

```bash
bash /installed/templates/lemvo-run.sh status /project
bash /installed/templates/lemvo-run.sh steer /project reply.md --reply-to wait-IDENTITY
```

An explicitly mismatched `--reply-to` stays queued, without waking the wrong
question. Legacy unscoped messages still reach a human-wait assessment. Runtime
completion is recorded separately from `next_action`: a successful reply-handling
call can still end in `needs_user`. The three-line `turns/<id>/runtime-summary.md`
and `.lemvo-last-turn.json` carry the runtime result, evidence/log paths and next
action into the next call. They do not judge a hypothesis.

### Scoped instruction versions

Plain `steer /project message.md` remains append-only guidance. For a continuing
task that replaces an earlier task, explicitly name the scope and replaced ID:

```bash
bash /installed/templates/lemvo-run.sh steer /project assess.md --id assess-v1 --scope implementation
bash /installed/templates/lemvo-run.sh steer /project build.md --id build-v2 --scope implementation --supersedes assess-v1
```

IDs are immutable and unique. Supersession must name an existing current
instruction in the same scope. Global/constraints/authorization/safety scopes
cannot be superseded through this task shortcut. Use the existing explicit
authorization/constraint-change procedure instead. No last-message-wins policy
is applied to unrelated work or resource boundaries.

The controller activates replacements at the turn boundary and stores the exact
revision in `instructions.json` and the turn manifest. A mid-turn message does not
change the in-flight snapshot. Superseded queued messages are archived as
`superseded`, not falsely marked `delivered`, only after the carrying turn
succeeds; failed delivery keeps the files. `.lemvo-instructions.json` retains the
active named directives across resumes/restarts, and prompts identify them so old
conversation history does not reactivate replaced work. Ordinary unversioned
messages are not repeated forever as active directives.

`steer /project /absolute/path/message.md` publishes an immutable message.
`.lemvo-delivery.jsonl` records queued, submitted and delivered with message_id
and turn_id. Delivered means a successful terminal event for the turn carrying
that message, not a guarantee the model performed every requested action.
Malformed/unknown terminal events and failed turns leave messages queued.
Messages arriving during a turn remain for the next turn. At a safe phase
boundary the agent should checkpoint and return when it sees pending guidance;
guidance alone does not interrupt an in-progress model call. The activity watchdog
below handles unresponsive calls independently.

### In-flight turn watchdog

The adapter observes CLI stdout/stderr growth while the CLI is running. Claude
uses `--output-format stream-json --verbose --include-partial-messages`; Codex
uses `exec --json`. Final-only JSON would conceal activity until the turn ends.
Observation is not proof of useful research: polling, state-file rewrites, queued
guidance and unrelated training output never refresh this activity timer.

Defaults (seconds; environment variables on the controller/adapter):

| Variable | Default | Meaning |
| --- | ---: | --- |
| `LEMVO_TURN_SOFT_IDLE_SEC` | 1800 | Visible warning after no CLI output |
| `LEMVO_TURN_HARD_IDLE_SEC` | 3600 | Interrupt a still-silent CLI; 0 disables |
| `LEMVO_TURN_MAX_SEC` | 0 | Optional absolute turn deadline; disabled by default |
| `LEMVO_TURN_POLL_SEC` | 5 | Observation interval, no model calls |
| `LEMVO_TURN_TERM_GRACE_SEC` | 15 | Grace before SIGKILL if the same CLI ignores SIGTERM |

These are runtime liveness thresholds, not GPU-hour, model-dollar or research
budgets. A multi-hour turn with continuing output can run without an absolute cap.
Silence is only an operational heuristic: adjust/disable the hard idle timer for
known long silent foreground tools. Prefer independent registered background
jobs for long training. No idle timeout judges a hypothesis or erases a result.

`status /project` shows the latest `.lemvo-inflight.json`; each turn retains
`heartbeat.json` and transition-only `watchdog-events.jsonl`. Inspect the observed
timestamp as well as state: a dead observer cannot keep its timestamp fresh.
The files show idle/elapsed seconds, pending guidance count and any protected job.
During a live turn, an operator may grant a turn-local lease:

```bash
bash /installed/templates/lemvo-run.sh renew /project 1800
```

This defers automatic interruption for 30 minutes, does not fabricate output,
does not reset resource/research counters, and cannot renew a later turn.

At hard timeout the watchdog rechecks PID plus Linux starttime and signals only
that CLI, never a process group or training PID. **A CLI's own signal handler can
still cancel its foreground tools.** If a registered live experiment is a CLI
descendant (or the job registry is unreadable), status becomes
`timeout-protected-job`: automatic termination is withheld, and the operator must
inspect, detach/finish the job or explicitly decide how to interrupt it. This is
an observable safety hold, not a guarantee that all tool hangs self-recover.
Independent registered jobs keep running. Pending steer is not an interrupt request.

Interrupted turns settle as failed (`next_action=runtime_error`, with
`runtime_reason=turn-timeout` for watchdog expiry), retain
SID and guidance, and follow the existing bounded runtime-error retry/backoff.
They record elapsed time and available usage (missing usage stays unknown), but
neither increment nor reset prep/stall/poison counters. Partial files remain for
the next turn to inspect. A timeout marker also prevents orphan recovery from
mistaking an interrupted turn for successful delivery.

An ordinary nonzero CLI exit also remains a runtime error, not a stagnant
research turn. It neither increments nor clears prep/stall/poison counters.
Partial files remain available for inspection, without accepting a successful
progress baseline. Runtime retries remain bounded by the existing failure policy;
this does not automatically impose a human HOLD on every recoverable tool error.
Waiting and runtime-error receipts participate in cumulative usage/cost
baselines, so changing a turn category cannot make the next turn charge it again.

Streaming/cancellation semantics: [Claude headless documentation](https://code.claude.com/docs/en/headless)
and [Codex non-interactive documentation](https://developers.openai.com/codex/noninteractive).

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
`doctor` checks all four runtime files, including the contract helper; startup saves the
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

### One progress record for engineering and research

`turns/<id>/progress.json` is the shared input to prep and stall accounting.
Runnable code, configuration, launchers, literature/analysis notes and experiment
evidence can be construction progress; no new metric, novelty proof or significance
test is required to continue implementation. Job registration with a verified
PID/starttime, and matching terminal result JSON, are recorded once as job
transitions. Negative experimental results remain useful evidence.

Record the useful file in `prep.last_informative_artifact`; do not write
`last_artifact_sha`, which is an acknowledgement owned by the runner. A model
writing that legacy field cannot cancel a real before/after change. For a deep,
excluded or external implementation file, register it **before** editing:

```bash
bash /installed/templates/lemvo-run.sh track /project /authorized/fork/model.py
```

Registration is not an access grant and does not duplicate the file. Explicit
hashing is bounded by `LEMVO_EXPLICIT_HASH_BYTES` (64 MiB) and
`LEMVO_EXPLICIT_HASH_SEC` (2 seconds) per file; larger files should supply a compact
result/manifest. Late registration without an earlier baseline is unknown, not
invented progress; the next turn will capture its baseline. Unknown inspection,
human/resource waiting and interrupted calls do not increment stagnation.

The accepted baseline is `.lemvo-progress-accepted.json`, not agent-written state.
Same-content rewrites, bookkeeping-only changes, polls and merely receiving
steer do not reset counters. File changes measure execution progress, not
scientific quality; do not generate filler notes to keep a loop alive. Total
turns, wall time, token/USD/GPU statistics and authorization caps remain cumulative.
A no-progress stop is displayed as `STALLED`, not mislabeled budget exhaustion.
