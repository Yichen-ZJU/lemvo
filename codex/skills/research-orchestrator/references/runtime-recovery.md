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

Missing usage remains unknown with a visible warning; it does not disable the
independent turn, wall-time and liveness counters. Unrecognized terminal protocols
are runtime errors, not successful turns. Rewriting STATE.md or bookkeeping alone
does not reset liveness. New code, analysis and evidence count as construction;
an observed negative result is still evidence. A liveness pause requests a concrete
next action or runtime repair, never a declaration that the idea is false.
GPU hours and model dollars retain the existing statistics-only default.
