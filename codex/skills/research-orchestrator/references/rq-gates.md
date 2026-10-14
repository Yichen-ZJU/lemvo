# Gate 0/1 协议：带证据的引导 + FINER 问题审判

> 改编自 ARS (academic-research-skills, Cheng-I Wu, CC-BY-NC 4.0) 的 research_question_agent 与 devils_advocate_agent Checkpoint 1，并按用户工作流改造：AI 先查证再提问，用户只做选择题。

## Gate 0 — 带证据的引导（输入模糊时触发）

触发条件：用户输入没有明确研究问题（"我对 X 感兴趣但不确定做啥"、"帮我想想方向"）。

```
1. AI 侦察（不问用户）：arxiv MCP + web_search 快速扫描
   —— 领域最近在做什么、什么已饱和、公开 gap 在哪
2. AI 生成 3-5 个候选研究问题，每个附证据（相关论文、缺口、风险）
3. AI 对每个候选做 FINER 打分（见下）
4. 给用户一张对比表：推荐项 + 理由 + 各候选的证据
   用户只需反应（"B 有意思"/"换 A"/"都不对劲"）
5. 最多 1-2 轮微调 → 收敛 → 进 Gate 1
   超过 3 轮不收敛 → 摆明分歧点，请用户直接拍板，不纠缠
```

原则：能查证的 AI 先查，能推理的 AI 先推，只把品味判断和拍板留给用户。

可选教学变体（用户明确说"用苏格拉底模式引导我"时启用）：纯提问五层引导（问题框架→方法反思→证据设计→批判自省→意义贡献），每层≥2轮，绝不直接给答案。完整协议见 `socratic-protocol.md`。

## Gate 1 — FINER 问题审判（每次 BOOTSTRAP 必过）

```
1. 主题分解 → 生成 3-5 个候选 RQ（描述/比较/因果/评估型混编）
2. FINER 打分（1-5 分/维，附一句理由）：
   | 维度 | 1 分 | 5 分 |
   | Feasible  | 现有方法/数据答不了 | 方法明确、数据可得 |
   | Interesting | 琐碎或已是共识 | 触及真正的谜题/矛盾 |
   | Novel | 纯重复前人 | 新视角/方法/证据 |
   | Ethical | 重大伦理问题 | 收益大于风险 |
   | Relevant | 无理论/实践意义 | 直接指导实践/理论 |
   判决线：平均 ≥ 3.0 且无一维 < 2；不过 → REVISE 重来
3. Scope 边界：IN SCOPE / OUT OF SCOPE / ASSUMPTIONS
4. 方法论蓝图：范式、方法选择、数据策略、分析框架、效度标准
   （ML 实验类课题可精简为：评估指标、baseline、数据、oracle）
5. 魔鬼代言人 Checkpoint：RQ 可回答吗？方法配得上问题吗？scope 过宽/过窄？
   最强反对意见是什么？→ PASS / REVISE（REVISE 附具体修改意见，回 step 1）
6. 产出 RQ Brief 落盘到**项目工作区** `docs/<slug>-rq-brief.md`（orchestrator 工作区为项目根布局；不写到全局 outputs/.plans/，避免两套路径约定分叉），下游消费：
   - scope 边界 → literature-review 的检索约束
   - 方法论蓝图 → experiment-forge 的任务包输入
```

用户确认点：RQ Brief 给用户看一眼（一段话总结），无重大异议即继续——不为每个细节打断用户。
