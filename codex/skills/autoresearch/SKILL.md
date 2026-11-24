---
name: autoresearch
description: Autonomous experiment loop with evidence-driven move selection — scales from parameter tuning (T0/T1) to mechanism & architecture leaps (T2/T3) with incubation protection, duplicate-config guard, git ledger, and trend-driven escalation. Use when the user asks to optimize a research metric, run an experiment loop, tune hyperparameters, explore architectural changes, or improve model performance iteratively.
argument-hint: <idea>
---

# Autoresearch

When a candidate borrows a neighboring method, read its actual source operation,
target-condition adaptation and direct parent comparison from the existing handoff;
do not rebuild a weaker baseline merely because the proposal has a new title.
The optional `../shared-references/scripts/construction_context.py --methods <notes>`
argument can carry selected mechanism notes with the candidate. Reuse or adapt the
actual component and record the concrete change, without a new literature/review gate.

自主实验循环。单策略表覆盖多种尺度：**T0/T1 参数与配方探索**（核对/重排/调参）与 **T2/T3 机制及架构跃迁**（新目标、新数据/推理机制、结构重构），并受错例诊断与成长期保护驱动，防止陷在同一方法家族里做“小农式”微雕。

分工：本 skill 负责**运行**循环。两种入口：
- **完整任务包**（锁定评估 + 开放文件 + program.md，可交付的自驱包）→ 缺失时
  用 `experiment-forge` 锻造；
- **最小就地契约**（主控按"裸库直接迭代"路由到本 skill 时）→ **不造包**：
  就地生成/接受最小运行契约即可开跑——评测命令 + 指标名/单位/方向 +
  循环边界（maxIterations/timeout）写进 `autoresearch.md` 即算契约成立，
  不强制 program.md 与开放文件结构。

Session files: `autoresearch.md`, `autoresearch.sh`, `autoresearch.jsonl`, `results.tsv`, `run-<N>.log`.

**继承方法而非只继承标题**：收到候选交接时读取其源方法必要条件、具体代码改动、
父 commit 与有范围的已有结果（`../shared-references/grounded-ideation.md` 的实现交接）。
开改前核对当前代码确实包含所选父方法；不静默从旧基线重做。结果返回外循环时
带实现 commit/记录引用，区分运行错误、开发收益与确认结论，并给出具体下一版改动
或暂存理由。按共享接口的“历史事实与新设计”“研究记忆”处理报告与更正：实际
消费操作不等于文件保存内容，更正同步替换当前相关陈述而非仅追加勘误。
保留有效父版本和原测量，不要求所有场景先通过，也不把预测写成结果。

**契约单一来源**：results.tsv 表头、循环边界、crash/revert、Guard、min_delta、日志命名的权威定义在 `experiment-forge/references/contract.md`。本文件与契约冲突时以契约为准。

## 两种运行模式

| | 交互式（默认） | 无人值守（`claude -p` / program.md 驱动） |
|---|---|---|
| 停止建议 | 只是建议，用户决定 | 不停止；停滞 → 强制"升级 / 机制跃迁" |
| 硬停止 | maxIterations / 用户打断 | maxIterations / timeout |
| 问用户 | 环境选择、计划确认、停滞建议 | 不问（NEVER STOP） |

**被 research-orchestrator 调用时**：视为无人值守 —— Step 1 的续跑/新开询问与 Step 2/3 的交互闸门（AskUserQuestion 环境选择、计划确认）**全部跳过**：orchestrator 的 BOOTSTRAP 双闸门 + state 文件即上游授权，scope/预算/环境/恢复意图以 research-state.yaml 为准。环境沿用调用方已确认的选择或任务包 program.md 的约定；若没有任何已确认配置且无法交互，报 `blocked` 而不是擅自假设。orchestrator 的 Gate 已经做过确认，这里不再二次确认。交接携带 `stage / plan_version / 活跃约束 ID / eval contract 命名空间`（program.md 头部 envelope 区）：本循环服从该 envelope，不自建更苛刻的门；plan_version 变化意味着新包版本，本循环不沿用旧版本身份。

## Step 0: 前置检查

