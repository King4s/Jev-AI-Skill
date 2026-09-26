# AGENTS.md - instructions for AI maintainers

This repository is maintained by AI agents, not by a human. The owner states
*what* they want in plain language (usually Danish) and never runs commands,
remembers paths, or edits config by hand. Do the whole job: change, test,
document, commit, release, push. Ask the owner only for real product decisions,
and answer them in Danish.

## What this is

- `jev_mcp.py` - MCP server (`jev-loop`). Jev (TypeSafe, `api.typesafe.ai/v1/systemone`)
  decides route / done / recovery; the server runs checks, hard stops and the tape.
  The client harness (Claude Code / Codex / Hermes) is the executor. Run state:
  `runs/<id>.state.json`, tape: `runs/<id>.jsonl`.
- `skill/jev/SKILL.md` - the harness-neutral skill (interview -> goal.json -> loop).
  Three parts: Loop, Route and Git. The standard-library helpers `route.py` and
  `git_decide.py` live alongside SKILL.md; private Git policies stay in
  `~/.config/jev-git/policies/`, never in the repository.
  One skill, all harnesses: tool names are given per harness inline (`clarify` vs
  `AskUserQuestion` vs plain chat, `delegate_task` vs the `general-purpose` agent vs
  `codex exec`). Installed copies: `~/.claude/skills/jev/` (Claude Code),
  `~/.agents/skills/jev/` (Codex) and `~/.hermes/skills/jev/` (Hermes);
  both installers sync them.
- `loop.py` - older standalone variant (executor/reviewer via OpenRouter). Secondary.
- `tests/test_jev_mcp.py` - protocol tests with Jev mocked. No network, no key needed.

Jev API facts (verified against docs.typesafe.ai/api): response is
`{"answers": {id: {"type": "choice", "choice", "probabilities", "confidence"} |
{"type": "noul", "noul": p}}}`. Model `jev-latest`. Read the live docs
(https://docs.typesafe.ai/llms.txt) before changing questions or parsing.

## Machines

Installed on the owner's machines as a clone in `~/jev-loop` (Linux: venv in `.venv`,
`install.sh`; Windows: `F:\AI-Projekter\jev-loop`, `install.ps1`). SSH aliases are in the
Windows machine's `~/.ssh/config`. Installed (2026-09-24): Windows (Gamer), loki, thor, odin,
mb-pro. Not yet (offline then): tilbud-grok-bot. To update a
machine: `ssh <host> 'cd ~/jev-loop && git pull -q && ./install.sh'`. The key lives in
`~/.config/jev-loop/typesafe_api_key` (mode 600); copy it over SSH stdin, never as an argument.
Health check anywhere: `python jev_mcp.py --check`. On Odin, `mb`'s clone is the working
copy; Hermes reads its own clone in `/home/hermes/jev-loop`.

## Harness facts

- Hermes drives the same loop through the same server; the tools appear as
  `mcp_jev_loop_loop_start` etc. (`mcp_<server>_<tool>`).
- Hermes gives a stdio MCP subprocess a **filtered** environment, so `TYPESAFE_API_KEY`
  reaches the server only through the `env` block in `mcp_servers.jev-loop`, stored as the
  literal `${TYPESAFE_API_KEY}` which Hermes resolves from `~/.hermes/.env`. An unresolved
  placeholder would be sent to Jev as the key, so it is passed only when the key is really
  there - otherwise the server reads `~/.config/jev-loop/typesafe_api_key` itself.
  `install.sh` makes that call.
- `hermes mcp add NAME --command ... [--env K=V] --args ...` is discovery-first and asks
  "Enable all N tools?". With stdin piped (`printf 'y\n' |`) it is non-interactive, exits 0
  and writes `enabled: true`; `--args` must be last. `HERMES_HOME` redirects the whole
  config, so the install path can be tested against a throwaway home.

## Every change

1. Make the change. Keep `jev_mcp.py` the source of truth for loop behaviour.
2. `python -m pytest -q tests` must pass. Add a test for new protocol behaviour.
3. If you changed `skill/jev/SKILL.md`, sync the installed copies: run `./install.sh`
   and/or `.\install.ps1` (both are safe to re-run; each syncs the skill and registers the
   server). The copies live in `~/.claude/skills/jev/`, `~/.agents/skills/jev/`
   and `~/.hermes/skills/jev/`.
4. Add a line under `## [Unreleased]` in `CHANGELOG.md` (English, sections
   `Added` / `Changed` / `Fixed`). Everything in the repo and on GitHub (docs, changelog,
   release notes, comments, commit messages) is in English; only replies to the owner are Danish.
5. Commit with a clear message ending in the agent's `Co-Authored-By` line.

## Releasing

Versions are date-based: `yyyy.mm.dd.ttmm` (local time, 24h, e.g. `2026.09.24.1422`).
Release after every meaningful change - there is no human to remember to do it.

```powershell
pwsh -File .\release.ps1
```

Requires a clean working tree and a non-empty `[Unreleased]` section. It writes
`VERSION`, moves `[Unreleased]` under the new version in `CHANGELOG.md`, commits,
tags `v<version>`, pushes and creates the GitHub release. Never edit `VERSION` by hand.

## Things only the owner can do

- GitHub OAuth scope changes (browser device flow). Start `gh auth refresh -s <scope>` in the
  background and give the owner the one-time code. Git pushes use SSH (`~/.ssh/github_ed25519`).
- Setting `TYPESAFE_API_KEY` if it is missing.

## Don'ts

- Never commit `runs/`, `goal.json`, `workspace/` or any API key.
- Don't make Jev's decisions in code or in the skill; the server asks Jev.
- Don't send raw agent output to Jev; keep its state compact and structured.
