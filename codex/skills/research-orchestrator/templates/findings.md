# Research Findings

## Research Question

<!-- What are we trying to discover? One clear sentence. -->

## Current Understanding

<!-- Updated after each outer loop cycle. What do we know so far?
     What patterns explain our results? What's the mechanism?
     This section should read like the core argument of a paper. -->

## Key Results

<!-- Significant experimental findings. Include metrics, comparisons, and
     brief interpretation. Link to experiment directories for full details. -->

## Patterns and Insights

<!-- What emerges across multiple experiments? What types of changes
     consistently work or fail? Why? -->

## Lessons and Constraints

<!-- Specific actionable learnings that should guide future experiments.
     Things you tried that didn't work and WHY, so you don't repeat them.
     Constraints you discovered about the problem space.

     每条失败/排除记录带裁决标签与 scope（IMPLEMENTATION_INVALID /
     MEASUREMENT_INCONCLUSIVE / EXPLORATORY_POSITIVE / EXPLORATORY_NEGATIVE /
     CONFIRMATION_NOT_ESTABLISHED / NOVELTY_OVERLAP / UNTESTED），scope 限定到
     具体配方/预算/指标/条件——整族禁令禁止。代理自拟的限制写来源与
     reconsider 条件（详见 research-orchestrator/references/constraint-governance.md）。

     Examples:
     - Weight decay > 0.1 causes training instability at 125M param scale
     - SwiGLU and RoPE improvements stack because they're orthogonal (FFN vs positional)
     - Baseline only reproduces published numbers with batch_size=64, not 32
     - Sleep phases before memorization completion hurt — model needs memories to consolidate -->

## Plan Changes（计划修订记录）

<!-- 每次限制复审/计划修订一条：触发原因 / 调整哪条约束(id) / 权限依据 /
     旧证据保留范围 / 下一具体实验 / 成本与止损 / 是否需用户批准。
     只记实际变更；没有变更不要为刷新"进展"而复审。 -->

## Open Questions

<!-- What remains unanswered? What would strengthen or challenge
     our current understanding? -->

## Optimization Trajectory

<!-- Summary of inner loop progress. How has the metric evolved?
     Note inflection points and what caused them. -->
