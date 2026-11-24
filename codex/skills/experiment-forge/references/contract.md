# Experiment Loop Contract (single source of truth)

Version: 2.4 · 2026-11-24 (v2.4 adds custom collator/trainer tensor & grad-accum scaling parity micro-check and retraction sync; retains v2.3 §8 Tiers T0-T3 & r29 A4/A6/A7)
Owner: `experiment-forge` generates task packages; `autoresearch` executes
the loop. **优先级范围**：本契约只对**包内技术约定**（results.tsv schema、
日志命名、局部循环规则、crash/guard/min_delta 协议）是单一来源——这些与
SKILL.md/program.md 冲突时以本契约为准。**本契约不覆盖**：人类明确权限与
安全边界、已批准的资源上限（envelope）、全局阶段语义与约束治理（见
research-orchestrator/references/constraint-governance.md）——那些来自用户
与 orchestrator 主控，包内任何文件不得以"契约优先"改写它们。Changes here
must be propagated to both skills in the same commit.

## 0. Envelope 与计划版本（REQUIRED，v2.1 新增）

每个实验包携带上游交接的四个字段（写在 program.md 头部与 package 的
contract 副本注释中）：

- `stage`：exploration | confirmation（继承 research_plan.stage）；
- `plan_version`：交接时的 research_plan.version；外循环修订计划后产生
  **新包版本**，旧包的 evaluator/冻结协议不在同一 loop 内改动；
- `constraint_ids`：交接时活跃的约束 ID 清单（research-state.yaml
  research_plan.constraints）；包内不得新增比清单更苛刻的准入条件；
- `eval_contract`：评测合同命名空间（指标/split/判据的命名标识）；换
  metric/split/数据集 = 新命名空间 + 新版本，旧 best_value 与旧对照保留，
  不跨命名空间比较收益；
- `comparison_basis`（r29 A4）：等 wall-clock | 等 updates | 等数据量 |
  训练到约定收敛——按研究问题在 Forge 时选定；固定时长只是可选协议，
  不是普适公平标准，也不得成为变相的总时长限制。协议内的 kill 阈值
  是失控作业护栏：超限记 `timeout`（执行中断），不是方法否证。

局部循环服从 envelope：maxIterations/timeout/min_delta 是**本次交接的
局部约定**，执行期间必须兑现；需要调整时由外循环产生新版本，不在循环内
静默放宽。停滞（见 §2）升级为外循环修订信号，不是局部无限续跑的理由。

## 1. results.tsv schema (TAB-separated, never commas)

Header (exactly, one line):

```
iteration	timestamp	commit	metric	delta	guard	status	move	description
```

Columns:

1. `iteration` — integer, baseline = 0
2. `timestamp` — ISO8601 UTC
3. `commit` — short (7-char) git hash of the experiment commit
4. `metric` — numeric result; **empty** for crash / timeout / metric-error rows (never a fabricated 0 as a score — r29 A6)
5. `delta` — change vs **eval_baseline（预注册冻结的原基线）**；代码保留另对照 best_value（当前最佳实现）：delta>0 但 < best 时，成果记改进、代码不覆盖最佳。两口径分别是 research-state.yaml 的 eval_baseline / best_value 字段，禁止混用
6. `guard` — `pass` / `fail` / `na` (no guard configured)
7. `status` — `keep` / `discard` / `crash` / `timeout` / `metric-error` / `no-op` / `blocked`
   （r29 A6 分类：`crash` = 进程失败退出；`timeout` = 超过 kill 阈值的执行中断，非科学否证；`metric-error` = 进程正常退出但指标解析失败（输出格式与 grep 模式不匹配等），修适配不改评判。三者都不写入占位成绩，也不自动升级为方向切换）
8. `move` — strategy-table move: `follow` / `reverse` / `switch` /
   `escalate` / `incubate` / `leap` / `simplify` / `baseline`
9. `description` — short, no TABs, no commas-in-place-of-TABs (recommended prefix `[T0]`/`[T1]`/`[T2]`/`[T3]` and `[family=<id>]` when logging)

`results.tsv` is **never committed** (leave untracked).

## 2. Loop bounds

- The loop is bounded: `maxIterations` (default 20; unattended runs
  50+ or a value agreed up front) **and/or** a wall-clock `timeout`.
- "LOOP FOREVER" / "NEVER STOP" means *do not pause to ask the human
  whether to continue* — it does NOT override maxIterations/timeout.
- Stagnation rule per autoresearch SKILL §4.10 (with macro plateau separation per §8).

## 3. Run logs (per iteration per phase, never overwritten)

- Iteration N writes `run-<N>.log` (e.g. `run-7.log`), never a shared
  `run.log`. Baseline is `run-0.log`.
- Within one iteration, each execution phase gets its OWN file —
  overwriting a log mid-iteration destroys the evidence chain:
  - `run-<N>.log` — the primary run;
  - `run-<N>-confirm<K>.log` — min_delta confirmation re-runs, K = 1, 2 (§6); each re-run keeps its own file;
  - `run-<N>-fix<K>.log` — crash-fix attempts, K = 1, 2, 3 (§4).
- Redirect everything: `cmd > run-<N>...log 2>&1`; tee/direct output is
  forbidden (context explosion).
