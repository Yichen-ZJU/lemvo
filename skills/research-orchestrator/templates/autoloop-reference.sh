#!/usr/bin/env bash
# 参考外层循环脚本 v4.6（round-26：codex 线联调六缺陷修复）
#  v4.6 变更（对 v4.5，均带探针）：
#  BUG-1 bootstrap：STATE 缺失 → 首轮消息改 bootstrap 指令（建 STATE+首个产物），
#    不再 READY 循环模板；settle/account/budget-add 对缺失 STATE 不崩溃；
#    连续 5 轮零产物（引擎 rc=0 礼貌空转）→ rc2 决策点（STALL_NO_ARTIFACT）。
#  BUG-2 codex 沙箱：exec 显式 -s ${CODEX_SANDBOX:-workspace-write}（0.16x 默认
#    只读+审批与无人值守不兼容）；不默认 danger-full-access。
#  BUG-3 毒会话：连续 3 轮阻塞话术+零产物 → 清 SID 强制新会话（session-poisoned
#    事件入账）；--recover 不收养有 poison 记录的 SID；lemvo-run 新增 purge 命令。
#  BUG-4 codex ≥0.162 计量：turn.completed usage 为会话累计值 → 按 SID 从账本
#    求当轮差值（负差=基线重置按当轮值计）；字段含 cache_write_input_tokens。
#  BUG-5 预检：config 含 service_tier="default" 警告（部分 0.16x 环境拒载、
#    实测有分歧，不一律拒跑）；exec 错误文本含 unknown variant/无效 config 时
#    精确判 CONFIG_INVALID rc64；approval/sandbox 键落在 TOML 节内 → 警告。
#  BUG-6 预检：git 仓库缺 user.email → 从 global 复制 repo-local；都无则 rc64。
# v4.5 变更（对 v4.4）：
#  R2/R3 整轮占有权与孤儿回收：turns/<turn_id>/ 持久 manifest；引擎输出落
#    turn 目录（engine-stdout.jsonl / engine-stderr.log）；.autoloop-turn-active
#    覆盖 派发→SID提取→计量结算→日志 全程（模型结束≠整轮结束）；--recover
#    幂等结算孤儿轮（抢救 SID/usage/进展，不重跑实验，不调引擎）。
#  A1 错误分类收口：401/403/500/Unauthorized/Forbidden/overloaded 等账号与
#    服务基础设施错误 → exit 8（保留 SID、不计 resume-fail、不算研究停滞）；
#    结构化引擎失败（claude result is_error / codex turn.failed）→ engine-error
#    记账（区分 工具/协议 损坏），rc=0 也判失败（结构化模式 exit 9）。
#  E-计量 usage 全 JSON 解析：input/output/cache_read/cache_write 分列，缺失
#    保留 unknown 不补 0；total_cost_usd 走 JSON 数值（1e-5 不再截断）；
#    CACHE_W 用 claude cache_creation 真值；费用标 client-estimate；
#    modelUsage 子树存在性单独标注（口径≠主 usage）。
#  E-cost Claude resume 累计口径：cost_usd_reported + cost_usd_delta（同 SID
#    对上次报告求差）+ cost_basis（claude=delta-from-prev / codex=per-call）；
#    不逐轮重复相加累计值，state total_cost 保持不累计。
#  E-sid 冷启动成功必须取得会话身份：缺 SID 记 protocol-warning 并输出
#    LEMVO_RC_REASON=PROTOCOL_NO_SID（控制器 3 连计后转 BLOCKED）；
#    resume-ok 依旧只在有真实 SID 时记账。
#  E-result 每轮 result.json：schema_version/campaign_id/turn_id/status/
#    next_action(continue|waiting_jobs|needs_user|completed|runtime_error)/
#    scientific_verdict(沿用现有状态标签，不造新评分)/artifact_refs。
#  E-reason 结构化模式在各退出点输出 LEMVO_RC_REASON（OVER_BUDGET/服务 SERVICE/
#    阻塞态原文/WAITING_RESOURCE/PROTOCOL/ACCOUNTING），控制器按 reason 转状态。
# v4.1-v4.4 行为保留：进展凭据类型化、助手版本迁移、授权真 JSON、usage 只认
#  JSON 事件、阻塞态闭环、调用者消息永不删除、YAML 权威、预算决策四选项、
#  引擎身份在 cd 后读取、双引擎适配、结构化 rc6/7 映射。
# 红线：替换活体战役脚本前先备份；对在跑进程只观察不杀。
set -uo pipefail
HELPER_VERSION="4.6"

# ── --init 模式 ──
if [ "${1:-}" = "--init" ]; then
  PROJ_DIR="${2:?用法: autoloop-reference.sh --init <项目目录>}"
  cd "$PROJ_DIR" || exit 64
  STATE=research-state.yaml
  [ -f "$STATE" ] || { echo "缺少 $STATE"; exit 64; }
  python3 - <<'PYINIT'
import re
p = "research-state.yaml"
lines = open(p, encoding="utf-8").read().splitlines()
def section_idx(name):
    idxs = [i for i, l in enumerate(lines) if re.match(r"^" + name + r"\s*:", l)]
    return idxs
changed = False
prep_fields = ["  prep_turns: 0", "  prep_wall_time_min: 0.0", "  prep_cost: null",
               "  last_informative_observation: \"\"", "  last_informative_artifact: none",
               "  last_artifact_sha: none", "  last_seen_observation: none",
               "  ready_blocker: \"\"", "  next_min_probe: \"\"", "  session_id: null"]
total_fields = ["  total_turns: 0", "  total_wall_time_min: 0.0", "  total_cost: null",
                "  total_tokens_in: 0", "  total_tokens_out: 0"]
for name, fields in (("prep", prep_fields), ("total", total_fields)):
    idxs = section_idx(name)
    if not idxs:
        lines.extend([name + ":"] + fields)
        changed = True
    else:
        idx = idxs[-1]
        end = idx + 1
        while end < len(lines) and (not lines[end].strip() or lines[end][0] in " #"):
            end += 1
        present = {m.group(1) for l in lines[idx+1:end] for m in [re.match(r"\s*([\w-]+)\s*:", l)] if m}
        for f in fields:
            key = re.match(r"\s*([\w-]+)\s*:", f).group(1)
            if key not in present:
                lines.insert(end, f); end += 1; changed = True
