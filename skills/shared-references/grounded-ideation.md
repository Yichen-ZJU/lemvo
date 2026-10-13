# 文献驱动构思与建设接口（Grounded Ideation）

> 共享参考件，不是独立 skill（无入口、不计技能数）。六个主技能
> （research-orchestrator / deep-research / literature-review / brainstorming-
> research-ideas / creative-thinking-for-research / idea-evaluator）按触发
> 条件引用本文件对应章节，不复制长流程。证据正典 =
> `full-text-verification-policy.md`；权限与约束正典 =
> `research-orchestrator/references/constraint-governance.md`；
> 预算正典 = research-state.yaml 的 `budget`/`total`/`backend_ledger` 总账。

## 何时用（触发条件）

具备以下任一场景，走本接口，而不是硬想或硬审：

- 当前实现/候选被一个**钉得住的瓶颈**卡住（质量、延迟、隐私、成本等明确 desideratum 未满足）；
- 同域近邻已知，但不知道该**改哪个机制**才有效；
- 换方法族、换后端、方向复审前的换向（constraint-governance §4 的触发条件命中）。

**不需要构思时不要用**：已有明确方案 + 可跑代码 → 直接进
autoresearch/forge 实验，不强制先走整条构思链。已有方案不重开头脑风暴。

## 机制检索与阅读复用（gap → 同域/跨域机制检索（按需）→ 提案三段链）

借鉴 IdeaScientist（arXiv:2610.04074，独立撰写、只引链接）已消融验证的设计：

1. **gap finding**：先在本领域近邻工作里点名**共同方法学缺陷**——"当前方案为什么没解决这个 challenge，甚至放大它"。缺陷要点名到机制，不是"效果不够好"。
2. **机制检索（innovation，同域/跨域按需）**：带着"这个 challenge 的本质结构是什么"找解决过同构困难的机制。**同域已找到适合瓶颈的机制即可采用并结束检索**，不必为跨域而跨域；同域不够时再扩展到其他领域。量化依据：IdeaScientist 提案级消融中，结构化跨场景迁移判定比例（论文称 In-domain novelty）从 36.3% 增至 67.0%（增加 30.7 个百分点，非相对增长 30.7%，亦非一般科研成功率）；据此借鉴"按机制检索"的思路——**检索源可以跨域，选择标准永远是机制契合（结构保真 + 可用资产 + 实现成本）**，领域距离只是多样性因素。
3. **落成提案**：把机制适配写成可执行候选（见下节候选卡）。三段链的中间产物（gap 声明 / 机制直觉 / 候选）**可回溯、可修订**，不是许可门——来源不全、机制未证明、delta 尚不清楚，通常意味着补一处信息或限定结论，不意味着禁止小试。

**定向阅读复用**：优先读项目已有的 `literature/` 笔记、调研产物（
deep-research/literature-review 的 Reusable Assets）与 provenance，沿用其来源 ID，
不另建平行文献库。针对实际候选问未回答的问题：源论文的操作是什么、解决的
困难是什么、哪些输入/训练信息是关键、代价与效果实际如何（只有论文**实际报告**
的资源才可作配方成本）。承重声明仍走 full-text 级核验；同问题的已回答部分不
重复问，来源版本/关键条件变化时只复核受影响结论。这**不是**每次诊断都启动
researcher→verifier→reviewer 全链的理由——外循环补一个局部机制问题，用定向
阅读，核对影响决策的声明后继续建设。

## 候选交接格式（候选卡是交接工具，不是许可门）

默认存项目现有候选产物；没有则用 `outputs/.notes/<slug>-candidates.json`。
**只保留一份权威候选记录**，其他文件引用，不复制多份状态。字段按任务适用性
填写；只改已有实现的简短任务可在既有笔记写两三句交接，不必另造 JSON。