- 在 git 仓库内运行；若 `git config user.email` 为空，`git commit` 会失败 —— 先补 repo-local 身份（`git config user.name/email`，询问用户或用已有仓库的惯用身份），再进循环。
- 工作树不干净 / detached HEAD → **不再"开专支即可"**（旧规则会让实验 revert 吞掉用户未提交修改）：
  1. 记录基线 SHA：`git rev-parse HEAD`。
  2. 把用户未提交改动固化为独立 commit：`chore: wip snapshot before autoresearch (<tag>)`。实验 revert 链从此只碰 `experiment:` commit，用户工作永远在链外。
  3. **三份内容快照，各有用途、时机不同**：
     - **user-wip**：iteration 0 之前，用户原始 WIP 状态的 sha256 清单，随 wip snapshot commit 提交（如 `.autoresearch-user-wip.sha256`）。**永不作为 revert 目标**。
     - **iter-start**：每轮 modify 之前，对 scope 内文件做 sha256 清单（**不提交**）。它是本轮 discard 的校验对象。
     - **last-good**：最近一次 keep 之后的状态（初始 = user-wip）。多轮 keep 后的 discard 若只 revert 一轮不够，回到 last-good 必可运行。
     清单绝不能留到实验 commit 里。收尾验证用 `git show <snapshot-sha>:.autoresearch-user-wip.sha256` 对比，不只看分支名。
  4. detached HEAD → 先建工作分支再进循环。
  无人值守模式同样执行，不跳过。

## Step 1: Gather

如果 `autoresearch.md` 和 `autoresearch.jsonl` 已存在，问用户续跑还是新开。`CHANGELOG.md` 存在则先读最近条目。

否则收集（一次问完）：
- 优化目标、benchmark 命令、指标名/单位/方向
- 可改文件范围（scope）
- **改动尺度（scale）**：三选一 ——
  - `params`：只调参数/配置（仅当用户显式限制架构时使用）
  - `arch`：专注机制/架构/数据表示级 `T2`/`T3` 改动，参数只作配套
  - `auto`（默认）：全策略可用，优先在验证饱和后及时从 `T0`/`T1` 跃迁到 `T2`/`T3`
- **Guard 命令**（可选但推荐）：每轮必须通过的安全检查（如冒烟测试）
- **min_delta**（可选）：小于此幅度的改进触发确认跑——它是**追加测量触发器，不是 keep 门槛**（r29 A7）：复测后同向小正信号保留并记录确认状态；只有任务/用户显式声明"最小实用效应"时才按声明决定保留；且**噪声带内的微小改善不重置 `T0`/`T1` 宏平台期计数**
- maxIterations（默认 20；无人值守模式建议 50+ 或 unlimited）

未明确指定 scale 时默认 `auto` 并在确认计划时向用户说明。

## Step 2: Environment

用 `AskUserQuestion` 让用户选：Local / 新 git 分支 / venv / Docker / Modal / RunPod（详见 `docker`、`modal-compute`、`runpod-compute` skills）。没有明确答案不继续。

## Step 3: Confirm

展示完整计划（目标、命令、范围、环境、Guard、min_delta、迭代数、模式），用户确认后才启动。

## Step 4: 循环

初始化：建专支 `autoresearch/<tag>`，创建 session files，**先跑 baseline（不改任何代码）并记录为 iteration 0**。

每一轮：

### 4.1 Review（读历史与策略账，别凭记忆）
- 读 `results.tsv` 最近 10-20 行 + `git log --oneline -20`
- 若运行目录下有 `research-strategy.py`（或主控注入了 `RESEARCH STRATEGY LEDGER`），先确认：当前家族是否已 `SATURATED` / `CLOSED`、是否存在处于 `INCUBATING` 保护期的分支、`incremental_streak` 是否逼近平台阈值，并避免与历史 config 重复
- 上轮是 keep → `git diff HEAD~1` 看具体改动
- 明确：什么worked、什么failed、哪个家族已经饱和、下一步该继续还是跃迁

### 4.2 选步（策略表，证据与错例驱动）

**新机制与错例分析的明确路由**：用户要求“想想有什么值得做的/提出下一种方法”，或进入 `escalate` / `incubate` / `leap`（包括连续 `T0`/`T1` 平台触发 `OUTER_MACRO_REVIEW`）时：先抽取当前最强模型的 **20–50 个失败案例归纳 Failure Taxonomy**（必要时运行 `research-strategy.py record-failure-taxonomy`），再读取 [macro-strategy](../research-orchestrator/references/macro-strategy.md)、[brainstorming-research-ideas](../brainstorming-research-ideas/SKILL.md) 与 [neighbor-to-method](../shared-references/neighbor-to-method.md)，生成**正交的 `T2`/`T3` 机制或架构方案**。已有明确 `T2`/`T3` 改法时直接实施。scale=params 仍尊重用户所选范围：范围内构思配方，需要结构性改动则交回外循环处理范围调整。

