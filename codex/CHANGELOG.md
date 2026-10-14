# CHANGELOG — codex-for-research

## 2026-10-14 Runtime recovery and policy consistency

- Preserve cumulative model costs across recovered turns, including old string snapshots; missing authorization sources do not activate a monetary limit. GPU usage remains statistics only.
- Generate the accounting helper version from one source and migrate existing 4.8 helpers once to 4.9; handle NUL-containing process identities explicitly.
- Judge baselines by the task and contribution rather than publication year; keep exploration separate from paper-level comparisons.
- Add isolated CPU adapter regression tests to the standard test command. Source validation does not imply live deployment or a real-model research-loop acceptance.

## v1.0-codex-engine（2026-09-30）

- 首个公开版：与 claude-for-research 同源的技能体系 Codex 移植（170 技能；按公开版口径去除团队作战/深度攻坚引擎及路由引用、个人 infra 组件）。
- AGENTS.md：研究约定 + Codex 适配说明（修复移植漂移：双后端文案、`~/.codex/skills` 路径、codex exec 委托、技能计数）。
- 可逆安装器 setup.sh（install/uninstall，备份+manifest 精确回退）+ arxiv MCP 幂等注册脚本。
- 论文检索后端：arxiv MCP 主（无 key）→ alphaxiv 增强（自动降级）→ web_search/browser 兜底。

## 2026-10-03 系统性修复与改进（与 claude-for-research 同批，verified）

A 队列 / B 契约 / C setup.sh+register-arxiv-mcp.sh / D 引用 / F 许可 / G 规则全部同步落地（内容与 claude 仓对应 commit 一致；E1 本仓保持 Codex 适配不变）。关键差异：setup.sh 备份唯一化+staging 原子替换+失败回滚；register-arxiv-mcp.sh 退出码语义（0 成功/3 跳过/1 失败）+写后回读验证。技能集合保持 115。未打 tag、未发 release。

## 2026-10-03 系统性修复与改进（与 claude-for-research 同批，verified）

H 队列 / I setup.sh 事务回滚+互斥锁+SIGTERM 回滚 / J shared-references 随包交付+forge 契约随包 / K rsync 白名单+远端计数 / L ARS 自检解耦 / M 日志命名+快照语义 / N AUTHORSHIP+MIT 附录 / O watchdog 加固+路径穿越拒绝+测试套件——与 claude 仓对应 commit 一致。tests/run_tests.sh 一键回归（引擎相关场景按 Codex 宿主语义执行）。已知限制节见 claude 仓 CHANGELOG（F06/F08/V04/S01/D01）。

### 已知限制（同步记录）

- F06 断点恢复协议 / F08 split 与指标来源核验 / V04 ARS 运行依赖 doctor / S01 preset 进程内 exec / D01 forge description 元数据——处置与理由同 claude-for-research 仓 CHANGELOG。

## 2026-10-03 评审语义修正：近邻存在 ≠ 否决

与 claude-for-research 同批：idea-evaluator 与 research-orchestrator 增加"近邻存在 ≠ 否决"一节（delta 声明四要件 + 决策规则），修正发现近邻即降分/绕开的隐性偏置。v1.2 增量构思的前置补丁。

## 2026-10-09 装载器修复 + 实验路由自动选择

codex 0.160.0 字段报告驱动：experiment-forge 补 description（0.160.0 必填、argument-hint 可选）；全舰队 589 个 SKILL.md 按最严 schema lint 清零（含 anti-defensive-writing-en 的 frontmatter 内嵌段修复）；orchestrator 增加可验证的实验引擎分流判据（有包/有会话→autoresearch；新课题无包→先 forge；单目标→Arbor、开放方向→多方案竞争引擎），交接显式传预算与停止条件。138.6/140.30 热修文本与上游统一。

## 2026-10-09 路由哲学修正 + SOTA 复现改进路线

路线改菜单制（判据可验证、裁决在 orchestrator、用户指定优先）；撤销"新课题必须先 forge"的刚性条款（裸库 autoresearch 合法）；多方案竞争引擎双用途（撒网 + 单项目多方案竞争，与 Arbor 不互斥）；新增 SOTA 复现改进路线（限时复现 ±5% 量级对上→官方 checkpoint 降级→SOTA 代码库上直接迭代，基线预注册冻结）。