if changed:
    open(p, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    print("accounting-initialized")
else:
    print("accounting-already-present")
PYINIT
  cat <<'WARN'
诚实边界：--init 只初始化字段，不建立持续采集。持续记账需要（a）以本脚本
作为外层循环逐轮运行（每轮自动结算墙钟/token/进展），或（b）交互式会话
每轮手动调用本脚本一次（它结算当轮后退出）。只跑 --init 不跑循环 =
字段存在但永远是 0。
预算口径（round-23）：total_wall_time_min=研究墙钟（引擎调用累计，控制器
等待/训练不在内）；GPU/训练时长按 runner 回执（.lemvo-receipts 的 metrics）
计；累计研究预算在 research-state.yaml + ledger 持久账本，不随模式/进程
重启重置。
WARN
  exit $?
fi

# --recover 前置解析：cd 须发生在任何相对路径副作用之前
RECOVER_TD=""
if [ "${1:-}" = "--recover" ]; then
  PROJ_DIR="${2:?用法: autoloop-reference.sh --recover <项目目录> <turn目录>}"
  RECOVER_TD="${3:?缺 turn 目录}"
else
  PROJ_DIR="${1:?用法: autoloop-reference.sh <项目目录> [调用者消息文件(只读)]}"
  CALLER_MSG="${2:-}"
fi
cd "$PROJ_DIR" || exit 64
# v4.3a：引擎身份在项目目录内读取
ENGINE="${ENGINE:-$(cat .lemvo-engine 2>/dev/null || echo codex)}"
CLAUDE_BIN="${CLAUDE_BIN:-claude}"
RC_MODE="${LEMVO_RC_MODE:-legacy}"
TURN_START=$(date +%s)
STATE=research-state.yaml
SID_FILE=.autoloop-session-id
LEDGER=.autoloop-prep-ledger.jsonl
OWN_SUMMARY=.autoloop-summary.md
HELPER=.autoloop-helper.py
PREP_CAP="${PREP_CAP_TURNS:-20}"
PREP_WALL_CAP="${PREP_WALL_CAP_MIN:-0}"
RESUME_FAIL_THRESHOLD="${RESUME_FAIL_THRESHOLD:-2}"

say() { echo "[$(date +%H:%M:%S)] $*"; }
emit_reason() { [ "$RC_MODE" = "structured" ] && say "LEMVO_RC_REASON=$1"; return 0; }
[ "$PREP_CAP" -ge 0 ] 2>/dev/null || { say "PREP_CAP_TURNS 非法: $PREP_CAP"; exit 64; }
python3 -c "import sys; v=float(sys.argv[1]); sys.exit(0 if v>=0 else 1)" "$PREP_WALL_CAP" 2>/dev/null \
  || { say "PREP_WALL_CAP_MIN 非法（须非负数）: $PREP_WALL_CAP"; exit 64; }

# ── 助手版本管理（S2）──
write_helper() {
  cat > "$HELPER" <<'PYEOF'
#!/usr/bin/env python3
"""autoloop v4.6 助手（HELPER_VERSION 4.6）。py3.6 兼容，标准库 only。"""
import fnmatch, hashlib, json, os, re, sys

HELPER_VERSION = "4.6"
STATE = "research-state.yaml"
BLOCKING = {"HOLD", "STOPPED", "PAUSED", "EVIDENCE_HOLD", "STOPPED_INCONCLUSIVE",
            "BLOCKED", "BLOCKED_USER"}
PREP_DEFAULTS = [("prep_turns", "0"), ("prep_wall_time_min", "0.0"), ("prep_cost", "null"),
                 ("last_informative_observation", "none"), ("last_seen_observation", "none"),
                 ("last_informative_artifact", "none"), ("last_artifact_sha", "none"),
                 ("ready_blocker", "none"), ("next_min_probe", "none"), ("session_id", "null")]
TOTAL_DEFAULTS = [("total_turns", "0"), ("total_wall_time_min", "0.0"), ("total_cost", "null"),
                  ("total_tokens_in", "0"), ("total_tokens_out", "0")]
DEFAULT_ARTIFACT_GLOBS = "runs/*:experiments/*:results/*:*metrics*.json:eval*.json:*-preds.jsonl"

def strip_inline_comment(line):
    out, quote = [], None
    for ch in line:
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
        else:
            if ch in ("'", '"'):
                quote = ch; out.append(ch)
            elif ch == "#":
                break
            else:
                out.append(ch)
    return "".join(out).rstrip()

def unquote(val):
    val = val.strip()
    if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
        return val[1:-1]
    return val

def section_header(name):
    return re.compile(r"^" + name + r"\s*:")

def read_fields():
    f = {"campaign_status": "", "measurement_state": "", "root_status": "",
         "status": "", "active_role": ""}
    prep, total, project = {}, {}, {"status": ""}
    top = ""
    try:
        for raw in open(STATE, encoding="utf-8", errors="replace"):
            line = strip_inline_comment(raw)
            s = line.strip()
            if not s or s == "-":
                continue
            m = re.match(r"^(\s*)([A-Za-z_][\w-]*)\s*:\s*(.*)$", line)
            if not m:
                continue
            indent, key, val = len(m.group(1)), m.group(2), m.group(3).strip()
            if indent == 0:
                top = key
                if key in f:
                    f[key] = unquote(val)
                if key == "project":
                    project = {"status": ""}
            elif top == "project" and key == "status":
                project["status"] = unquote(val)
            elif top == "prep" and key in dict(PREP_DEFAULTS):
                prep[key] = unquote(val)
            elif top == "total" and key in dict(TOTAL_DEFAULTS):
                total[key] = unquote(val)
    except OSError:
        pass
    f["project_status"] = project.get("status", "")
    return f, prep, total

def read_budget():
    """budget 区（unit/total_cap/used/remaining）；缺失字段返回 None。"""
    out = {"unit": None, "total_cap": None, "used": None, "remaining": None}
    top = ""
    try:
        for raw in open(STATE, encoding="utf-8", errors="replace"):
            line = strip_inline_comment(raw)
            m = re.match(r"^(\s*)([A-Za-z_][\w-]*)\s*:\s*(.*)$", line)
            if not m:
                continue
            indent, key, val = len(m.group(1)), m.group(2), m.group(3).strip()
            if indent == 0:
                top = key
            elif top == "budget" and indent == 2 and key in out:
                out[key] = unquote(val)
    except OSError:
        pass
    return out

def read_plan():
    """按层级只读 research_plan 区的 version/stage（F4：不做全文件首个匹配）。
    缺区或缺字段返回 none，不借用其他同名键。"""
    out = {"version": "none", "stage": "none"}
    top = ""
    try:
        for raw in open(STATE, encoding="utf-8", errors="replace"):
            line = strip_inline_comment(raw)
            m = re.match(r"^(\s*)([A-Za-z_][\w-]*)\s*:\s*(.*)$", line)
            if not m:
                continue
            indent, key, val = len(m.group(1)), m.group(2), m.group(3).strip()
            if indent == 0:
                top = key
            elif top == "research_plan" and indent == 2 and key in out:
                v = unquote(val)
                if v:
                    out[key] = v
    except OSError:
        pass
    return out

def num_or_none(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None

def cmd_budget_gate():
    """F2：金额预算派发前闸门。unit=usd 且 cap/used 均为数值且 used >= cap
    才判耗尽（unknown/缺失不视为耗尽，也不视为无限授权——仅不拦截）。"""
    b = read_budget()
    cap, used = num_or_none(b["total_cap"]), num_or_none(b["used"])
    exhausted = "unknown"
    if b["unit"] == "usd" and cap is not None and used is not None:
        exhausted = "yes" if used >= cap else "no"
    print("BUDGET_UNIT=" + str(b["unit"]))
    print("BUDGET_CAP=" + str(b["total_cap"]))
    print("BUDGET_USED=" + str(b["used"]))
    print("BUDGET_EXHAUSTED=" + exhausted)

def cmd_budget_add():
    """F2：结算时累计 budget.used。argv: budget-add <increment|unknown>
    increment 由调用方按成本口径算好（per-call/delta>=0 用 delta；
    负 delta=基线重置用 reported；unknown 不加）。只动 budget 区，
    不碰 prep/total 总账。剩余量随 cap 联动；cap 缺失不写 remaining。"""
    global LINES
    inc = sys.argv[2] if len(sys.argv) > 2 else "unknown"
    if inc == "unknown":
        print("BUDGET_USED_NOW=unknown"); return
    try:
        incf = float(inc)
    except ValueError:
        print("BUDGET_USED_NOW=unknown"); return
    if not os.path.exists(STATE):
        print("BUDGET_USED_NOW=state-missing"); return
    b = read_budget()
    used = num_or_none(b["used"])
    if used is None:
        used = 0.0
    used = round(used + incf, 6)
    LINES = open(STATE, encoding="utf-8").read().splitlines()
    set_in("budget", "used", used)
    cap = num_or_none(b["total_cap"])
    if cap is not None:
        set_in("budget", "remaining", round(cap - used, 6))
    open(STATE, "w", encoding="utf-8").write("\n".join(LINES) + "\n")
    print("BUDGET_USED_NOW=" + str(used))

def cmd_plan_info():
    p = read_plan()
    print("PLAN_STAGE=" + p["stage"])
    print("PLAN_VERSION=" + p["version"])


def init_sections(f, prep, total):
    try:
        lines = open(STATE, encoding="utf-8").read().splitlines()
    except OSError:
        return False
    changed = False
    for name, defaults in (("prep", PREP_DEFAULTS), ("total", TOTAL_DEFAULTS)):
        idxs = [i for i, l in enumerate(lines) if section_header(name).match(l)]
        if not idxs:
            lines.extend([name + ":"] + ["  " + k + ": " + v for k, v in defaults])
            changed = True
            continue
        idx = idxs[-1]
        end = idx + 1
        while end < len(lines) and (not lines[end].strip() or lines[end][0] in " #"):
            end += 1
        present = set()
        for l in lines[idx+1:end]:
            m = re.match(r"\s*([\w-]+)\s*:", l)
            if m:
                present.add(m.group(1))
        for k, v in defaults:
            if k not in present:
                lines.insert(end, "  " + k + ": " + v)
                end += 1
                changed = True
    if changed:
        open(STATE, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    return changed

def ledger_append(obj):
    with open(".autoloop-prep-ledger.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(obj, ensure_ascii=False) + "\n")

def ledger_events():
    try:
        for ln in open(".autoloop-prep-ledger.jsonl", encoding="utf-8"):
            ln = ln.strip()
            if not ln:
                continue
            try:
                yield json.loads(ln)
            except ValueError:
                pass
    except OSError:
        return

def sha256_file(path):
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None

def artifact_is_construction(path):
    globs = os.environ.get("ARTIFACT_GLOBS", DEFAULT_ARTIFACT_GLOBS).split(":")
    base = os.path.basename(path)
    full = path.lstrip("./")
    return any(fnmatch.fnmatch(full, g) or fnmatch.fnmatch(base, g) for g in globs if g)

LINES = []
def set_in(section, key, val):
    global LINES
    out, inside, done, header_seen = [], False, False, False
    for ln in LINES:
        if not header_seen and section_header(section).match(ln):
            header_seen = True; inside = True; out.append(ln); continue
        if inside and not done:
            m = re.match(r"^(\s*)" + key + r"\s*:\s*", ln)
            if m:
                out.append(m.group(1) + key + ": " + str(val)); done = True
                continue
            if re.match(r"^\S", ln):
                inside = False
        out.append(ln)
    if not done:
        out.append("  " + key + ": " + str(val))
    LINES = out

def cmd_init():
    f, prep, total = read_fields()
    init_sections(f, prep, total)
    print("initialized")

def cmd_prepare():
    f, prep, total = read_fields()
    init_sections(f, prep, total)
    def q(v):
        return "'" + str(v).replace("'", "'\\''") + "'"
    print("CAMPAIGN_STATE=" + q(f["campaign_status"]))
    print("MEASURE_STATE=" + q(f["measurement_state"]))
    print("ROOT_STATUS=" + q(f["root_status"]))
    print("PROJ_STATUS=" + q(f["project_status"]))
    print("ACTIVE_ROLE=" + q(f["active_role"]))
    print("PREP_TURNS=" + q(prep.get("prep_turns", "0")))
    print("WALL_MIN=" + q(total.get("total_wall_time_min", "0.0")))

def cmd_block_check():
    f, prep, total = read_fields()
    states = [f["campaign_status"], f["measurement_state"], f["project_status"],
              f["root_status"], f["status"]]
    blocking = [s.upper() for s in states if s and s.upper() in BLOCKING]
    print("BLOCKING=" + ("|".join(blocking) if blocking else "none"))
    print("WAITING=" + ("yes" if any(s.upper() == "WAITING_RESOURCE" for s in states if s) else "no"))

def cmd_settle():
    """S1 凭据类型化：进展 = artifact 匹配建设类白名单 且 sha 变化。
    BUG-1：STATE 缺失（新战役 bootstrap 期间）不崩溃——记无进展并输出默认值。"""
    global LINES
    if not os.path.exists(STATE):
        print("NEW_PROGRESS=no"); print("ARTIFACT_TYPED=no")
        print("PREP_TURNS_NOW=0"); print("TOTAL_TURNS_NOW=0")
        print("STATE_MISSING=yes"); return
    f, prep, total = read_fields()
    init_sections(f, prep, total)
    artifact = prep.get("last_informative_artifact", "none")
    last_sha = prep.get("last_artifact_sha", "none")
    new_sha = None
    typed = False
    if artifact and artifact != "none":
        typed = artifact_is_construction(artifact)
        if typed:
            new_sha = sha256_file(artifact)
    new_progress = bool(new_sha) and new_sha != last_sha
    last_obs = prep.get("last_informative_observation", "none")
    last_seen = prep.get("last_seen_observation", "none")
    try:
        turns = int(prep.get("prep_turns", "0"))
    except (TypeError, ValueError):
        turns = 0
    try:
        total_turns = int(total.get("total_turns", "0"))
    except (TypeError, ValueError):
        total_turns = 0
    turns = 0 if new_progress else turns + 1
    total_turns += 1
    LINES = open(STATE, encoding="utf-8").read().splitlines()
    set_in("prep", "prep_turns", turns)
    set_in("prep", "last_seen_observation", last_obs if new_progress else last_seen)
    if new_progress:
        set_in("prep", "last_artifact_sha", new_sha)
    set_in("total", "total_turns", total_turns)
    open(STATE, "w", encoding="utf-8").write("\n".join(LINES) + "\n")
    print("NEW_PROGRESS=" + ("yes" if new_progress else "no"))
    print("ARTIFACT_TYPED=" + ("yes" if typed else "no"))
    print("PREP_TURNS_NOW=" + str(turns))
    print("TOTAL_TURNS_NOW=" + str(total_turns))

def cmd_account():
    """S5：墙钟秒 + token（unknown 允许）。argv: account <elapsed_sec> <in|unknown> <out|unknown>
    BUG-1：STATE 缺失时不崩溃，跳过写账（bootstrap 首轮后可写）。"""
    global LINES
    if not os.path.exists(STATE):
        print("WALL_NOW=state-missing"); return
    elapsed = float(sys.argv[2])
    tin, tout = sys.argv[3], sys.argv[4]
    f, prep, total = read_fields()
    init_sections(f, prep, total)
    def num(v, d=0.0):
        try:
            return float(v)
        except (TypeError, ValueError):
            return d
    wall = round(num(total.get("total_wall_time_min")) + elapsed / 60.0, 2)
    LINES = open(STATE, encoding="utf-8").read().splitlines()
    set_in("total", "total_wall_time_min", wall)
    if tin != "unknown":
        set_in("total", "total_tokens_in", int(num(total.get("total_tokens_in"))) + int(tin))
    if tout != "unknown":
        set_in("total", "total_tokens_out", int(num(total.get("total_tokens_out"))) + int(tout))
    open(STATE, "w", encoding="utf-8").write("\n".join(LINES) + "\n")
    print("WALL_NOW=" + str(wall))

def cmd_usage_from():
    """v4.6：只从 JSON 事件的 usage/token_usage 容器解析；input/output/
    cache_read/cache_write 分列，逐字段判存在——缺失保留 unknown 不补 0；
    total_cost_usd 走 JSON 数值（科学计数法不截断）；modelUsage 子树存在性
    单独标注（其口径与主 usage 不同，费用须带 scope）。
    BUG-4：codex ≥0.162 的 turn.completed usage 是【会话累计值】（字段
    input_tokens/cached_input_tokens/cache_write_input_tokens/output_tokens/
    reasoning_output_tokens）。argv: usage-from <file> [sid] [engine]
    engine=codex 且有 sid 时按 SID 从账本取上次累计求差（与 cost-delta 同
    构）；负差=基线重置按当轮累计计。旧版 codex（逐轮值）不受影响——
    账本中无同 SID 累计基线时以当轮值为基线。"""
    engine = sys.argv[4] if len(sys.argv) > 4 else ""
    sid = sys.argv[3] if len(sys.argv) > 3 else "none"
    sums = {"in": 0, "out": 0, "cr": 0, "cw": 0}
    seen = {"in": False, "out": False, "cr": False, "cw": False}
    cum = {"in": None, "out": None, "cr": None, "cw": None}
    cost = None
    modelusage = False
    def acc(u, keys, sk):
        for k in keys:
            v = u.get(k)
            if isinstance(v, (int, float)):
                if engine == "codex":
                    cum[sk] = int(v)   # 累计语义：取最后值，不相加
                else:
                    sums[sk] += int(v)
                seen[sk] = True
                return
    try:
        for ln in open(sys.argv[2], encoding="utf-8", errors="replace"):
            ln = ln.strip()
            if not ln.startswith("{"):
                continue
            try:
                ev = json.loads(ln)
            except ValueError:
                continue
            if not isinstance(ev, dict):
                continue
            u = ev.get("usage") or ev.get("token_usage")
            if isinstance(u, dict):
                acc(u, ("input_tokens", "input_tokens_count", "prompt_tokens"), "in")
                acc(u, ("output_tokens", "output_tokens_count", "completion_tokens"), "out")
                acc(u, ("cache_read_input_tokens", "cached_input_tokens"), "cr")
                acc(u, ("cache_creation_input_tokens", "cache_write_input_tokens"), "cw")
                if not any(seen.values()):
                    continue
            c = ev.get("total_cost_usd")
            if isinstance(c, (int, float)):
                cost = c
            if isinstance(ev.get("modelUsage"), dict):
                modelusage = True
    except OSError:
        pass
    # codex 累计 → 当轮差值（基线从账本同 SID 最近一条带 codex_cum 的事件取）
    if engine == "codex" and sid != "none" and any(v is not None for v in cum.values()):
        prev = {}
        for ev in ledger_events():
            cc = ev.get("codex_cum")
            if ev.get("sid") == sid and isinstance(cc, dict):
                prev = cc
        out_vals = {}
        for sk in ("in", "out", "cr", "cw"):
            cur = cum[sk]
            if cur is None:
                out_vals[sk] = "unknown"; continue
            pv = prev.get(sk)
            if isinstance(pv, (int, float)) and cur >= int(pv):
                out_vals[sk] = cur - int(pv)
            else:
                out_vals[sk] = cur   # 无基线或回退（新会话/基线重置）→ 当轮=累计值
        print("TOKENS_IN=%s" % out_vals["in"])
        print("TOKENS_OUT=%s" % out_vals["out"])
        print("CACHE_READ=%s" % out_vals["cr"])
        print("CACHE_WRITE=%s" % out_vals["cw"])
        print("CODEX_CUM=" + json.dumps(cum, separators=(",", ":")))
    else:
        print("TOKENS_IN=%s" % (sums["in"] if seen["in"] else "unknown"))
        print("TOKENS_OUT=%s" % (sums["out"] if seen["out"] else "unknown"))
        print("CACHE_READ=%s" % (sums["cr"] if seen["cr"] else "unknown"))
        print("CACHE_WRITE=%s" % (sums["cw"] if seen["cw"] else "unknown"))
    print("COST_USD=%s" % (repr(cost) if cost is not None else "unknown"))
    print("MODELUSAGE=%s" % ("present" if modelusage else "absent"))

def cmd_outcome_from():
    """v4.5：从结构化终端事件判引擎自报成败（与进程 rc 分离）。
    claude: type=result 且 is_error=true / subtype=error* → failed；
    codex:  type=turn.failed/turn.aborted → failed；claude result 正常 → ok。"""
    outcome = "none"
    err = ""
    try:
        for ln in open(sys.argv[2], encoding="utf-8", errors="replace"):
            ln = ln.strip()
            if not ln.startswith("{"):
                continue
            try:
                ev = json.loads(ln)
            except ValueError:
                continue
            if not isinstance(ev, dict):
                continue
            t = ev.get("type", "")
            if t in ("turn.failed", "turn.aborted"):
                outcome = "failed"
                e = ev.get("error")
                err = json.dumps(e, ensure_ascii=False)[:300] if e else t
            elif t == "result":
                if ev.get("is_error") is True or str(ev.get("subtype", "")).startswith("error"):
                    outcome = "failed"
                    errs = ev.get("errors")
                    err = ("; ".join(str(x) for x in errs))[:300] if errs else str(ev.get("result", ""))[:300]
                else:
                    outcome = "ok"
    except OSError:
        pass
    print("OUTCOME=%s" % outcome)
    print("ERRMSG=%s" % err.replace("\n", " "))

def cmd_cost_delta():
    """v4.5：argv: cost-delta <sid> <reported_cost> <engine>
    claude resume（2.1.277+）total_cost_usd 为累计量 → delta = 本次-上次同SID报告；
    codex 每次调用独立 → delta=reported, basis=per-call。不重复相加累计值。"""
    sid = sys.argv[2] if len(sys.argv) > 2 else "none"
    try:
        rep = float(sys.argv[3])
    except (IndexError, ValueError):
        print("COST_DELTA=unknown"); print("COST_BASIS=unknown"); return
    engine = sys.argv[4] if len(sys.argv) > 4 else "codex"
    if engine == "claude":
        prev = None
        for ev in ledger_events():
            if ev.get("type") in ("work-turn", "prep-turn") and ev.get("sid") == sid:
                v = ev.get("cost_usd_reported")
                if isinstance(v, (int, float)):
                    prev = v
        if prev is not None:
            print("COST_DELTA=%s" % (rep - prev)); print("COST_BASIS=delta-from-prev")
        else:
            print("COST_DELTA=%s" % rep); print("COST_BASIS=first-reported")
    else:
        print("COST_DELTA=%s" % rep); print("COST_BASIS=per-call")

def cmd_prep_snapshot():
    f, prep, total = read_fields()
    print("LAST_ARTIFACT=" + str(prep.get("last_informative_artifact", "none")))
    print("LAST_OBS=" + str(prep.get("last_informative_observation", "none")))

def cmd_auth_json():
    """S3：授权事件真序列化（justification 经环境变量传入）。"""
    just = os.environ.get("AUTHORIZE_JUSTIFICATION", "")
    obj = {"type": "authorize", "new_cap": int(sys.argv[2]),
           "justification": just, "ts": sys.argv[3]}
    print(json.dumps(obj, ensure_ascii=False))

def cmd_ledger_append():
    obj = json.loads(sys.stdin.read())
    if isinstance(obj, dict) and obj.get("turn_id"):
        for ev in ledger_events():
            if ev.get("type") == obj.get("type") and ev.get("turn_id") == obj.get("turn_id"):
                print("duplicate-skipped")
                return
    ledger_append(obj)
    print("appended")

def cmd_resume_fails():
    sid = sys.argv[2] if len(sys.argv) > 2 else "none"
    count = 0
    for ev in reversed(list(ledger_events())):
        if ev.get("type") == "resume-fail" and ev.get("sid") == sid:
            count += 1
        elif ev.get("type") in ("resume-ok", "resume-recovery"):
            break
    print(str(count))

def cmd_last_authorize_cap():
    last = None
    for ev in ledger_events():
        if ev.get("type") == "authorize":
            last = ev
    print(str(last.get("new_cap")) if last else "")

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "init": cmd_init()
    elif cmd == "prepare": cmd_prepare()
    elif cmd == "settle": cmd_settle()
    elif cmd == "block-check": cmd_block_check()
    elif cmd == "account": cmd_account()
    elif cmd == "usage-from": cmd_usage_from()
    elif cmd == "outcome-from": cmd_outcome_from()
    elif cmd == "cost-delta": cmd_cost_delta()
    elif cmd == "prep-snapshot": cmd_prep_snapshot()
    elif cmd == "auth-json": cmd_auth_json()
    elif cmd == "ledger-append": cmd_ledger_append()
    elif cmd == "resume-fails": cmd_resume_fails()
    elif cmd == "last-authorize-cap": cmd_last_authorize_cap()
    elif cmd == "budget-gate": cmd_budget_gate()
    elif cmd == "budget-add": cmd_budget_add()
    elif cmd == "plan-info": cmd_plan_info()
    else: sys.exit(2)
PYEOF
  chmod +x "$HELPER" 2>/dev/null || true
}

if [ ! -f "$HELPER" ]; then
  write_helper
elif ! grep -q "HELPER_VERSION = \"$HELPER_VERSION\"" "$HELPER" 2>/dev/null; then
  TS=$(date +%Y%m%d-%H%M%S)
  cp "$HELPER" "$HELPER.legacy-$TS"
  write_helper
  say "检测到旧版/无版本记账助手——已备份为 $HELPER.legacy-$TS 并升级到 v$HELPER_VERSION"
  [ -f "$LEDGER" ] && printf 'accounting-helper-upgraded\tfrom=legacy-%s\tto=%s\tdt=%s\n' "$TS" "$HELPER_VERSION" "$(date -u +%FT%TZ)" >> "$LEDGER"
fi

# 助手调用包装（S2：失败显式）
helper() {
  python3 "$HELPER" "$@" || { say "记账助手失败（$*）——记 accounting-error，不假报成功";
    printf 'accounting-error\tcmd=%s\tdt=%s\n' "$1" "$(date -u +%FT%TZ)" >> "$LEDGER"; return 1; }
}

pid_start() {
  sed 's/.*) //' "/proc/$1/stat" 2>/dev/null | cut -d' ' -f20
}

# ── usage 解析（v4.5：分列 + cost JSON 数值 + modelUsage 存在性）──
U_IN=unknown; U_OUT=unknown; U_CR=unknown; U_CW=unknown; U_COST=unknown; U_MU=absent; U_CUM=""
parse_usage() {  # $1=引擎 stdout 文件 $2=当前SID（codex 累计差分用）
  U_IN=unknown; U_OUT=unknown; U_CR=unknown; U_CW=unknown; U_COST=unknown; U_MU=absent; U_CUM=""
  [ -f "$1" ] || return 0
  local u v
  u=$(python3 "$HELPER" usage-from "$1" "${2:-none}" "$ENGINE" 2>/dev/null || true)
  v=$(printf '%s\n' "$u" | grep -oE 'TOKENS_IN=[0-9]+' | cut -d= -f2); [ -n "$v" ] && U_IN="$v"
  v=$(printf '%s\n' "$u" | grep -oE 'TOKENS_OUT=[0-9]+' | cut -d= -f2); [ -n "$v" ] && U_OUT="$v"
  v=$(printf '%s\n' "$u" | grep -oE 'CACHE_READ=[0-9]+' | cut -d= -f2); [ -n "$v" ] && U_CR="$v"
  v=$(printf '%s\n' "$u" | grep -oE 'CACHE_WRITE=[0-9]+' | cut -d= -f2); [ -n "$v" ] && U_CW="$v"
  v=$(printf '%s\n' "$u" | grep -oE 'COST_USD=[0-9.eE+-]+' | cut -d= -f2); [ -n "$v" ] && U_COST="$v"
  v=$(printf '%s\n' "$u" | grep -oE 'MODELUSAGE=[a-z]+' | cut -d= -f2); [ -n "$v" ] && U_MU="$v"
  v=$(printf '%s\n' "$u" | sed -n 's/^CODEX_CUM=//p'); [ -n "$v" ] && U_CUM="$v"
  return 0
}
append_engine_logs() {  # 原始 stdout/stderr 全量入 campaign_log（服务失败也不丢）
  [ -n "${TD:-}" ] || return 0
  [ -f "$TD/engine-stdout.jsonl" ] && cat "$TD/engine-stdout.jsonl" >> campaign_log.txt
  [ -f "$TD/engine-stderr.log" ] && cat "$TD/engine-stderr.log" >> campaign_log.txt 2>/dev/null || true
  return 0
}

# ── R3：--recover 幂等结算孤儿轮（不调引擎；重复调用无副作用）──
if [ -n "$RECOVER_TD" ]; then
  TD="$RECOVER_TD"
  [ -d "$TD" ] || { echo "turn 目录不存在: $TD"; exit 64; }
  if [ -f "$TD/result.json" ]; then echo "already-settled"; exit 0; fi
  TURN_ID=$(python3 -c 'import json,sys
try: print(json.load(open(sys.argv[1]+"/manifest.json")).get("turn_id",""))
except Exception: print("")' "$TD" 2>/dev/null)
  [ -n "$TURN_ID" ] || TURN_ID=$(basename "$TD")
  OUT="$TD/engine-stdout.jsonl"; ERR="$TD/engine-stderr.log"
  NEW_SID=""
  if [ -f "$OUT" ] || [ -f "$ERR" ]; then
    NEW_SID=$(grep -h -oE '"session_id"[": ]+[a-f0-9-]{8,}' "$OUT" "$ERR" 2>/dev/null | head -1 | grep -oE '[a-f0-9]{8}-[a-f0-9-]+' \
           || grep -h -oE '"thread_id"[": ]+[a-f0-9-]{8,}' "$OUT" 2>/dev/null | head -1 | grep -oE '[a-f0-9]{8}-[a-f0-9-]+' \
           || grep -h -oiE 'session( id|_id)?: [a-f0-9-]{8,}' "$OUT" 2>/dev/null | head -1 | grep -oE '[a-f0-9]{8}-[a-f0-9-]+' \
           || true)
  fi
  # BUG-3：毒会话不复活——账本有该 SID 的 session-poisoned 事件则不收养
  if [ -n "$NEW_SID" ] && grep -q "\"session-poisoned\".*\"$NEW_SID\"" "$LEDGER" 2>/dev/null; then
    say "recover：SID $NEW_SID 有 session-poisoned 记录——不收养（防毒会话复活）"
    NEW_SID=""
  fi
  [ -n "$NEW_SID" ] && echo "$NEW_SID" > "$SID_FILE"
  helper init >/dev/null 2>&1 || true
  helper settle >/dev/null 2>&1 || say "recover：settle 失败（保留现场）"
  parse_usage "$OUT"
  # 墙钟：manifest.started_utc → 现在（尽力而为）
  RECOVER_WALL=$(python3 -c 'import json,sys,time
try:
    s = json.load(open(sys.argv[1]+"/manifest.json")).get("started_utc")
    import datetime
    d = datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ")
    print(max(0, int(time.time() - d.replace(tzinfo=datetime.timezone.utc).timestamp())))
except Exception: print(0)' "$TD" 2>/dev/null || echo 0)
  python3 "$HELPER" account "${RECOVER_WALL:-0}" "$U_IN" "$U_OUT" >/dev/null 2>&1 || true
  RT=$(mktemp)
  printf '{"type":"recovered-turn","turn_id":"%s","sid":"%s","tokens_in":"%s","tokens_out":"%s","ts":"%s"}\n' \
    "$TURN_ID" "${NEW_SID:-none}" "$U_IN" "$U_OUT" "$(date -u +%FT%TZ)" > "$RT"
  helper ledger-append < "$RT" || true; rm -f "$RT"
  append_engine_logs
  CAMPAIGN_STATE="" ; MEASURE_STATE="" ; ROOT_STATUS=""
  python3 - "$TD" "$TURN_ID" "$PROJ_DIR" "$U_IN" "$U_OUT" <<'PYRW'
import json, os, socket, sys, time
td, tid, proj, tin, tout = sys.argv[1:6]
obj = {"schema_version": 1, "schema": "lemvo-turn-result",
       "campaign_id": socket.gethostname() + ":" + os.path.basename(os.path.realpath(proj)),
       "turn_id": tid, "host": socket.gethostname(),
       "status": "recovered-orphan", "next_action": "continue", "engine_rc": None,
       "usage": {"tokens_in": tin if tin != "unknown" else None,
                 "tokens_out": tout if tout != "unknown" else None},
       "scientific_verdict": {}, "artifact_refs": [],
       "ended_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
p = os.path.join(td, "result.json")
tmp = p + ".tmp-%d" % os.getpid()
json.dump(obj, open(tmp, "w"), indent=1)
os.replace(tmp, p)
PYRW
  [ -f "$TD/engine.json" ] && python3 -c 'import json,sys
p=sys.argv[1]+"/engine.json"
try:
    d=json.load(open(p)); d["status"]="recovered"; json.dump(d,open(p,"w"),indent=1)
except Exception: pass' "$TD"
  # 若 turn-active 仍指向本 TD（adapter 亡），清掉
  if [ -f .autoloop-turn-active ] && grep -q "^$TURN_ID|" .autoloop-turn-active 2>/dev/null; then
    rm -f .autoloop-turn-active
  fi
  say "recover：孤儿轮 $TURN_ID 已幂等结算（status=recovered-orphan；sid=${NEW_SID:-none}）"
  exit 0
fi

# ── 0) 文件级阻塞标志（结构化模式映射 rc=6 + 原因行，不再伪装成功轮）──
for f in STOP HOLD CAMPAIGN-DONE BLOCKED-USER; do
  if [ -f "$f" ]; then
    say "标志 $f 存在——不调用引擎"
    if [ "$RC_MODE" = "structured" ]; then
      case "$f" in
        STOP) emit_reason "STOP_FILE";;
        HOLD) emit_reason "HOLD_FILE";;
        CAMPAIGN-DONE) emit_reason "CAMPAIGN_DONE_FILE";;
        BLOCKED-USER) emit_reason "BLOCKED_USER_FILE";;
      esac
      exit 6
    fi
    exit 0
  fi
done

# ── 0b) 预检（ZCode 联调缺陷 BUG-5/6：config 兼容与 git 身份——失败要响，不静默）──
if [ "$ENGINE" = "codex" ]; then
  CODEX_CFG="${CODEX_HOME:-$HOME/.codex}/config.toml"
  if [ -f "$CODEX_CFG" ]; then
    # BUG-5：service_tier="default" 在部分 0.16x 环境拒载、在另一些正常（实测
    # 分歧）——预检只警告；真失败由 exec 错误文本精确识别（见 §7 分类）。
    grep -qE '^service_tier\s*=\s*"default"' "$CODEX_CFG" 2>/dev/null && \
      say "预检提示：config 含 service_tier=\"default\"——部分 codex 0.16x 环境拒载该值（若 exec 报 unknown variant 请注释或改 fast/flex）"
    # TOML 节错位：approval_policy/sandbox_mode 落在 [mcp_servers.*] 等节内会被 codex 忽略
    MISPLACED=$(awk '/^\[/{sec=$0} /^[[:space:]]*(approval_policy|sandbox_mode)[[:space:]]*=/ && sec != "" && sec !~ /^\[(tui|notice|features|projects)/ {print NR": "sec" -> "$0}' "$CODEX_CFG" 2>/dev/null | head -3)
    [ -n "$MISPLACED" ] && say "预检警告：codex config 键落在 TOML 节内将被忽略（应放文件顶部顶层）：$MISPLACED"
  fi
fi
if [ -d .git ]; then
  if ! git config user.email >/dev/null 2>&1; then
    if [ -n "$(git config --global user.email 2>/dev/null)" ]; then
      git config user.email "$(git config --global user.email)" && git config user.name "$(git config --global user.name 2>/dev/null || echo lemvo)" \
        && say "预检：git 身份缺失——已从 global 复制到本仓（repo-local）"
    else
      say "预检失败：本目录是 git 仓库但无 user.email（提交会静默失败）——设 global 或 repo-local 身份后重跑"
      emit_reason "CONFIG_INVALID"; exit 64
    fi
  fi
fi

# ── 1) 初始化 + 读取状态 ──
helper init >/dev/null || exit 4
DECISION_FILE=$(mktemp); helper prepare > "$DECISION_FILE" || { rm -f "$DECISION_FILE"; exit 4; }
. "$DECISION_FILE"; rm -f "$DECISION_FILE"

# ── 2) 阻塞与等待（C6：结构化 reason 由控制器按原因转状态）──
BLOCK_CHECK=$(mktemp); helper block-check > "$BLOCK_CHECK" || { rm -f "$BLOCK_CHECK"; exit 4; }
. "$BLOCK_CHECK"; rm -f "$BLOCK_CHECK"
if [ "$BLOCKING" != "none" ]; then
  say "阻塞态 $BLOCKING（ACTIVE 不得绕过）——不调用引擎"
  if [ "$RC_MODE" = "structured" ]; then emit_reason "$BLOCKING"; exit 6; fi
  exit 0
fi
if [ "$WAITING" = "yes" ]; then
  say "WAITING_RESOURCE——减频等待"
  if [ "$RC_MODE" = "structured" ]; then emit_reason "WAITING_RESOURCE"; exit 7; fi
  sleep 600; exec "$0" "$@"
fi
# 文件级标志在结构化模式下同样映射 rc=6（读态后再查一次）
for f in STOP HOLD CAMPAIGN-DONE BLOCKED-USER; do
  if [ "$RC_MODE" = "structured" ] && [ -f "$f" ]; then
    say "标志 $f——blocked"
    case "$f" in
      STOP) emit_reason "STOP_FILE";;
      HOLD) emit_reason "HOLD_FILE";;
      CAMPAIGN-DONE) emit_reason "CAMPAIGN_DONE_FILE";;
      BLOCKED-USER) emit_reason "BLOCKED_USER_FILE";;
    esac
    exit 6
  fi
