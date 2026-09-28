# AGENTS.md - instructions for AI maintainers

This repository contains Jev-AI-Skill, one installed skill with four capabilities: Loop, Gate,
Route and Git. Keep docs and implementation aligned. Repository docs and source comments are in
English; answer the owner in their language.

## Components

- `jev_mcp.py` is the `jev-loop` MCP server. Jev provides Loop decisions; the server runs
  configured checks, enforces stop conditions and records state and a decision tape. The
  client harness does the work and obtains an independent review. Run state is stored in
  `runs/<id>.state.json`; the tape is `runs/<id>.jsonl`. The same file exposes the four
  stateless Gate tools (`gate_triage`, `gate_verdict`, `gate_decide`, `gate_pick`), logged
  to `runs/gate.jsonl`, and the `--gate <name>` command line for scripts without an MCP
  host. Gate inputs are clipped (`SUMMARY_CHARS`, `ITEM_CHARS`, `CONTEXT_CHARS`,
  `MAX_BATCH`); keep them that way. `--http` serves the same tools over stateless
  streamable HTTP for a shared host (`deploy/jev-loop-http.service`); the installers
  register `JEV_MCP_URL` instead of a local server when it is set.
- `skill/jev/SKILL.md` is the shared skill for Loop, Route and Git. Its standard-library
  helpers are `route.py` and `git_decide.py` in the same directory. Installed skills use
  the name `jev`, in the harness-specific skills directory.
- `loop.py` is a legacy standalone variant using OpenRouter models as executor and
  reviewer. The MCP Loop is the maintained workflow.

The MCP server identity remains `jev-loop`, and the TypeSafe key file remains
`~/.config/jev-loop/typesafe_api_key`. Git policies are private local files under
`~/.config/jev-git/policies/`; never put them in the repository. Route returns a model
tier, skill and delegation recommendation. Git returns repository facts, blocks, display
guidance and argv vectors. The active harness maps capability tiers to models and carries
out authorized work. Neither helper launches an agent or mutates Git state.

## API and installer facts

The TypeSafe System One response shape is documented at
https://docs.typesafe.ai/api: `answers` contains `choice` answers with probabilities and
confidence, or `noul` answers with a probability. The loop uses `jev-latest`. Consult the
live docs before changing API questions or response parsing.

Both installers copy the complete skill and register `jev-loop` for harness CLIs they
find. On Windows, `install.ps1` always copies the Claude Code skill, even if `claude` is
not on PATH; Claude MCP registration is conditional. Codex and Hermes setup is conditional
on each CLI being available. On POSIX, `install.sh` conditionally sets up Claude Code,
Codex and Hermes. It creates a repository-local `.venv`; Windows uses `python` from PATH.
Hermes uses `$HERMES_HOME` when set and otherwise `~/.hermes`. The Hermes installer passes
`TYPESAFE_API_KEY` through an environment reference only if Hermes' `.env` contains the
key; otherwise the server reads its key file. A live `--check` runs only when an API key
is available.

## Workflow

- Keep `jev_mcp.py` as the source of truth for MCP Loop behavior.
- Keep the README, skill instructions, helper docstrings and changelog aligned with code.
- Do not make Jev's decisions in code or in the skill; the server asks Jev for Loop
  decisions. Helpers may apply deterministic rules around Jev's recommendation.
- Do not send raw agent output to Jev; the loop state is compact and structured.
- Treat `checks` as shell commands that execute in the goal's `workdir`.
- Do not claim the Git scanner guarantees detection of secrets or private data. It uses
  heuristic text patterns over outgoing additions and commit subjects.
- Preserve legacy trigger and migration names where installers recognize them, while
  keeping the installed skill's canonical name `jev` and MCP server identity `jev-loop`.

The loop's hard stops are `max_turns`, `max_consecutive_failures` and Jev choosing
`escalate`. A red check by itself is not necessarily a failed turn; see `turn_failed` and
the protocol in `jev_mcp.py` before changing those semantics.

## Sensitive files

Never add `runs/`, `goal.json`, `workspace/` or API keys to Git. Private policy files and
machine-specific configuration stay outside the repository.

## Every change

1. Make the requested change and keep MCP Loop behavior in `jev_mcp.py`.
2. Run `python -m pytest -q tests`; add protocol tests when behavior changes.
3. If `skill/jev/SKILL.md` changes, run the applicable installer to sync installed copies.
4. Add an English entry under `## [Unreleased]` in `CHANGELOG.md`, using `Added`,
   `Changed` or `Fixed` sections.
5. Commit with a clear message ending in the agent's `Co-Authored-By` line.

## Releasing

Versions use local date and time: `yyyy.mm.dd.hhmm`. Release meaningful changes with
`pwsh -File .\release.ps1`. It requires a clean tree and a non-empty `[Unreleased]`
section, then updates `VERSION` and the changelog, commits, tags, pushes and creates a
GitHub release. Do not edit `VERSION` by hand.

After a release, roll it out where the owner runs it. When the owner uses one shared
HTTP server (`deploy/jev-loop-http.service`), pull the server's own clone and restart the
unit if `jev_mcp.py` or its dependencies changed; then, on each client, `git pull` and
rerun the installer **with the same `JEV_MCP_URL`** (without it the installer registers a
local server instead). Over non-interactive SSH, put `~/.local/bin` on `PATH` so the
installer finds `claude`, `codex` and `hermes`. Verify with `claude mcp list` /
`codex mcp list` and one `curl` against the server (see README). Host names, addresses
and the list of machines are private: keep them in the operator's notes, not here.

## Owner-only access

The owner handles TypeSafe API key setup if no key is available and GitHub OAuth scope
changes that require browser device authorization. Never expose a key in command-line
arguments; when transferring one, use a protected channel such as SSH stdin.