## 2026-10-09 路由执行链接线（六项）

autoresearch 最小就地契约与 Forge 解耦；公开菜单可用性过滤（目录存在性先于偏好）；团队引擎交接规格（TASK.md 四字段+LAUNCH.md 底稿+launcher 路径）；research-state.yaml 累计预算总账与 backend_ledger；KEEP 口径拆分为 eval_baseline/best_value 两字段（主控+契约+模板三处统一）；文献路由改 ArXiv MCP 全文链。arbor 文档示例改绝对解释器（140.16 glibc 墙）并实测调用链。

## 2026-10-09 准备循环失控修复

真续接（codex exec resume + 项目级持久化会话 id，禁 --last，冷启动发状态摘要不发裸 continue）；准备预算跨重启累计（prep 六字段入 state 模板）；READY_TO_PROBE 就绪即启真实最小 probe；审查限界（门上限+新增门须论证、分级审查、审计不递归、可阻塞/不可阻塞清单）；修复重试政策（真实改动+新信息，替代一刀切）。GPT 事实裁定全部独立复核通过（账本 38.7408 GPU·h 复算一致）。活体战役脚本未动，参考外层脚本作 drop-in 交付。
## 2026-10-09 常规叙事骨架并入 paper-narrative（范式×骨架正交，verified）

- 新增 skills/paper-narrative/references/conventional-structures.md：骨架 1 技术类方法论论文常规六段（intro-drafter 六段 + Supervisor 写作手册 3.2/3.3 思考模型同源合并）；骨架 2 benchmark/evaluation 论文六段（benchmark-paper-template paper-structure，主轴 Evaluation Gap + Design Rationale，与范式 4"新基准暴露失效"区分且可叠加）；骨架 3 类型骨架指针（systems/ml/research-paper-writing）；范式×骨架组合规则（范式=论证立场、骨架=行文组织，正交可组合；benchmark 论文不得误路由成"另造新范式"）。
- paper-narrative/SKILL.md：Step 0 问题 5 扩为"主线与骨架"双问；Step 1 增行文骨架选择（技术类默认常规六段、benchmark 默认骨架 2）；Step 3 交接随计划带骨架。
- story-patterns.md：顶部增范式/骨架正交说明；范式 4 增与 benchmark 行文骨架的区分段。
- intro-drafter/SKILL.md Step 0：主线对接保留，新增骨架选择（默认六段 flowchart，叙事计划可指定其他骨架；integrity gate 仍 function-based，段落数永不是发现）。
- 行为验收（独立代理，5/5 PASS，行级证据）：技术论文任务→计划同时给立场+六段骨架；benchmark 任务→骨架 2 优先不误入范式选择；根因手术刀×六段组合不冲突；round-12 A/B/C 实验路由回归不破；审查纪律回归（骨架非硬门槛、pre-submission-reviewer 零触碰）。fm_lint 四仓 115/115/178/181 全 0 缺陷。记录：/home/yyc/outputs/lemvo-narrv2-20261009/。


## 2026-10-10 运行时 v2（round-23：占用窗口/原子锁/结果闭环）