| 字段 | 内容 |
|---|---|
| candidate_id / version | 稳定身份 + 具体方案版本，关联现有 hypotheses.id（不新增第二套假说状态） |
| target_bottleneck | 本任务要改善什么；抽象困难是什么（gap 声明的落点） |
| source_refs | 论文/代码来源 + 版本 + 节页定位；原创假说允许为空 |
| transfer_map | 源域对象/关系/操作在目标任务的对应；哪些条件有差异 |
| assumptions | 已知条件 / 合理推测 / 待查或待测；不能全部自动升为 blocker |
| closest_neighbor_and_delta | 相对谁多做了什么；未知标 unknown，不编造"无人做过" |
| concrete_change | 文件/模块/损失/采样的具体改动，必要时最小伪代码 |
| predicted_effect | 待测预测及适用范围，不冒充已观察事实 |
| falsifiable_prediction | 一个可被实验证伪的具体预测：指向具体可观测变化（方向/相对大小）。**量级有依据就填，没有就标 `unknown`/`待探索估计`——不因缺量级阻塞有效直接小试**，也不为填数字而编。测量失效或无识别能力时记 inconclusive，不自动当候选被证伪。 |
| refutation_condition | 什么结果算推翻该候选（预设）。**"不得改"只限定同一候选版本、同一次冻结实验的事后改判据**——保留原判据/原结果/原结论后，新版本（plan_version+1）可修订预测、方法与 eval contract 重跑（见 constraint-governance §3 版本语义）；不回写旧结果把失败改成成功。 |
| next_min_probe | baseline、候选、开发指标、有效比较、预计成本、结果如何改变建设决策 |
| result_refs | 之后实际产生的日志、指标、代码版本、运行条件 |

**执行准入不变**：仍只有现有 READY_TO_PROBE（能跑通 + 评测出口有效 + 预算内）
+ 真实权限/安全/度量条件。卡片完整程度不取代就绪判据，不新增资格文件。

## 结果回流（回读 → 修改 → 修订）

1. **首次实验优先直接检验目标量**；只有诊断能改变具体方法决策时才做诊断。
2. 结果关联到具体 candidate version / plan_version / eval_contract（
   experiment-forge contract v2.1 的 envelope 语义），不覆盖旧关联。
3. 未确认的小正信号 → EXPLORATORY_POSITIVE（标"未确认"，r25 F1 语义）；
   负信号按当前实现/预算/指标限定 scope，不建整族 dead list。
4. 共同资产阻塞多个候选 → 先复审共同条件（constraint-governance §4），
   不把相同不可行方案换名重开。
5. 候选文字润色/重命名/卡片格式变化**不是实验结果**，不指定为进展凭据
   绕过准备预算——引用真实实验产物。
6. 方法超出授权 → 复用现有 scoped resource request（BLOCKED-USER 协议）。
7. paper-narrative 读取积累的观察/推断/预测；论证不得把预测写成结果，
   也不要求先有完美故事才实施。

## 两个短例

**例 1（跨域迁移）**：瓶颈=推荐系统精排延迟过高 → gap=现有两阶段粗排+精排
结构里粗排召回质量拖垮上限 → 跨域机制=网络搜索的"低代价检索候选集 + 精排"
两段式（同构：延迟约束下的候选集筛选）→ transfer_map：检索器→粗排、ranker→精排，
差异=推荐有用户实时行为而搜索没有 → concrete_change：粗排换成学习的双塔检索器 →
falsifiable_prediction：精排输入 top-K 质量升，端到端 AUC 不降、延迟 ≤ 预算 →
refutation_condition：延迟超预算或 AUC 降 > min_delta → next_min_probe=离线 A/B。

**例 2（原创假说，无跨域源）**：source_refs 为空；assumptions 里标出哪条是
推测；concrete_change 给最小伪代码；falsifiable_prediction/refutation_condition
照常填——原创假说不因"无文献来源"被拦，只因 READY_TO_PROBE 不满足而被缓。
