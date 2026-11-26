#!/usr/bin/env bash
# 单轮适配器 v4.16：具体任务/结果交接、带状态的安全会话轮换；保留等待、进展与指令契约
#  v4.8 变更（对 v4.7，均带隔离回归）：
#  费用政策（用户决定 2026-10-14）：USD 费用默认只统计、不停止、不缩实验、
#    不否定方向；金额停止门仅当 budget.cap_authorized: yes（真实用户明确授权、
#    cap_source 出处可核对）时生效。模型自拟宪章/模板默认/历史孤立数值 ≠
#    授权；total_cap:null/未设/来源不可考 = 只统计，不逐轮追问补授权。
#    GPU 小时维持纯统计；token/累计墙钟阈值不得替代金额门成为默认限制。
#  P1-1 恢复计费：recovered-turn 事件携带 cost_usd_reported 累计快照并参与
#    后续 cost-delta 差分——序列 1 → 3(恢复) → 4 的账本由 1/3/6 修为 1/3/4；
#    连续恢复/重复恢复幂等不变。
#  迁移修正：旧模板行尾注释（used: 7.0  # gpu hours）注释感知解析；已误标
#    migrated=yes 但 used_hours=unknown 且旧账可解析的项目幂等回填真实用量；
#    迁移显式标 budget.cap_authorized: no（旧上限数字不构成用户授权）。
#  v4.7 变更（对 v4.6，均带探针）：
#  R1 计量单位分账：模型费用（USD）只入 unit=usd 的金额账；GPU hours 从预算
#    限制中移出成 gpu_usage 纯统计区（无门、无评分惩罚），墙钟另记不混合；
#    旧非 usd 预算一次性迁移（历史用量与 cap 原样保留注明失效，不清账、
#    不写 0/假上限）；unknown 不补 0；新增 gpu-add 统计入口。
#  R3 阻塞态组合值（如 HOLD|PAUSED）单引号输出——source 按数据读取，
#    管道符不再成为 shell 语法（多阻塞字段不再 rc1 崩溃）。
#  R4 孤儿轮恢复把恢复出的 engine/SID 传给计量解析（codex 累计差分复用
#    正常结算口径，不再落 0/0 假账）；恢复轮费用同口径入账；重复恢复由
#    result.json 守卫幂等。
#  v4.6 变更（round-26：codex 线联调六缺陷修复）：
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
# One version source: write_helper emits this marker into the generated Python.
# Bump on helper changes so existing project-local helpers migrate exactly once.
HELPER_VERSION="5.0"
RUNTIME_SUPPORT="$(cd "$(dirname "$0")" && pwd)/runtime-support.py"
RUNTIME_CONTRACT="$(cd "$(dirname "$0")" && pwd)/runtime-contract.py"
WORK_UNIT="$(cd "$(dirname "$0")" && pwd)/work-unit.py"
RUNTIME_IDLE="$(dirname "$WORK_UNIT")/runtime-idle.py"
SELF="$(cd "$(dirname "$0")" && pwd)/$(basename -- "$0")"
[ -f "$RUNTIME_SUPPORT" ] || { echo "Missing runtime-support.py: install the complete research-orchestrator skill."; exit 64; }
[ -f "$RUNTIME_CONTRACT" ] || { echo "Missing runtime-contract.py: install the complete runtime bundle."; exit 64; }

# ── --init 模式 ──
if [ "${1:-}" = "--init" ]; then
  PROJ_DIR="${2:?用法: autoloop-reference.sh --init <项目目录>}"
  cd "$PROJ_DIR" || exit 64
  STATE=research-state.yaml
  [ -f "$STATE" ] || { echo "缺少 $STATE"; exit 64; }
  python3 "$(dirname "$SELF")/autoloop-helper.py" init-scaffold
  cat <<'WARN'
诚实边界：--init 只初始化字段，不建立持续采集。持续记账需要（a）以本脚本
作为外层循环逐轮运行（每轮自动结算墙钟/token/进展），或（b）交互式会话
每轮手动调用本脚本一次（它结算当轮后退出）。只跑 --init 不跑循环 =
字段存在但永远是 0。
预算口径（round-23/29/r29-fix2）：total_wall_time_min=研究墙钟（引擎调用累计，控制器
等待/训练不在内）；budget 区只作 USD 金额账——费用恒记录，但金额停止门仅当
budget.cap_authorized: yes（真实用户明确授权、cap_source 可核对）时存在；
模型自拟/模板默认/历史孤立数值 ≠ 授权；total_cap:null/未设 = 只统计不停止，
不逐轮追问补授权。GPU 小时是纯统计（gpu_usage 区，gpu-add 记录，永不作准入/
派发/停止限制，缺测量标 unknown 不补 0）；token/墙钟阈值不得替代金额门成为
默认限制。累计研究预算在 research-state.yaml + ledger 持久账本，不随模式/进程
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
PROJ_DIR="$(pwd -P)"
SESSION_EPOCH="$(python3 "$RUNTIME_SUPPORT" epoch)" || exit 4
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
  printf '#!/usr/bin/env python3\nHELPER_VERSION = "%s"\n' "$HELPER_VERSION" > "$HELPER"
  printf 'CONTRACT_PATH = %s\n' "$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$RUNTIME_CONTRACT")" >> "$HELPER"
  cat >> "$HELPER" <<'PYEOF'
import pathlib as _pathlib
_helper_impl = _pathlib.Path(CONTRACT_PATH).resolve().with_name("autoloop-helper.py")
with open(str(_helper_impl), "r", encoding="utf-8") as _helper_stream:
    exec(compile(_helper_stream.read(), str(_helper_impl), "exec"), globals())
PYEOF
  chmod +x "$HELPER" 2>/dev/null || true
}

if [ ! -f "$HELPER" ]; then
  write_helper