| 选步 | 触发 | 层级 | 动作 |
|---|---|---|---|
| 跟进 `follow` | 上轮有显著/超噪声带改进 | T1/T2 | 同杠杆同方向深化或补配对验证 |
| 反转 `reverse` | 上轮变差 | T1 | 同杠杆反方向 |
| 换杆 `switch` | 上轮无变化 | T1/T2 | 换模块/换特征/换约束（不得在同一饱和小参数内微调打转） |
| 孵化 `incubate` | 新启用 `T2`/`T3` 家族前 2–3 轮 | T2/T3 | **成长期保护**：允许粗糙初版暂低于老 champion，重在诊断改善与打通链路，不因首跑输老精调基线立刻 revert 判死 |
| 跃迁 `leap` | 主线陷入 `T0`/`T1` 平台（连续 ≥4-5 轮未破噪声带）或 `macro_review` 触发 | T2/T3 | 先做错例 Failure Taxonomy，直接做机制/数据构造/训练或推理范式重构 |
| 升级 `escalate` | 连续多轮无真实突破或发现当前表示/目标上限 | T2/T3 | 合并历次 near-miss / 机制替换 / 读文献切新方向 |
| 简化 `simplify` | 指标持平但代码更少更稳 | T1 | 保留，记 keep（简洁性胜利） |

这是策略指导不是僵化状态机 —— 每轮在 jsonl 里同时记 `move`、`intervention_tier`（`T0`–`T3`）、`family_id` 与理由。

**尺度约束（scale）对选步表的裁剪**：
- `params`：**禁用 `leap`/架构 escalate**。plateau 时换未试过的参数区间或向用户报告申请解除限制。
- `arch`：**跳过纯参数微调**，重点使用 `incubate`/`leap`/`switch` 在机制与结构层面推进；参数仅作新架构配套。
- `auto`：全表可用，由噪声带突破与家族饱和度证据驱动，禁止同一家族 `SATURATED` 后继续滞留 `T1`。

### 4.3 Modify + Commit + 去重拦截
- 启动耗时运行前，若存在 `research-strategy.py` 且有配置对象，先执行 `research-strategy.py check-dup --root . --family <family> --config-json '<json>'`，拦截同配置重跑（防止类似重复跑同一 seed/recipe 浪费 GPU）。
- 原子改动（一个清晰逻辑单元），只在 scope 内
- commit message 前缀 `experiment: {描述}`，记录 SHA

**方法改动的就地检查（按改动选择，不另起审查）**：修改权重、掩码、
选择器、指标方向或调度语义时，用真实函数做能区分错误的廉价检查；已有测试或
本次实际实验能覆盖时直接复用，不重复造夹具。**引入自定义 collator、自定义 `compute_loss`
或 `Trainer.training_step` 封装时，提交耗时训练前必须加做两项微核验**：（1）实测 1 个
真实训练 batch（CPU/单步即可），对照 SFT 基线确认多模态/关键输入张量（如视觉模型的
`pixel_values`、`image_grid_thw`、attention mask 与 labels）悉数送达 `model(**inputs)`
而未被 collator 静默丢弃；（2）在目标 `gradient_accumulation_steps` 下实测 1 微步并核对
$\lambda=0$ 退化等价性，确认与基准 `Trainer.training_step` 梯度尺度一致（无额外 `/ grad_accum`
双除缩水）。双输入高低值反例适合方向性权重/选择器操作，不是每种代码改动的必做项。
无关路径不重新核验，普通配置/文字改动不自动触发全量回归。若微核验失败，修复对应实现并将
历史误伤试次同步标记 `IMPLEMENTATION_INVALID` + `retracted: true`（同步 `logs/experiments.jsonl`、
`research-state.yaml` 与 `findings.md`），不判方法家族失败；修复后只复查受影响行为。
评测路径可信且改动可运行，就进入本轮真实实验；不为凑齐检查形式推迟执行。

### 4.4 Run（防上下文爆炸）
```bash
<benchmark 命令> > run-<N>.log 2>&1   # 每迭代独立日志，禁止覆盖 / tee / 直接输出
grep "^<metric_name>:" run-<N>.log    # 只提取指标行
```
grep 为空 ≠ 直接判崩溃——先分类（r29 A6：进程退出码 / 解析状态 / 科学成绩三者分开）：退出码非零或有栈 = **crash**（`tail -n 50 run-<N>.log` 看栈）；退出码为零但日志里有合法指标、只是格式与 grep 模式不符（如 JSON `{"accuracy":0.91}` vs `^accuracy:`）= **metric-error**（适配失配，修提取模式后重新提取，不改评判协议）；退出码为零且全无指标 = 按 crash 排查。results.tsv 的 iteration 列与日志文件号一一对应，可对账。
**同迭代内的每次执行各用各的文件**：确认跑 `run-<N>-confirm<K>.log`（K=1,2，各自留档），第 K 次修复跑 `run-<N>-fix<K>.log`。

### 4.5 Verify（噪声防护与平台期分离）
- 改进幅度 < `min_delta`（或位于已知种子 `noise_band` 内） → **确认跑**：重跑 1–2 次取中位数确认方向一致性（日志写 `run-<N>-confirm.log`）；中位数仍同向改进 → **keep 并记录确认状态**（r29 A7：不抹除同向小正信号），但**明确标记尚未构成突破（non-breakthrough），不重置宏平台期计数**；只有突破噪声带/CI 排零的进展才重置增量计数。
- 指标非数字/提取失败 → 记 `metric-error`，先按 4.4 分类：格式适配失配则修提取模式重新提取（评判协议不变），commit 确实坏掉才 revert。

