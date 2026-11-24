# Macro Strategy & Anti-Incrementalism Protocol (Lemvo v2.2)

当实验陷入局部微调平台期时，继续依靠“最小改动 + 单步接力”会在同一方法家族里反复消耗预算。本协议把**干预层级、可比指标锚点、噪声带隔离、正交探索保护、结构化家族闭环与错例第一性原理诊断**接入主控工作单元（`templates/research-strategy.py` 与 `templates/work-unit.py`）。

## 1. 干预层级（Intervention Tiers: `T0`–`T3`）

每次实验与方案建议必须在台账（`logs/experiments.jsonl` 或 `results.tsv`）显式标注层级：

| 层级 | 定义 | 示例 | 对 `incremental_streak` 的影响 |
|---|---|---|---|
| **T0** | 复现、seed 复测、纯算力/步数加倍（`2x`/`3x`）、工程修 bug / baseline | `seedvar`, `budget2x`, `backbone512` | 计入非突破增量计数（除非首轮 baseline 或有效突破） |
| **T1** | 超参、loss 系数、权重重排、课程顺序、checkpoint soup / 投票、小 trick | `lambda=0.1`, `order-last`, `soup3`, `margin` | 若未突破 `noise_band` / 有利显著性，`incremental_streak + 1` |
| **T2** | 新机制、新目标函数结构、新反事实/合成数据构造、新推理期搜索或校验算法 | `new_mechanism`, `verifier`, `synthetic_cf`, `incubate` | 立即清零 `incremental_streak`；进入 `INCUBATING` 保护期 |
| **T3** | 架构级重构、表征/介入空间切换、多阶段问题重建模 | `arch`, `representation`, `steer_vec`, `leap` | 立即清零 `incremental_streak`；进入 `INCUBATING` 保护期 |

## 2. 噪声带 vs 突破判定（方向敏感 + 保留 `r29 A7`）

- **保留小正信号记录（r29 A7）**：处于随机种子噪声带（`noise_band`）内的同向微小正波动，或未校准 `noise_band` 时的微弱正偏移，仍可按科学诚实性记为开发正信号/keep；**禁止抹除微弱证据**。
- **突破严格门槛（同指标可比作用域内）**：所有锚点按 `(eval_contract, split, metric_name, direction)` 作用域独立比较（辅助/延迟指标不覆盖主指标最佳值与优化方向）；判定 `breakthrough` 必须同时满足：
  1. **有利方向（`direction`）**：指标必须严格优于当前同作用域锚点 `best_so_far`（`maximize` 时 `delta > 0`，`minimize` 时 `delta < 0`），且主终点不得伴随 `rejected`/`null`/`discard`/`duplicate` 等负向结论（次要终点 `secondary endpoint NULL` 不否决主终点正信号）；**退步结果或已撤回记录（`retracted: true`）绝不判为突破**。
  2. **校准幅度或统计显著**：存在匹配基准或当前锚点的同向排除 `0` 配对置信区间（`maximize` 时 `ci_low > 0`，`minimize` 时 `ci_high < 0`）、明确同向 `significant=true`，或严格超过已校准的 `noise_band > 0`；当 `noise_band` 未校准且无置信区间/显著性依据时，不将任意微涨误判为突破。
- 目的：**既不错把退步或次要指标当作突破，也不靠随机噪声或未收尾 `T2` 家族无限续命**；当 `T2`/`T3` 家族超出 `incubation_budget` 仍无突破时，后续失败试次同样推进 `incremental_streak` 与 `SATURATED` 判定。

## 3. 平台期宏观评审策略（默认 `advisory` + 可选 `gate` + 完整冷却周期）

`work-unit.py prepare` 在每次派发前调用 `research-strategy.py analyze`：