elif ! grep -qFx "HELPER_VERSION = \"$HELPER_VERSION\"" "$HELPER" 2>/dev/null; then
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
append_engine_logs() {  # 原始 stdout/stderr 入 campaign_log（过滤逐 token stream_event；TD/engine-stdout.jsonl 保留原文）
  [ -n "${TD:-}" ] || return 0
  local max_bytes="${LEMVO_CAMPAIGN_LOG_MAX_BYTES:-52428800}"
  if [ "${max_bytes:-0}" -gt 0 ] 2>/dev/null && [ -f campaign_log.txt ]; then
    local cur_bytes
    cur_bytes=$(wc -c < campaign_log.txt 2>/dev/null || echo 0)
    if [ "${cur_bytes:-0}" -ge "$max_bytes" ] 2>/dev/null; then
      mv -f campaign_log.txt campaign_log.prev.txt 2>/dev/null || true
    fi
  fi
  if [ -f "$TD/engine-stdout.jsonl" ]; then
    if [ "${LEMVO_CAMPAIGN_LOG_RAW_PARTIALS:-0}" = "1" ]; then
      cat "$TD/engine-stdout.jsonl" >> campaign_log.txt
    else
      python3 "$HELPER" append-campaign-log "$TD/engine-stdout.jsonl" campaign_log.txt 2>/dev/null || cat "$TD/engine-stdout.jsonl" >> campaign_log.txt
    fi
  fi
  [ -f "$TD/engine-stderr.log" ] && cat "$TD/engine-stderr.log" >> campaign_log.txt 2>/dev/null || true
  return 0
}

# ── R3：--recover 幂等结算孤儿轮（不调引擎；重复调用无副作用）──
if [ -n "$RECOVER_TD" ]; then
  TD="$RECOVER_TD"
  [ -d "$TD" ] || { echo "turn 目录不存在: $TD"; exit 64; }
  META_TMP=$(mktemp)
  python3 "$HELPER" recover-meta "$TD" > "$META_TMP" 2>/dev/null || { rm -f "$META_TMP"; exit 4; }
  . "$META_TMP"; rm -f "$META_TMP"
  if [ "$REC_EPOCH" != "$SESSION_EPOCH" ]; then
    say "Old session epoch: keeping evidence without adopting $TD"
    emit_reason "STALE_SESSION_EPOCH"; exit 9
  fi
  if [ -f "$TD/result.json" ]; then
    if [ -f "$WORK_UNIT" ]; then
      python3 "$WORK_UNIT" settle "$TD" --rc 0 --session-file "$SID_FILE" >/dev/null \
        || say "Existing work result remains recoverable from saved evidence."
    fi
    echo "already-settled"; exit 0
  fi
  if [ -f "$TD/timeout.json" ]; then
    say "Timed-out orphan is not a successful research turn; preserve guidance and evidence."
    emit_reason "TURN_TIMEOUT"; exit 124
  fi
  # TURN_ID already loaded by recover-meta above
  export LEMVO_TURN_DIR="$TD" LEMVO_TURN_ID="$TURN_ID"
  OUT="$TD/engine-stdout.jsonl"; ERR="$TD/engine-stderr.log"
  REC_OUTCOME=$(python3 "$HELPER" outcome-from "$OUT" | sed -n 's/^OUTCOME=//p')
  if [ "$REC_OUTCOME" != "ok" ]; then
    say "Orphan output has no successful terminal event; keeping inputs and evidence for recovery."
    emit_reason "PROTOCOL"; exit 9
  fi
  NEW_SID=""
  if [ -f "$OUT" ] || [ -f "$ERR" ]; then
    NEW_SID=$(grep -h -oE '"session_id"[": ]+[a-f0-9-]{8,}' "$OUT" "$ERR" 2>/dev/null | head -1 | grep -oE '[a-f0-9]{8}-[a-f0-9-]+' \
           || grep -h -oE '"thread_id"[": ]+[a-f0-9-]{8,}' "$OUT" 2>/dev/null | head -1 | grep -oE '[a-f0-9]{8}-[a-f0-9-]+' \
           || grep -h -oiE 'session( id|_id)?: [a-f0-9-]{8,}' "$OUT" 2>/dev/null | head -1 | grep -oE '[a-f0-9]{8}-[a-f0-9-]+' \
           || true)
  fi
  # BUG-3：毒会话不复活——账本有该 SID 的 session-poisoned 事件则不收养
  if [ -n "$NEW_SID" ] && python3 "$HELPER" is-sid-poisoned "$NEW_SID" 2>/dev/null; then
    say "recover：SID $NEW_SID 有 session-poisoned 记录——不收养（防毒会话复活）"
    NEW_SID=""
  fi
  helper init >/dev/null 2>&1 || true
  if [ -f "$TD/progress-explicit-before.json" ]; then
    # A successful orphan uses the same construction record as a normal turn.
    # Preserve an already measured record; do not count later unrelated edits.
    if [ ! -f "$TD/progress.json" ]; then
      python3 "$RUNTIME_SUPPORT" progress-changed "$TD/progress-before.json" >/dev/null || exit 4
      python3 "$RUNTIME_CONTRACT" progress-end "$TD" >/dev/null || exit 4
    fi
    export LEMVO_PROGRESS_RECORD="$TD/progress.json"
  fi
  helper settle >/dev/null 2>&1 || say "recover：settle 失败（保留现场）"
  # R4：把恢复出的 engine/SID 传给计量解析——复用正常结算口径（codex 累计差分），
  # 不再落 0/0 假账；归因不可得时 helper 按无基线口径取当轮值，unknown 保持 unknown
  parse_usage "$OUT" "${NEW_SID:-none}"
  # 墙钟：manifest.started_utc → 现在（尽力而为）
  # RECOVER_WALL already loaded by recover-meta
  python3 "$HELPER" account "${RECOVER_WALL:-0}" "$U_IN" "$U_OUT" >/dev/null 2>&1 || true
  # R4：恢复轮费用与正常结算同口径（codex=per-call；claude 按 SID 差分）——
  # 不重复累加（重复 recover 由 result.json 守卫拦截）
  REC_COST_DELTA="unknown"; REC_COST_BASIS="unknown"
  if [ "$U_COST" != "unknown" ]; then
    RCD_OUT=$(python3 "$HELPER" cost-delta "${NEW_SID:-none}" "$U_COST" "$ENGINE" 2>/dev/null || true)
    REC_COST_DELTA=$(printf '%s\n' "$RCD_OUT" | sed -n 's/^COST_DELTA=//p')
    REC_COST_BASIS=$(printf '%s\n' "$RCD_OUT" | sed -n 's/^COST_BASIS=//p')
    [ -z "$REC_COST_DELTA" ] && REC_COST_DELTA=unknown
    [ -z "$REC_COST_BASIS" ] && REC_COST_BASIS=unknown
  fi
  if [ "$REC_COST_DELTA" != "unknown" ]; then
    RBINC="$REC_COST_DELTA"
    case "$(python3 "$HELPER" is-negative "$REC_COST_DELTA" 2>/dev/null; echo $?)" in
      0) RBINC="$U_COST";;   # 负 delta=基线重置，按本轮 reported 计
    esac
    [ "$RBINC" != "unknown" ] && python3 "$HELPER" budget-add "$RBINC" >/dev/null 2>&1 || true
  fi
  helper emit-recover-ledger "$TURN_ID" "${NEW_SID:-none}" "$U_IN" "$U_OUT" "$U_COST" "$REC_COST_DELTA" "$U_CUM" >/dev/null || true
  append_engine_logs
  CAMPAIGN_STATE="" ; MEASURE_STATE="" ; ROOT_STATUS=""
  python3 "$HELPER" write-recover-result "$TD" "$TURN_ID" "$PROJ_DIR" "$U_IN" "$U_OUT" || exit 4
  python3 "$RUNTIME_CONTRACT" receipt "$TD" || exit 4
  if [ -f "$WORK_UNIT" ]; then
    REC_SID_ARG="$SID_FILE"
    if [ -n "$NEW_SID" ]; then
      printf '%s\n' "$NEW_SID" > "$TD/.recovered-sid"
      REC_SID_ARG="$TD/.recovered-sid"
    fi
    if python3 "$WORK_UNIT" settle "$TD" --rc 0 --session-file "$REC_SID_ARG"; then
      if [ -n "$NEW_SID" ] && python3 -c 'import json,sys
