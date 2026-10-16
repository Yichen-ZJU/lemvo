---
name: compute-env-setup
description: Set up a reproducible compute environment for research jobs. Use when a task needs Python/R packages, GPU libraries, containers, Modal, SSH, caches, or managed model runtime setup.
---

# Compute Environment Setup

Use this skill before running a research job that needs a nontrivial runtime.

Workflow:

1. Identify the job type, hardware need, data size, package stack, secrets, and expected artifacts.
2. Choose the smallest working route: local notebook runtime, project virtualenv/conda, Modal, SSH/Slurm, or managed endpoint.
3. Verify credentials and CLIs through environment variables or CLI status checks without printing secret values.
4. Write an environment note containing package versions, install commands, cache paths, hardware, and failure modes.
5. Run a tiny smoke job before the expensive job and save the smoke logs.

The done state is a recorded, reproducible environment plus a successful smoke or a precise missing dependency.

## Environment failure escalation: search GitHub before replace

When a runtime failure resists the obvious fixes, do not conclude that the
machine, driver, or stack "cannot run" the workload, and do not propose
replacement (another machine, driver upgrade, migration, abandoning the
host) before an explicit external search:

1. Search GitHub issues and discussions for the exact error signature: the
   top function names, the error strings, and the version tuple (library,
   CUDA, driver). Do a general web search too. Record the queries and
   candidate hits in the campaign notes.
2. Exhaust library-level fixes before infrastructure-level conclusions.
   Many "hardware wall" failures are library bugs replaceable without root:
   pip-installable versions (e.g., NCCL, vLLM, torch), `LD_PRELOAD` for
   statically linked libraries, and library environment variables.
3. Report "this environment cannot run X" only after each applicable layer
   is ruled out with evidence. A BLOCKER whose options include replacing or
   abandoning the environment must cite the search performed and why each
   candidate fix does not apply.
