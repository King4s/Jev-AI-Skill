#!/usr/bin/env bash
# One-time / update setup for Claude Code, Codex and Hermes on Linux, macOS or WSL: creates a
# venv, installs dependencies, copies the skill, registers the MCP server and runs a live
# self-check. Safe to run again after `git pull`.
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$(pwd)"
KEY_FILE="$HOME/.config/jev-loop/typesafe_api_key"

if [ ! -x .venv/bin/python ] || ! .venv/bin/python -m pip --version >/dev/null 2>&1; then
  rm -rf .venv
  if ! python3 -m venv .venv 2>/dev/null; then
    # Debian/Ubuntu without python3-venv (no ensurepip): bootstrap pip from PyPA instead.
    rm -rf .venv
    python3 -m venv --without-pip .venv
    curl -fsSL https://bootstrap.pypa.io/get-pip.py -o .venv/get-pip.py
    .venv/bin/python .venv/get-pip.py -q
    rm -f .venv/get-pip.py
  fi
fi
.venv/bin/python -m pip install -q --upgrade pip
.venv/bin/python -m pip install -q -r requirements.txt

if command -v claude >/dev/null 2>&1; then
  .venv/bin/python install_skill.py skill/jev "$HOME/.claude/skills" --legacy jev-loop jev-route jev-git

  claude mcp remove jev-loop --scope user >/dev/null 2>&1 || true
  claude mcp add jev-loop --scope user -- "$ROOT/.venv/bin/python" "$ROOT/jev_mcp.py"
  echo "MCP server registered: jev-loop -> $ROOT/jev_mcp.py"
else
  echo "claude not on PATH - skipping Claude Code setup."
fi

# Codex: the skill is read from ~/.agents/skills (the shared agent-skills location), and
# the server is registered in ~/.codex/config.toml.
if command -v codex >/dev/null 2>&1; then
  .venv/bin/python install_skill.py skill/jev "$HOME/.agents/skills" --legacy jev-loop

  codex mcp remove jev-loop >/dev/null 2>&1 || true
  codex mcp add jev-loop -- "$ROOT/.venv/bin/python" "$ROOT/jev_mcp.py"
  echo "MCP server registered: jev-loop -> $ROOT/jev_mcp.py (restart Codex)"
else
  echo "codex not on PATH - skipping Codex setup."
fi

# Hermes: same server, same loop; the tools show up as mcp_jev_loop_*.
if command -v hermes >/dev/null 2>&1; then
  HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
  .venv/bin/python install_skill.py skill/jev "$HERMES_HOME/skills" --legacy jev-loop

  # A Hermes stdio MCP subprocess gets a filtered environment, so the key is handed over in
  # the server's env block - but ONLY when Hermes can resolve it: an unresolved
  # ${TYPESAFE_API_KEY} would reach Jev as the key itself instead of falling back to the key
  # file. Without the env block the server reads $KEY_FILE on its own.
  env_args=()
  if grep -q '^TYPESAFE_API_KEY=.' "$HERMES_HOME/.env" 2>/dev/null; then
    env_args=(--env 'TYPESAFE_API_KEY=${TYPESAFE_API_KEY}')
  fi
  hermes mcp remove jev-loop >/dev/null 2>&1 || true
  printf 'y\n' | hermes mcp add jev-loop --command "$ROOT/.venv/bin/python" \
    ${env_args[@]+"${env_args[@]}"} --args "$ROOT/jev_mcp.py"
  echo "MCP server registered: jev-loop -> $ROOT/jev_mcp.py (start a new Hermes session)"
else
  echo "hermes not on PATH - skipping Hermes setup."
fi

if [ -z "${TYPESAFE_API_KEY:-}" ] && [ ! -s "$KEY_FILE" ]; then
  echo "WARNING: no TypeSafe key. Put it in $KEY_FILE (chmod 600) or export TYPESAFE_API_KEY." >&2
  exit 0
fi
.venv/bin/python jev_mcp.py --check