try:
 d=json.load(open(sys.argv[1]))
 sys.exit(1 if d.get("promoted") is False else 0)
except Exception:
 sys.exit(0)' "$TD/work-settlement.json" 2>/dev/null; then
        printf '%s\n' "$NEW_SID" > "$SID_FILE.tmp-$$" && mv "$SID_FILE.tmp-$$" "$SID_FILE"
      elif [ -n "$NEW_SID" ]; then
        say "recover：旧轮次 SID $NEW_SID 未覆盖当前主会话（work-settlement promoted=false）"
      fi
    else
      say "Recovered work handoff can be reconstructed from the saved result."
    fi
  elif [ -n "$NEW_SID" ]; then
    printf '%s\n' "$NEW_SID" > "$SID_FILE.tmp-$$" && mv "$SID_FILE.tmp-$$" "$SID_FILE"
  fi
  python3 "$HELPER" finalize-engine-json "$TD/engine.json" recovered || true
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
python3 "$RUNTIME_SUPPORT" bootstrap || { emit_reason "ACCOUNTING"; exit 4; }
helper init >/dev/null || exit 4
DECISION_FILE=$(mktemp); helper prepare > "$DECISION_FILE" || { rm -f "$DECISION_FILE"; exit 4; }
. "$DECISION_FILE"; rm -f "$DECISION_FILE"

# ── 2) 阻塞与等待（C6：结构化 reason 由控制器按原因转状态）──
BLOCK_CHECK=$(mktemp); helper block-check > "$BLOCK_CHECK" || { rm -f "$BLOCK_CHECK"; exit 4; }
. "$BLOCK_CHECK"; rm -f "$BLOCK_CHECK"
GUIDANCE_WAKE=no
if [ "${LEMVO_RESOURCE_WAKE:-}" = user-advance ] || { [ -n "${CALLER_MSG:-}" ] && [ -f "${CALLER_MSG:-}" ]; }; then
  GUIDANCE_WAKE=yes
fi
if [ "$GATE_KIND" = done ]; then emit_reason "CAMPAIGN_DONE"; exit 6; fi
if [ "$GATE_KIND" = hard ] || { [ "$GATE_KIND" = human ] && [ "$GUIDANCE_WAKE" != yes ]; }; then
  say "阻塞态 $BLOCKING（ACTIVE 不得绕过）——不调用引擎"
  if [ "$RC_MODE" = "structured" ]; then emit_reason "$BLOCKING"; exit 6; fi
  exit 0
fi
if [ "$WAITING" = "yes" ] && [ -z "${LEMVO_RESOURCE_WAKE:-}" ]; then
  say "WAITING_RESOURCE——减频等待"
  if [ "$RC_MODE" = "structured" ]; then emit_reason "WAITING_RESOURCE"; exit 7; fi
  sleep 600; exec "$SELF" "$@"
fi
# 文件级标志在结构化模式下同样映射 rc=6（读态后再查一次）
for f in STOP HOLD CAMPAIGN-DONE BLOCKED-USER; do
  if [ "$RC_MODE" = "structured" ] && [ -f "$f" ]; then
    if [ "$GATE_KIND" = human ] && [ "$GUIDANCE_WAKE" = yes ]; then continue; fi
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
  A) 继续：AUTHORIZE_JUSTIFICATION='<新增益预期>' 重跑（一次性续行 +10；新授权可加 AUTHORIZE_REQUEST_ID）
  B) 收窄：修改 $STATE 的 scope/ready_blocker 后，AUTHORIZE_JUSTIFICATION='<收窄说明>' 重跑
  C) 终止：touch CAMPAIGN-DONE（交付当前状态；准备耗尽 != 方向被证伪，记 untested）
  D) 用户显式重置：编辑 $STATE 对应计数字段（须在 research-log 留一句重置理由）
