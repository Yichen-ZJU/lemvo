#!/usr/bin/env bash
# codex-for-research 可逆安装器
# 用法: ./setup.sh install | uninstall | -h|--help
#
# 工程约束（发布前核验 C1-C5 + 二轮 I1-I4，与 install.sh 同语义）：
#   C1 参数先于写入; C2 备份唯一（时间戳+PID，存在即拒）;
#   C3 staging+原子替换; C4 MCP 如实报告、部分失败退出码非 0;
#   I2 事务清单回滚（新建删除/替换还原），恢复消息先校验;
#   I3 符号链接按原位置重建（悬空链接同样恢复）;
#   I4 互斥锁 + SIGTERM/INT 回滚。
set -euo pipefail
set -E  # errtrace：ERR trap 继承进函数（回滚已先事务化，删除目标前验证备份）
CODEX_DIR="$HOME/.codex"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP_ROOT="$HOME/.codex-research-backups"
MANIFEST="$CODEX_DIR/.codex-for-research-manifest"

usage() {
  cat <<'EOF'
用法: ./setup.sh install | uninstall | -h|--help

  install    备份现有 ~/.codex/skills 与 AGENTS.md 后安装（可回退）
  uninstall  按 manifest 移除已装技能，AGENTS.md 恢复最近备份
EOF
}

path_exists() { [ -e "$1" ] || [ -L "$1" ]; }

