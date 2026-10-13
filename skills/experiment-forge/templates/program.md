# <TASK_NAME> — autonomous experiment loop

> Authoritative loop contract: `references/contract.md` inside this
> package (copied from `experiment-forge/references/contract.md` at
> forge time). If this template and the contract ever disagree on
> **package-internal technical conventions** (schema, log naming, loop
> rules), the contract wins. Neither overrides human permissions,
> approved resource caps, or global stage semantics.

## Handoff envelope (v2.1, filled at forge/handoff time)

- stage: <exploration | confirmation>        # from research_plan.stage
- plan_version: <N>                          # research_plan.version at handoff
- constraint_ids: <id1, id2, ...>            # active constraints; the package
                                             # must not add stricter admission gates
- eval_contract: <namespace@version>         # metric/split/criteria identity;
                                             # changing any of these = new namespace
                                             # + new version; old best_value and
                                             # old comparisons are preserved

Outer-loop plan revisions create a NEW package version — the frozen
evaluator/protocol of an in-flight loop is never edited silently inside
that loop.


This repo is an autonomous research task. You (the agent) are the researcher. The human sets the direction; you run experiments indefinitely until interrupted.

## Setup

1. **Agree on a run tag** with the human: propose one from today's date (e.g. `jul26`). The branch `autoresearch/<tag>` must not already exist — this is a fresh run.
2. **Create the branch**: `git checkout -b autoresearch/<tag>`.
3. **Read the in-scope files** for full context:
   - `<LOCKED_FILE>` — data prep, evaluation, fixed constants. **Do not modify.**
   - `<OPEN_FILE(S)>` — the file(s) you modify, per the task's declared editable scope. <OPEN_FILE_DESCRIPTION, e.g. "model, optimizer, training loop" — the task may open additional training-pipeline files (model/data/augmentation modules); everything declared editable is fair game>.
4. **Verify prerequisites**: <DATA/ENV CHECK, e.g. "data shards exist at ~/.cache/<task>/ — if not, tell the human to run `<PREP_COMMAND>`">.
5. **Initialize results.tsv** with just the header row (the baseline is recorded after the first run).
6. Confirm setup looks good, then kick off experimentation.

## Experimentation

Each experiment runs: `<RUN_COMMAND>`, under this task's **comparison basis `<COMPARISON_BASIS>`** — one of:
- **equal wall-clock**: fixed `<BUDGET>` per run (excluding startup/compilation) — the right protocol when the research question is "which is better under fixed compute";
- **equal updates**: same step/epoch count;
- **equal data**: same tokens/examples seen;
- **train to agreed convergence**: the declared convergence criterion.
Fixed wall-clock is one optional protocol, not a universal fairness standard — a slower-but-better method is not a scientific failure for being slow (r29 A4).

**What you CAN do:**
- Modify `<OPEN_FILE(S)>` — the task's declared editable training-pipeline scope. Everything declared editable is fair game: <EXAMPLES: architecture, optimizer, hyperparameters, batch size, ...>.

**What you CANNOT do:**
- Modify `<LOCKED_FILE>`. It is read-only — it contains the fixed evaluation and data pipeline (protected evaluation integrity).
- Install packages silently. Dependency changes go through the task's pre-authorized procedure: <DEPENDENCY_POLICY, e.g. "declare package+version; install in the isolated env" or "no changes — use only what's in <DEPENDENCY_FILE>">. Network, cost and data boundaries still apply.
- Modify the evaluation harness. `<EVAL_FUNCTION>` is the ground truth metric.

**The goal: <DIRECTION, e.g. "lowest"> <METRIC_NAME>.** All comparisons use the declared comparison basis — keep it consistent across the whole run. Hard constraints: the code runs without crashing and respects the resource envelope; legitimate longer training within the authorized envelope is not a failure (r29 A4).

**Resources** are a soft constraint: some increase is acceptable for meaningful gains, no blow-ups.

**Simplicity criterion**: all else equal, simpler is better. Weigh complexity cost against improvement magnitude. A tiny improvement that adds hacky complexity is not worth keeping; equal results with less code is a win — keep it.

**The first run** is always the unmodified baseline.

## Reading results

The run prints a summary containing `<METRIC_NAME>: <value>`. Extract it with:

```bash
grep "^<METRIC_NAME>:" run-<N>.log
```