MSG
  helper emit-over-budget "$2" "$PREP_TURNS" "$EFFECTIVE_CAP" "$WALL_MIN" >/dev/null || true
  exit 2
}
if [ "$PREP_TURNS" -ge "$EFFECTIVE_CAP" ] && ! { [ "$GATE_KIND" = human ] && [ "$GUIDANCE_WAKE" = yes ]; }; then
  if [ "$(python3 "$HELPER" auth-available)" = "yes" ]; then
    AUTH=$(mktemp)
    OLD_CAP="$EFFECTIVE_CAP"
    [ "$PREP_TURNS" -gt "$EFFECTIVE_CAP" ] && EFFECTIVE_CAP="$PREP_TURNS"
    EFFECTIVE_CAP=$((EFFECTIVE_CAP + 10))
    python3 "$HELPER" auth-json "$EFFECTIVE_CAP" "$(date -u +%FT%TZ)" > "$AUTH" || { rm -f "$AUTH"; say "授权序列化失败"; emit_reason "ACCOUNTING"; exit 5; }
    if helper ledger-append < "$AUTH"; then
      say "一次性授权续行已记账（上限 $OLD_CAP → $EFFECTIVE_CAP）：$AUTHORIZE_JUSTIFICATION"
    else
      rm -f "$AUTH"; say "授权记账失败——拒绝在无账状态下超预算执行"; emit_reason "ACCOUNTING"; exit 5
    fi
    rm -f "$AUTH"
  else
    over_budget "准备停滞 $PREP_TURNS 轮 ≥ 上限 $EFFECTIVE_CAP" "prep-stall"
  fi
fi
if [ "$PREP_WALL_CAP" != "0" ]; then
  WALL_OK=$(python3 "$HELPER" gte-float "${WALL_MIN:-0}" "$PREP_WALL_CAP" 2>/dev/null && echo yes || echo no)
  [ "$WALL_OK" = "yes" ] && over_budget "累计墙钟 ${WALL_MIN}min ≥ 上限 ${PREP_WALL_CAP}min" "wall-time"
fi

# ── 3b) 金额总预算派发前闸门（F2：unit=usd 且 used>=cap → 零调用决策点）──
BG=$(python3 "$HELPER" budget-gate 2>/dev/null || true)
BUDGET_EXHAUSTED=$(printf '%s\n' "$BG" | sed -n 's/^BUDGET_EXHAUSTED=//p')
BUDGET_USED_NOW=$(printf '%s\n' "$BG" | sed -n 's/^BUDGET_USED=//p')
BUDGET_CAP_NOW=$(printf '%s\n' "$BG" | sed -n 's/^BUDGET_CAP=//p')
if [ "$BUDGET_EXHAUSTED" = "yes" ]; then
  say "已授权金额上限耗尽：budget.used=${BUDGET_USED_NOW} ≥ total_cap=${BUDGET_CAP_NOW}（usd，cap_authorized=yes 且 cap_source 可核对）——派发前拦截，本轮不调用引擎"
  emit_reason "OVER_BUDGET"
  cat <<MSG
金额上限决策选项（任选其一，全部留痕）：
  A) 用户提高上限：编辑 $STATE 的 budget.total_cap，并把 cap_authorized: yes、
     cap_source 注明用户指令出处（来源不可核对的数字不构成授权），记 authorize 事件后重跑
  B) 收窄：修改 scope/ready_blocker 后重跑
  C) 终止：touch CAMPAIGN-DONE（交付当前状态；预算耗尽 ≠ 方向被证伪，记 untested）
  D) 查明细：budget.used 由账本逐轮累计（口径见 cost_basis 字段；负 delta=基线重置已按 reported 计）
说明：未设上限或 cap_authorized 非 yes 时费用只统计、不停止，无需逐轮补授权。
MSG
  helper emit-over-budget budget-exhausted "$BUDGET_USED_NOW" "$BUDGET_CAP_NOW" >/dev/null || true
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
- 下一步构造动作：$(grep -E 'next_constructive_experiment|next_min_probe' "$STATE" 2>/dev/null | head -n 1 | cut -d: -f2-)
按当前方法路线与策略台账（RESEARCH STRATEGY LEDGER）推进：工程冒烟从简验证，科学迭代优先推进有机制/结构区分度的 T2/T3 改动，不陷在同家族 T1 微调里空转；确实缺少执行前提才写 ready_blocker（含解除条件），不做泛化审计。
MSG
  fi
  MSG_SOURCE="$OWN_SUMMARY"; MSG_OWNED=yes
fi

# ── R2/R3：turn 目录 + manifest（控制器给 LEMVO_TURN_DIR 则用之，否则自建）──
FULL_MSG=$(mktemp)
python3 "$RUNTIME_SUPPORT" prompt "$MSG_SOURCE" "$FULL_MSG" || { rm -f "$FULL_MSG"; emit_reason "PROTOCOL"; exit 9; }
MSG_SOURCE="$FULL_MSG"
TD="${LEMVO_TURN_DIR:-}"
TURN_ID="${LEMVO_TURN_ID:-}"
if [ -z "$TD" ]; then
  TURN_ID="t$(date -u +%Y%m%dT%H%M%SZ)-$$-solo"
  TD="turns/$TURN_ID"
  mkdir -p "$TD"
export LEMVO_TURN_DIR="$TD" LEMVO_TURN_ID="$TURN_ID"
  python3 "$HELPER" write-manifest-solo "$TD" "$TURN_ID" "$PROJ_DIR" "$ENGINE" || exit 4
