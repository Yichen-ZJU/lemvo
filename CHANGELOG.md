# 2026-11-10 — Neighbor mechanisms in method generation

## 2026-11-23 — Idle settlement, job-event hardening, cross-tree parity and companion observability

- Upstreamed `runtime-idle.py` and settlement ordering (`capture-worker` -> `settle` -> `recovery-mark`) across all 4 distribution trees, gating `--auto-wake` strictly on `armed == True` so `WAITING_RESOURCE` steer wakes preserve `user-steer` semantics.
- Hardened `job-events.py` and `lemvo-run.sh`: support `done_path`/`failed_path`/`result_path`/`terminal_receipt` aliases, suppress phantom 900s `unknown` wakes on `already_harvested` entries and dead PIDs with live `resume_pid`, and make `stamp-started` atomic/locked and consolidated into `reconcile --stamp-started`.
- Hardened `autoloop-reference.sh` and `work-unit.py`: fixed multi-line YAML quoted/block scalar parsing in `read_fields()`/`prep-snapshot`, added bounded `campaign_log.txt` rotation (`LEMVO_CAMPAIGN_LOG_MAX_BYTES`), and cached sibling module imports in `runtime-idle.py`/`work-unit.py`.
- Fixed `codex-pro` `research-orchestrator/SKILL.md` schema drift (`metric_name`, `maximize|minimize`, `r29 B3` checks), split oversized `skills/deepspeed/SKILL.md` into `references/full-guide.md`, added `scripts/check-runtime-drift.py`, and updated `console/` and `desktop/` for `native-event-owner`, `idle_state`, `pending_receipts`, and rotated job snapshots.
- Verification (CPU stubs/replays only; no real model or GPU benchmark claimed): 11/11 CPU idle-settlement replays/regressions passed (0 real model calls), `console/tests/acceptance.py` 9/9 passed, cross-repo drift check passed, and all 4 parallelized `tests/run_tests.sh` suites passed.

## 2026-11-17 — Event-driven experiment waiting and CLI continuity

- Deliver each completed or failed experiment branch independently while preserving other live jobs; persist and deduplicate execution receipts across restarts.
- Keep recognized Claude producer waits in ordinary CPU code, with project-local hooks and verified wait heartbeats instead of repeated model polling. Existing permissions and unrelated hooks are preserved.
- Add an optional native CLI lifecycle owner with same-session result delivery, exact process adoption and a shared lock that prevents parallel native/Lemvo dispatchers.
- Preserve pending delivery on missing/failed CLI results and ignore incomplete or explicitly nonterminal JSON snapshots. No new scientific acceptance criteria, GPU limits or default cost caps.
- Include actual adapter, process-lock, partial-result and restart regressions. Runtime configuration and real model observations are reported separately.


## 2026-11-16 — Work-unit dispatch and context handoffs

- Dispatch concrete tasks and retain structured or backward-compatible work results; no new scientific approval gate.
- Preserve campaign state, authorization, job IDs and accounting across safe fresh/resume session selection.
- Reuse unchanged source material, project bounded context and return existing producer metrics without guessing missing values.
- Prevent stale recovered work, queue metadata and purged handoffs from reviving old tasks; retain interrupted first-session identity.
- Validation: all four distribution suites and desktop installation tests passed. Real Claude CPU two-work-unit loop passed; Codex runtime fixtures passed, while real tool execution is blocked by host sandbox/network conditions. No fixed token savings claim.


- Carry neighboring methods' actual operations, inputs/outputs and necessary conditions into generation, then adapt them to target conditions rather than adding citations after drafting.
- Reuse the existing candidate schema and read-only context helper; optional repeatable --methods forwards source notes before candidates, preserving legacy calls and missing-note fallback. Explicit UTF-8 output supports Windows consumers.
- Connect literature assets, brainstorming, orchestrator, Forge/Autoresearch, native Arbor skill/freeform prompts and AutoScientists analyst handoff. No new research stage, role, source quota or novelty gate.
- Relevant context tests pass in all four distributions; a fresh real-model Reframe run consumed method excerpts through the helper and used them in concrete adapted operations. Source-conditioned generation only; no training or end-to-end automatic retrieval claim.
- Existing research/review step order, runtime, authorization and project state remain intact.

# 2026-11-10 — Paper-seeded method construction

- Extend existing ideation with concrete operator, supervision, objective, representation and information-flow design moves inspired by ResearchStudio.
- Turn a seed paper into implementable candidates and first development comparisons; reuse current candidate/experiment interfaces without new gates, quotas or review roles.
- Route public Claude/Codex and Pro ideation; cover native Arbor skill/freeform prompts and the AutoScientists analyst handoff.
- Real-model demonstration on Reframe (2611.12886) generated three methods and revised local objective/provenance/accounting issues in the same session. Full source supplied inline after isolated CLI tool-read failures; not an automatic-loading or training-success benchmark.
- Existing full research/review workflows, runtime state and experiment permissions unchanged.

# 2026-11-05 — Restore research workflows; narrow action-first scope

- Restore deep-research, literature-review and global conventions exactly to the prior revision: full research workflow and multi-level review retained.
- Limit anti-repetition guidance to ideation and experiment development. No weakening of literature verification.
- Retain scoped development checks and exploratory/confirmatory distinction. No runtime or campaign changes.

# 2026-11-05 — Research action before repeated process

- Scope validation to changed behavior; reuse applicable evidence. Targeted literature lookups return directly to implementation.
- Development iterations reuse evaluation contracts; protocol freezing stays specific to confirmatory experiments.
- Remove automatic verifier/reviewer chains for narrow research and mandatory two-input checks for unrelated changes.
- No runtime, approval, resource-limit or running-campaign changes.

# Changelog

## 2026-11-02 — Windows decision worker and SSH handoff

- Add an opt-in native Windows Codex worker and per-project SSH bridge, reusing existing Linux Lemvo controllers and ledgers.
- Add durable request claims, output acknowledgement, separate provider sessions, worker restart recovery, and safe planned Windows/server handoff.
- Add managed Windows skill installation, preserving local overrides and existing Windows GPU reservation skills.
- No active research campaigns are automatically adopted or restarted. See desktop/README.md for recovery boundaries.
