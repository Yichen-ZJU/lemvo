#!/usr/bin/env bash
# 在 ~/.codex/config.toml 幂等注册 arxiv MCP server（备份后操作）
# 退出码: 0=已注册/本次注册成功; 3=跳过（无配置或无二进制）; 1=失败
set -euo pipefail
CFG="$HOME/.codex/config.toml"
SERVER_BIN="${ARXIV_MCP_SERVER:-$HOME/.local/bin/arxiv-mcp-server}"

if [ ! -f "$CFG" ]; then echo "未找到 $CFG，跳过（codex 未配置？）"; exit 3; fi
if grep -q '^\[mcp_servers\.arxiv\]' "$CFG"; then echo "arxiv MCP 已注册，跳过"; exit 0; fi
if [ ! -x "$SERVER_BIN" ]; then echo "未找到 $SERVER_BIN。安装: pip install arxiv-mcp-server"; exit 3; fi

# 注册前备份；写入后回读验证（C4：按实际生效内容判定，不看写入命令的退出码）
cp "$CFG" "$CFG.bak.$(date +%Y%m%d-%H%M%S)-$$"
cat >> "$CFG" << EOF

[mcp_servers.arxiv]
command = "$SERVER_BIN"
args = []
EOF
if grep -q '^\[mcp_servers\.arxiv\]' "$CFG"; then
  echo "✓ arxiv MCP 已注册到 $CFG（原文件已备份，已回读验证）"
  exit 0
else
  echo "❌ 写入后回读验证失败：$CFG 中未出现 [mcp_servers.arxiv]" >&2
  exit 1
fi
