# Macro Strategy & Anti-Incrementalism Protocol (Lemvo v2)

当实验陷入局部微调平台期时，继续依靠“最小改动 + 单步接力”会在同一方法家族里反复消耗预算。本协议把**干预层级、噪声带隔离、正交探索保护与错例第一性原理诊断**接入主控工作单元（`templates/research-strategy.py` 与 `templates/work-unit.py`）。

## 1. 干预层级（Intervention Tiers: `T0`–`T3`）

每次实验与方案建议必须在台账（`logs/experiments.jsonl` 或 `results.tsv`）显式标注层级：

| 层级 | 定义 | 示例 | 对 `incremental_streak` 的影响 |
|---|---|---|---|
| **T0** | 复现、seed 复测、纯算力/步数加倍（`2x`/`3x`）、工程修 bug / baseline | `seedvar`, `budget2x`, `backbone512` | 计入非突破增量计数（除非首轮 baseline 或突破噪声带） |
| **T1** | 超参、loss 系数、权重重排、课程顺序、checkpoint soup / 投票、小 trick | `lambda=0.1`, `order-last`, `soup3`, `margin` | 若未突破 `noise_band` 或 CI 未排 0，`incremental_streak + 1` |
| **T2** | 新机制、新目标函数结构、新反事实/合成数据构造、新推理期搜索或校验算法 | `new_mechanism`, `verifier`, `synthetic_cf`, `incubate` | 立即清零 `incremental_streak`；进入 `INCUBATING` 保护期 |
| **T3** | 架构级重构、表征/介入空间切换、多阶段问题重建模 | `arch`, `representation`, `steer_vec`, `leap` | 立即清零 `incremental_streak`；进入 `INCUBATING` 保护期 |

## 2. 噪声带 vs 突破判定（保留 `r29 A7` 同时防局部锁死）

- **保留小正信号记录（r29 A7）**：处于随机种子噪声带（`noise_band`）内的同向微小正波动，仍可按科学诚实性记为开发正信号/keep；**禁止抹除微弱证据**。
- **不重置平台期计数**：只有**超越 `noise_band`**（若未设 `noise_band` 且存在配对区间，则需严格显著——置信区间排除 `0`）且非重复/非空判断的改进，才计为 `breakthrough` 并清零 `incremental_streak`。
- 目的：**不靠随机波动给饱和的方法家族无限续命**。

## 3. 硬性 `OUTER_MACRO_REVIEW` 门控

`work-unit.py prepare` 在每次派发前调用 `research-strategy.py analyze`：

- 当连续已完成实验达到 `macro_review_threshold`（默认 `5`）轮均为未突破噪声带的 `T0`/`T1` 改动，且上一轮未处于用户 steer / 等待作业收尾 / 在跑训练时，运行时硬门控触发：
  1. 将战术性 `next_task` 降级暂存为 `deferred_tactical_next_task`，改为派发 **`kind = "macro_review"`**；
  2. 在 `auto`/`fresh` 策略下强制 **开启 `fresh` 会话**（`reason = "macro-review-plateau"`），清除局部隧道视野；
  3. 注入 `RESEARCH STRATEGY LEDGER`（各家族饱和度、重复配置告警、组合槽位、最近错例分类）并加载 `macro-strategy.md` + `brainstorming-research-ideas` + `paradigm-shift-probe`。
- **一轮交接闭环**：该 `macro_review` 轮次 `rc=0` 结算后，`work-unit.py settle` 自动调用 `mark_macro_review` 记录当前完成数，避免死循环重触发，直到后续继续积累新 `T0/T1` 且无突破才再次武装。
- 可通过项目根 `.lemvo-strategy-policy.json`（如 `{"macro_review_threshold": 5, "noise_band": 0.02, "direction": "maximize"}`）或环境变量 `LEMVO_MACRO_REVIEW_THRESHOLD=0` 按需调整。

## 4. 错例驱动的第一性原理诊断（Error-Driven Design）

进入 `OUTER_MACRO_REVIEW` 后，**禁止直接拍脑袋提参数/系数小改动**，必须按序执行：

1. 抽取当前最强实现的 **20–50 个具体失败样本**（或对照差错最大簇）；
2. 归纳失败根因分布（如信息缺失、表征坍缩、训练-推理目标错配、监督冲突、边界脆弱性等），落盘记录：
   ```bash
   python3 <templates>/research-strategy.py record-failure-taxonomy \
     --root . --turn-id <turn_id> \
     --summary "主失败模式集中在..." \
     --modes-json '[{"mode":"representation_bleed","count":18,"fraction":0.45}]'
   ```
3. 针对占比最大的根本瓶颈，设计正交于饱和家族的 **`T2` 或 `T3` 候选方案**；若当前方向所有根本路径均已被证据封顶，正式形成结论并 `CONCLUDE` / `PIVOT`。

## 5. 双槽组合探索与成长期保护（Portfolio + Incubation）

- 维护正交双槽 `.lemvo-portfolio.json`：
  - **`exploit` 槽**：守住当前已验证最稳妥的主线基线（不盲目丢失已验证成果）；
  - **`explore` 槽**：强制运行**机制正交**的 `T2`/`T3` 新家族，禁用对饱和老家族的 `T1` 调参；
  ```bash
  python3 <templates>/research-strategy.py portfolio-set \
    --root . --slot explore --family latent_intervention --tier T3 \
    --incubation-budget 3 --hypothesis "以表征解耦替代输出端表面正则"
  ```
- **成长期保护（Incubation Protection）**：新开启的 `T2`/`T3` 家族前 `incubation_budget`（默认 `3`）轮标记为 `INCUBATING`。保护期内允许粗糙初版指标低于精调老 champion——评估重点在于**工程打通 + 诊断信号斜率 + 失败根因是否缓解**，不在第 1 次粗糙跑就因未打赢老 champion 而被 `DISCARD`/revert 杀掉。

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
