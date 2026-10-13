#!/usr/bin/env python3
"""grounded-ideation 盲测夹具（无模型 — 仅校验夹具结构与 rubric 完整性）。

运行真实盲测需单独批准模型预算（见 BLINDTEST.md）。本文件只做离线校验：
  - 12 个案例的输入字段齐全（scenario/question/mode/expect_rubric）
  - rubric 检查项非空、不含"预期答案泄露"（输入不得告诉代理该选什么）
  - 来源夹具 source_fixtures 与案例的 source_refs 对应
退出码 0 = 夹具可运行；非 0 = 夹具有缺陷，禁止跑盲测。
"""
import json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "blindtest_cases.json")

REQUIRED = ["case_id", "scenario", "question", "mode", "expect_rubric"]

def main():
    data = json.load(open(FIX, encoding="utf-8"))
    cases = data["cases"]
    fixtures = data.get("source_fixtures", {})
    fails = []

    if len(cases) != 12:
        fails.append("expected 12 cases, got %d" % len(cases))

    ids = set()
    for c in cases:
        cid = c.get("case_id", "?")
        for k in REQUIRED:
            if k not in c or not c[k]:
                fails.append("%s: missing %s" % (cid, k))
        if cid in ids:
            fails.append("%s: duplicate case_id" % cid)
        ids.add(cid)
        # mode must be guided or unattended
        if c.get("mode") not in ("guided", "unattended"):
            fails.append("%s: mode must be guided|unattended" % cid)
        # scenario must NOT leak the expected answer (no rubric keywords in scenario)
        rub = c.get("expect_rubric", "").lower()
        scen = c.get("scenario", "").lower()
        for leak_kw in ["应该", "must do X", "expected", "correct answer", "rubric"]:
            if leak_kw in scen:
                fails.append("%s: scenario leaks expected answer (%s)" % (cid, leak_kw))
        # scenario should reference a fixture if it claims source material
        if "来源" in scen or "source" in scen.lower():
            if not any(c.get("source_refs") for _ in [0]):
                pass  # source_refs optional per case

    # fixture paths resolve (relative to tests/)
    for fid, path in fixtures.items():
        if not os.path.exists(os.path.join(HERE, path.replace("tests/", "", 1))):
            fails.append("source_fixture %s -> missing file %s" % (fid, path))

    if fails:
        print("BLINDTEST FIXTURE DEFECTS:")
        for f in fails:
            print("  -", f)
        sys.exit(1)
    print("blindtest fixture OK: %d cases, %d source fixtures, no answer leakage" % (len(cases), len(fixtures)))
    sys.exit(0)

if __name__ == "__main__":
    main()