fi
mkdir -p "$TD"
export LEMVO_TURN_DIR="$TD" LEMVO_TURN_ID="$TURN_ID"
# A work packet is an execution handoff, never a new scientific admission gate.
SESSION_ACTION=resume
if [ -f "$WORK_UNIT" ]; then
  UNIT_MSG="$TD/context-prompt.md"
  if [ -z "${LEMVO_MESSAGE_KIND:-}" ]; then
    if [ "$MSG_OWNED" = yes ]; then export LEMVO_MESSAGE_KIND=runtime; else export LEMVO_MESSAGE_KIND=user; fi
  fi
  if [ "${LEMVO_MESSAGE_KIND:-}" = user ] && [ -z "${LEMVO_CURRENT_MESSAGE_PATH:-}" ]; then
    export LEMVO_CURRENT_MESSAGE_PATH="${CALLER_MSG:-}"
  fi
  if python3 "$WORK_UNIT" prepare "$TD" "$MSG_SOURCE" "$UNIT_MSG" --engine "$ENGINE" --session-file "$SID_FILE"; then
    MSG_SOURCE="$UNIT_MSG"
    SESSION_ACTION=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("session_action","resume"))' "$TD/context-plan.json")
  else
    say "Work packet warning: use existing runtime message; original task, state and authorization remain available."
  fi
fi
ENGINE_JSON="$TD/engine.json"
ADAPTER_START=$(pid_start $$)
python3 "$HELPER" write-engine-json "$ENGINE_JSON" "$$" "$ADAPTER_START" "$ENGINE" || exit 4
echo "$TURN_ID|$$|$ADAPTER_START" > .autoloop-turn-active
touch "$TD/.turn-start"   # BUG-3：本轮产物判定的 mtime 基准
cleanup_turn() {
  rm -f "$FULL_MSG"
  if [ "$(python3 "$RUNTIME_SUPPORT" epoch)" = "$SESSION_EPOCH" ]; then
    grep -q "^$TURN_ID|" .autoloop-turn-active 2>/dev/null && rm -f .autoloop-turn-active .autoloop-engine.pid
  fi
  return 0
}
trap cleanup_turn EXIT
python3 "$RUNTIME_SUPPORT" progress-snapshot "$TD/progress-before.json" || exit 4
python3 "$RUNTIME_CONTRACT" freeze "$TD" || exit 4
python3 "$RUNTIME_CONTRACT" progress-begin "$TD" || exit 4

# ── result.json（E-result：结果驱动闭环的每轮凭据）──
write_result() {  # $1=status $2=next_action $3=engine_rc
  python3 "$HELPER" write-turn-result "$1" "$2" "$3" "$TD" "$TURN_ID" "$PROJ_DIR" "${CAMPAIGN_STATE:-}" "${MEASURE_STATE:-}" "${ROOT_STATUS:-}" || return 4
  python3 "$RUNTIME_CONTRACT" receipt "$TD" || return 4
  if [ -f "$WORK_UNIT" ]; then
    local work_rc="$3"
    case "$1" in failed|service-error|protocol-error) [ "$work_rc" = 0 ] && work_rc=9;; esac
    python3 "$WORK_UNIT" settle "$TD" --rc "$work_rc" --session-file "$SID_FILE" \
      || say "Work result warning: runtime result and original evidence retained; next turn can reconstruct the handoff."
  fi
}
finalize_engine_json() {  # $1=最终 status
  python3 "$HELPER" finalize-engine-json "$ENGINE_JSON" "$1" || true
  return 0
}