### 4.6 Guard（如配置）
Guard 失败 → 无论指标如何都 revert（保护可运行分支），记 `guard-fail`，先定位工程原因、暂存候选。修复重试上限只约束当前迭代——Guard 失败是工程事件，**不构成方向否证**（r29 A6）。

### 4.7 Decide（分态判定，与 contract v2.3 status 枚举一致）
- **keep**：指标按既定比较口径改进（过确认跑的见 4.5）且 Guard 过；或 `move=incubate/leap` 处于成长期保护期且需保留以继续下一版迭代 → 分支前进（仅当真正超越 `best_value` 时覆盖 `best_value`）。
- **discard**：指标未改进且不在 `incubate` 保护期内 → `git revert HEAD --no-edit`（**不用 reset —— 失败保留在历史里供学习**）；revert 后按本轮 iter-start 快照校验文件内容确已还原。若处于 `INCUBATING` 新家族首两轮，优先做基于错例的修复/深化（`incubate`）而非直接淘汰整个方向。
- **crash**：运行崩溃 → 语法/导包错误立即免费修复重跑（日志 `run-<N>-fix<K>.log`）；运行时错误最多自动修 3 次（只限当前迭代，不据此否证方法）；修不好 revert 记 crash，metric 列留空（不写占位 0）。
- **timeout**：超过 kill 阈值的执行中断 → revert 记 `timeout`，**不是科学否证**（r29 A4）。
- **metric-error**：正常退出但指标解析失败 → 按 4.4 分类处置，不与 crash 混记。
- **no-op**：本轮无有效改动。
- **blocked**：外部依赖失效（数据集/服务/权限）。

### 4.8 Log
`results.tsv`（TAB 分隔）：`iteration timestamp commit metric delta guard status move description`
`autoresearch.jsonl`（及 `logs/experiments.jsonl`，若接主控）：完整 JSON 行（`exp_id`、`family_id`、`intervention_tier` `T0`–`T3`、`config`、`hypothesis`、`metric`、`paired_ci`/`significant`、`files changed`、`wall time`、`evidence pointer`）。

### 4.9 Checkpoint（每 5 轮）
趋势分析：各 `T0`–`T3` 层级占比、饱和家族列表、是否连续在噪声带内波动、最大单次飞跃、错例根因是否缓解。
输出建议：**continue / leap-to-T2-T3 / change-strategy / stop**（交互式=建议；无人值守=平台期强制 `leap`/`escalate`）。

### 4.10 停滞与跃迁规则
触发条件（满足任一）：
- 传统停滞：连续 10 轮无任何 keep；
- **宏平台期（Incrementalism Plateau）**：连续 5 轮已完成实验均为未突破噪声带/未排零的 `T0`/`T1` 改动（即使有 `min_delta` 内微小波动的 keep）。

处置：
- 交互式 → 列出饱和家族与 `T0`/`T1` 空转统计，向用户展示 Failure Taxonomy 与 `T2`/`T3` 跃迁方案建议；
- 无人值守 → **禁止继续加一轮同家族 `T1` 微调**，必须先做错例 Failure Taxonomy，再按 4.2 路由执行正交的 `leap`/`incubate` 新机制（`T2`/`T3`），并记入 jsonl。

停滞升级选步**只能在当前 envelope（约束清单、预算、eval contract）内**进行。
升级跃迁后仍停滞、或判断需要改动 envelope 本身（换评估命名空间、放宽局部
预算、修订代理自拟的配方默认）→ **交回外循环**：在 results/jsonl 与交接状态
中写明停滞事实、已尝试的层级与升级、建议的修订点，结束本局部循环，由 orchestrator
走约束复审产生新版本。局部循环不得自行扩大 envelope 或无限续跑；局部 discard
只否当前候选，不否整个方向（裁决标签见 orchestrator 裁决语义分层）。

## Step 5: 收尾

循环结束（达成/停滞/上限/打断）时输出：
- 总迭代数、`T0`–`T3` 分布、keep/discard/crash 分布、baseline → 最终指标、提升 %
- Top 3 最有效改动（区分实质突破 vs 噪声内微调） + 最大失败教训 / 饱和家族汇总
- 最优 commit 指针（`git log` 定位）
- CHANGELOG 追加条目；关键结论按全局约定写 provenance

## Subcommands

- `autoresearch <text>` — 启动/续跑
- `autoresearch off` — 停止，保留数据
- `autoresearch clear` — 清除状态重来