(N = iteration number; every iteration gets its own `run-<N>.log` —
logs are never overwritten, so results.tsv can be reconciled against
logs by iteration number.)

## Logging results

Log every experiment to `results.tsv` (TAB-separated, NOT commas — commas break in descriptions):

```
iteration	timestamp	commit	metric	delta	guard	status	move	description
```

1. iteration number (baseline = 0)
2. timestamp (ISO8601 UTC)
3. git commit hash (short, 7 chars)
4. metric achieved — leave EMPTY for crash/timeout/metric-error rows (never write a fabricated 0 as a score; r29 A6)
5. delta vs previous keep (empty for baseline and for empty-metric rows)
6. guard: `pass` / `fail` / `na`
7. status: `keep`, `discard`, `crash`, `timeout`, `metric-error`, `no-op`, `blocked` — `timeout` = execution abort (NOT a scientific negative); `metric-error` = output parse/adapter mismatch (NOT a crash)
8. move: `follow` / `reverse` / `switch` / `escalate` / `simplify` / `baseline`
9. short description of what this experiment tried

The full schema and loop rules live in one place: see the experiment
loop contract (`experiment-forge/references/contract.md`). If this
template ever disagrees with the contract on package-internal technical
conventions, the contract wins (scope per contract header).

## The experiment loop

LOOP up to <MAX_ITERATIONS (default 50)> iterations and within <TIMEOUT, e.g. 12h>:

1. Check git state (current branch/commit).
2. Modify `<OPEN_FILE>` with one experimental idea.
3. git commit.
4. Run: `<RUN_COMMAND> > run-<N>.log 2>&1` (redirect everything — do NOT use tee or flood your context). Each re-execution within the same iteration gets its own file: confirmation runs use `run-<N>-confirm1.log`, `run-<N>-confirm2.log`, crash-fix attempts use `run-<N>-fix1.log`, `run-<N>-fix2.log`, ... — never reuse the primary log.
5. Read results: `grep "^<METRIC_NAME>:\|^<RESOURCE_METRIC>:" run-<N>.log`.
6. If grep is empty, do NOT assume crash yet — check how the run actually ended (r29 A6: separate process exit code, parse status, and scientific score):
   - Process exited nonzero / stack trace in log → **crash**: `tail -n 50 run-<N>.log`. Easy fix (typo, missing import) → fix and re-run. Runtime error → up to 3 automatic fix attempts for THIS iteration. Still broken → log `crash`, `git revert HEAD --no-edit` (crashes always end in a revert), move on. Fix-attempt caps bound the current iteration only — they never refute the direction.
   - Process exited zero but the log shows a valid metric in a different format (e.g. JSON `{"accuracy": 0.91}` while the grep pattern expects `accuracy:`) → **metric-error** (adapter mismatch, not a crash): fix the extraction pattern to match the actual output and re-extract — the metric itself and the eval protocol are unchanged, so this is not moving the goalposts.
   - Process exited zero and no metric anywhere → treat as crash and investigate.
7. **Guard** (if `<GUARD_COMMAND>` configured): run it every iteration; on failure revert regardless of metric and record `guard-fail`.
8. **min_delta** (if configured): improvements smaller than `<MIN_DELTA>` trigger a confirmation run (re-run 1–2×, take the median). min_delta is a **confirmation trigger, not a keep bar** (r29 A7): if the median still favors the change, KEEP it and record the confirmation status — a small positive signal is never erased for being below an arbitrary absolute number. Only a task/user-declared "minimum practical effect" may gate keeps, and only when explicitly declared in this program.
9. Record in results.tsv (do NOT commit results.tsv — leave it untracked).
10. Metric improved → keep the commit, the branch advances.
11. Metric equal/worse → `git revert HEAD --no-edit` (NOT reset — failed experiments stay in history so future iterations can learn what doesn't work).

**Timeout**: if a run exceeds <KILL_THRESHOLD, e.g. 2× the protocol duration>, kill it and record status `timeout` — an execution abort, NOT a scientific negative (r29 A4). Under an equal-wall-clock comparison basis it additionally means the run is not comparable under this protocol (note the protocol-discard in the description); under other bases, fix and re-run or continue under the outer loop's authorization.

**NEVER STOP** means: do not pause to ask the human whether to continue. It does NOT override the iteration/time bounds above. Out of ideas? Think harder — read referenced papers, re-read the in-scope files for new angles, combine previous near-misses, try more radical changes.
