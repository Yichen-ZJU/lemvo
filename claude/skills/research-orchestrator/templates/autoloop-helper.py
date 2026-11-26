#!/usr/bin/env python3
"""autoloop accounting and turn helper. Python 3.6 compatible; standard library only."""
HELPER_VERSION = globals().get("HELPER_VERSION", "5.0")
if "CONTRACT_PATH" not in globals():
    import os as _os
    CONTRACT_PATH = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "runtime-contract.py")
import fnmatch, hashlib, json, math, os, re, sys, subprocess, tempfile

STATE = "research-state.yaml"
PREP_DEFAULTS = [("prep_turns", "0"), ("prep_wall_time_min", "0.0"), ("prep_cost", "null"),
                 ("last_informative_observation", "none"), ("last_seen_observation", "none"),
                 ("last_informative_artifact", "none"), ("last_artifact_sha", "none"),
                 ("ready_blocker", "none"), ("next_min_probe", "none"), ("session_id", "null")]
TOTAL_DEFAULTS = [("total_turns", "0"), ("total_wall_time_min", "0.0"), ("total_cost", "null"),
                  ("total_tokens_in", "0"), ("total_tokens_out", "0"),
                  ("cost_usd_total", "unknown")]
# R1：GPU 小时只统计不限制——独立于 budget 区，无任何门读取本区
GPU_DEFAULTS = [("used_hours", "unknown"), ("basis", "unknown"), ("source", "none"),
                ("migrated", "no")]
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

def _yaml_quote_closed(val, quote_char):
    i = 1
    n = len(val)
    while i < n:
        ch = val[i]
        if quote_char == "'":
            if ch == "'":
                if i + 1 < n and val[i + 1] == "'":
                    i += 2
                    continue
                return True
        else:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                return True
        i += 1
    return False