# ── 5) 真续接调用（双引擎；输出入 turn 目录；PID+starttime 身份发布）──
run_engine() {
  local engpid engstart watch_rc heartbeat_pid=""
  local -a event_settings=()
  SID=""
  python3 "$RUNTIME_SUPPORT" watchdog-config || { emit_reason "CONFIG_INVALID"; exit 64; }
  # Project-local, immutable hook helper. This never changes permissions or calls a model.
  # Failed/disabled hook configuration is visible and does not prevent useful research.
  if [ "$ENGINE" = "claude" ] && [ -f "$(dirname "$SELF")/configure-event-runtime.py" ]; then
    python3 "$(dirname "$SELF")/configure-event-runtime.py" install --root "$PROJ_DIR" \
      --engine claude --templates "$(dirname "$SELF")" > "$TD/event-runtime-install.json" \
      || say "Event wait hook could not be configured; see project installation status."
    if python3 "$HELPER" is-event-active "$TD/event-runtime-install.json"
    then
      event_settings=(--settings "$PROJ_DIR/.claude/settings.local.json")
    fi
  fi
  if [ "$ENGINE" = "claude" ]; then
    if [ "$SESSION_ACTION" = resume ] && [ -s "$SID_FILE" ]; then
      SID="$(cat "$SID_FILE")"
      CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0 "$CLAUDE_BIN" "${event_settings[@]}" -p "$(cat "$MSG_SOURCE")" --resume "$SID" --output-format stream-json --verbose --include-partial-messages $CLAUDE_PERMS > "$TD/engine-stdout.jsonl" 2> "$TD/engine-stderr.log" &
    else
      CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0 "$CLAUDE_BIN" "${event_settings[@]}" -p "$(cat "$MSG_SOURCE")" --output-format stream-json --verbose --include-partial-messages $CLAUDE_PERMS > "$TD/engine-stdout.jsonl" 2> "$TD/engine-stderr.log" &
    fi
  else
    if [ "$SESSION_ACTION" = resume ] && [ -s "$SID_FILE" ]; then
      SID="$(cat "$SID_FILE")"
      codex exec --skip-git-repo-check --json -s "$CODEX_SANDBOX" resume "$SID" "$(cat "$MSG_SOURCE")" < /dev/null > "$TD/engine-stdout.jsonl" 2> "$TD/engine-stderr.log" &
    else
      codex exec --skip-git-repo-check --json -s "$CODEX_SANDBOX" "$(cat "$MSG_SOURCE")" < /dev/null > "$TD/engine-stdout.jsonl" 2> "$TD/engine-stderr.log" &
    fi
  fi
  engpid=$!
  engstart=$(pid_start "$engpid")
  echo "$engpid ${engstart:-unknown}" > .autoloop-engine.pid
  # A real hook can block for hours while training. Surface only that verified CPU wait
  # to the existing CLI watchdog; it is not a scientific-progress claim or model call.
  if [ "$ENGINE" = "claude" ] && [ -f "$(dirname "$SELF")/runtime-heartbeat.py" ]; then
    python3 "$(dirname "$SELF")/runtime-heartbeat.py" --root "$PROJ_DIR" --parent "$engpid" \
      >> "$TD/engine-stderr.log" 2>&1 &
    heartbeat_pid=$!
  fi
  python3 "$HELPER" update-engine-pid "$ENGINE_JSON" "$engpid" "${engstart:-unknown}" || true
  python3 "$RUNTIME_SUPPORT" watch-turn "$TD" "$TURN_ID" "$engpid" "${engstart:-unknown}"
  watch_rc=$?
  if [ "$watch_rc" -ne 0 ] && [ "$watch_rc" -ne 124 ]; then
    say "WATCHDOG_ERROR: supervision failed; CLI is still owned, inspect .lemvo-status."
    printf 'RUNNING|WATCHDOG_ERROR|turn_id=%s\n' "$TURN_ID" > .lemvo-status
  fi
  wait "$engpid"; rc=$?
  if [ "$rc" -eq 0 ] && [ "$watch_rc" -ne 124 ] && [ -f "$RUNTIME_IDLE" ]; then
    # Capture before bookkeeping and before publishing successful completion.
    # If this write fails, recovery has only the recorded dispatch versions.
    python3 "$RUNTIME_IDLE" capture-worker "$TD" || say "Idle worker snapshot unavailable; preserve dispatch versions for recovery."
  fi
  if [ -n "$heartbeat_pid" ]; then
    kill "$heartbeat_pid" 2>/dev/null || true
    wait "$heartbeat_pid" 2>/dev/null || true
  fi
  export LEMVO_TURN_INTERRUPTED=no
  if [ "$watch_rc" -eq 124 ] || [ -f "$TD/timeout.json" ]; then
    rc=124; export LEMVO_TURN_INTERRUPTED=yes
  elif [ "$rc" -eq 137 ] || [ "$rc" -eq 143 ] || [ "$rc" -eq 130 ]; then
    export LEMVO_TURN_INTERRUPTED=yes
  fi
  rm -f .autoloop-engine.pid
}
CLAUDE_PERMS="${CLAUDE_PERMS_EXTRA:-}"   # 窄白名单入口（如 --allowedTools ...）；不受信任目录下项目 settings 会被忽略，CLI 级白名单不受影响
[ "${CLAUDE_SKIP_PERMS:-0}" = "1" ] && CLAUDE_PERMS="--dangerously-skip-permissions"
CODEX_SANDBOX="${CODEX_SANDBOX:-workspace-write}"   # BUG-2：0.16x exec 默认只读+需审批；显式给可写工作区（campaign 目录=cwd）；不要用 danger-full-access 做全局默认
run_engine

if [ "$(python3 "$RUNTIME_SUPPORT" epoch)" != "$SESSION_EPOCH" ]; then
  say "Session was reset; this old turn will not write SID, state or accounting."
  emit_reason "STALE_SESSION_EPOCH"; exit 9
fi

# BUG-3：产物判定在引擎结束后立即做（settle/account 的账本书写不算研究产物；
# 引擎对 research-state.yaml 的真实编辑算产物）
NEW_ARTIFACT=""
NEW_ARTIFACT=$(python3 "$RUNTIME_SUPPORT" progress-changed "$TD/progress-before.json") || exit 4
export LEMVO_PROGRESS_INCOMPLETE="$(python3 "$RUNTIME_SUPPORT" snapshot-incomplete "$TD/progress-before.json")"
[ "$LEMVO_PROGRESS_INCOMPLETE" = yes ] && say "Progress inspection incomplete (bounded scan): unknown, not evidence of no research progress. Use an explicit artifact or snapshot exclusions."
export LEMVO_WAIT_ASSESSMENT=no

# ── 6) 会话 ID 提取（claude session_id | codex thread_id | 文本三拼写）──
NEW_SID=$( grep -h -oE '"session_id"[": ]+[a-f0-9-]{8,}' "$TD/engine-stdout.jsonl" "$TD/engine-stderr.log" 2>/dev/null | head -1 | grep -oE '[a-f0-9]{8}-[a-f0-9-]+' \
        || grep -h -oE '"thread_id"[": ]+[a-f0-9-]{8,}' "$TD/engine-stdout.jsonl" 2>/dev/null | head -1 | grep -oE '[a-f0-9]{8}-[a-f0-9-]+' \
        || grep -h -oiE 'session( id|_id)?: [a-f0-9-]{8,}' "$TD/engine-stdout.jsonl" 2>/dev/null | head -1 | grep -oE '[a-f0-9]{8}-[a-f0-9-]+' \
        || true )
# Attribute usage to the actual attempt. Commit a new session pointer only after
# a successful terminal outcome, so failed fresh dispatch keeps the recoverable SID.
CUR_SID="${NEW_SID:-${SID:-}}"
# An interrupted first invocation can already have a real recoverable session.
# Preserve that identity when there is no prior pointer; do not replace an older
# usable session after a failed fresh attempt or count this as successful work.
if [ "${LEMVO_TURN_INTERRUPTED:-no}" = yes ] && [ -n "$NEW_SID" ] && [ ! -s "$SID_FILE" ]; then
  printf '%s\n' "$NEW_SID" > "$SID_FILE.tmp-$$" && mv "$SID_FILE.tmp-$$" "$SID_FILE"
fi

