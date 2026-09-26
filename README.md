# jev-loop

[![tests](https://github.com/King4s/jev-loop/actions/workflows/test.yml/badge.svg)](https://github.com/King4s/jev-loop/actions/workflows/test.yml)

A build loop where **Jev decides** and **Claude Code, Codex or Hermes does the work**.

[Jev](https://docs.typesafe.ai) (TypeSafe) is a fast decision model that answers with
typed choices and probabilities instead of text. In jev-loop, Jev decides on every turn:

- **route**: which role works next (`build`, `test`, `fix`, ...)
- **done**: the probability that the goal is met
- **recovery**: after a failure, whether to retry, switch role or give up

Claude Code, Codex or Hermes writes the code. The server runs your checks. An independent
subagent reviews the work, and **only the review can declare the task done**.

## Getting started

Requires Python 3.11+, a harness - [Claude Code](https://claude.com/claude-code),
[Codex](https://developers.openai.com/codex) or
[Hermes](https://hermes-agent.nousresearch.com) - and a
[TypeSafe API key](https://docs.typesafe.ai), either in the `TYPESAFE_API_KEY` environment
variable or in the file `~/.config/jev-loop/typesafe_api_key` (readable only by you).

```powershell
# Windows
git clone https://github.com/King4s/jev-loop.git; cd jev-loop; .\install.ps1
```

```bash
# Linux / macOS / WSL (creates a venv inside the repo)
git clone https://github.com/King4s/jev-loop.git ~/jev-loop && ~/jev-loop/install.sh
```

The installers set up every harness they find on PATH:

| Harness | Skill | MCP server |
| --- | --- | --- |
| Claude Code | `~/.claude/skills/jev/` | `claude mcp add` |
| Codex | `~/.agents/skills/jev/` | `codex mcp add` (`~/.codex/config.toml`) |
| Hermes | `~/.hermes/skills/jev/` | `hermes mcp add` |

The installer finishes with `jev_mcp.py --check`, a tiny live call to Jev that shows
dependencies, key and network work. To update: `git pull` and run the installer again.

In Hermes the tools are called `mcp_jev_loop_loop_start` etc.; start a new session afterwards.
In Claude Code and Codex the tools come from the `jev-loop` server as `loop_start` etc.
Restart the harness, then just say what you want built:

> build with jev: a small tool that renames my photos by capture date

or invoke the skill directly: `/jev` (Claude Code), `$jev` or `/skills`
(Codex). The skill asks a few concrete questions (folder, language, how to test it,
size), writes the acceptance criteria and `goal.json` for you, shows a summary and runs
the loop when you say go.

## One skill, three parts

The installed `jev` skill combines **Loop** (goal, execution, checks and independent
review), **Route** (Jev selects the model tier, skill and delegation for each task),
and **Git** (Jev proposes commit, push or PR behind deterministic publishing rules).
Say `jev`, `jev-loop`, `jev-route` or `jev-git`; they all use the same skill.
The MCP server remains named `jev-loop`.

`route.py` and `git_decide.py` live next to `SKILL.md` and use only Python's standard
library. Both read the same TypeSafe key as the server. Claude's model tiers map to
available models in Codex and Hermes; when delegation is unavailable, the harness
runs in-session and reports that fallback. Reviews use the main model or stronger.

The installers copy the entire skill directory and migrate recognized legacy skills:
`jev-loop` for each harness, plus `jev-route` and `jev-git` for Claude Code. A legacy
folder is removed only when its SKILL.md frontmatter identifies that exact skill;
custom folders with different names in frontmatter are preserved.

Git's private per-repository rules live outside repositories in
`~/.config/jev-git/policies/*.json`. Protected branches block direct publishing by
default. A trusted local policy can adapt that rule to a repository's documented
release workflow; secret, identity and review checks still apply. A recommendation
never grants permission to publish beyond the user's authorized scope.

## How a turn runs

```
loop_decide ──► execute ──► loop_record_turn ──► (checks run) ────┐
     ▲             │                                              │
     │             └──(Jev: checks OK and p_done ≥ threshold)─► review ──► loop_record_review
     └────────────────────────────────────────────────────────────┘          │
                                                                    done ──► stop
```

Hard stops: `max_turns`, `max_consecutive_failures`, or Jev chooses `escalate`.
A turn fails when the executor reports failure, a previously green check breaks, or
nothing moves (no files, same failure output). A check that is simply still red is not enough.
If the loop stalls - same role, all checks green and unchanged output for `stall_turns`
turns in a row - Jev is told so (`stalled`) and asked an extra question: can more executor
work change anything, or should an independent reviewer judge now? If Jev says yes
(≥ `jev_review_threshold`), the loop goes to review even though `p_done` is below the
threshold. The same question is asked after `review_turns` green executor turns since the
last review, or since the start when no reviewer has looked yet (`reviewed_before` /
`unreviewed`): a run that keeps doing real work never stalls, and Jev's `p_done` - it sees
file names, check results and notes, not the work - can stay below the threshold for a
whole run. The code decides nothing; the review still decides whether the goal is met.
Everything is logged in `runs/<id>.jsonl` (the decision tape, including Jev's raw answers),
and the state in `runs/<id>.state.json`, so a run can be resumed.

What Jev sees: goal, acceptance criteria, the project's file list, check results, the latest
turns, and the last review's missing items *together with the turns since*. Raw agent output
is not sent; Jev is weak against noise.

## goal.json

The skill writes it for you, but the format is simple (see [goal.example.json](goal.example.json)):

| Field | Meaning |
| --- | --- |
| `goal` | The goal in one sentence |
| `acceptance` | List of concrete, checkable criteria |
| `checks` | Shell commands run in `workdir`; exit 0 = pass. The more deterministic checks, the more honest the loop |
| `workdir` | The project folder, relative to the goal file |
| `roles` | `when` = Jev's routing criterion, `brief` = instructions for the executor |
| `jev_model` | Default `jev-latest` |
| `jev_done_threshold` | When Jev may send the run to review (default 0.8) |
| `stall_turns` | Unchanged green turns with the same role before Jev is asked about review (default 2) |
| `jev_review_threshold` | When Jev's answer to that question sends the run to review (default 0.5) |
| `review_turns` | Green executor turns since the last review, or since the start, before Jev is asked about review (default 3) |
| `max_turns`, `max_consecutive_failures`, `check_timeout` | Hard limits |

## MCP tools

| Tool | Does |
| --- | --- |
| `loop_start(goal_path)` | Creates a run, runs the checks once |
| `loop_decide(run_id)` | Asks Jev; answers `execute`, `review` or `stop` |
| `loop_record_turn(run_id, notes, files, executor_ok)` | Records the turn and runs the checks |
| `loop_record_review(run_id, done, missing)` | Records the reviewer's verdict |
| `loop_status(run_id)` | Shows phase, history and tape |

## Standalone script: loop.py

`loop.py` is the original variant without a harness: executor and reviewer are models
via OpenRouter. Requires `OPENROUTER_API_KEY` and `TYPESAFE_API_KEY`.

```powershell
copy goal.example.json goal.json
python loop.py goal.json
```

Exit codes: 0 = done, 1 = max_turns, 2 = needs a human.
Note: the MCP variant is the maintained one; `loop.py` does not yet send the file list or
review progress to Jev.

## Development

The repo is maintained by AI agents; the rules are in [AGENTS.md](AGENTS.md).

```powershell
pip install -r requirements.txt
python -m pytest -q tests     # protocol tests with Jev mocked, no network
```

Versions are date-based, `yyyy.mm.dd.hhmm`. New release (requires a clean working tree):

```powershell
.\release.ps1
```

The script sets `VERSION`, moves `[Unreleased]` in [CHANGELOG.md](CHANGELOG.md) under the
new version, commits, tags `v<version>`, pushes and creates a GitHub release.

## Security

`checks` runs commands in a shell on your machine, and the code they test was written by a
model. Run unknown goals in a VM or container. The API key is read only from the environment
or the key file and is never stored in the repo or in the tape.

## License

[MIT](LICENSE)
