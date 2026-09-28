# Changelog

Versions are date-based: `yyyy.mm.dd.hhmm` (local time of the release).
New releases are made with `.\release.ps1`.

## [Unreleased]

### Fixed
- Loop failed with "file not found" on machines pointed at the shared server, because the
  remote server cannot see their goal files or run their checks. With `JEV_MCP_URL` the
  installers now register a local server that forwards only its Jev calls to the shared
  host (`JEV_UPSTREAM`), which answers them on a new `/jev` endpoint with its key.

## [2026.09.28.1858] - 2026-09-28

### Changed
- The README opens with everything the skill now offers (Gate tools, `--watch`, the
  shared HTTP server) and the banner shows Gate beside Loop, Route and Git.

## [2026.09.28.1831] - 2026-09-28

### Changed
- The documentation now names everything the skill contains: README's introduction
  says four capabilities (Loop, Gate, Route, Git) plus `find_code.py` and `--watch`, lists
  all three helpers and the `jev-gate` trigger; AGENTS describes Gate, `find_code.py` and
  `--watch`; the Codex metadata (`agents/openai.yaml`) lists the Gate tools and code repair.

## [2026.09.28.1826] - 2026-09-28

### Changed
- Documented how to run the shared HTTP server in practice (README, skill, AGENTS):
  Loop's `workdir` and `checks` run on the server host, every reinstall must keep
  `JEV_MCP_URL` or the machine returns to a local server, how to update the host and
  clients, how to check the server, and `~/.local/bin` on `PATH` over non-interactive SSH.
- `deploy/jev-loop-http.service` uses a `TAILNET_IP` placeholder instead of a concrete
  address, and its liveness check now matches the server (a POST returns 400; a GET
  never ends).

## [2026.09.28.1642] - 2026-09-28

### Fixed
- `install.sh` registers a shared server URL with older Hermes releases that reject
  `--connect-timeout` (seen with v0.17): it retries the registration without the flag.

## [2026.09.28.1636] - 2026-09-28

### Fixed
- The installers hung on Windows (and in any terminal) at `hermes mcp remove`, which asks
  "Remove server? [Y/n]" and waits on a console stdin. Both now answer the prompt.

## [2026.09.28.1623] - 2026-09-28

### Fixed
- Git scanned a branch that origin did not have yet from the root of its history, so
  secret-looking test fixtures already public on origin blocked every new branch. Outgoing
  commits are now those reachable from HEAD but from no origin remote-tracking ref.

## [2026.09.28.1616] - 2026-09-28

### Fixed
- Registering a shared server URL with Hermes no longer stalls the installers: they
  answer Hermes' authentication prompt with no before enabling the tools.
- `--watch` fingerprints a failure by its problem lines without clock times, so the same
  problem on the next run is recognized without a Jev call instead of being reported again.
  A failed Jev call reports the problem once, and an unwritable gate tape no longer turns
  a decision into an error.

### Added
- `jev_mcp.py --http --host <ip> --port <port>` serves the same tools over stateless
  streamable HTTP, so one always-on host can serve every machine on a private network.
  Setting `JEV_MCP_URL` makes both installers register that URL with Claude Code, Codex
  and Hermes instead of a local server. A systemd unit is in `deploy/`.
- `jev_mcp.py --watch` turns a recurring check into a script-only job: silent on success,
  and a failure is triaged by Jev and reported once until the check passes again.
- Gate: four stateless MCP tools that ask Jev before a large model runs. `gate_triage`
  classifies scheduled, polled and notified events as ignore, known, report or act;
  `gate_verdict` judges which review findings the evidence already closes and whether the
  next review is none, narrow or full; `gate_decide` answers a question the harness was
  about to ask the owner from its own options and says whether the owner must decide;
  `gate_pick` chooses a tool, skill, session or file from one-line descriptions. Decisions
  are logged to `runs/gate.jsonl`, and `python jev_mcp.py --gate <name>` gives scripts
  and cron jobs the same decisions without an MCP host.
- Skill rules for when to call each Gate tool, and Loop now asks `gate_verdict` before a
  repeat review round.
- Document a code search-and-repair workflow within Jev Loop: inspect matches in
  context, change only confirmed defects, and verify the repair through Loop.
- Add `find_code.py` to score every source chunk or literal match in the selected
  scope with Jev, retain all leads for a capable coding model, and report coverage
  or provider failures without silent truncation.

## [2026.09.27.1827] - 2026-09-27

### Changed
- Treat Jev Loop's check results as authoritative and avoid redundant test reruns;
  use minimal manual diagnostics only if Jev is stuck on a test.

## [2026.09.27.0056] - 2026-09-27

### Added
- Add the project banner to the README.

