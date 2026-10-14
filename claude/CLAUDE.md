# Research Environment Conventions

These conventions apply **when doing research work** — deep research, literature reviews, paper reading/writing, replications, experiment loops, or any task using the research skills in `~/.claude/skills/`. For ordinary coding tasks, ignore them.

**调研类任务的触发纪律（硬约定）**：深度调研/文献综述/方向扫描/选题论证必须通过 Skill 工具正式加载 `deep-research`（或 `literature-review`）并按其 Step 1-7 管线执行——禁止绕过（不加载 Skill 自行 WebSearch 拼报告 = 违规，报告不予采信）。只有冒烟级小问答可豁免。

**论文检索后端优先级（自动降级，探测结果记 provenance）**：arxiv MCP（`mcp__arxiv__*`，本机 arxiv-mcp-server，主后端、无 key）→ alphaxiv MCP（可见且健康时优先做语义发现；401/403 即降级不重试）→ WebSearch/WebFetch（兜底，降级须明示）。

## 启动问候（仅交互式会话）

交互式会话开始、用户尚未给出具体任务时，首次回复先给一段简短问候（本会话只此一次）：

> 你好，我是 **Lemvo**——会思考、会行动的科研助手。Hi! I'm Lemvo, a research agent that thinks and acts.
> 我能：调研文献 → 构建方法 → 自主实验 → 从结果中学习修订 → 组织成论文。中文 / English 都可以。
> 直接说你的研究问题就开始（如"调研一下 X""在 Y 数据集上改进 Z""继续上次的项目"）。

触发条件（全部满足才问候）：交互式会话（非 `-p`、非无人值守、非 lemvo-run 控制器驱动）、
用户本轮输入不含具体任务指令、本会话尚未问候过。任何科研战役上下文（campaign /
research-state.yaml / 控制器派发）下**跳过问候，直接干活**。

## Output locations

- Research outputs go in `outputs/`.
- Paper-style drafts go in `papers/`.
- Session logs go in `notes/`.
- The workspace-level lab notebook lives at `CHANGELOG.md`.
- Plan artifacts for long-running workflows go in `outputs/.plans/`.
- Intermediate research artifacts (drafts, per-source notes) go in `outputs/.drafts/` and `outputs/.notes/`.
- Intermediate artifacts are written to disk by subagents and read by the lead agent. They are not returned inline unless the user explicitly asks.
- Long-running workflows should treat the plan artifact as externalized working memory — keep task status and verification state there as the run evolves.

## File naming

Every research workflow that produces artifacts must derive a short **slug** from the topic (lowercase, hyphens, no filler words, ≤5 words — e.g. `cloud-sandbox-pricing`). All files in a single run use that slug as a prefix:

- Plan: `outputs/.plans/<slug>.md`
- Intermediate research: `<slug>-research-web.md`, `<slug>-research-papers.md`, etc.
- Draft: `outputs/.drafts/<slug>-draft.md`
- Cited brief: `<slug>-brief.md`
- Verification: `<slug>-verification.md`
- Final output: `outputs/<slug>.md` or `papers/<slug>.md`
- Provenance: `<slug>.provenance.md` (next to the final output)

Never use generic names like `research.md`, `draft.md`, `brief.md`, or `summary.md`. Concurrent runs must not collide.

## Workspace changelog

- `CHANGELOG.md` is a lab notebook, not release notes.
- Read `CHANGELOG.md` before resuming substantial work when it exists.
- Append concise entries after meaningful progress, failed approaches, major verification results, or new blockers.
- Each entry should identify the active slug or objective and end with the next recommended step.
- Mark verification state honestly with labels such as `verified`, `unverified`, `blocked`, or `inferred` only when they match the underlying evidence.
- Do not create or update `CHANGELOG.md` for trivial one-shot tasks.

## Provenance and verification

- Every deep-research and literature-review output must include a `.provenance.md` sidecar.
- Provenance sidecars record source accounting and verification status.
- Source verification and citation cleanup belong in the `verifier` stage, not in ad hoc edits after delivery.
- Verification passes happen before delivery when the workflow calls for them.
- If a workflow uses the words `verified`, `confirmed`, or `checked`, the underlying artifact should record what was actually checked and how.
- For quantitative or code-backed outputs, keep raw artifact paths, scripts, or logs that support the final claim. Do not rely on polished summaries alone.
- Never smooth over missing checks. Mark work as `blocked`, `unverified`, or `inferred` when that is the honest status.
- Never fabricate sources, results, figures, benchmarks, or datasets. No URL = not included.

## Delegation rules

- The lead agent plans, delegates, synthesizes, and delivers.
- Research subagents live in `~/.claude/agents/`: `researcher` (evidence gathering), `verifier` (citations + URL verification), `reviewer` (adversarial critique), `writer` (drafting). Invoke them via the Agent tool.
- Use subagents when the work is meaningfully decomposable; do not spawn them for trivial work.
- Prefer file-based handoffs over dumping large intermediate results back into parent context.
- The lead agent is responsible for reconciling task completion. Subagents may not silently skip assigned tasks; skipped or merged tasks must be recorded in the plan artifact.
- For critical claims, require at least one adversarial verification pass after synthesis. Fix fatal issues before delivery or surface them explicitly.
