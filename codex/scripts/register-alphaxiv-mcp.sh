#!/usr/bin/env bash
# Register the alphaxiv MCP server (streamable HTTP) for Codex CLI.
# alphaxiv is the enhanced paper-discovery backend for deep-research
# (semantic search / community discussion). Without it, deep-research
# automatically degrades to the arxiv MCP — full-text still works.
#
# Requires codex >= 0.162 (native streamable-HTTP MCP + bearer token support).
#
# Usage:
#   1. Get a free API key at https://www.alphaxiv.org/
#   2. export ALPHAXIV_API_KEY=<your-key>
#   3. bash scripts/register-alphaxiv-mcp.sh
#
# The token is passed via the environment (bearer_token_env_var) and is
# never written to config.toml.
set -euo pipefail
if [ -z "${ALPHAXIV_API_KEY:-}" ]; then
  echo "ALPHAXIV_API_KEY is not set."
  echo "  1. Get a free key at https://www.alphaxiv.org/"
  echo "  2. export ALPHAXIV_API_KEY=<your-key>"
  echo "  3. re-run: bash scripts/register-alphaxiv-mcp.sh"
  echo "Note: skipping is safe — deep-research degrades to the arxiv MCP automatically."
  exit 1
fi
codex mcp add alphaxiv --url https://api.alphaxiv.org/mcp/v1 \
  --bearer-token-env-var ALPHAXIV_API_KEY
echo "registered. verify with: codex mcp list"