# ── 7) 失败分类（A1 收口：服务/账号错误 ≠ 会话死亡 ≠ 研究停滞）──
if [ "$LEMVO_TURN_INTERRUPTED" = yes ]; then
  say "CLI turn interrupted: retain SID and queued guidance; no prep/stall/poison verdict."
  if [ -f "$TD/timeout.json" ]; then emit_reason "TURN_TIMEOUT"; else emit_reason "TURN_INTERRUPTED"; fi
elif [ "$rc" -ne 0 ]; then
  # r29-fix2：优先按结构化终止事件分类（codex turn.failed / claude result
  # error 的 error 消息），全日志 grep 降级为无终止事件时的兜底——防止早期
  # 非致命配置警告遮蔽真正终止原因（试点实录：日志前部有 unrecognized
  # configuration 忽略警告，真正终止事件是 workspace routing discovery
  # timed out；旧逻辑对全日志 grep 把网络终止误判 CONFIG_INVALID/rc64）。
  OB=$(python3 "$HELPER" outcome-from "$TD/engine-stdout.jsonl" 2>/dev/null || true)
  OUTCOME=$(printf '%s\n' "$OB" | sed -n 's/^OUTCOME=//p')
  ERRMSG=$(printf '%s\n' "$OB" | sed -n 's/^ERRMSG=//p')
  if [ "$OUTCOME" = "failed" ] && [ -n "$ERRMSG" ]; then
    TERM_TEXT="$ERRMSG"
  else
    TERM_TEXT="$(tail -n 20 "$TD/engine-stderr.log" 2>/dev/null; tail -n 5 "$TD/engine-stdout.jsonl" 2>/dev/null)"
  fi
  # BUG-5：config 拒载判定的对象是真正的终止原因（terminate-reason-first），
  # 不是全日志任一匹配——警告 ≠ 终止。
  if printf '%s' "$TERM_TEXT" | grep -qiE "unknown variant|unrecognized configuration|invalid.*config|failed to (parse|load).*config"; then
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
    SE_COST_DELTA=unknown; SE_COST_BASIS=unknown
    if [ "$U_COST" != "unknown" ]; then
      SE_CD_OUT=$(python3 "$HELPER" cost-delta "${CUR_SID:-none}" "$U_COST" "$ENGINE" 2>/dev/null || true)
      SE_COST_DELTA=$(printf '%s\n' "$SE_CD_OUT" | sed -n 's/^COST_DELTA=//p')
      SE_COST_BASIS=$(printf '%s\n' "$SE_CD_OUT" | sed -n 's/^COST_BASIS=//p')
      [ -z "$SE_COST_DELTA" ] && SE_COST_DELTA=unknown
      [ -z "$SE_COST_BASIS" ] && SE_COST_BASIS=unknown
    fi
    helper emit-service-error "$TURN_ID" "${CUR_SID:-none}" "$rc" "$U_IN" "$U_OUT" "$U_CR" "$U_CW" "$U_COST" "$SE_COST_DELTA" "$SE_COST_BASIS" >/dev/null || true
    if [ "$SE_COST_DELTA" != "unknown" ]; then
      SE_BINC="$SE_COST_DELTA"
      case "$(python3 "$HELPER" is-negative "$SE_COST_DELTA" 2>/dev/null; echo $?)" in
        0) SE_BINC="$U_COST";;
      esac
      [ "$SE_BINC" != "unknown" ] && python3 "$HELPER" budget-add "$SE_BINC" >/dev/null 2>&1 || true
    fi
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
  if [ "$OUTCOME" != "ok" ]; then
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
  if [ -n "$NEW_SID" ] && [ "${LEMVO_TURN_INTERRUPTED:-no}" != yes ]; then
    printf '%s\n' "$NEW_SID" > "$SID_FILE.tmp-$$" && mv "$SID_FILE.tmp-$$" "$SID_FILE"
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
PROGRESS_ARGS=()
[ "${LEMVO_TURN_INTERRUPTED:-no}" = yes ] && PROGRESS_ARGS+=(--interrupted)
[ "$rc" -ne 0 ] && PROGRESS_ARGS+=(--failed)
python3 "$RUNTIME_CONTRACT" progress-end "$TD" "${PROGRESS_ARGS[@]}" >/dev/null || exit 4
export LEMVO_PROGRESS_RECORD="$TD/progress.json"
PROG_M=$(mktemp); python3 "$HELPER" progress-meta "$LEMVO_PROGRESS_RECORD" > "$PROG_M" || { rm -f "$PROG_M"; exit 4; }
. "$PROG_M"; rm -f "$PROG_M"
export LEMVO_WAIT_ASSESSMENT="${WAIT_ASSESS:-no}"
# Read final state, not the state loaded before the model worked.
POST_STATE=$(mktemp); helper prepare > "$POST_STATE" || exit 4
. "$POST_STATE"; rm -f "$POST_STATE"
SETTLE=$(mktemp); helper settle > "$SETTLE" || { rm -f "$SETTLE"; say "Accounting failed; preserving output and queued inputs."; emit_reason "ACCOUNTING"; exit 4; }
. "$SETTLE" 2>/dev/null || true; rm -f "$SETTLE"
parse_usage "$TD/engine-stdout.jsonl" "${CUR_SID:-none}"
if [ "$U_IN" = "unknown" ] || [ "$U_OUT" = "unknown" ]; then
  say "Usage warning: missing/unrecognized token fields; counters, wall time and liveness checks remain active."
fi
TURN_ELAPSED=$(( $(date +%s) - TURN_START ))
python3 "$HELPER" account "$TURN_ELAPSED" "$U_IN" "$U_OUT" >/dev/null \
  || { emit_reason "ACCOUNTING"; exit 4; }