## [2026.09.27.0041] - 2026-09-27

### Changed
- Rename the project to Jev-AI-Skill and update canonical repository links.
- Explain Loop, Route and Git benefits, shared workflows across AI clients, and current
  compatibility and handoff requirements.

## [2026.09.27.0035] - 2026-09-27

### Changed
- Refresh Jev branding and document the unified Loop, Route and Git capabilities, current
  installer behavior, helper outputs and Git scanning limitations.

## [2026.09.26.2347] - 2026-09-26

### Added
- One harness-neutral `jev` skill combining the build loop, model/skill routing and
  guarded Git decisions, with standard-library Python helpers and offline tests.

### Changed
- Install the full `skill/jev/` directory for Claude Code, Codex and Hermes, and
  remove recognized legacy skills without deleting unrelated custom skills.
- Keep the MCP server name `jev-loop` and private Git policies outside repositories.

### Fixed
- Scan outgoing Git history against the actual origin branch rather than an unrelated
  tracking branch; reject unsupported fetch/push destination mismatches.
- Include merge-resolution changes in the outgoing secret and private-pattern scan.

## [2026.09.26.0313] - 2026-09-26

### Added
- `install.ps1` sets up Hermes on Windows the way `install.sh` already did: syncs the skill to
  `~/.hermes/skills/jev-loop/` and registers the MCP server, passing `TYPESAFE_API_KEY`
  through the env block only when `~/.hermes/.env` can resolve it.

### Fixed
- A run whose executor kept doing real work could never reach a reviewer: every turn
  changed the check output, so the stall path never fired, and Jev's `p_done` stayed below
  `jev_done_threshold` for the whole run because it sees file names, check results and
  notes rather than the work. Runs 20260925-200554, 20260925-232600 and 20260926-024217
  all ended by `max_turns` or `escalate` without one review, each on a tree an independent
  reviewer then approved. Two paths now lead to the same `review_now` question: after a
  recorded review has had `review_turns` (default 3) executor turns (`reviewed_before`,
  `why: "revisit"`), and after `review_turns` green turns with no review yet
  (`unreviewed`, `why: "unreviewed"`). Jev still decides, and the review still decides
  whether the goal is met. Turn notes reach Jev at 600 characters and review findings at
  500, so the evidence that a finding was addressed survives. New tests in
  `tests/test_jev_mcp.py`.

## [2026.09.24.2104] - 2026-09-24

### Changed
- The project is now in English on GitHub: `README.md`, `CHANGELOG.md` (including earlier
  entries and the GitHub release notes), `AGENTS.md`, comments in `install.sh` and the
  interview headings in `skill/jev-loop/SKILL.md`. The skill still talks to the user in
  the user's own language.

## [2026.09.24.2101] - 2026-09-24

### Fixed
- The loop could run the same no-op role over and over when all checks were green but Jev's
  `p_done` hovered just below the threshold (seen in runs 20260924-193331 and 20260924-203249).
  The server now counts turns without change (same role, all checks green before and after,
  check output unchanged apart from timings). After `stall_turns` (default 2) Jev gets a
  `stalled` field in its state and an extra noul question `review_now`: can more executor
  work change anything, or should an independent reviewer judge now? At
  `p_review_now` ≥ `jev_review_threshold` (default 0.5) the loop goes to review
  (`why: "stalled"`). Jev makes the decision, the review still decides whether the goal is
  met, and a rejected review resets the counter. New tests in `tests/test_jev_mcp.py`.

## [2026.09.24.1956] - 2026-09-24