do_install() {
  # I4: 互斥锁
  local LOCK="$CODEX_DIR/.setup.lock"
  mkdir -p "$CODEX_DIR"
  if ! mkdir "$LOCK" 2>/dev/null; then
    local existing; existing="$(cat "$LOCK/pid" 2>/dev/null || true)"
    if [ -n "$existing" ] && kill -0 "$existing" 2>/dev/null; then
      echo "错误: 另一安装实例正在运行 (pid $existing)。" >&2
      exit 1
    fi
    rm -rf "$LOCK"; mkdir "$LOCK"
  fi
  echo $$ > "$LOCK/pid"

  # C2: 唯一备份目录
  local bak="$BACKUP_ROOT/$(date +%Y%m%d-%H%M%S)-$$"
  if [ -e "$bak" ]; then
    echo "错误: 备份目录已存在（秒级+PID 碰撞）: $bak —— 拒绝覆盖旧备份。" >&2
    rm -rf "$LOCK"; exit 1
  fi
  mkdir -p "$bak/restore"

  # I2/I3: 事务清单
  local TXN="$bak/txn.log"; : > "$TXN"
  txn_add() { printf '%s\t%s\n' "$1" "$2" >> "$TXN"; }

  rollback() {
    local failures=0 op path rel orig
    echo "==> 安装中断/失败，按事务清单回滚 ..."
    if [ -s "$TXN" ]; then
      while IFS=$'\t' read -r op path; do
        [ -n "$op" ] || continue
        case "$op" in
          created) rm -rf -- "$path" ;;
          replaced)
            rel="${path#$CODEX_DIR/}"
            orig="$bak/restore/$rel"
            # I01: 备份确认存在才动目标；缺失则保现状并告警，绝不删唯一原件
            if path_exists "$orig"; then
              rm -rf -- "$path"
              # cp -a 重建链接本身（含悬空链接）
              mkdir -p "$(dirname "$path")"
              cp -a -- "$orig" "$path"
            else
              echo "    ⚠️ 备份缺失，保留现状不删: $path" >&2
              failures=$((failures+1))
            fi ;;
        esac
      done < <(tac "$TXN" 2>/dev/null || tail -r "$TXN")
    fi
    while IFS=$'\t' read -r op path; do
      [ -n "$op" ] || continue
      case "$op" in
        created) path_exists "$path" && { echo "    ⚠️ 应已删除但仍存在: $path"; failures=$((failures+1)); } ;;
        replaced) path_exists "$path" || { echo "    ⚠️ 应已还原但缺失: $path"; failures=$((failures+1)); } ;;
      esac
    done < "$TXN"
    if [ "$failures" -eq 0 ]; then
      echo "==> 回滚完成且已校验：恢复到安装前状态（备份保留在 $bak）"
    else
      echo "==> 回滚后有 $failures 处不一致，请对照 $bak/restore 手动修复。" >&2
    fi
    rm -rf "$LOCK"
  }
  fail_install() {  # 显式统一失败路径（S3）；I03: 恢复旧 manifest
    trap - ERR TERM INT
    if [ -f "$bak/manifest.prev" ]; then
      cp "$bak/manifest.prev" "$MANIFEST"
    else
      rm -f "$MANIFEST"
    fi
    rollback
    exit 1
  }
  trap 'trap - ERR TERM INT; rollback; exit 130' TERM INT
  trap 'trap - ERR TERM INT; rollback; exit 1' ERR

  # 1) 备份原版（整体快照，独立于事务还原目录）
  if [ -d "$CODEX_DIR/skills" ]; then cp -r "$CODEX_DIR/skills" "$bak/skills"; fi
  if [ -f "$CODEX_DIR/AGENTS.md" ]; then cp "$CODEX_DIR/AGENTS.md" "$bak/AGENTS.md"; fi
  echo "✓ 原版已备份: $bak"

  # 2) 安装技能（staging -> 原子替换，逐项事务；manifest 先写临时文件，
  #    全部成功后原子替换，失败由 fail_install 恢复旧 manifest —— I03）
  mkdir -p "$CODEX_DIR/skills"
  local manifest_tmp="$bak/manifest.new"
  : > "$manifest_tmp"
  # 备份现有 manifest（若有），失败恢复用
  [ -f "$MANIFEST" ] && cp "$MANIFEST" "$bak/manifest.prev"
  local n=0 name stage
  for s in "$REPO_DIR/skills"/*/; do
    name="$(basename "$s")"
    stage="$CODEX_DIR/skills/.staging-$name-$$"
    rm -rf "$stage"
    cp -r "$s" "$stage"
    if path_exists "$CODEX_DIR/skills/$name"; then
      mkdir -p "$bak/restore/skills"
      # I01: 先确认原件备份成功落盘，再登记可恢复替换（S3: 显式判定，不靠 ERR trap）
      if ! mv -T -- "$CODEX_DIR/skills/$name" "$bak/restore/skills/$name" 2>/dev/null \
         && ! mv -- "$CODEX_DIR/skills/$name" "$bak/restore/skills/$name"; then
        echo "错误: 备份 $name 失败 —— 目标未改动，安装中止并回滚。" >&2
        fail_install
      fi
      txn_add replaced "$CODEX_DIR/skills/$name"
      if ! mv -T -- "$stage" "$CODEX_DIR/skills/$name"; then
        echo "错误: 安装 $name 失败 —— 回滚。" >&2
        fail_install
      fi
    else
      txn_add created "$CODEX_DIR/skills/$name"
      if ! mv -T -- "$stage" "$CODEX_DIR/skills/$name"; then
        echo "错误: 安装 $name 失败 —— 回滚。" >&2
        fail_install
      fi
    fi
    echo "$name" >> "$manifest_tmp"; n=$((n+1))
  done

  # 3) AGENTS.md（事务）
  local stage_md="$CODEX_DIR/AGENTS.md.new-$$"
  cp "$REPO_DIR/AGENTS.md" "$stage_md"
  if path_exists "$CODEX_DIR/AGENTS.md"; then
    txn_add replaced "$CODEX_DIR/AGENTS.md"
    mv -T -- "$CODEX_DIR/AGENTS.md" "$bak/restore/AGENTS.md"
    mv -T -- "$stage_md" "$CODEX_DIR/AGENTS.md"
  else
    txn_add created "$CODEX_DIR/AGENTS.md"
    mv -T -- "$stage_md" "$CODEX_DIR/AGENTS.md"
  fi

  mv -T "$manifest_tmp" "$MANIFEST"   # I03: 全部成功才替换 manifest
  trap - ERR TERM INT
  rm -rf "$LOCK"

  # 4) arxiv MCP 注册（C4: 如实报告）
  local mcp_status=ok
  set +e
  bash "$REPO_DIR/scripts/register-arxiv-mcp.sh"
  local rc=$?
  set -e
  case $rc in
    0) mcp_status=ok ;;
    3) mcp_status=skipped ;;
    *) mcp_status=failed ;;
  esac

  echo
  echo "==> 安装结果汇总"
  echo "    skills:     ok ($n 个)"
  echo "    AGENTS.md:  ok"
  echo "    MCP arxiv:  $mcp_status"
  echo "    备份:       $bak"
  echo "    回退: ./setup.sh uninstall；被覆盖原件从 $bak/restore 复制回原位（删备份≠恢复）"
  if [ "$mcp_status" = failed ]; then
    echo "⚠️  arxiv MCP 注册失败（见上方输出），退出码 1。" >&2
    exit 1
  fi
}

do_uninstall() {
  if [ ! -f "$MANIFEST" ]; then echo "未找到 manifest（非本安装器安装），拒绝盲删。"; exit 1; fi
  local n=0
  while read -r name; do
    [ -n "$name" ] && rm -rf "$CODEX_DIR/skills/$name" && n=$((n+1))
  done < "$MANIFEST"
  rm -f "$MANIFEST"
  local last; last="$(ls -1d "$BACKUP_ROOT"/* 2>/dev/null | sort | tail -1)"
  if [ -n "$last" ] && [ -f "$last/AGENTS.md" ]; then cp "$last/AGENTS.md" "$CODEX_DIR/AGENTS.md"; echo "✓ AGENTS.md 已从 $last 恢复"; fi
  echo "✓ 已卸载 $n 个技能。备份保留在 $BACKUP_ROOT（被覆盖的同名原件在各自备份的 restore/ 下，需手动复制回原位）"
}

case "${1:-}" in
  install)   do_install ;;
  uninstall) do_uninstall ;;
  -h|--help) usage ;;
  *) echo "错误: 未知参数 '${1:-}'" >&2; usage >&2; exit 2 ;;
esac