done

# ── 3) 准备停滞预算 + 墙钟预算（浮点）──
EFFECTIVE_CAP="$PREP_CAP"
LAST_CAP=$(python3 "$HELPER" last-authorize-cap 2>/dev/null)
case "$LAST_CAP" in ''|*[!0-9]*) ;; *) EFFECTIVE_CAP="$LAST_CAP" ;; esac
over_budget() {
  say "$1 —— 明确决策点（账本只追加，无清零后门）"
  emit_reason "OVER_BUDGET"
  cat <<MSG
超预算决策选项（任选其一，全部留痕）：
  A) 继续：AUTHORIZE_JUSTIFICATION='<新增益预期>' 重跑本脚本（上限 +10，记 authorize 事件）
  B) 收窄：修改 $STATE 的 scope/ready_blocker 后，AUTHORIZE_JUSTIFICATION='<收窄说明>' 重跑
  C) 终止：touch CAMPAIGN-DONE（交付当前状态；准备耗尽 != 方向被证伪，记 untested）
  D) 用户显式重置：编辑 $STATE 对应计数字段（须在 research-log 留一句重置理由）
MSG
  OB=$(mktemp)
  python3 - "$PREP_TURNS" "$EFFECTIVE_CAP" "$WALL_MIN" "$2" <<'PYOB' > "$OB"