# 费用口径：reported=引擎报告值；delta=同 SID 对上次报告求差（claude resume 累计量）；
# codex 每调独立（per-call）。不把累计值逐轮相加，state total_cost 保持不累计。
COST_DELTA=unknown; COST_BASIS=unknown
if [ "$U_COST" != "unknown" ]; then
  CD_OUT=$(python3 "$HELPER" cost-delta "${CUR_SID:-none}" "$U_COST" "$ENGINE" 2>/dev/null || true)
  COST_DELTA=$(printf '%s\n' "$CD_OUT" | sed -n 's/^COST_DELTA=//p')
  COST_BASIS=$(printf '%s\n' "$CD_OUT" | sed -n 's/^COST_BASIS=//p')
  [ -z "$COST_DELTA" ] && COST_DELTA=unknown
  [ -z "$COST_BASIS" ] && COST_BASIS=unknown
fi
PREP_SNAP=$(python3 "$HELPER" prep-snapshot 2>/dev/null || true)
T_ART=$(printf '%s\n' "$PREP_SNAP" | grep -oE 'LAST_ARTIFACT=.*' | cut -d= -f2-)
[ -z "$T_ART" ] && T_ART=none
export T_ART
SESSION_TAG="${CUR_SID:-none}"
T_TYPE="work-turn"; [ "${NEW_PROGRESS:-no}" != "yes" ] && T_TYPE="prep-turn"
[ "${PROG_KIND:-}" = waiting ] && T_TYPE="wait-assessment"
[ "$rc" -ne 0 ] && T_TYPE="runtime-error"
[ "$LEMVO_TURN_INTERRUPTED" = yes ] && T_TYPE="runtime-interrupted"
T_PREP="${PREP_TURNS_NOW:-unknown}"; T_TOTAL="${TOTAL_TURNS_NOW:-unknown}"
T_SCOPE="client-estimate;modelUsage=$U_MU;engine=$ENGINE"
T_TYPE="$T_TYPE" T_PREP="$T_PREP" T_TOTAL="$T_TOTAL" T_IN="$U_IN" T_OUT="$U_OUT" \
T_CR="$U_CR" T_CW="$U_CW" T_COST="$U_COST" T_DELTA="$COST_DELTA" T_BASIS="$COST_BASIS" T_CUM="$U_CUM" \
T_SCOPE="$T_SCOPE" T_WALL="$TURN_ELAPSED" T_SID="$SESSION_TAG" T_TURNID="$TURN_ID" T_ART="$T_ART" \
helper emit-turn-ledger >/dev/null || true

# F2/R1：金额累计（负 delta=基线重置按 reported 计；unknown 不加）。
# 单位分账在 helper 内完成：unit=usd → budget.used/remaining（有门）；
# 非 usd → total.cost_usd_total 纯统计（无门）。GPU hours 永不经此路径。
if [ "$COST_DELTA" != "unknown" ]; then
  BINC="$COST_DELTA"
  case "$(python3 "$HELPER" is-negative "$COST_DELTA" 2>/dev/null; echo $?)" in
    0) BINC="$U_COST";;   # 负 delta = 上报基线重置（如 claude 版本口径差异），按本轮 reported 计
  esac
  [ "$BINC" != "unknown" ] && python3 "$HELPER" budget-add "$BINC" >/dev/null 2>&1 || true
fi

append_engine_logs
say "本轮 rc=$rc；NEW_PROGRESS=${NEW_PROGRESS:-?}（类型化凭据=${ARTIFACT_TYPED:-?}）；prep=${PREP_TURNS_NOW:-?}/$EFFECTIVE_CAP；total_turns=${TOTAL_TURNS_NOW:-?}；tokens +${U_IN}/+${U_OUT} cache_r=${U_CR} cache_w=${U_CW} cost=${U_COST}(${COST_BASIS},Δ=${COST_DELTA}) modelUsage=${U_MU}；turn=$TURN_ID；session=$SESSION_TAG"

# ── BUG-3：空转/毒会话检测（引擎 rc=0 也会空转——礼貌 loop 无产物无进展）──
# NEW_ARTIFACT 已在引擎结束时判定（不含账本/控制器写入）
PROGRESS_NEUTRAL="${PROG_NEUTRAL:-no}"
if [ "${LEMVO_TURN_INTERRUPTED:-no}" = yes ]; then
  : # Neither increment nor reset research counters for an unfinished turn.
elif [ "${NEW_PROGRESS:-no}" = yes ]; then
  echo 0 > .autoloop-stall-count; echo 0 > .autoloop-poison-count
elif [ "$PROGRESS_NEUTRAL" != yes ]; then
  STALL_N=$(( $(cat .autoloop-stall-count 2>/dev/null || echo 0) + 1 )); echo "$STALL_N" > .autoloop-stall-count
fi
# 毒会话：连续 N 轮输出含阻塞话术且零产物 → 清 SID 强制新会话（避免 resume
# 复读污染上下文）；短语可经 LEMVO_POISON_PHRASES 覆盖（| 分隔的 grep -E 模式）
POISON_RE="${LEMVO_POISON_PHRASES:-只读|read.only|READY_TO_PROBE.*不满足|ready_blocker}"
if [ "$PROGRESS_NEUTRAL" != yes ] && [ "${NEW_PROGRESS:-no}" != yes ] && grep -qiE "$POISON_RE" "$TD/engine-stdout.jsonl" 2>/dev/null; then
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
if [ "$PROGRESS_NEUTRAL" != yes ] && [ "${NEW_PROGRESS:-no}" != yes ] && [ "${STALL_N:-0}" -ge "${LEMVO_STALL_ARTIFACT_N:-5}" ]; then
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
POST_GATE=$(mktemp); helper block-check > "$POST_GATE" || exit 4
. "$POST_GATE"; rm -f "$POST_GATE"
case "$GATE_KIND" in human) RESULT_NEXT=needs_user;; hard) RESULT_NEXT=paused;; resource) RESULT_NEXT=waiting_jobs;; done) RESULT_NEXT=completed;; esac
if [ "$rc" -ne 0 ]; then
  RESULT_STATUS="failed"; RESULT_NEXT="runtime_error"
fi
finalize_engine_json "$RESULT_STATUS"
write_result "$RESULT_STATUS" "$RESULT_NEXT" "$rc"
exit $rc