- **冷却周期重算**：只统计**自上次已结算评审（`last_macro_review_exp_count`）之后新增**的低层级无突破实验数（`streak_since_last_review`）；完成评审后，不会因仅多完成 1 条确认实验就立刻再次触发，而需重新累计满 `macro_review_threshold`（默认 `5`）。
- **默认 `advisory` 模式（不劫持既定派工）**：当 `streak_since_last_review >= macro_review_threshold` 且非用户消息 / 等待 / 阻塞作业时，保持原定的复现、随机种子复测或有依据的研究步骤正常推进（不覆盖 `kind` 或 `objective`），同时向工作单元注入 `PLATEAU ADVISORY` 与 `RESEARCH STRATEGY LEDGER`，并将探索 `T2`/`T3` 与错例诊断列为清晰选项；该轮 `rc=0` 结算后通过 `mark_macro_review` 进入下一冷却周期。
- **可选强制 `gate` 模式**：若项目显式在 `.lemvo-strategy-policy.json` 或 `research-state.yaml` `strategy:` 中配置 `"macro_review_mode": "gate"`（或环境变量 `LEMVO_MACRO_REVIEW_MODE=gate`），则暂存战术 `next_task` 为 `deferred_tactical_next_task`，并以 `fresh` 会话派发 `kind="macro_review"`。可通过 `macro_review_threshold: 0` 关闭平台期提醒。

## 4. 错例驱动的第一性原理诊断（Error-Driven Design）

进入平台期分析或 `OUTER_MACRO_REVIEW` 后，**禁止直接拍脑袋提参数/系数小改动**，建议按序执行：

1. 抽取当前最强实现的 **20–50 个具体失败样本**（或对照差错最大簇）；
2. 归纳失败根因分布（如信息缺失、表征坍缩、训练-推理目标错配、监督冲突、边界脆弱性等），落盘记录：
   ```bash
   python3 <templates>/research-strategy.py record-failure-taxonomy \
     --root . --turn-id <turn_id> \
     --summary "主失败模式集中在..." \
     --modes-json '[{"mode":"representation_bleed","count":18,"fraction":0.45}]'
   ```
3. 针对占比最大的根本瓶颈，设计正交于饱和家族的 **`T2` 或 `T3` 候选方案**；若当前方向所有根本路径均已被证据封顶，再正式形成家族级退役结论。

## 5. 双槽组合探索、成长期保护与家族级关闭边界

- **单次配置否决与自然语言猜测绝不关闭整个家族**：某次实验失败或 `rejected`（例如 `"this variant rejected; family remains open"`）、或条件式阻塞文本（例如 `"Before declaring the family closed, repair the parser"`）**不会**关闭家族或撤除剩余孵化保护；只有**显式结构化决定**（`portfolio-set --status closed`、`log-experiment --family-status closed`、`close_family: true`、`family_decision: closed` 或终态 `[CLOSE_FAMILY]` 标记）才将家族标为 `CLOSED`，且可通过 `--status active` / `reopen_family: true` 显式重开。
- 维护正交双槽 `.lemvo-portfolio.json`：
  - **`exploit` 槽**：守住当前已验证最稳妥的主线基线（不盲目丢失已验证成果）；
  - **`explore` 槽**：开启**机制正交**的 `T2`/`T3` 新家族；
  ```bash
  python3 <templates>/research-strategy.py portfolio-set \
    --root . --slot explore --family latent_intervention --tier T3 \
    --incubation-budget 3 --hypothesis "以表征解耦替代输出端表面正则"
  ```
- **成长期保护（Incubation Protection）**：新开启的 `T2`/`T3` 家族前 `incubation_budget`（默认 `3`）轮标记为 `INCUBATING`。保护期内允许粗糙初版指标低于精调老 champion——评估重点在于**工程打通 + 诊断信号斜率 + 失败根因是否缓解**，不因单次变体失败而提前扼杀整个方向。

## 6. 提交前配置去重检查

在提交耗时的 GPU/训练实验前，务必运行标准化配置指纹检查，杜绝同配置重跑：

```bash
python3 <templates>/research-strategy.py check-dup \
  --root . --family <family_id> --config-json '{"recipe":"...","seed":20261117}'
# 返回 0 = 新配置；返回 2 = 与已有实验重复（打印 matched_exp_id）

python3 <templates>/research-strategy.py log-experiment \
  --root . --exp-id <exp_id> --family <family_id> --tier T2 \
  --config-json '{"recipe":"..."}' --metric 0.65 --outcome KEEP --significant true
```