- Extract metrics with `grep "^<metric_name>:" run-<N>.log`; on empty
  grep, classify before judging (§4): exit nonzero / stack trace →
  crash; exit zero + valid metric in another output format →
  metric-error (fix the extraction pattern — the metric protocol is
  unchanged); exit zero + no metric → crash investigation.
- results.tsv `iteration` must match the log file number so results
  and logs can be reconciled.

## 4. Failure classification & crash protocol (r29 A6)

1. Classify first — process exit code, parse status, and scientific
   score are three separate things: nonzero exit / stack trace =
   **crash**; zero exit + valid metric in an unexpected output format =
   **metric-error** (adapter mismatch — fix the extraction pattern, the
   eval protocol is unchanged); zero exit + no metric = crash
   investigation. Empty grep alone proves nothing.
2. Easy fix (typo, missing import) → fix and re-run, same iteration.
3. Runtime errors: at most 3 automatic fix attempts **for this
   iteration** — the cap bounds the current batch, it is NOT evidence
   the method is invalid and never cuts off a later informed fix or
   explicit handoff.
4. Still broken → `git revert HEAD --no-edit`, record `crash` in
   results.tsv (**metric column empty**), move on. **Crashes always end
   in a revert** so the branch stays runnable. Never write a crash's
   placeholder 0 as a score.
5. `timeout` (run exceeded the kill threshold) = execution abort:
   record `timeout`, revert the commit, note in the description whether
   the comparison basis makes the run non-comparable (equal-wall-clock
   protocol) — it never refutes the method by itself.

## 5. Guard (optional but recommended)

- If a Guard command is configured, it runs every iteration.
- Guard fail → revert regardless of metric (protect the runnable
  branch); record `guard-fail` in the description and `fail` in the
  `guard` column. **Guard failures are engineering events, not
  scientific evidence** (r29 A6): locate the cause first, stage the
  candidate, continue via an informed fix or explicit handoff. Retry
  limits bound the current iteration only and never escalate to a
  direction switch.

## 6. min_delta (confirmation trigger — r29 A7)

- Improvements smaller than `min_delta` trigger a **confirmation run**:
  re-run 1–2 times, take the median. min_delta is an additional-
  measurement trigger, NOT a minimum-effect keep bar: if the median
  still favors the change, keep it and record the confirmation status —
  a repeated same-direction small positive is never erased for being
  below an arbitrary absolute number.
- Only a task/user-**declared** minimum practical effect may gate keeps
  (declared in program.md / package contract); never invent one, and
  never reuse another task's threshold across different metric scales.
- Champion ordering always uses the consistent aggregate basis;
  confirmation status is recorded separately, not mixed into the score.
- Non-numeric / unextractable metric → record `metric-error`, classify
  per §4.1, and revert only if the commit is actually broken (a
  parse-pattern fix plus re-extraction is preferred over discarding a
  valid run).

## 7. Revert rule (keep history, protect user work)

- Discards use `git revert HEAD --no-edit`, never `reset`.
- The revert chain must only ever touch experiment commits. User
  work-in-progress is snapshotted as its own commit BEFORE iteration 0
  (see autoresearch Step 0), so reverting can never swallow it.

## 8. Intervention Tiers, Noise-Band Separation & Incubation Protection (v2.3)

- **干预层级（T0–T3）**：在 `autoresearch.jsonl` / `logs/experiments.jsonl` 显式记录 `intervention_tier`（`T0` 复现/倍步、`T1` 参数/权重/正则小调、`T2` 新机制/新数据或推理结构、`T3` 架构或范式重构）与 `family_id`；`results.tsv` 第 8 列 `move` 支持 `incubate` 与 `leap`。
- **小正信号保留 ≠ 重置平台期**：按 §6 保留噪声带内的同向小正信号（r29 A7），但其 **不重置** `T0/T1` 增量平台期计数；连续 `T0`/`T1` 未突破噪声带（或未见置信区间排除 0 的跃升）达到策略阈值时，必须触发错例根因分析 + `escalate`/`leap`，而不是无休止拧参数。
- **新机制成长期保护（Incubation）**：标记 `move=incubate` / `leap` 的新 `T2`/`T3` 机制前 2–3 轮享有保护期（可在独立探索分支/槽位演进）：首版粗糙实现若低于精调老 champion 但工程可运行且诊断信号朝目标改善，不得在首次跑分即永久淘汰整个方法家族。
- **自定义 Collator / Trainer 等价微核验**：引入自定义 data collator、自定义损失或梯度累积封装时，启动 GPU 正式训练前必须在 1 个真实样本 batch 与目标 `grad_accum` 单微步上核验：（a）所有基线模态张量（如 `pixel_values`、`image_grid_thw`、mask/labels）完整到达 `model(**inputs)`；（b）梯度/loss 缩放与基线 `Trainer.training_step` 及 $\lambda=0$ 退化行为一致（无双重 `/ grad_accum` 缩放）。若追溯发现历史实验违反本条，须在 `logs/experiments.jsonl`、`research-state.yaml` 与 `findings.md` 同步标记 `IMPLEMENTATION_INVALID` + `retracted: true`。
- **禁止配置重跑浪费**：提交昂贵实验前先跑 `research-strategy.py check-dup`，确认与历史有效配置不重复后再启动。