### Added
- Codex support: `install.sh` and `install.ps1` install the skill to
  `~/.agents/skills/jev-loop/` (Codex's user scope for skills, including `agents/openai.yaml`)
  and register the server with `codex mcp add jev-loop -- <python> jev_mcp.py`
  (`~/.codex/config.toml`). The Codex part is skipped without error when `codex` is not on PATH.
- `tests/test_skill.py`: the frontmatter in `skill/jev-loop/SKILL.md` stays within Codex's
  limits (`name` <= 100, `description` <= 500 characters), otherwise Codex skips the skill.
- `tests/test_install_sh.py`: runs the real `install.sh` in a sandbox and requires the
  Codex skill to land in `~/.agents/skills/jev-loop/` and the server to be registered with
  `codex mcp add` - and that a missing `codex` is simply skipped.
- `tests/test_install_ps1.py`: the PowerShell parser must accept `install.ps1`, and the script
  must still contain the Codex block.

### Changed
- `skill/jev-loop/SKILL.md` now covers three harnesses: questions via `clarify` (Hermes),
  `AskUserQuestion` (Claude Code) or plain chat (Codex), review via `delegate_task`,
  the `general-purpose` agent or a fresh `codex exec -s read-only` process, and the
  setup section shows the skill and MCP location per harness.
- `README.md`, `AGENTS.md` and the `jev_mcp.py` docstring are harness-neutral: Claude Code,
  Codex or Hermes is the executor.

## [2026.09.24.1936] - 2026-09-24

### Fixed
- A turn no longer counts as a failure just because a check is red. It only counts when
  the executor reports failure, when a previously green check breaks, or when nothing
  moved (no files and the same failure output). A loop with a check that can only turn
  green at the end (e.g. a live verification) therefore no longer escalates in the middle
  of progress. `loop_record_turn` now also returns `turn_failed`.

## [2026.09.24.1922] - 2026-09-24

### Changed
- `AGENTS.md`: freja and macbook-pro no longer exist and were removed from the machine list.

## [2026.09.24.1541] - 2026-09-24

### Changed
- The README heading and the GitHub description mention both harnesses (`Claude Code or Hermes`).

## [2026.09.24.1525] - 2026-09-24

### Added
- `AGENTS.md` describes the Hermes harness: tool names (`mcp_jev_loop_*`), the environment
  filtering and `${TYPESAFE_API_KEY}`, and how `hermes mcp add` behaves.

## [2026.09.24.1457] - 2026-09-24

### Added
- Hermes support in `install.sh`: the skill is also synced to `~/.hermes/skills/jev-loop/`,
  and the server is registered with `hermes mcp add` (the tools are called `mcp_jev_loop_*`).
  `TYPESAFE_API_KEY` is passed through the server's `env` block only when the key is actually
  in `~/.hermes/.env` - an unresolved `${TYPESAFE_API_KEY}` would otherwise be sent to Jev as
  the key itself. Otherwise the server reads the key file itself.
- `install.sh` skips the Claude Code part when `claude` is not on PATH (and likewise for
  Hermes), so the installer works on a machine with only one of the harnesses.

### Changed
- `skill/jev-loop/SKILL.md` is now harness-neutral: questions via `clarify` (Hermes) or
  `AskUserQuestion` (Claude Code), review via `delegate_task` or the `general-purpose` agent,
  and Hermes' tool names mentioned explicitly. The default project folder is no longer
  Windows-specific, and the setup section covers both harnesses and the key file.

## [2026.09.24.1438] - 2026-09-24

### Changed
- `AGENTS.md` lists which machines jev-loop is installed on.

## [2026.09.24.1436] - 2026-09-24

### Fixed
- `install.ps1`: the path to the key file was broken (`\t` became a tab character).
- `install.sh` also works without `python3-venv`/ensurepip (fetches pip from bootstrap.pypa.io).

## [2026.09.24.1435] - 2026-09-24

### Added
- `install.sh` for Linux/macOS (venv, skill, MCP registration, live check).
- `jev_mcp.py --check`: a tiny live call to Jev as a health check; the installers run it at the end.
- The API key can live in `~/.config/jev-loop/typesafe_api_key` when the environment variable is not set.

### Fixed
- The tests use the running Python instead of `python` from PATH.

## [2026.09.24.1430] - 2026-09-24

### Added
- GitHub Actions CI: the tests run on Ubuntu and Windows on every push.

## [2026.09.24.1425] - 2026-09-24

### Added
- `AGENTS.md` (+ `CLAUDE.md`): instructions for AI maintainers - the repo is maintained by AI, not by a human.

### Changed
- `release.ps1` rejects a release with an empty `[Unreleased]` section.

## [2026.09.24.1422] - 2026-09-24

First release.

### Added
- `jev_mcp.py`: MCP server with the tools `loop_start`, `loop_decide`,
  `loop_record_turn`, `loop_record_review` and `loop_status`. The server owns the Jev calls,
  checks, hard stops and the decision tape; Claude Code is the executor.
- Skill `jev-loop`: interviews the user, writes `goal.json` and runs the loop.
- `install.ps1` (skill + MCP registration) and `release.ps1` (date version, tag, GitHub release).
- Protocol tests with Jev mocked (`tests/`).

### Fixed
- Jev is called directly at `api.typesafe.ai/v1/systemone` with `TYPESAFE_API_KEY`;
  the response format is verified against the documentation (instead of guessed parsing).
- `recovery=retry` no longer overrides Jev's `route` choice.
- `reroute` now forces a different role if `route` points at the same one.
- Jev sees the project's file list, so it does not ask to build what already exists.
- The reviewer's missing items are shown together with the turns since the review, so a
  resolved item does not keep `p_done` down and make the loop go in circles.
- No Jev calls when `max_consecutive_failures` has already been reached.
- Consistent `checks_ok` when no checks are configured.