import json, sys, datetime
print(json.dumps({"type":"over-budget","kind":sys.argv[4],"turns":int(sys.argv[1]),
 "cap":int(sys.argv[2]),"wall_min":float(sys.argv[3]),
 "ts":datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")}))
PYOB
  helper ledger-append < "$OB" || true; rm -f "$OB"
  exit 2
}
if [ "$PREP_TURNS" -ge "$EFFECTIVE_CAP" ]; then
  if [ -n "${AUTHORIZE_JUSTIFICATION:-}" ]; then
    AUTH=$(mktemp)
    python3 "$HELPER" auth-json "$((EFFECTIVE_CAP + 10))" "$(date -u +%FT%TZ)" > "$AUTH" || { rm -f "$AUTH"; say "授权序列化失败"; emit_reason "ACCOUNTING"; exit 5; }
    if helper ledger-append < "$AUTH"; then
      say "授权续行已记账（上限 $EFFECTIVE_CAP → $((EFFECTIVE_CAP + 10))）：$AUTHORIZE_JUSTIFICATION"
    else
      rm -f "$AUTH"; say "授权记账失败——拒绝在无账状态下超预算执行"; emit_reason "ACCOUNTING"; exit 5
    fi
    rm -f "$AUTH"
  else
    over_budget "准备停滞 $PREP_TURNS 轮 ≥ 上限 $EFFECTIVE_CAP" "prep-stall"
  fi
fi
if [ "$PREP_WALL_CAP" != "0" ]; then
  WALL_OK=$(python3 -c "import sys; print('yes' if float(sys.argv[1]) >= float(sys.argv[2]) else 'no')" "${WALL_MIN:-0}" "$PREP_WALL_CAP" 2>/dev/null || echo no)
  [ "$WALL_OK" = "yes" ] && over_budget "累计墙钟 ${WALL_MIN}min ≥ 上限 ${PREP_WALL_CAP}min" "wall-time"
fi

# ── 3b) 金额总预算派发前闸门（F2：unit=usd 且 used>=cap → 零调用决策点）──
BG=$(python3 "$HELPER" budget-gate 2>/dev/null || true)
BUDGET_EXHAUSTED=$(printf '%s\n' "$BG" | sed -n 's/^BUDGET_EXHAUSTED=//p')
BUDGET_USED_NOW=$(printf '%s\n' "$BG" | sed -n 's/^BUDGET_USED=//p')
BUDGET_CAP_NOW=$(printf '%s\n' "$BG" | sed -n 's/^BUDGET_CAP=//p')
if [ "$BUDGET_EXHAUSTED" = "yes" ]; then
  say "金额预算耗尽：budget.used=${BUDGET_USED_NOW} ≥ total_cap=${BUDGET_CAP_NOW}（usd）——派发前拦截，本轮不调用引擎"
  emit_reason "OVER_BUDGET"
  cat <<MSG
金额预算决策选项（任选其一，全部留痕）：
  A) 用户提高上限：编辑 $STATE 的 budget.total_cap（须来源可核对），记 authorize 事件后重跑
  B) 收窄：修改 scope/ready_blocker 后重跑
  C) 终止：touch CAMPAIGN-DONE（交付当前状态；预算耗尽 ≠ 方向被证伪，记 untested）
  D) 查明细：budget.used 由账本逐轮累计（口径见 cost_basis 字段；负 delta=基线重置已按 reported 计）