def _clean_multiline(text, quote_char=None):
    text = text.strip()
    if quote_char == "'" and len(text) >= 2 and text[0] == "'" and text[-1] == "'":
        text = text[1:-1].replace("''", "'")
    elif quote_char == '"' and len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        text = text[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return " ".join(text.split())

def read_fields():
    f = {"campaign_status": "", "measurement_state": "", "root_status": "",
         "status": "", "active_role": ""}
    prep, total, project = {}, {}, {"status": ""}
    top = ""
    prep_keys = dict(PREP_DEFAULTS)
    total_keys = dict(TOTAL_DEFAULTS)
    try:
        raw_lines = open(STATE, encoding="utf-8", errors="replace").read().splitlines()
        idx, total_n = 0, len(raw_lines)
        while idx < total_n:
            raw = raw_lines[idx]
            idx += 1
            line = strip_inline_comment(raw)
            s = line.strip()
            if not s or s == "-":
                continue
            m = re.match(r"^(\s*)([A-Za-z_][\w-]*)\s*:\s*(.*)$", line)
            if not m:
                continue
            indent, key, val = len(m.group(1)), m.group(2), m.group(3).strip()
            if (val.startswith("'") and not _yaml_quote_closed(val, "'")) or (val.startswith('"') and not _yaml_quote_closed(val, '"')):
                qc = val[0]
                buf = [val]
                while idx < total_n:
                    nxt = raw_lines[idx]
                    nxt_strip = nxt.strip()
                    nxt_indent = len(nxt) - len(nxt.lstrip(" "))
                    if nxt_strip and nxt_indent <= indent and re.match(r"^([A-Za-z_][\w-]*)\s*:", nxt_strip):
                        break
                    idx += 1
                    buf.append(nxt_strip)
                    if _yaml_quote_closed(" ".join(buf), qc):
                        break
                resolved = _clean_multiline(" ".join(buf), qc)
            elif re.match(r"^[|>][+-]?\d*$", val):
                buf = []
                while idx < total_n:
                    nxt = raw_lines[idx]
                    nxt_strip = nxt.strip()
                    nxt_indent = len(nxt) - len(nxt.lstrip(" "))
                    if nxt_strip and nxt_indent <= indent:
                        break
                    idx += 1
                    if nxt_strip:
                        buf.append(nxt_strip)
                resolved = " ".join(buf)
            else:
                resolved = unquote(val)
                if len(val) >= 2 and val[0] == "'" and val[-1] == "'":
                    resolved = resolved.replace("''", "'")
            if indent == 0:
                top = key
                if key in f:
                    f[key] = resolved
                if key == "project":
                    project = {"status": ""}
            elif top == "project" and key == "status":
                project["status"] = resolved
            elif top == "prep" and key in prep_keys:
                prep[key] = resolved
            elif top == "total" and key in total_keys:
                total[key] = resolved
    except OSError:
        pass
    f["project_status"] = project.get("status", "")
    return f, prep, total

def read_budget():
    """budget 区（unit/total_cap/cap_authorized/cap_source/used/remaining）；
    缺失字段返回 None。cap_authorized 缺省即 no（金额门默认不生效）。"""
    out = {"unit": None, "total_cap": None, "cap_authorized": None,
           "cap_source": None, "used": None, "remaining": None}
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
    """r29-fix2/fix3 费用政策（用户决定 2026-10-14）：USD 费用默认只统计、
    不停止——金额门仅当授权可核对时生效，双条件缺一不可：
      (a) budget.cap_authorized: yes
      (b) budget.cap_source 非空且非占位（unknown/none/null/-/n/a 视为来源
          缺失）——只给模型可自行置 yes 的标签不视作用户授权；出处可核对
          才启用对应金额停止条件。
    total_cap:null / 未设上限 / 任一条件不满足 = 只统计，不拦研究，也不逐轮
    追问"补授权"。未知值不视为耗尽，也不视为无限授权。GPU 小时与本门无关
    （纯统计，见 gpu_usage 区）；token/墙钟阈值不得替代本门成为默认限制。"""
    b = read_budget()
    cap, used = num_or_none(b["total_cap"]), num_or_none(b["used"])
    src = (b["cap_source"] or "").strip().lower()
    provenance_ok = src not in ("", "unknown", "none", "null", "-", "n/a")
    authorized = (b["cap_authorized"] or "").strip().lower() == "yes" and provenance_ok
    exhausted = "unknown"
    if b["unit"] == "usd" and authorized and cap is not None and used is not None:
        exhausted = "yes" if used >= cap else "no"
    print("BUDGET_UNIT=" + str(b["unit"]))
    print("BUDGET_CAP=" + str(b["total_cap"]))
    print("BUDGET_USED=" + str(b["used"]))
    print("BUDGET_CAP_AUTH=" + ("yes" if authorized else "no"))
    print("BUDGET_CAP_SOURCE=" + (b["cap_source"] or ""))
    print("BUDGET_GATE_ACTIVE=" + ("yes" if authorized else "no"))
    print("BUDGET_EXHAUSTED=" + exhausted)

def _applied_path():
    td = os.environ.get("LEMVO_TURN_DIR", "").strip()
    return os.path.join(td, ".accounting-applied.json") if (td and os.path.isdir(td)) else None

def _turn_stamp():
    td = os.environ.get("LEMVO_TURN_DIR", "").strip()
    if not td or not os.path.isdir(td):
        return None
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", os.path.basename(os.path.normpath(td))).strip("-") or "turn"
    digest = hashlib.sha256(os.path.realpath(td).encode("utf-8")).hexdigest()[:12]
    return "%s-%s" % (base, digest)

def _read_state_stamps():
    stamps = {}
    if not os.path.exists(STATE):
        return stamps
    try:
        raw_lines = open(STATE, encoding="utf-8", errors="replace").read().splitlines()
    except OSError:
        return stamps
    top = ""
    for l in raw_lines:
        m = re.match(r"^(\s*)([A-Za-z_][\w-]*)\s*:\s*(.*)$", strip_inline_comment(l))
        if not m:
            continue
        indent, key, val = len(m.group(1)), m.group(2), m.group(3).strip()
        if indent == 0:
            top = key
        elif indent == 2 and key in ("last_settled_turn", "last_accounted_turn", "last_budget_turn"):
            v = unquote(val)
            if v and v not in ("none", "null", "~"):
                stamps[(top, key)] = v
    return stamps

def _set_or_append_section_key(lines, section, key, val):
    if not set_section_key(lines, section, key, val):
        lines.extend([section + ":", "  " + key + ": " + str(val)])

def _atomic_write_state(lines):
    target = os.path.abspath(STATE)
    parent = os.path.dirname(target) or "."
    fd, tmp = tempfile.mkstemp(prefix=".research-state.tmp-", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        os.replace(tmp, target)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

def _load_applied(path):
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}

def _save_applied(path, data):
    if not path:
        return
    tmp = "%s.tmp-%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, sort_keys=True)
    os.replace(tmp, path)

def cmd_budget_add():
    """F2 结算累计（R1 单位分账）：模型费用（USD）只入 unit=usd 的金额账
    budget.used/remaining；非 usd 旧账（gpu_hours/wall_hours 等）不再吸收美元
    费用——增量改记 total.cost_usd_total 纯统计（无门、只报告）。
    used 显式 unknown/坏值不补 0、不累计。argv: budget-add <increment|unknown>
    increment 由调用方按成本口径算好（per-call/delta>=0 用 delta；
    负 delta=基线重置用 reported；unknown 不加）。"""
    global LINES
    apath = _applied_path()
    applied = _load_applied(apath)
    if "budget_add" in applied and isinstance(applied["budget_add"], dict):
        for ln in applied["budget_add"].get("lines", []):
            print(ln)
        print("IDEMPOTENT_SKIP=yes")
        return
    tstamp = _turn_stamp()
    stamps = _read_state_stamps()
    if tstamp and (
        stamps.get(("budget", "last_budget_turn")) == tstamp
        or stamps.get(("total", "last_budget_turn")) == tstamp
    ):
        b = read_budget()
        if b["unit"] != "usd":
            _, _, total = read_fields()
            cur = num_or_none(total.get("cost_usd_total"))
            val = str(cur) if cur is not None else str(total.get("cost_usd_total", "unknown"))
            out_lines = [
                "BUDGET_ADD_SKIPPED=non-usd-unit(" + str(b["unit"]) + ")",
                "COST_USD_TOTAL_NOW=" + val,
            ]
        else:
            cur = num_or_none(b.get("used"))
            val = str(cur) if cur is not None else str(b.get("used", "unknown"))
            out_lines = ["BUDGET_USED_NOW=" + val]
        applied["budget_add"] = {"lines": out_lines}
        _save_applied(apath, applied)
        for ln in out_lines:
            print(ln)
        print("IDEMPOTENT_SKIP=yes")
        return
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
    if b["unit"] != "usd":
        f, prep, total = read_fields()
        init_sections(f, prep, total)
        cur = num_or_none(total.get("cost_usd_total"))
        newv = incf if cur is None else round(cur + incf, 6)
        LINES = open(STATE, encoding="utf-8").read().splitlines()
        _set_or_append_section_key(LINES, "total", "cost_usd_total", newv)
        if tstamp:
            _set_or_append_section_key(LINES, "total", "last_budget_turn", tstamp)
        _atomic_write_state(LINES)
        out_lines = [
            "BUDGET_ADD_SKIPPED=non-usd-unit(" + str(b["unit"]) + ")",
            "COST_USD_TOTAL_NOW=" + str(newv),
        ]
        applied["budget_add"] = {"lines": out_lines}
        _save_applied(apath, applied)
        for ln in out_lines:
            print(ln)
        return
    if b["used"] not in (None, "") and num_or_none(b["used"]) is None:
        print("BUDGET_USED_NOW=unknown"); return
    used = num_or_none(b["used"])
    if used is None:
        used = 0.0
    used = round(used + incf, 6)
    LINES = open(STATE, encoding="utf-8").read().splitlines()
    # Missing budget fields must be inserted before the next top-level section,
    # never appended under whichever section happens to end the YAML document.
    set_section_key(LINES, "budget", "used", used)
    cap = num_or_none(b["total_cap"])
    if cap is not None:
        set_section_key(LINES, "budget", "remaining", round(cap - used, 6))
    if tstamp:
        _set_or_append_section_key(LINES, "budget", "last_budget_turn", tstamp)
    _atomic_write_state(LINES)
    out_lines = ["BUDGET_USED_NOW=" + str(used)]
    applied["budget_add"] = {"lines": out_lines}
    _save_applied(apath, applied)
    for ln in out_lines:
        print(ln)

def cmd_gpu_add():
    """R1：GPU 小时纯统计入口（只记录，永不作门）。argv:
    gpu-add <hours|unknown> [basis] [source]
    unknown 不补 0 也不动账；数值累加进 gpu_usage.used_hours 并记录计量口径。
    本命令与美元账本、预算门完全无关。"""
    global LINES
    if not os.path.exists(STATE):
        print("GPU_USED_NOW=state-missing"); return
    hours = sys.argv[2] if len(sys.argv) > 2 else "unknown"
    basis = sys.argv[3] if len(sys.argv) > 3 else ""
    source = sys.argv[4] if len(sys.argv) > 4 else ""
    LINES = open(STATE, encoding="utf-8").read().splitlines()
    changed = False
    if hours != "unknown":
        try:
            h = float(hours)
        except ValueError:
            h = None
        if h is not None:
            cur = num_or_none(read_gpu_used(LINES))
            newv = h if cur is None else round(cur + h, 6)
            changed = set_section_key(LINES, "gpu_usage", "used_hours", newv)
    if basis:
        changed = set_section_key(LINES, "gpu_usage", "basis", basis) or changed
    if source:
        changed = set_section_key(LINES, "gpu_usage", "source", source) or changed
    if changed:
        open(STATE, "w", encoding="utf-8").write("\n".join(LINES) + "\n")
    got = read_gpu_used(LINES)
    print("GPU_USED_NOW=" + (str(got) if got is not None else "unknown"))

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
    for name, defaults in (("prep", PREP_DEFAULTS), ("total", TOTAL_DEFAULTS),
                           ("gpu_usage", GPU_DEFAULTS)):
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
    changed = migrate_legacy_budget(lines) or changed
    if changed:
        open(STATE, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    return changed

def set_section_key(lines, section, key, val):
    """在指定顶层区内替换或追加 2 级缩进键；区不存在返回 False。"""
    si = None
    for i, l in enumerate(lines):
        if re.match(r"^" + section + r"\s*:", l):
            si = i
    if si is None:
        return False
    end = si + 1
    while end < len(lines) and (not lines[end].strip() or lines[end][0] in " #"):
        end += 1
    for i in range(si + 1, end):
        m = re.match(r"(\s*)" + key + r"\s*:\s*(.*)$", lines[i])
        if m:
            lines[i] = m.group(1) + key + ": " + str(val)
            return True
    lines.insert(end, "  " + key + ": " + str(val))
    return True

def migrate_legacy_budget(lines):
    """R1 一次性迁移：旧 budget.unit 非 usd（gpu_hours/wall_hours 等）→ 历史用量
    转记 gpu_usage 统计区（仅 gpu_hours；其他单位语义不符不转、保持 unknown），
    标 migrated=yes；budget 区 cap/used 原样保留为注明失效的历史信息——
    不写 0、不设假上限、不清账。预算门与费用累计只认 usd，旧阈值自此退出
    一切控制逻辑。缺区/缺字段照常续跑，不要求先补表才能实验。"""
    b_unit = b_used = None
    top = ""
    for l in lines:
        # r29-fix2 P2：行尾注释感知——旧模板 `used: 7.0  # gpu hours` 原解析
        # 把注释吃进数值导致判 unknown 却标 migrated=yes；与 read_budget/read_fields
        # 同一 strip_inline_comment 口径。
        m = re.match(r"^(\s*)([A-Za-z_][\w-]*)\s*:\s*(.*)$", strip_inline_comment(l))
        if not m:
            continue
        indent, key, val = len(m.group(1)), m.group(2), m.group(3).strip()
        if indent == 0:
            top = key
        elif top == "budget" and indent == 2:
            if key == "unit":
                b_unit = unquote(val)
            elif key == "used":
                b_used = unquote(val)
    if b_unit is None or b_unit == "usd":
        return False
    gi = None
    for i, l in enumerate(lines):
        if re.match(r"^gpu_usage\s*:", l):
            gi = i
            break
    if gi is None:
        return False
    end = gi + 1
    while end < len(lines) and (not lines[end].strip() or lines[end][0] in " #"):
        end += 1
    cur = {}
    for l in lines[gi + 1:end]:
        m = re.match(r"\s*([\w-]+)\s*:\s*(.*)$", l)
        if m:
            cur[m.group(1)] = unquote(m.group(2))
    already = str(cur.get("migrated", "no")) == "yes"
    changed = False
    if b_unit == "gpu_hours":
        hist = b_used if (b_used not in (None, "") and num_or_none(b_used) is not None) else "unknown"
        # r29-fix2 P2 补迁：已误标 migrated=yes 但 used_hours 缺失/unknown 且
        # 旧账数值可解析——回填真实历史用量（不填伪零、不清旧账、幂等）。
        if (not already) or cur.get("used_hours") in (None, "", "unknown"):
            if hist != "unknown" or not already:
                changed = set_section_key(lines, "gpu_usage", "used_hours", hist)
    if already:
        return changed
    changed = set_section_key(lines, "gpu_usage", "basis", "legacy-budget-migration") or changed
    changed = set_section_key(lines, "gpu_usage", "source", "budget.unit=" + str(b_unit)) or changed
    changed = set_section_key(lines, "gpu_usage", "migrated", "yes") or changed
    changed = set_section_key(lines, "budget", "limit_enforced", "no") or changed
    # r29-fix2 费用政策：迁移项目里的旧上限数字不构成用户授权——显式标
    # cap_authorized: no（门默认不生效），费用只统计。
    changed = set_section_key(lines, "budget", "cap_authorized", "no") or changed
    return changed

def read_gpu_used(lines):
    gi = None
    for i, l in enumerate(lines):
        if re.match(r"^gpu_usage\s*:", l):
            gi = i
            break
    if gi is None:
        return None
    end = gi + 1
    while end < len(lines) and (not lines[end].strip() or lines[end][0] in " #"):
        end += 1
    for l in lines[gi + 1:end]:
        m = re.match(r"\s*used_hours\s*:\s*(.*)$", l)
        if m:
            return unquote(m.group(1))
    return None

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
    # Single wait enum/priority contract, also used by the controller.
    subprocess.check_call([sys.executable, CONTRACT_PATH, "state-shell"])

def cmd_settle():
    """Consume runner-owned progress; typed fallback only for pre-contract recovery."""
    global LINES
    if not os.path.exists(STATE):
        print("NEW_PROGRESS=no"); print("ARTIFACT_TYPED=no")
        print("PREP_TURNS_NOW=0"); print("TOTAL_TURNS_NOW=0")
        print("STATE_MISSING=yes"); return
    apath = _applied_path()
    applied = _load_applied(apath)
    if "settle" in applied and isinstance(applied["settle"], dict):
        cached = applied["settle"]
        print("NEW_PROGRESS=" + ("yes" if cached.get("new_progress") else "no"))
        print("ARTIFACT_TYPED=" + ("yes" if cached.get("typed") else "no"))
        print("PREP_TURNS_NOW=" + str(cached.get("prep_turns", 0)))
        print("TOTAL_TURNS_NOW=" + str(cached.get("total_turns", 0)))
        print("IDEMPOTENT_SKIP=yes")
        return
    tstamp = _turn_stamp()
    stamps = _read_state_stamps()
    if tstamp and stamps.get(("prep", "last_settled_turn")) == tstamp:
        _, prep, total = read_fields()
        try:
            turns = int(prep.get("prep_turns", "0"))
        except (TypeError, ValueError):
            turns = 0
        try:
            total_turns = int(total.get("total_turns", "0"))
        except (TypeError, ValueError):
            total_turns = 0
        artifact = prep.get("last_informative_artifact", "none")
        typed = bool(artifact and artifact != "none" and artifact_is_construction(artifact))
        record_path = os.environ.get("LEMVO_PROGRESS_RECORD")
        if record_path and os.path.exists(record_path):
            try:
                with open(record_path, encoding="utf-8") as fh:
                    new_progress = bool(json.load(fh).get("progress", False))
            except (OSError, ValueError):
                new_progress = False
        elif os.environ.get("LEMVO_TURN_INTERRUPTED") == "yes":
            new_progress = False
        else:
            new_sha = sha256_file(artifact) if typed else None
            new_progress = (
                turns == 0
                and bool(new_sha)
                and new_sha == prep.get("last_artifact_sha", "none")
            )
        applied["settle"] = {
            "new_progress": bool(new_progress),
            "typed": bool(typed),
            "prep_turns": turns,
            "total_turns": total_turns,
        }
        _save_applied(apath, applied)
        print("NEW_PROGRESS=" + ("yes" if new_progress else "no"))
        print("ARTIFACT_TYPED=" + ("yes" if typed else "no"))
        print("PREP_TURNS_NOW=" + str(turns))
        print("TOTAL_TURNS_NOW=" + str(total_turns))
        print("IDEMPOTENT_SKIP=yes")
        return
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
    record_path = os.environ.get("LEMVO_PROGRESS_RECORD")
    record = None
    if record_path:
        with open(record_path, encoding="utf-8") as fh:
            record = json.load(fh)
        new_progress = record["progress"]
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
    interrupted = os.environ.get("LEMVO_TURN_INTERRUPTED") == "yes"
    inspection_unknown = (os.environ.get("LEMVO_PROGRESS_INCOMPLETE") == "yes"
                          or os.environ.get("LEMVO_WAIT_ASSESSMENT") == "yes")
    if record is not None:
        inspection_unknown = record["neutral"]
    turns = turns if interrupted else (0 if new_progress else (turns if inspection_unknown else turns + 1))
    total_turns += 1
    LINES = open(STATE, encoding="utf-8").read().splitlines()
    _set_or_append_section_key(LINES, "prep", "prep_turns", turns)
    _set_or_append_section_key(LINES, "prep", "last_seen_observation", last_obs if new_progress else last_seen)
    if new_progress and new_sha and not interrupted:
        _set_or_append_section_key(LINES, "prep", "last_artifact_sha", new_sha)
    if tstamp:
        _set_or_append_section_key(LINES, "prep", "last_settled_turn", tstamp)
    _set_or_append_section_key(LINES, "total", "total_turns", total_turns)
    _atomic_write_state(LINES)
    applied["settle"] = {
        "new_progress": bool(new_progress),
        "typed": bool(typed),
        "prep_turns": turns,
        "total_turns": total_turns,
    }
    _save_applied(apath, applied)
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
    apath = _applied_path()
    applied = _load_applied(apath)
    if "account" in applied and isinstance(applied["account"], dict):
        print("WALL_NOW=" + str(applied["account"].get("wall_min", "0.0")))
        print("IDEMPOTENT_SKIP=yes")
        return
    tstamp = _turn_stamp()
    stamps = _read_state_stamps()
    if tstamp and stamps.get(("total", "last_accounted_turn")) == tstamp:
        _, _, total = read_fields()
        wall_num = num_or_none(total.get("total_wall_time_min"))
        wall_val = wall_num if wall_num is not None else total.get("total_wall_time_min", "0.0")
        applied["account"] = {"wall_min": wall_val}
        _save_applied(apath, applied)
        print("WALL_NOW=" + str(wall_val))
        print("IDEMPOTENT_SKIP=yes")
        return
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
    _set_or_append_section_key(LINES, "total", "total_wall_time_min", wall)
    if tin != "unknown":
        _set_or_append_section_key(LINES, "total", "total_tokens_in", int(num(total.get("total_tokens_in"))) + int(tin))
    if tout != "unknown":
        _set_or_append_section_key(LINES, "total", "total_tokens_out", int(num(total.get("total_tokens_out"))) + int(tout))
    if tstamp:
        _set_or_append_section_key(LINES, "total", "last_accounted_turn", tstamp)
    _atomic_write_state(LINES)
    applied["account"] = {"wall_min": wall}
    _save_applied(apath, applied)
    print("WALL_NOW=" + str(wall))

def output_events(path):
    """JSON object/array or JSONL; unwrap only declared CLI event envelopes."""
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return
    try:
        obj = json.loads(text)
        candidates = obj if isinstance(obj, list) else [obj]
    except ValueError:
        candidates = []
        for line in text.splitlines():
            try: candidates.append(json.loads(line))
            except ValueError: continue
    for ev in candidates:
        if not isinstance(ev, dict): continue
        if ev.get("type") in ("event_msg", "event") and isinstance(ev.get("payload"), dict):
            ev = ev["payload"]
        yield ev

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
    账本中无同 SID 累计基线时以当轮值为基线。R4：sid 缺失（如孤儿轮恢复
    归因失败）时不再退回逐轮 sums 印假 0，按无基线口径取当轮值。"""
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
        for ev in output_events(sys.argv[2]):
            if ev.get("type") not in ("result", "turn.completed", "token_count"):
                continue
            u = ev.get("usage") or ev.get("token_usage")
            if ev.get("type") == "token_count" and isinstance(ev.get("info"), dict):
                u = ev["info"].get("total_token_usage")
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
    # codex 累计 → 当轮差值（基线从账本同 SID 最近一条带 codex_cum 的事件取）。
    # R4：SID 归因不可得（sid=none）不再退回逐轮 sums（codex 事件是累计值，
    # 那条路径只会印出假 0）——按无基线口径取当轮=累计值，unknown 保持 unknown。
    if engine == "codex" and any(v is not None for v in cum.values()):
        prev = {}
        if sid != "none":
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
        for ev in output_events(sys.argv[2]):
            t = ev.get("type", "")
            if t in ("turn.failed", "turn.aborted"):
                outcome = "failed"
                e = ev.get("error")
                err = json.dumps(e, ensure_ascii=False)[:300] if e else t
            elif t == "turn.completed":
                outcome = "ok"
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
        # r29-fix2 P1-1：恢复事件同样携带 cost_usd_reported（累计快照），
        # 必须参与后续差分——否则恢复轮的金额会被下一正常轮重复计入
        # （序列 1 → 3(恢复) → 4 曾被记成 1+2+3=6，正确应为 4）。
        # r29-fix3 P1-1：reader 兼容旧字符串快照（writer 曾写 "3"）——
        # 可解析为有限数则采用，bool/非数拒收；不清零旧账。
        for ev in ledger_events():
            if ev.get("type") in ("work-turn", "prep-turn", "recovered-turn", "runtime-interrupted", "runtime-error", "wait-assessment", "service-error") and ev.get("sid") == sid:
                v = ev.get("cost_usd_reported")
                if isinstance(v, str):
                    try:
                        f = float(v)
                    except ValueError:
                        continue
                    v = f if math.isfinite(f) else None
                if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v):
                    prev = v
        if prev is not None:
            print("COST_DELTA=%s" % (rep - prev)); print("COST_BASIS=delta-from-prev")
        else:
            print("COST_DELTA=%s" % rep); print("COST_BASIS=first-reported")
    else:
        print("COST_DELTA=%s" % rep); print("COST_BASIS=per-call")

def cmd_prep_snapshot():
    f, prep, total = read_fields()
    art = " ".join(str(prep.get("last_informative_artifact", "none")).split()) or "none"
    obs = " ".join(str(prep.get("last_informative_observation", "none")).split()) or "none"
    print("LAST_ARTIFACT=" + art)
    print("LAST_OBS=" + obs)

def cmd_auth_json():
    """S3：授权事件真序列化（justification 经环境变量传入）。"""
    just = os.environ.get("AUTHORIZE_JUSTIFICATION", "")
    obj = {"type": "authorize", "new_cap": int(sys.argv[2]),
           "justification": just, "request_id":os.environ.get("AUTHORIZE_REQUEST_ID", ""), "ts": sys.argv[3]}
    print(json.dumps(obj, ensure_ascii=False))

def cmd_auth_available():
    just=os.environ.get("AUTHORIZE_JUSTIFICATION", "")
    request=os.environ.get("AUTHORIZE_REQUEST_ID", "")
    used=any(ev.get("type")=="authorize" and
             ((request and ev.get("request_id")==request) or
              (not request and ev.get("justification")==just)) for ev in ledger_events())
    print("yes" if just and not used else "no")

TURN_USAGE_TYPES = ("work-turn", "prep-turn", "recovered-turn", "runtime-interrupted", "runtime-error", "wait-assessment", "service-error")

def cmd_ledger_append():
    obj = json.loads(sys.stdin.read())
    if isinstance(obj, dict) and obj.get("turn_id"):
        tid = obj.get("turn_id")
        otyp = obj.get("type")
        for ev in ledger_events():
            if ev.get("turn_id") != tid:
                continue
            if otyp in TURN_USAGE_TYPES and ev.get("type") in TURN_USAGE_TYPES:
                print("duplicate-skipped")
                return
            if ev.get("type") == otyp:
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


def _atomic_json_file(path, obj, indent=1):
    p = os.path.abspath(path)
    parent = os.path.dirname(p) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + os.path.basename(p) + "-", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=indent)
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

def _utc_now():
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _json_num_or_str(val):
    try:
        f = float(val)
        return f if math.isfinite(f) else val
    except (TypeError, ValueError):
        return val

def cmd_init_scaffold():
    lines = open(STATE, encoding="utf-8").read().splitlines()
    def sec_idx(name):
        return [i for i, l in enumerate(lines) if re.match(r"^" + name + r"\s*:", l)]
    changed = False
    prep_fields = ["  prep_turns: 0", "  prep_wall_time_min: 0.0", "  prep_cost: null",
                   "  last_informative_observation: \"\"", "  last_informative_artifact: none",
                   "  last_artifact_sha: none", "  last_seen_observation: none",
                   "  ready_blocker: \"\"", "  next_min_probe: \"\"", "  session_id: null"]
    total_fields = ["  total_turns: 0", "  total_wall_time_min: 0.0", "  total_cost: null",
                    "  total_tokens_in: 0", "  total_tokens_out: 0"]
    for name, sfields in (("prep", prep_fields), ("total", total_fields)):
        idxs = sec_idx(name)
        if not idxs:
            lines.extend([name + ":"] + sfields)
            changed = True
        else:
            idx = idxs[-1]
            end = idx + 1
            while end < len(lines) and (not lines[end].strip() or lines[end][0] in " #"):
                end += 1
            present = set()
            for l in lines[idx + 1:end]:
                mm = re.match(r"\s*([\w-]+)\s*:", l)
                if mm:
                    present.add(mm.group(1))
            for f in sfields:
                key = re.match(r"\s*([\w-]+)\s*:", f).group(1)
                if key not in present:
                    lines.insert(end, f); end += 1; changed = True
    if changed:
        _atomic_write_state(lines)
        print("accounting-initialized")
    else:
        print("accounting-already-present")

def cmd_append_campaign_log():
    src = sys.argv[2]
    out_path = sys.argv[3] if len(sys.argv) > 3 else "campaign_log.txt"
    with open(src, encoding="utf-8", errors="replace") as fh, open(out_path, "a", encoding="utf-8") as out:
        for line in fh:
            s = line.strip()
            if s.startswith("{"):
                try:
                    obj = json.loads(s)
                    if isinstance(obj, dict) and obj.get("type") == "stream_event":
                        continue
                except ValueError:
                    pass
            out.write(line)

def cmd_recover_meta():
    import datetime, time
    td = sys.argv[2]
    with open(os.path.join(td, "manifest.json"), encoding="utf-8") as fh:
        man = json.load(fh)
    epoch = man.get("session_epoch", "legacy")
    tid = man.get("turn_id", "") or os.path.basename(os.path.normpath(td))
    wall = 0
    try:
        s = man.get("started_utc")
        if s:
            d = datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
            wall = max(0, int(time.time() - d.timestamp()))
    except Exception:
        wall = 0
    def q(v):
        return "'" + str(v).replace("'", "'\\''") + "'"
    print("REC_EPOCH=" + q(epoch))
    print("TURN_ID=" + q(tid))
    print("RECOVER_WALL=" + q(wall))

def cmd_is_sid_poisoned():
    sid = sys.argv[2] if len(sys.argv) > 2 else ""
    if sid:
        for ev in ledger_events():
            if ev.get("type") == "session-poisoned" and ev.get("sid") == sid:
                sys.exit(0)
    sys.exit(1)

def cmd_write_manifest_solo():
    import socket
    td, tid, proj, eng = sys.argv[2:6]
    host = socket.gethostname()
    epoch = open(".lemvo-session-epoch", encoding="utf-8").read().strip() if os.path.exists(".lemvo-session-epoch") else "legacy"
    man = {
        "schema_version": 1, "schema": "lemvo-turn-manifest",
        "session_epoch": epoch,
        "campaign_id": host + ":" + os.path.basename(os.path.realpath(proj)),
        "turn_id": tid, "host": host, "controller_pid": None,
        "engine": eng, "status": "dispatched", "started_utc": _utc_now(),
    }
    _atomic_json_file(os.path.join(td, "manifest.json"), man, indent=1)

def cmd_write_engine_json():
    p, apid, astart, eng = sys.argv[2], int(sys.argv[3]), sys.argv[4], sys.argv[5]
    _atomic_json_file(p, {"adapter_pid": apid, "adapter_start": astart, "engine": eng, "status": "running"}, indent=1)

def cmd_update_engine_pid():
    p, epid, estart = sys.argv[2], int(sys.argv[3]), sys.argv[4]
    try:
        d = json.load(open(p, encoding="utf-8"))
    except (OSError, ValueError):
        d = {}
    d["pid_engine"] = epid
    d["pid_engine_start"] = estart
    _atomic_json_file(p, d, indent=1)

def cmd_finalize_engine_json():
    p, st = sys.argv[2], sys.argv[3]
    if os.path.exists(p):
        try:
            d = json.load(open(p, encoding="utf-8"))
        except (OSError, ValueError):
            d = {}
        d["status"] = st
        _atomic_json_file(p, d, indent=1)

def cmd_is_event_active():
    p = sys.argv[2]
    try:
        active = json.load(open(p, encoding="utf-8")).get("active") is True
    except (OSError, ValueError, AttributeError):
        active = False
    sys.exit(0 if active else 1)

def cmd_write_recover_result():
    import socket
    td, tid, proj, tin, tout = sys.argv[2:7]
    host = socket.gethostname()
    obj = {
        "schema_version": 1, "schema": "lemvo-turn-result",
        "campaign_id": host + ":" + os.path.basename(os.path.realpath(proj)),
        "turn_id": tid, "host": host,
        "status": "recovered-orphan", "next_action": "continue", "engine_rc": None,
        "usage": {"tokens_in": tin if tin != "unknown" else None,
                  "tokens_out": tout if tout != "unknown" else None},
        "scientific_verdict": {}, "artifact_refs": [], "ended_utc": _utc_now(),
    }
    progress_path = os.path.join(td, "progress.json")
    if os.path.exists(progress_path):
        progress = json.load(open(progress_path, encoding="utf-8"))
        kind = progress.get("wait", {}).get("kind")
        obj["next_action"] = {"human": "needs_user", "hard": "paused", "resource": "waiting_jobs", "done": "completed"}.get(kind, "continue")
        obj["progress_kind"] = progress.get("kind")
        obj["wait"] = progress.get("wait")
        obj["artifact_refs"] = [c["path"] for c in progress.get("changes", [])]
    _atomic_json_file(os.path.join(td, "result.json"), obj, indent=1)

def cmd_write_turn_result():
    import socket
    status, nxt, rcv, td, tid, proj, cs, ms, rs = sys.argv[2:11]
    art = os.environ.get("T_ART", "none")
    host = socket.gethostname()
    obj = {
        "schema_version": 1, "schema": "lemvo-turn-result",
        "campaign_id": host + ":" + os.path.basename(os.path.realpath(proj)),
        "turn_id": tid, "host": host,
        "status": status, "next_action": nxt,
        "engine_rc": int(rcv) if rcv not in ("", "None") else None,
        "scientific_verdict": {"campaign_status": cs, "measurement_state": ms, "root_status": rs},
        "artifact_refs": [art] if art and art != "none" else [],
        "ended_utc": _utc_now(),
    }
    if status == "succeeded" and nxt == "continue" and rcv == "0":
        try:
            with open(os.path.join(td, "work-result.json"), encoding="utf-8") as stream:
                work = json.load(stream)
            if isinstance(work, dict) and work.get("status") == "waiting":
                obj["next_action"] = "waiting_jobs"
                obj["runtime_reason"] = "work-unit-idle"
        except (OSError, ValueError):
            pass
    if os.path.exists(os.path.join(td, "timeout.json")):
        obj["runtime_reason"] = "turn-timeout"
    elif os.environ.get("LEMVO_TURN_INTERRUPTED") == "yes":
        obj["runtime_reason"] = "turn-interrupted"
    elif status == "failed" and rcv not in ("", "0", "None"):
        obj["runtime_reason"] = "engine-error"
    progress_path = os.path.join(td, "progress.json")
    if os.path.isfile(progress_path):
        progress = json.load(open(progress_path, encoding="utf-8"))
        obj["progress_kind"] = progress["kind"]
        obj["wait"] = progress["wait"]
        obj["artifact_refs"] = list(dict.fromkeys(obj["artifact_refs"] + [item["path"] for item in progress["changes"]]))
    instructions = os.path.join(td, "instructions.json")
    if os.path.isfile(instructions):
        obj["instruction_revision"] = json.load(open(instructions, encoding="utf-8"))["revision"]
    p = os.path.join(td, "result.json")
    if not os.path.exists(p):
        _atomic_json_file(p, obj, indent=1)

def _ledger_append_dedup(obj):
    if isinstance(obj, dict) and obj.get("turn_id"):
        tid = obj.get("turn_id")
        otyp = obj.get("type")
        for ev in ledger_events():
            if ev.get("turn_id") != tid:
                continue
            if otyp in TURN_USAGE_TYPES and ev.get("type") in TURN_USAGE_TYPES:
                return "duplicate-skipped"
            if ev.get("type") == otyp:
                return "duplicate-skipped"
    ledger_append(obj)
    return "appended"

def cmd_emit_turn_ledger():
    e = os.environ
    def num(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            try:
                return float(v)
            except (TypeError, ValueError):
                return v
    def iv(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return v
    obj = {
        "type": e["T_TYPE"], "turn_id": e["T_TURNID"], "sid": e["T_SID"],
        "prep_turns": iv(e["T_PREP"]), "total_turns": iv(e["T_TOTAL"]),
        "tokens_in": num(e["T_IN"]), "tokens_out": num(e["T_OUT"]),
        "cache_read": num(e["T_CR"]), "cache_write": num(e["T_CW"]),
        "cost_usd_reported": num(e["T_COST"]), "cost_usd_delta": num(e["T_DELTA"]),
        "cost_basis": e["T_BASIS"], "cost_scope": e["T_SCOPE"],
        "codex_cum": (json.loads(e["T_CUM"]) if e.get("T_CUM") else None),
        "wall_sec": iv(e["T_WALL"]), "wall_scope": "engine-call",
        "progress_inspection": "incomplete" if e.get("LEMVO_PROGRESS_INCOMPLETE") == "yes" else "complete",
        "waiting_assessment": e.get("LEMVO_WAIT_ASSESSMENT") == "yes",
        "runtime_interrupted": e.get("LEMVO_TURN_INTERRUPTED") == "yes",
        "artifact_ref": e["T_ART"],
        "ts": _utc_now(),
    }
    print(_ledger_append_dedup(obj))

def cmd_emit_over_budget():
    kind = sys.argv[2]
    if kind == "budget-exhausted":
        obj = {"type": "over-budget", "kind": kind, "used": sys.argv[3], "cap": sys.argv[4], "ts": _utc_now()}
    else:
        obj = {"type": "over-budget", "kind": kind, "turns": int(sys.argv[3]),
               "cap": int(sys.argv[4]), "wall_min": float(sys.argv[5]), "ts": _utc_now()}
    print(_ledger_append_dedup(obj))

def cmd_emit_recover_ledger():
    turn_id, sid, u_in, u_out, u_cost, cost_delta, u_cum = sys.argv[2:9]
    obj = {
        "type": "recovered-turn", "turn_id": turn_id, "sid": sid or "none",
        "tokens_in": u_in, "tokens_out": u_out,
        "cost_usd_reported": _json_num_or_str(u_cost),
        "cost_usd_delta": cost_delta, "ts": _utc_now(),
    }
    if u_cum:
        try:
            obj["codex_cum"] = json.loads(u_cum)
        except ValueError:
            obj["codex_cum"] = u_cum
    print(_ledger_append_dedup(obj))

def cmd_emit_service_error():
    turn_id, sid, rc_s, u_in, u_out, u_cr, u_cw, u_cost, cost_delta, cost_basis = sys.argv[2:12]
    obj = {
        "type": "service-error", "turn_id": turn_id, "sid": sid or "none", "rc": int(rc_s),
        "tokens_in": u_in, "tokens_out": u_out, "cache_read": u_cr, "cache_write": u_cw,
        "cost_usd_est": u_cost, "cost_usd_reported": _json_num_or_str(u_cost),
        "cost_usd_delta": cost_delta, "cost_basis": cost_basis, "ts": _utc_now(),
    }
    print(_ledger_append_dedup(obj))

def cmd_progress_meta():
    p = sys.argv[2]
    d = json.load(open(p, encoding="utf-8"))
    wait_kind = d.get("wait", {}).get("kind", "")
    print("PROG_KIND=" + str(d.get("kind", "")))
    print("PROG_NEUTRAL=" + ("yes" if d.get("neutral") else "no"))
    print("WAIT_ASSESS=" + ("yes" if wait_kind in ("human", "resource") else "no"))

def cmd_is_negative():
    try:
        sys.exit(0 if float(sys.argv[2]) < 0 else 1)
    except (IndexError, ValueError):
        sys.exit(1)

def cmd_gte_float():
    try:
        sys.exit(0 if float(sys.argv[2]) >= float(sys.argv[3]) else 1)
    except (IndexError, ValueError):
        sys.exit(1)

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
    elif cmd == "auth-available": cmd_auth_available()
    elif cmd == "ledger-append": cmd_ledger_append()
    elif cmd == "resume-fails": cmd_resume_fails()
    elif cmd == "last-authorize-cap": cmd_last_authorize_cap()
    elif cmd == "budget-gate": cmd_budget_gate()
    elif cmd == "budget-add": cmd_budget_add()
    elif cmd == "gpu-add": cmd_gpu_add()
    elif cmd == "plan-info": cmd_plan_info()
    elif cmd == "init-scaffold": cmd_init_scaffold()
    elif cmd == "append-campaign-log": cmd_append_campaign_log()
    elif cmd == "recover-meta": cmd_recover_meta()
    elif cmd == "is-sid-poisoned": cmd_is_sid_poisoned()
    elif cmd == "write-manifest-solo": cmd_write_manifest_solo()
    elif cmd == "write-engine-json": cmd_write_engine_json()
    elif cmd == "update-engine-pid": cmd_update_engine_pid()
    elif cmd == "finalize-engine-json": cmd_finalize_engine_json()
    elif cmd == "is-event-active": cmd_is_event_active()
    elif cmd == "write-recover-result": cmd_write_recover_result()
    elif cmd == "write-turn-result": cmd_write_turn_result()
    elif cmd == "emit-turn-ledger": cmd_emit_turn_ledger()
    elif cmd == "emit-over-budget": cmd_emit_over_budget()
    elif cmd == "emit-recover-ledger": cmd_emit_recover_ledger()
    elif cmd == "emit-service-error": cmd_emit_service_error()
    elif cmd == "progress-meta": cmd_progress_meta()
    elif cmd == "is-negative": cmd_is_negative()
    elif cmd == "gte-float": cmd_gte_float()
    else: sys.exit(2)
