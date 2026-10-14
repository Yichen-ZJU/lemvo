# grounded-ideation 12 案例盲测运行方法（需批准模型预算后执行）

本目录的 `blindtest_cases.json` 是**输入与 rubric 夹具**，本身不调用模型。
运行真实盲测前必须先：① 批准本次模型用量（案例数 × 单次调用上限 × 引擎）；
② 用 `tests/ideation_blindtest_fixture_test.py` 确认夹具通过（无模型）。

## 原则

- **输入不告诉代理该选什么**：只给任务/材料/授权；期望行为只在评分侧（rubric）。
- 测试用**真实技能的隔离安装副本**（不覆盖默认 live），新会话运行。
- rubric 事先写定，不看输出漂亮或关键词命中判 PASS。
- 失败案例保留原输出 + 修正记录，不通过改测试预期宣布通过。
- 这些是**实现验收用例**，不是日常科研新增的十二道门。

## 运行步骤

1. 夹具自检（无模型）：
   `python3 tests/ideation_blindtest_fixture_test.py`
2. 构造隔离技能根（复制 `skills/` 到临时目录），逐案新开单轮会话。
3. 每案 prompt = 案例 `scenario` + `question` + `authorization` +（若引用来源）对应
   fixture 文件内容；**把案例 `mode` 写入测试项目的 `.lemvo-mode`**（单一来源，
   子技能读取同一模式）；**不注入** `expect_rubric`、不注入 grounded-ideation.md
   全文（测真实加载链，不向测试代理预注入治理规范来替代加载链）。
4. 按 `expect_rubric` 逐项人工判定，记录：原始输出、实际调用、判定、失败原因。
5. 汇总到 `blindtest-results.json`（cases / pass / fail / 未跑），失败案例附原输出。

## 通过标准

看 source_refs 是否真实、机制是否吻合、能否指出具体改动、是否走了建设出口、
原始实验结果有没有被改写。不要求每个案例取得正结果——失败与零效应可以合理
结束，重点是"实施→真实测量→回读→改方法"的闭环与负结果范围的准确限定。

## 预算

12 案例 × 单轮；引擎与每次 token 上限在发起前确定并获批。盲测通过后再申请
或使用已获批预算做隔离 CPU 双 CLI 小闭环（见 grounded-ideation 计划 §六）。