lemvo-run v3：owner 原子锁（tmp 填充后 mv -T 发布，pid+starttime 双写身份，owner_alive 三态、宽限 60s 不抢锁）；C5 启动对账（turns/*+engine.json 判活、孤儿引擎等待+--recover 打捞、manifest settled 标记）；C6 结构化原因（HOLD→WAITING_USER、BLOCKED-USER→BLOCKED rc9、STOP→rc6）；引导常驻+双向切换（REQ_ADV 触摸、无 owner advance 共享步进）；作业六终态+回执 v2（run_id 预注册、JSON done_file、stale/corrupt 判定）。autoloop v4.5：整轮占用（.autoloop-turn-active=turn_id|pid|start，模型结束≠轮结束）；JSON-only 计量（cache_read/cache_creation 缺失=unknown 不凑 0、total_cost_usd 保真 1e-05、claude 累计成本按 SID 取差值、codex 单次计）；服务/引擎错误分类（429/401/5xx→rc8 保 SID，引擎结构化失败 rc0 也判 rc9）；--recover 孤儿打捞。SKILL.md：作业契约更新。回归（isolated stub，如实计数）：probe-v9 26/26、events 15/15、recovery 5 场景全过、adapter 双 CLI 夹具全过——口径=源码通过，非安装通过、非真实能力通过。活体战役（138.6/195）未触碰。下一步：小预算真实双 CLI 试点 + 交付 outputs/lemvo-runtime-v2-20261010/。


## 2026-10-12 全局可修订研究计划（round-24，constraint-governance）

research-orchestrator 内联约束治理规范（新 references/constraint-governance.md 单一来源）：计划默认可修订；约束六类×正交 authority（user_explicit/agent_proposed/needs_source…）；复审触发+change note 七字段；research_plan 区（version/stage/constraints/last_review）；旧项目缺区兼容运行（探针实证）。Gate 1 FINER 改摸底非数值否决；Forge contract v2.1 优先级限缩包内；autoresearch 停滞交回外循环；Arbor/团队引擎交接带 stage/plan_version/约束ID；调研技能增"配方与限制对照"。验收：CPU 契约 18/18 + probe-v9 26/26 + 服务分类 6/6 + resume-recovery 5/5 + fm_lint 0 缺陷；行为矩阵人工 rubric 21/21（软信号 14/21 如实记录）；真实 CPU 闭环三阶段（诚实"无需修订"→steer 驱动修订 v1→v2→超限 steer 发 BLOCKED-USER 不执行）。F1/F2 修复在真实失败上生效。交付 /home/yyc/gpt-output/lemvo-global-revisable-planning-implementation-2026-10-12/。迁移 dry-run 与部署清单待批准/ZCode。


## 2026-10-12 round-24 评审收尾（F1-F5 定点补缺）

F1 方向/确认标签拆分（CI 含零≠EXPLORATORY_NEGATIVE，正点估计记 EXPLORATORY_POSITIVE 未确认；constraint-governance + 主控标签表 + measurement-fairness 三处对齐，c07/c08 重判 2/2）；F2 金额预算派发前拦截（unit=usd 且 used≥cap → rc2 零调用决策点；结算按 cost_basis 累计 budget.used，负 delta=基线重置计 reported；探针 15/15）；F3 Arbor 阶段分层（授权交接免例行人审、unattended 映射 auto、IDEATE 新颖性滤网限最终方法主张）；F4 摘要按 research_plan 层级读取+改版刷新（15/18 探针覆盖同名键反例）+冷启动治理指针；F5 五维尾门阶段化。验收：最终字节 15+18+26+6+5 全过；自主 CPU 战役（无 steer 无预写分支）证明实验发现驱动的自主修订成立（方差缩减 26× envelope 内达标）但 version/change-note 记账缺失（裸调用未加载治理文本）——指针已入模板，复验待部署。F2 真实生效实证：budget.used=1.47 逐轮入账。活体战役零触碰。


## 2026-10-12 探索语义残留冲突定点修复（r26，grounded-ideation 文档发现项）

GPT grounded-ideation 计划指出的七处与 round-24 探索/确认分层、近邻不否决规范相冲突的残留，已在四仓定点修复并推送（claude 386dbf5 / codex 402dae6 / claude-pro b8f2017 / codex-pro 875d5c9）：① 预注册≠confirmatory（开发实验仍探索，确认需确认设计）；② 近邻 delta 模糊→先 provisional+有限对照小试，不自动 PIVOT（主控+evaluator 两处）；③ 探索门接受 provisional delta 声明（要素可标待核）；④ idea-evaluator 从"杀弱想法"改为"路由到下一步"，探索读法给优先级+可修复点+下一最小实验，两维 8+ 阈值仅限论文成熟评价；⑤ brainstorm 已知联系≠新颖性消失（可开发/组合/新条件验证）；⑥ creative 最佳契合（保真+资产+成本）替代"越远越好"；⑦ deep-research 授权下传不重复 yes 门（先查已有授权）。fm_lint 0 缺陷，公开仓无新增外部引擎字样。grounded-ideation 共享参考件本体与盲测/真实闭环仍待批准，另行处理。


## 2026-10-12 grounded-ideation 共享接口（IdeaScientist 借鉴，无新 skill）

新增 skills/shared-references/grounded-ideation.md（共享参考件非 skill，公开技能数仍 115）：gap→跨域机制→提案三段链（arXiv:2610.04074 消融量化依据 + r26 机制契合优先修正）、候选卡 13 字段（新增 falsifiable_prediction/refutation_condition 必填，交接工具非许可门，准入仍 READY_TO_PROBE）、结果回流七条。六主技能短链接按需触发。结构探针 28/28，fm_lint 0 缺陷，四仓推送核验（f54373f/44220e4/a5014d1/c76bacc）。盲测与真实双 CLI 闭环未跑，待批准。

## 2026-10-13 双运行模式统一 + grounded-ideation 评审收尾（r27）

用户要求的两种运行模式落地：启动选一次写入项目根 `.lemvo-mode`（单一来源），guided=阶段交接点汇报等确认、unattended=授权内自主推进仅授权缺口集中请求；中途切换改同一文件不重开课题，共享状态/结果/预算。主控+constraint-governance 对齐。评审收尾：brainstorming/creative 清无条件"询问研究者/研究者最终选择"改双模式分支；10-20 想法/3 实验/2 周 pilot 改可选建议按任务缩放；六引用改 `../shared-references/` 安装相对（探针加 G5 安装布局）；同域机制够用即可结束检索；预测量级可 unknown 不编数字；"不得改"限当前版本既有实验；30.7% 勘误为 30.7 个百分点（提案级 In-domain novelty）。盲测夹具入仓（tests/blindtest_cases.json + 3 来源夹具 + rubric + fixture 自检 + BLINDTEST.md，挂 run_tests.sh，无模型）。验收：探针 0/34、fm_lint 115/0、全仓测试套件过。四仓推送（b8cbfc5/a12f414/771a79e/263a3d5）。盲测 RUN 与双 CLI 闭环待批预算；140.30 live 未部署（不声称已改 live 行为）。


## 2026-10-13 启动模式接线修复（r28，评审 P1 闭环）

独立评审复现的确定缺口已修：lemvo-run.sh start 原无条件 `echo unattended > .lemvo-mode` 覆盖已选 guided（启动与重启均如此）。现支持 `--mode guided|unattended`（显式指定优先），未指定时**保留已有合法值不覆盖**，无合法值才默认 unattended；无效 --mode 忽略并记日志。启动/重启都保留选择，switch 与运行中切换不变。盲测夹具补模式覆盖（12→14 案，加 guided 交接 + 中途切换案）+ 全案授权字段 + source_refs 校验（修真自检空操作），BLINDTEST.md 补写 .lemvo-mode 步骤。回归：probe-mode-start-r28 8/8、probe-v9 26/26、fm_lint 115/115/178/181 零缺陷、四仓 run_tests.sh 过。四仓推送（7f85334/0afc578/6ece6ac/c2e28e4）。盲测 RUN + 双 CLI 闭环待批预算；live 未部署。


## 2026-10-13 r29：被拒 start 不改配置 + 参数缺值 rc64 + 盲测案例 14 修正

评审 P1：重复 start 曾被锁拒绝却仍先改 .lemvo-mode/引擎，把活 guided 控制器偷偷切 unattended。修：参数解析后**成功拿 owner 锁才应用模式与引擎配置**，被拒 start 不再改动活控制器；switch 不变。评审 P2：裸 --mode/--max-turns/--engine 缺值原卡解析死循环，现统一 need_value 校验缺值即 rc64 用法错误。盲测案例 14 原 mode=guided 却 expect unattended 自相矛盾，统一为 unattended（切换已完成）。回归 probe-mode-start 扩至 15 检查（活 owner 拒绝重启不改模式/引擎/不新增派发 + switch 恢复推进 + 缺值 rc64），probe-v9 26/26，fm_lint 115/115/178/181 零缺陷。四仓推送（c2a51bd/28c7f8d/d71754b/dc13546）。盲测 RUN + 双 CLI 闭环待批预算；live 未部署。


## 2026-10-13 r29-fix：发布前独立评审 P1 全量闭环（运行时 R1-R5 + 后端 D/PKG + 过度约束 A1-A9 + 入口/安装 B3）

Batch 1 运行时：R1 计量分账（USD 唯一硬门、GPU-hours 纯统计不设门、累计口径跨重启重放账本、负 delta 按 reported 基线重置、unknown 不补 0 不同时计费）；R2 结果 JSON 严格解析（失败退出也写回执进分析、日志先行内容不提前唤醒、analyst 至多唤醒一次、负向科研结果≠运行故障）；R3 多阻塞合并为结构化 BLOCKED（rc6，原因不互相覆盖）；R4 孤儿轮恢复（未结算轮 token/费用快照复用、不双计）；R5 set_status 结构化控制事件。
Batch 2 后端（Pro）：D1 指标合同规范化（champion 等首个基线显式建立、legacy 方向枚举在读边界归一 maximize|minimize、结果阶梯显式 BASELINE 档）；D2 剪枝=搜索调度态（未测后代保持 pending、已完成结果保留、视图声明"非失败方向"）；D3 merge 无测试分需显式 --accept-dev（merge_basis 落盘、test 隔离明示、改进门仅 test 基准生效）；D4 种子聚合=均值、best_seed_value 仅展示；PKG1 Arbor 安装正典源（仓库根技能套件优先解析、内嵌副本漂移告警并从正典安装、再安装 11 目录不回退）；PKG2 评分格式闸（bool/NaN/Infinity/溢出拒收，坏 eval 永不写 baseline/trunk，三道闸 parse→commit→meta）。团队路线启用标准=依赖齐全（launcher 可执行且协调服务可用），缺失自动回退。
Batch 2 过度约束修正（A1-A9）：crash/metric-error/timeout 三分（contract v2.2），Guard 失败/超时/解析失败均非科学否证；min_delta=确认跑触发器非 keep 门槛（同向小正信号保留并记确认状态）；比较口径按研究问题四选一（等墙钟/等 updates/等数据量/收敛）；可编辑范围按任务定义、依赖清单走预授权；评审裁决路由化（revision 与 build 可并行、无修订路径才 Reject、落后≠支配、severity 属于单个 flaw）；红旗降级为定位信号（无 top-3 论文/12 个月 SOTA 窗/外部受益人先验，资源区间=估算缺省）；家族统计仅真 DISCARD 降权（FAILED=fix-and-retry）；UNCONFIRMED（未确认保留候选）≠ DISCARD。
Batch 3：四仓 README 补常驻控制器入口（start --mode / switch / advance / steer / status）；净 HOME 安装验收三仓 rc=0 且安装产物与提交 diff -r 零差异、README 路由净安装根全通；分发脚本硬编码解释器清除（PY=${ARBOR_PY:-python3}）；危险整根删除回滚示例移除（改 git checkout <tag> && ./install.sh）。
回归：probe-r29fix 37/37 × 4 仓新鲜重跑 + B2 边界探针 28/28（PKG2/D2/D3 行为级 + D1/D4/A 系静态）+ probe-v9 26/26、mode-15 15/15（verified-then，运行时文件此后字节未变）+ 四仓 run_tests.sh 全过 + arbor pytest 384 过（7 失败为预存缺 demo 资产，基线 HEAD 同缺已核实）。24 条验收断言分级映射：直接演示 12 / 场景保留 9 / 机制核验 3（如实分级）。交付 /home/yyc/outputs/lemvo-r29-fix-20261013/。公开仓无新增受限字样（全树命中均为既有历史条目）。live 未部署（由 ZCode 执行）；真实双 CLI 小预算闭环随后补记。

## 2026-10-13 r29-fix 补记：真实双 CLI 小预算闭环

单轮冒烟（/tmp/r29-loop；证据 results/dual-cli-loop/）。claude 线真实闭环通过：start → 真实引擎轮 → 计量结算 → max-turns COMPLETED；账本实收 tokens 59457/15523、cache_read 642368、cost_usd_delta=2.0131（first-reported，client-estimate）、wall 404s、SID 冷启动取得；模型写 campaign_log.txt 未写 STATE.md，artifact_ref=none 如实入账（prep 计数如实记 0）。codex 线诚实记录阻断：本机 chatgpt.com 不可达（engine-stderr 连续请求超时），引擎分类 CONFIG_INVALID rc64（预检同时报告本机 codex config 键落在 TOML 节内的既有警告），退避重试 3 次后控制器转 BLOCKED|engine-fails（needs_user），SID 未弃、无假账。live 未部署不变（由 ZCode 通道执行）。

## 2026-10-13 r29-fix2：验收报告 P1 收尾 + 费用默认只统计（用户决定 2026-10-14）

**费用政策（最高优先）**：USD 费用默认只统计、不停止、不缩实验、不否定方向；金额停止门仅当 budget.cap_authorized: yes（真实用户明确授权、cap_source 出处可核对）时存在。模型自拟宪章/模板默认/历史孤立数值 ≠ 授权；total_cap:null/未设/来源不可考 = 只统计，不逐轮追问补授权。GPU 小时维持纯统计；token/累计墙钟不得替代金额门成为默认限制。恢复重复计费修复（recovered-turn 携带 cost_usd_reported 参与后续差分，序列 1→3→4 由记 6 修正为 4）；迁移行尾注释感知 + 误标项目幂等回填 + 迁移显式标 cap_authorized: no。

**P1-2 评分器**：JSON 主键阶梯全量 + 日志真实顺序统一取最终值（loss/val_bpb/f1 不再丢失、文本最终值不再被早期 JSON 遮蔽）；blob 级污染规则（同读数内任一主键 bool/NaN/Inf/溢出 → 整读数作废），坏 eval 永不写 baseline/trunk 不退化。

**P1-3 原生 Arbor**：prune_node 调度态语义同步 native（未测后代保持 pending、done/merged 保留 status+score、running 剪除）；constraints 视图去除 "these directions FAILED"，分 tested/untested 两栏（untested 明示 no re-proposal ban）。dev-only 真正接入调用处：native session_ops/MCP server/coordinator GitMergeBranchTool 三处 accept_dev；不跑不碰封存 test、不伪写 test_trunk_score、commit 标注 dev-only；fallback 先判 accept-dev 再决定是否跑 test 命令；merge-eval 技能与 coordinator prompts 接线。test 基线改进门仅 test basis。

**P1-4 团队路线分类闭合**：队列写入行同持久化 scientific_classification（发布前确定；σ 未知时小幅负默认 NEAR-MISS 不制造 DISCARD；FAILED 不算否证）；Step 9 改判必须版本化更新已持久化记录；analyst 家族计数优先读 scientific_classification（legacy 回落）；清除"skip this mechanism family"与"3+ DISCARD 不得提同族变体"禁令（降权≠禁令，真实负结果保留）；HEARTBEAT 恢复段同口径。三种子聚合纳入 third_seed_metric（全种子均值，best 仅展示）。

**运行时**：max-turns 终态 COMPLETED→PAUSED（运行器暂停≠课题完成；无产物绝不记完成；控制动作新增 paused）。Codex 失败分类按结构化终止事件（turn.failed/result error 消息），全日志 grep 降级兜底——非致命配置警告不再遮蔽真正终止原因（试点实录 routing discovery timeout 由误判 CONFIG_INVALID 修正为 SERVICE）。

**文档**：public README start 示例 --engine 显式（默认 codex 注明）；pro README 删除 install.sh 自动装团队组件的假声称、launcher --help 不构成依赖实证、打印 runbook≠已执行；constraint-governance GPU 条款按费用政策改写；fatal-flaws F4 按贡献类型判断受益。

**试点口径更正**：claude 试点不通过（10 项权限拒绝、STATE/artifact 未生成、campaign_log 是适配器追加）；max-turns=COMPLETED≠课题完成；codex 真正终止是 routing discovery timeout。详见交付目录 PILOT_ERRATA.md。

回归：probe-r29fix 39/39×4 仓（含 3 项费用政策断言）；审查方 decision 探针 23/24（1 项为探针预修复期望残留，规格已对齐）；B2 28/28；四仓 run_tests 全过；arbor pytest 排除预存缺资产后 0 failed（剪枝旧语义测试改写为新语义）；分类器真实 JSONL 重放 + 4 合成形态；入口 start/advance/status 实测。交付 /home/yyc/outputs/lemvo-r29fix2-20261013/。live 未部署（ZCode 通道；部署时需同步说明 cap_authorized 语义）。结果驱动真实双 CLI 链路待权限窄授权后重跑。