MSG
  OB=$(mktemp)
  python3 - "$BUDGET_USED_NOW" "$BUDGET_CAP_NOW" <<'PYOB' > "$OB"
import json, sys, datetime
print(json.dumps({"type":"over-budget","kind":"budget-exhausted","used":sys.argv[1],
 "cap":sys.argv[2],"ts":datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")}))
PYOB
  helper ledger-append < "$OB" || true; rm -f "$OB"
  exit 2
fi

# ── 4) 消息准备 ──
if [ -n "$CALLER_MSG" ]; then
  [ -f "$CALLER_MSG" ] || { say "调用者消息文件不存在: $CALLER_MSG"; exit 64; }
  MSG_SOURCE="$CALLER_MSG"; MSG_OWNED=no
elif [ ! -f "$STATE" ]; then
  # BUG-1：全新战役目录无 STATE——给 bootstrap 指令而非 READY 循环模板
  #（否则引擎只能"写 blocker 并结束"，造成无限礼貌空转）。
  cat > "$OWN_SUMMARY" <<'MSG'
【首次启动：bootstrap】这是新战役目录，没有 research-state.yaml。
本轮任务（按序完成，不要只写 blocker 等待）：
1. 读 CHARTER.md / 任务书 / prompt（若存在）提取研究目标与边界；
2. 按 research-orchestrator 模板创建 research-state.yaml（project/prep/total/
   budget/research_plan 区，目标与约束来源如实标注）；
3. 创建 findings.md 与 research-log.md（模板结构即可）；
4. 开始第一项有产物的实际工作（文献摸底、代码脚手架或最小实验），
   把产物路径写进 research-log.md。
若连 CHARTER/任务书都不存在：写明"缺任务定义"并落 BLOCKED-USER 文件（含需要
用户提供的具体内容），不要空转。
MSG
  MSG_SOURCE="$OWN_SUMMARY"; MSG_OWNED=yes
else
  # F4：stage/version 按 research_plan 层级读取（helper plan-info），不做全文件
  # 首个同名匹配；计划改版后（version 变化）刷新摘要，不沿用过期阶段。
  PL_STAGE="none"; PL_VER="none"
  PI=$(python3 "$HELPER" plan-info 2>/dev/null || true)
  PL_STAGE=$(printf '%s\n' "$PI" | sed -n 's/^PLAN_STAGE=//p'); PL_STAGE=${PL_STAGE:-none}
  PL_VER=$(printf '%s\n' "$PI" | sed -n 's/^PLAN_VERSION=//p'); PL_VER=${PL_VER:-none}
  if [ -s "$OWN_SUMMARY" ] && ! grep -q "plan_version=$PL_VER" "$OWN_SUMMARY" 2>/dev/null; then
    rm -f "$OWN_SUMMARY"   # 版本已变——摘要过期，重建
  fi
  if [ ! -s "$OWN_SUMMARY" ]; then
    cat > "$OWN_SUMMARY" <<MSG
状态摘要（冷启动恢复）：
- 研究目标：$(grep -m1 'objective\|question' "$STATE" 2>/dev/null | cut -d: -f2- || echo 见 STATE)
- 已消耗：total_turns=$(grep -A3 '^total:' "$STATE" 2>/dev/null | grep total_turns | grep -oE '[0-9]+' | head -1) wall=$(grep 'total_wall_time_min' "$STATE" | cut -d: -f2)
- 计划阶段/版本：stage=$PL_STAGE plan_version=$PL_VER（缺 research_plan 区=旧项目，照常运行）
- 约束治理：修订任何 research_plan 约束 = version+1 + research-log.md 写 plan-change（触发/调整哪条/权限依据/旧证据保留范围/下一实验/成本止损/是否需批准）；规则全文见 research-orchestrator 的 constraint-governance.md
- 上次有效进展：$(grep 'last_informative_observation' "$STATE" 2>/dev/null | cut -d: -f2-)
- 下一最小 probe：$(grep 'next_min_probe' "$STATE" 2>/dev/null | cut -d: -f2-)
按 READY_TO_PROBE 判据推进；不满足则写 ready_blocker（含解除条件），不做泛化审计。
MSG
  fi
  MSG_SOURCE="$OWN_SUMMARY"; MSG_OWNED=yes
fi

# ── R2/R3：turn 目录 + manifest（控制器给 LEMVO_TURN_DIR 则用之，否则自建）──
TD="${LEMVO_TURN_DIR:-}"
TURN_ID="${LEMVO_TURN_ID:-}"
if [ -z "$TD" ]; then
  TURN_ID="t$(date -u +%Y%m%dT%H%M%SZ)-$$-solo"
  TD="turns/$TURN_ID"
  mkdir -p "$TD"
  python3 - "$TD" "$TURN_ID" "$PROJ_DIR" "$ENGINE" <<'PYMS'
import json, os, socket, sys, time
td, tid, proj, eng = sys.argv[1:5]
man = {"schema_version": 1, "schema": "lemvo-turn-manifest",
       "campaign_id": socket.gethostname() + ":" + os.path.basename(os.path.realpath(proj)),
       "turn_id": tid, "host": socket.gethostname(), "controller_pid": None,
       "engine": eng, "status": "dispatched",
       "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
json.dump(man, open(os.path.join(td, "manifest.json"), "w"), indent=1)
PYMS
fi
mkdir -p "$TD"
ENGINE_JSON="$TD/engine.json"
ADAPTER_START=$(pid_start $$)
python3 - "$ENGINE_JSON" "$$" "$ADAPTER_START" "$ENGINE" <<'PYE'
import json, sys
p, apid, astart, eng = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]
d = {"adapter_pid": apid, "adapter_start": astart, "engine": eng, "status": "running"}
json.dump(d, open(p, "w"), indent=1)
PYE
echo "$TURN_ID|$$|$ADAPTER_START" > .autoloop-turn-active
touch "$TD/.turn-start"   # BUG-3：本轮产物判定的 mtime 基准
trap 'rm -f .autoloop-turn-active .autoloop-engine.pid' EXIT

# ── result.json（E-result：结果驱动闭环的每轮凭据）──
write_result() {  # $1=status $2=next_action $3=engine_rc
  python3 - "$1" "$2" "$3" "$TD" "$TURN_ID" "$PROJ_DIR" "${CAMPAIGN_STATE:-}" "${MEASURE_STATE:-}" "${ROOT_STATUS:-}" <<'PYRJ'
import json, os, socket, sys, time
status, nxt, rcv, td, tid, proj, cs, ms, rs = sys.argv[1:10]
art = os.environ.get("T_ART", "none")
obj = {"schema_version": 1, "schema": "lemvo-turn-result",
       "campaign_id": socket.gethostname() + ":" + os.path.basename(os.path.realpath(proj)),
       "turn_id": tid, "host": socket.gethostname(),
       "status": status, "next_action": nxt,
       "engine_rc": int(rcv) if rcv not in ("", "None") else None,
       "scientific_verdict": {"campaign_status": cs, "measurement_state": ms, "root_status": rs},
       "artifact_refs": [art] if art and art != "none" else [],
       "ended_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
p = os.path.join(td, "result.json")
if not os.path.exists(p):   # 幂等：已有结果不覆盖（recover 场景）
    tmp = p + ".tmp-%d" % os.getpid()
    json.dump(obj, open(tmp, "w"), indent=1)
    os.replace(tmp, p)
PYRJ
}
finalize_engine_json() {  # $1=最终 status
  [ -f "$ENGINE_JSON" ] && python3 - "$ENGINE_JSON" "$1" <<'PYEF'
import json, sys
d = json.load(open(sys.argv[1])); d["status"] = sys.argv[2]
json.dump(d, open(sys.argv[1], "w"), indent=1)
PYEF
  return 0
}

# ── 5) 真续接调用（双引擎；输出入 turn 目录；PID+starttime 身份发布）──
run_engine() {
  local engpid engstart
  if [ "$ENGINE" = "claude" ]; then
    if [ -s "$SID_FILE" ]; then
      SID="$(cat "$SID_FILE")"
      "$CLAUDE_BIN" -p "$(cat "$MSG_SOURCE")" --resume "$SID" --output-format json $CLAUDE_PERMS > "$TD/engine-stdout.jsonl" 2> "$TD/engine-stderr.log" &
    else
      "$CLAUDE_BIN" -p "$(cat "$MSG_SOURCE")" --output-format json $CLAUDE_PERMS > "$TD/engine-stdout.jsonl" 2> "$TD/engine-stderr.log" &
    fi
  else
    if [ -s "$SID_FILE" ]; then
      SID="$(cat "$SID_FILE")"
      codex exec --skip-git-repo-check --json -s "$CODEX_SANDBOX" resume "$SID" "$(cat "$MSG_SOURCE")" < /dev/null > "$TD/engine-stdout.jsonl" 2> "$TD/engine-stderr.log" &
    else
      codex exec --skip-git-repo-check --json -s "$CODEX_SANDBOX" "$(cat "$MSG_SOURCE")" < /dev/null > "$TD/engine-stdout.jsonl" 2> "$TD/engine-stderr.log" &
    fi
  fi
  engpid=$!
  engstart=$(pid_start "$engpid")
  echo "$engpid ${engstart:-unknown}" > .autoloop-engine.pid
  python3 - "$ENGINE_JSON" "$engpid" "${engstart:-unknown}" <<'PYEP'
import json, sys
d = json.load(open(sys.argv[1]))
d["pid_engine"] = int(sys.argv[2]); d["pid_engine_start"] = sys.argv[3]
json.dump(d, open(sys.argv[1], "w"), indent=1)
PYEP
  wait $engpid; rc=$?
  rm -f .autoloop-engine.pid
}
CLAUDE_PERMS="${CLAUDE_PERMS_EXTRA:-}"   # 窄白名单入口（如 --allowedTools ...）；不受信任目录下项目 settings 会被忽略，CLI 级白名单不受影响
[ "${CLAUDE_SKIP_PERMS:-0}" = "1" ] && CLAUDE_PERMS="--dangerously-skip-permissions"
CODEX_SANDBOX="${CODEX_SANDBOX:-workspace-write}"   # BUG-2：0.16x exec 默认只读+需审批；显式给可写工作区（campaign 目录=cwd）；不要用 danger-full-access 做全局默认
run_engine

# BUG-3：产物判定在引擎结束后立即做（settle/account 的账本书写不算研究产物；
# 引擎对 research-state.yaml 的真实编辑算产物）
NEW_ARTIFACT=""
[ -f "$TD/.turn-start" ] && NEW_ARTIFACT=$(find . -maxdepth 2 -type f -newer "$TD/.turn-start" \
  -not -path "./.*" -not -path "./turns/*" 2>/dev/null | head -1)

# ── 6) 会话 ID 提取（claude session_id | codex thread_id | 文本三拼写）──
NEW_SID=$( grep -h -oE '"session_id"[": ]+[a-f0-9-]{8,}' "$TD/engine-stdout.jsonl" "$TD/engine-stderr.log" 2>/dev/null | head -1 | grep -oE '[a-f0-9]{8}-[a-f0-9-]+' \
        || grep -h -oE '"thread_id"[": ]+[a-f0-9-]{8,}' "$TD/engine-stdout.jsonl" 2>/dev/null | head -1 | grep -oE '[a-f0-9]{8}-[a-f0-9-]+' \
        || grep -h -oiE 'session( id|_id)?: [a-f0-9-]{8,}' "$TD/engine-stdout.jsonl" 2>/dev/null | head -1 | grep -oE '[a-f0-9]{8}-[a-f0-9-]+' \
        || true )
[ -n "$NEW_SID" ] && { [ "$NEW_SID" != "$(cat "$SID_FILE" 2>/dev/null || true)" ] && echo "$NEW_SID" > "$SID_FILE"; }
CUR_SID="$(cat "$SID_FILE" 2>/dev/null || true)"

# ── 7) 失败分类（A1 收口：服务/账号错误 ≠ 会话死亡 ≠ 研究停滞）──
if [ "$rc" -ne 0 ]; then
  # BUG-5：config 拒载优先于服务分类（unknown variant / unrecognized setting 致 exec 未起）
  if grep -qiE "unknown variant|unrecognized configuration|invalid.*config|failed to (parse|load).*config" "$TD/engine-stdout.jsonl" "$TD/engine-stderr.log" 2>/dev/null; then
    say "配置类错误（codex config 拒载/无效键）——不修引擎不改状态，修 config 后重跑"
    parse_usage "$TD/engine-stdout.jsonl" "${CUR_SID:-none}"
    finalize_engine_json "config-error"
    write_result "failed" "needs_user" "$rc"
    emit_reason "CONFIG_INVALID"
    exit 64
  fi
  if grep -qiE 'rate.?limit|quota|billing|payment|429|500|502|503|504|401|403|unauthorized|forbidden|internal server error|overloaded|service unavailable|temporarily|timed.?out|timeout|ECONNRESET|network' "$TD/engine-stdout.jsonl" "$TD/engine-stderr.log" 2>/dev/null; then
    say "服务/账号类错误（限流/认证/基础设施）——保留 SID，不计 resume-fail；计量尽力而为"
    parse_usage "$TD/engine-stdout.jsonl" "${CUR_SID:-none}"
    python3 "$HELPER" account "$(( $(date +%s) - TURN_START ))" "$U_IN" "$U_OUT" >/dev/null 2>&1 || true
    SE=$(mktemp)
    printf '{"type":"service-error","turn_id":"%s","sid":"%s","rc":%d,"tokens_in":"%s","tokens_out":"%s","cache_read":"%s","cache_write":"%s","cost_usd_est":"%s","ts":"%s"}\n' \
      "$TURN_ID" "${CUR_SID:-none}" "$rc" "$U_IN" "$U_OUT" "$U_CR" "$U_CW" "$U_COST" "$(date -u +%FT%TZ)" > "$SE"
    helper ledger-append < "$SE" || true; rm -f "$SE"
    append_engine_logs   # v4.5：服务失败也不丢原始 stdout/stderr
    finalize_engine_json "service-error"
    write_result "service-error" "runtime_error" "$rc"
    emit_reason "SERVICE"
    [ "$RC_MODE" = "structured" ] && exit 8
    exit "$rc"
  fi
  OUTCOME=$(python3 "$HELPER" outcome-from "$TD/engine-stdout.jsonl" 2>/dev/null | grep -oE 'OUTCOME=\w+' | cut -d= -f2)
  if [ "$OUTCOME" = "failed" ]; then
    EE=$(mktemp)
    printf '{"type":"engine-error","turn_id":"%s","sid":"%s","kind":"structured","rc":%d,"ts":"%s"}\n' \
      "$TURN_ID" "${CUR_SID:-none}" "$rc" "$(date -u +%FT%TZ)" > "$EE"
    helper ledger-append < "$EE" || true; rm -f "$EE"
  fi
  FAILS=$(python3 "$HELPER" resume-fails "$(cat "$SID_FILE" 2>/dev/null || echo none)" 2>/dev/null || true)
  case "$FAILS" in ''|*[!0-9]*) FAILS=0 ;; esac
  if [ -n "${SID:-}" ] && [ "$FAILS" -ge "$RESUME_FAIL_THRESHOLD" ]; then
    if grep -qiE 'session.*not.*(found|exist)|unknown conversation|no conversation|expired' "$TD/engine-stdout.jsonl" "$TD/engine-stderr.log" 2>/dev/null; then
      say "会话确认不存在——冷启动恢复（清 SID，保留消息源）"
      emit_reason "RESUME_RECOVERED"   # 控制器据此不计连续失败，下一轮冷启动
      rm -f "$SID_FILE"
      RF=$(mktemp); printf '{"type":"resume-recovery","turn_id":"%s","reason":"session-not-found","sid":"%s","ts":"%s"}\n' "$TURN_ID" "$SID" "$(date -u +%FT%TZ)" > "$RF"
      helper ledger-append < "$RF" || true; rm -f "$RF"
      [ "$MSG_OWNED" = "yes" ] && rm -f "$OWN_SUMMARY"
    else
      say "resume 连续失败 $FAILS 次但非会话死亡证据——保留 SID 记 fail 待查"
      RF=$(mktemp); printf '{"type":"resume-fail","turn_id":"%s","sid":"%s","rc":%d,"ts":"%s"}\n' "$TURN_ID" "$SID" "$rc" "$(date -u +%FT%TZ)" > "$RF"
      helper ledger-append < "$RF" || true; rm -f "$RF"
    fi
  else
    RF=$(mktemp); printf '{"type":"resume-fail","turn_id":"%s","sid":"%s","rc":%d,"ts":"%s"}\n' "$TURN_ID" "${SID:-none}" "$rc" "$(date -u +%FT%TZ)" > "$RF"
    helper ledger-append < "$RF" || true; rm -f "$RF"
  fi
else
  OUTCOME=$(python3 "$HELPER" outcome-from "$TD/engine-stdout.jsonl" 2>/dev/null | grep -oE 'OUTCOME=\w+' | cut -d= -f2)
  if [ "$OUTCOME" = "failed" ]; then
    # E：引擎自报失败（结构化错误事件）——rc=0 也判失败，不写 resume-ok
    say "引擎结构化失败（rc=0 但结果事件为 error）——记 engine-error，保持 SID"
    EE=$(mktemp)
    printf '{"type":"engine-error","turn_id":"%s","sid":"%s","kind":"structured-rc0","rc":0,"ts":"%s"}\n' \
      "$TURN_ID" "${CUR_SID:-none}" "$(date -u +%FT%TZ)" > "$EE"
    helper ledger-append < "$EE" || true; rm -f "$EE"
    append_engine_logs
    finalize_engine_json "protocol-error"
    write_result "protocol-error" "runtime_error" 0
    emit_reason "PROTOCOL"
    if [ "$RC_MODE" = "structured" ]; then exit 9; fi
    exit 1
  fi
  if [ -n "$CUR_SID" ]; then   # v4.3d：无真实 SID 不写 resume-ok
    OK=$(mktemp); printf '{"type":"resume-ok","turn_id":"%s","sid":"%s","ts":"%s"}\n' "$TURN_ID" "$CUR_SID" "$(date -u +%FT%TZ)" > "$OK"
    helper ledger-append < "$OK" || true; rm -f "$OK"
  else
    # E-sid：冷启动成功必须取得会话身份——缺失记协议警告（软信号，控制器 3 连计后停）
    PW=$(mktemp)
    printf '{"type":"protocol-warning","turn_id":"%s","kind":"cold-start-no-session-id","ts":"%s"}\n' "$TURN_ID" "$(date -u +%FT%TZ)" > "$PW"
    helper ledger-append < "$PW" || true; rm -f "$PW"
    say "协议警告：冷启动成功但未取得会话身份——已记账，resume-ok 不写"
    say "LEMVO_RC_REASON=PROTOCOL_NO_SID"
  fi
fi

# ── 8) 结算（凭据类型化 + 真账 + usage 分列 + 费用 delta 口径）──
SETTLE=$(mktemp); helper settle > "$SETTLE" || { rm -f "$SETTLE"; say "结算失败——保留输出，账未更新"; }
. "$SETTLE" 2>/dev/null || true; rm -f "$SETTLE"
parse_usage "$TD/engine-stdout.jsonl" "${CUR_SID:-none}"
TURN_ELAPSED=$(( $(date +%s) - TURN_START ))
python3 "$HELPER" account "$TURN_ELAPSED" "$U_IN" "$U_OUT" >/dev/null \
  || printf 'accounting-error\tcmd=account\tdt=%s\n' "$(date -u +%FT%TZ)" >> "$LEDGER"
# 费用口径：reported=引擎报告值；delta=同 SID 对上次报告求差（claude resume 累计量）；
# codex 每调独立（per-call）。不把累计值逐轮相加，state total_cost 保持不累计。
COST_DELTA=unknown; COST_BASIS=unknown
if [ "$U_COST" != "unknown" ]; then
  CD_OUT=$(python3 "$HELPER" cost-delta "$(cat "$SID_FILE" 2>/dev/null || echo none)" "$U_COST" "$ENGINE" 2>/dev/null || true)
  COST_DELTA=$(printf '%s\n' "$CD_OUT" | sed -n 's/^COST_DELTA=//p')
  COST_BASIS=$(printf '%s\n' "$CD_OUT" | sed -n 's/^COST_BASIS=//p')
  [ -z "$COST_DELTA" ] && COST_DELTA=unknown
  [ -z "$COST_BASIS" ] && COST_BASIS=unknown
fi
PREP_SNAP=$(python3 "$HELPER" prep-snapshot 2>/dev/null || true)
T_ART=$(printf '%s\n' "$PREP_SNAP" | grep -oE 'LAST_ARTIFACT=.*' | cut -d= -f2-)
[ -z "$T_ART" ] && T_ART=none
export T_ART
SESSION_TAG=$(cat "$SID_FILE" 2>/dev/null || echo none)
TURN_F=$(mktemp)
T_TYPE="work-turn"; [ "${NEW_PROGRESS:-no}" != "yes" ] && T_TYPE="prep-turn"
T_PREP="${PREP_TURNS_NOW:-unknown}"; T_TOTAL="${TOTAL_TURNS_NOW:-unknown}"
T_SCOPE="client-estimate;modelUsage=$U_MU;engine=$ENGINE"
T_TYPE="$T_TYPE" T_PREP="$T_PREP" T_TOTAL="$T_TOTAL" T_IN="$U_IN" T_OUT="$U_OUT" \
T_CR="$U_CR" T_CW="$U_CW" T_COST="$U_COST" T_DELTA="$COST_DELTA" T_BASIS="$COST_BASIS" T_CUM="$U_CUM" \
T_SCOPE="$T_SCOPE" T_WALL="$TURN_ELAPSED" T_SID="$SESSION_TAG" T_TURNID="$TURN_ID" T_ART="$T_ART" \
python3 - <<'PYTURN' > "$TURN_F"
import json, os, datetime
e = os.environ
def num(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        try:
            return float(v)   # 1e-05 等科学计数费用保真，不截断
        except (TypeError, ValueError):
            return v
def iv(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return v
obj = {"type": e["T_TYPE"], "turn_id": e["T_TURNID"], "sid": e["T_SID"],
       "prep_turns": iv(e["T_PREP"]), "total_turns": iv(e["T_TOTAL"]),
       "tokens_in": num(e["T_IN"]), "tokens_out": num(e["T_OUT"]),
       "cache_read": num(e["T_CR"]), "cache_write": num(e["T_CW"]),
       "cost_usd_reported": num(e["T_COST"]), "cost_usd_delta": num(e["T_DELTA"]),
       "cost_basis": e["T_BASIS"], "cost_scope": e["T_SCOPE"],
       "codex_cum": (json.loads(e["T_CUM"]) if e.get("T_CUM") else None),
       "wall_sec": iv(e["T_WALL"]), "wall_scope": "engine-call",
       "artifact_ref": e["T_ART"],
       "ts": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")}
print(json.dumps(obj, ensure_ascii=False))
PYTURN
helper ledger-append < "$TURN_F" || true; rm -f "$TURN_F"

# F2：金额总账累计（单一权威 budget 区；负 delta=基线重置按 reported 计；unknown 不加）
if [ "$COST_DELTA" != "unknown" ]; then
  BINC="$COST_DELTA"
  case "$(python3 -c "import sys; sys.exit(0 if float(sys.argv[1]) < 0 else 1)" "$COST_DELTA" 2>/dev/null; echo $?)" in
    0) BINC="$U_COST";;   # 负 delta = 上报基线重置（如 claude 版本口径差异），按本轮 reported 计
  esac
  [ "$BINC" != "unknown" ] && python3 "$HELPER" budget-add "$BINC" >/dev/null 2>&1 || true
fi

append_engine_logs
say "本轮 rc=$rc；NEW_PROGRESS=${NEW_PROGRESS:-?}（类型化凭据=${ARTIFACT_TYPED:-?}）；prep=${PREP_TURNS_NOW:-?}/$EFFECTIVE_CAP；total_turns=${TOTAL_TURNS_NOW:-?}；tokens +${U_IN}/+${U_OUT} cache_r=${U_CR} cache_w=${U_CW} cost=${U_COST}(${COST_BASIS},Δ=${COST_DELTA}) modelUsage=${U_MU}；turn=$TURN_ID；session=$SESSION_TAG"

# ── BUG-3：空转/毒会话检测（引擎 rc=0 也会空转——礼貌 loop 无产物无进展）──
# NEW_ARTIFACT 已在引擎结束时判定（不含账本/控制器写入）
if [ -z "$NEW_ARTIFACT" ]; then
  STALL_N=$(( $(cat .autoloop-stall-count 2>/dev/null || echo 0) + 1 )); echo "$STALL_N" > .autoloop-stall-count
else
  echo 0 > .autoloop-stall-count; echo 0 > .autoloop-poison-count
fi
# 毒会话：连续 N 轮输出含阻塞话术且零产物 → 清 SID 强制新会话（避免 resume
# 复读污染上下文）；短语可经 LEMVO_POISON_PHRASES 覆盖（| 分隔的 grep -E 模式）
POISON_RE="${LEMVO_POISON_PHRASES:-只读|read.only|READY_TO_PROBE.*不满足|ready_blocker}"
if [ -z "$NEW_ARTIFACT" ] && grep -qiE "$POISON_RE" "$TD/engine-stdout.jsonl" 2>/dev/null; then
  POISON_N=$(( $(cat .autoloop-poison-count 2>/dev/null || echo 0) + 1 )); echo "$POISON_N" > .autoloop-poison-count
  if [ "$POISON_N" -ge "${LEMVO_POISON_N:-3}" ]; then
    OLD_SID=$(cat "$SID_FILE" 2>/dev/null || true)
    rm -f "$SID_FILE"
    PS=$(mktemp); printf '{"type":"session-poisoned","turn_id":"%s","sid":"%s","turns":%d,"ts":"%s"}\n' \
      "$TURN_ID" "${OLD_SID:-none}" "$POISON_N" "$(date -u +%FT%TZ)" > "$PS"
    helper ledger-append < "$PS" || true; rm -f "$PS"
    say "会话疑似污染（连续 $POISON_N 轮阻塞话术+零产物）——已清 SID，下一轮全新会话"
  fi
fi
# 零产物空转兜底闸门：连续 N 轮（默认 5）无任何项目产物 → 明确决策点，不礼貌空转
if [ -z "$NEW_ARTIFACT" ] && [ "$STALL_N" -ge "${LEMVO_STALL_ARTIFACT_N:-5}" ]; then
  say "连续 $STALL_N 轮零产物（引擎 rc=0 但无建设输出）——空转停滞，转决策点待人查"
  emit_reason "STALL_NO_ARTIFACT"
  SB=$(mktemp); printf '{"type":"stall-no-artifact","turn_id":"%s","turns":%d,"ts":"%s"}\n' \
    "$TURN_ID" "$STALL_N" "$(date -u +%FT%TZ)" > "$SB"
  helper ledger-append < "$SB" || true; rm -f "$SB"
  RESULT_STATUS="failed"; RESULT_NEXT="needs_user"
  finalize_engine_json "$RESULT_STATUS"
  write_result "$RESULT_STATUS" "$RESULT_NEXT" "$rc"
  exit 2
fi

RESULT_STATUS="succeeded"; RESULT_NEXT="continue"
if [ "$rc" -ne 0 ]; then
  RESULT_STATUS="failed"; RESULT_NEXT="runtime_error"
fi
finalize_engine_json "$RESULT_STATUS"
write_result "$RESULT_STATUS" "$RESULT_NEXT" "$rc"
exit $rc
