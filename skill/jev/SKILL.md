---
name: jev
description: Jev-AI-Skill is one skill for Claude Code, Codex and Hermes with four capabilities — Loop for building, finding and fixing code, and review; Gate for cheap Jev checks before a large model runs (event triage, review verdicts, owner questions, tool choice); Route for model/skill/delegation choices; Git for guarded repository steps. Triggers include Jev-AI-Skill, jev, jev-loop, jev-gate, kør loopen, byg med jev, lad Jev styre, jev-route, lad Jev vælge, jev-git and skal det pushes.
---

# Jev-AI-Skill

The project is Jev-AI-Skill; its installed skill name and invocation remain `jev`.

## Gate - ask Jev before spending a large model

Gate is four stateless tools on the `jev-loop` MCP server (in Hermes: `mcp_jev_loop_gate_*`).
Each takes a compact, structured state, costs one Jev call and is logged to
`runs/gate.jsonl`. Use them at these moments, every time, before reasoning:

| Moment | Tool | What you pass | What you do with the answer |
| --- | --- | --- | --- |
| A scheduled job, poll, background process, notification, chat-room event or ticket arrives | `gate_triage(events, known, policy)` | Up to 40 events: `kind`, `source`, `summary` (<= 600 chars: the tail, exit code, what routine looks like), plus `known` fingerprints of problems already reported | `ignore`/`known`: do nothing, no reply, no summary. `report`: pass the summary on in one line, unchanged. `act`: now investigate. `skip_agent=true` means end the turn. |
| A reviewer said `done=false`, or a check found gaps, and you are about to start another round | `gate_verdict(goal, items, evidence, changes)` | The open findings or criteria, compact evidence (check tails, gate results, turn notes) and what changed since | `review=none`: no new round. `narrow`: review only `open`. `full`: full independent review. Never re-run a full round on items Jev already closed. |
| You are about to ask the owner a question and you already have options or a recommended answer | `gate_decide(question, options, context, recommended, policy)` | Your own options as `{label: meaning}`, the context and the recommended label | `proceed=true`: act on `choice` and tell the owner in one line. Otherwise ask, leading with `recommended`. |
| You would read many tool, skill, session, channel or file descriptions to pick one | `gate_pick(task, candidates, context)` | `{name: one-line description}` for 2-40 candidates | Read only the picked candidate in full. `pick=null`: inspect the `top` candidates yourself. |

Rules:

- Never send raw output. Summaries carry the exit code, the last lines and what routine
  looks like; Jev is strong on a short state and weak on distractors.
- The tools decide whether a large model runs; they do not replace deterministic facts.
  An exit code, a `git rev-parse HEAD` comparison or a test count is code, not a question.
- Keep `known` current: when a problem is reported, add its one-line fingerprint so the
  next occurrence is `known`. Drop it when the problem is fixed.
- A scheduled job that only echoes a script's exit code should not run an agent at all.
  Run the script without an agent and, on a non-zero exit, call Gate from the script:
  `python jev_mcp.py --gate triage < request.json` prints the same decision as the MCP
  tool (`--gate verdict|decide|pick` likewise). The JSON object holds the tool's arguments.
- A timer that resumes an idle coordinator should feed `gate_triage` the state files'
  changed lines and resume only on `act`.
- Owner policy defaults to: decide and act alone; ask only for irreversible, paid, outward,
  credential or instruction-contradicting steps. Pass a `policy` string to change it.
- If a Gate tool returns `{"error": ...}`, the decision failed: do the work the expensive way
  and say so. Never treat an error as "ignore".

## Loop

The user should only have to say *what* they want. You work out the rest by asking
a few good questions, write the goal file yourself, and run the loop.
Talk to the user in their language (usually Danish).

### Find and fix existing code

When the goal is to find code that may need repair, use Loop for the repair and its
checks. The executor searches the project; Jev decides Loop steps from compact
findings, not from a raw dump of search results. Start with the supplied error,
symbol or literal text (use `rg -n -F` for literal text and `rg --files` for names),
then inspect nearby code and callers before deciding which matches are defects.
Exclude generated and dependency files unless the evidence points there. A match is
a lead, not permission for a global replacement. If the first search misses, broaden
to related symbols or behavior; report an unresolved search honestly if the relevant
code still cannot be found. Fix only confirmed, in-scope occurrences and use the
Loop checks and review to verify the requested behavior. If the user asked only to
locate code, report the locations and do not edit it.

## Phase 1 - Interview (skip what you already know)

Pull everything you can from the user's message and the current directory first.
Then ask only what is still unknown, and run each remaining question through
`gate_decide` first; ask the user only when it says so. Use an available structured
question tool when the harness provides one; otherwise ask clearly in chat. Keep questions concrete and concise,
with a recommended default where useful. If *what to build* is completely missing, ask
that first. Harness tool availability changes, so check the tools actually exposed in the
current session rather than assuming a fixed question or delegation tool.

What you need, and good defaults:

1. **What to build** - one sentence goal. Rewrite vague wishes into a concrete, testable goal.
2. **Where** - project folder. Default: a new folder for this project next to where the user
   keeps projects (use the current working
   directory). If the folder already has code, the loop continues from it.
3. **Language / stack** - infer from the goal or existing files; ask only if unclear
   (e.g. Python / Node-TypeScript / other).
4. **How we know it works** - this decides the `checks`. Propose them:
   Python -> `python -m pytest -q`; Node -> `npm test`; plus a build/lint/type-check
   when the stack has one. Deterministic checks are what keep the loop honest.
5. **Size** - small (`max_turns` 10) / medium (20, default) / large (40).

Then **draft 3-6 acceptance criteria yourself**: concrete, checkable, including one
for error handling and one saying tests cover the criteria. Don't ask the user to write them.

## Phase 2 - Write the goal file and confirm

Write `<project folder>\goal.json`:

```json
{
  "goal": "...",
  "acceptance": ["...", "..."],
  "workdir": ".",
  "checks": ["python -m pytest -q"],
  "jev_model": "jev-latest",
  "jev_done_threshold": 0.8,
  "max_turns": 20,
  "max_consecutive_failures": 4,
  "check_timeout": 300,
  "roles": {
    "build": {"when": "Implementation code is missing or incomplete relative to the goal",
              "brief": "Implement the features. Write complete, working files. Do not write tests."},
    "test":  {"when": "Tests are missing or do not cover every acceptance criterion",
              "brief": "Write or extend tests that verify the acceptance criteria. Do not change implementation code."},
    "fix":   {"when": "Checks are failing with a concrete error message",
              "brief": "Read the failing check output and fix the root cause with the smallest correct change."}
  }
}
```

Adapt roles only if the task clearly needs it (e.g. a `docs` or `ui` role, each with a
clear `when`). `workdir` "." = the project folder itself.

Show a short summary (goal, criteria, checks, folder and size) before starting. Ask for a
go only when the user has not already authorized the work. Apply any corrections, then
start. Preserve explicit authorization from the current conversation; do not ask again
just because this phase normally has a confirmation step.

If a `goal.json` already exists, inspect it and any run state. Continue or reuse it when
that matches the user's request; ask only when choosing between materially different goals
would affect the work.

## Phase 3 - Run the loop

Jev (via the `jev-loop` MCP server) is the decider; you are the executor. Never decide
routing, "done" or giving up yourself - the server does, and it runs the checks.
In Hermes the tools are named `mcp_jev_loop_loop_start`, `mcp_jev_loop_loop_decide`,
`mcp_jev_loop_loop_record_turn`, `mcp_jev_loop_loop_record_review`, `mcp_jev_loop_loop_status`
and `mcp_jev_loop_gate_triage`, `mcp_jev_loop_gate_verdict`, `mcp_jev_loop_gate_decide`,
`mcp_jev_loop_gate_pick`; in Claude Code and Codex they come from the `jev-loop` MCP
server as `loop_start`, `loop_decide`, `loop_record_turn`, `loop_record_review`,
`loop_status`, `gate_triage`, `gate_verdict`, `gate_decide` and `gate_pick`.

1. `loop_start(goal_path)` -> `run_id`.
2. `loop_decide(run_id)` and act on `next`:
   - **`execute`**: do ONE focused step as `role`, following `brief` strictly
     (a `test` role does not touch implementation, etc.). Address `failing_checks`
     and `reviewer_missing` if present. Only write inside `workdir`. Then
     `loop_record_turn(run_id, notes, files, executor_ok)` - `notes` is 1-2 honest
     sentences, `files` relative to `workdir`, `executor_ok=false` if you could not do
     the step. Don't run the configured checks yourself; the server does.
   - **`review`** after an earlier `loop_record_review(done=false)`: first call
     `gate_verdict` with the reviewer's `missing`, the check results and the turn notes since;
     on `review=narrow` brief the reviewer on `open` only. On the first review, or on
     `full`, review everything.
   - **`review`**: get an independent review in a context that cannot see your reasoning,
     using a review/delegation tool actually available in the harness. Examples include a
     delegated task in Hermes, an independent agent in Claude Code, or a fresh read-only
     Codex process where `codex exec` is available:
     `codex exec -s read-only -C <workdir> -o verdict.json "<review prompt>"` - a new
     process with no shared context, which cannot edit files; the verdict lands in
     `verdict.json` so it can be passed on unchanged. Give it the goal, acceptance
     criteria, workdir and check results; it reads the files (no edits) and answers
     strictly - passing checks are necessary, not sufficient - with
     `{"done": bool, "missing": [...]}`. Pass the verdict unchanged to
     `loop_record_review`, then follow that response's `next`.
   - **`stop`**: report `reason` (`goal_met` / `max_turns` / `escalate`), turns used,
     where the result is, and how to run it. On `escalate`, summarise what keeps failing.
3. Repeat until `stop`. One short line to the user per turn (turn, role, checks ok/fail).

Treat the checks run by `loop_record_turn` as authoritative. Do not rerun a
configured check or repeatedly run a focused test merely to verify that Jev
tested it, including after a transient failure. Give the failure back to
`loop_decide` and follow its recovery step. Override this only when Jev is
stuck on a test and cannot make progress from the evidence it has; then run
the smallest diagnostic needed to obtain the missing failure detail, not a
series of confirmatory reruns.

### When the run keeps answering `execute`

The server decides; you never route yourself. But the server's two facts about a
reviewer are worth knowing, because a run can sit in `execute` with nothing left to do:

- After `loop_record_review(done=false)` the server hands out the queued role's turn.
  Record it with `loop_record_turn` **before** the next `loop_decide`, or that call fails
  with `wrong step: run is in phase 'execute'`.
- A reviewer is offered when Jev thinks the goal is met, when the same role keeps running
  with unchanged green checks (`stall_turns`), or when `review_turns` (default 3) green
  executor turns have passed since the last review - or since the start, if no reviewer
  has looked yet. A run whose executor keeps doing real work never stalls (every turn
  changes the check output) and Jev's `p_done` can sit below the threshold for the whole
  run, so the third path is what gets such a run in front of a reviewer at all; Jev still
  answers `review_now` and decides.
- If `loop_decide` keeps returning `execute` with nothing left to do, record an audit turn
  (`files: []`) whose note says plainly what is finished and that only the verdict remains.
  That is evidence Jev can act on; process narration is not.
- Keep `notes` short and lead with the change and its evidence, not the story: Jev sees
  only the first `NOTE_CHARS` (600) characters of each note, and the state is built to
  keep him away from everything else.
- When multiple agents build separate copies of a project concurrently, check whether
  their build configuration points to a shared output directory. Give each copy an
  isolated build directory when needed to prevent stale or cross-version artifacts from
  making checks misleading.

If a tool returns `{"error": ...}`, fix the cause and retry that call; never bypass the
server. If the `jev-loop` tools are missing, tell the user to restart the harness so it
picks up the new MCP server (setup below).

## Setup (once)

From a clone of https://github.com/King4s/Jev-AI-Skill run `./install.sh` (Linux, macOS, WSL) or
`.\install.ps1` (Windows). Both install dependencies, sync this skill and register the
`jev-loop` MCP server for harness CLIs they find on PATH. Windows always copies the skill
to Claude Code's user skill directory; Claude MCP registration is conditional. Codex and
Hermes setup is conditional. POSIX setup is conditional for all three harnesses:

| Harness | Skill | MCP |
| --- | --- | --- |
| Claude Code | `~/.claude/skills/jev/` | `claude mcp add` |
| Codex | `~/.agents/skills/jev/` | `codex mcp add` (`~/.codex/config.toml`) |
| Hermes | `$HERMES_HOME/skills/jev/`, or `~/.hermes/skills/jev/` | `hermes mcp add` |

Then restart the harness / start a new session. In Codex, the skill is invoked with
`/skills` or `$jev` (and it also triggers on the description).

The TypeSafe key goes in the environment as `TYPESAFE_API_KEY` or in
`~/.config/jev-loop/typesafe_api_key`; the server and helpers read both. In Hermes, the
installer references the environment key only if `$HERMES_HOME/.env` (or `~/.hermes/.env`)
contains it. Otherwise the server reads the key file. The POSIX installer creates a
repository-local `.venv`; Windows uses `python` from PATH. Installers run a live server
check when a key is available. Checks execute shell commands from the goal file; use an
isolated environment for untrusted projects.


## Route

Jev chooses the next task's model tier, skill and delegation via `route.py` beside
this SKILL.md. The ordered capability tiers are `haiku < sonnet < opus < fable`;
Jev's importance judgment bumps the selected tier once, capped at fable. These are
capability labels, not model IDs. Map them to models exposed by the current harness.
If a model or delegation feature is unavailable, state the limitation and use a suitable
in-session fallback when possible. Do not invent a model named fable.

Run `python <this-skill-directory>/route.py <request.json>` (or `python3`). Supply:

```json
{"task": "Next concrete task and done criteria", "context": "Relevant constraints",
 "skills": {"available-skill": "One-line description"}, "main_model": "sonnet", "review": false}
```

`main_model` is the main session's mapped **tier**. Shortlist 3-12 relevant available
skills, or an empty object if none applies. Read the chosen skill before execution.
The JSON output contains `model`, `skill`, `subagent`, `reason` and `raw` probabilities.
Below the skill probability threshold, `skill` becomes `none`. Show one concise
routing line and apply the decision, unless the user requested advice only.

Any model mapped to a different model than the main session must run as a subagent. Use
an available agent/delegation tool with a self-contained brief. For independent review,
send `"review": true`; the helper requires delegation and raises its selected tier to at
least the main session tier. Check that the mapped review model meets that level.
Keep user conversation in the main session. Never delegate destructive/publishing
steps or final review to the cheapest tier. User-specified models, skills and
instructions to work in-session take priority over routing.

For coding tasks use Loop above as the outer workflow. At every `execute`, route
the role/brief plus failing checks and reviewer findings with that role's relevant
skills, execute one turn, then record it. On `review`, use the review flag and an
independent context. Keep the loop server responsible for checks and transitions.
An API/key failure may fall back to disclosed judgment for Route only; never use
that fallback to bypass the Loop server or Git hard rules.

## Git

After completed work, `git_decide.py` beside this SKILL.md gathers repository facts,
asks Jev for the next step, and enforces hard rules. This selects a step within the
user's existing authorization; invocation alone does not authorize publishing,
history rewriting or unrelated changes. Act directly when already authorized.

Run `python <this-skill-directory>/git_decide.py --repo <workdir> --summary "State of work"`
and add `--reviewed` only after independent approval, `--checks-green` only with
passing checks for the current work. `--scan-only` inspects facts/blocks without
calling Jev. Output includes `action`, `blocked`, `commands`, `argv`, `reason`, `facts`, `raw`.
`commands` contains POSIX-quoted display guidance only: never evaluate it as shell
code. Use `argv` as literal subprocess arguments with shell execution disabled;
fill commit/PR placeholders and split the finished file list into separate arguments.
Pass the PR body through the indicated body file.

- `none`: report the reason briefly.
- `commit`: stage only finished files, use a clear message and required attribution.
- `push_branch`: push the named branch to origin when authorized.
- `open_pr`: push and create the PR with changes and validation in its description;
  follow harness rules for attaching the PR. Never infer authorization to merge.

Hard rules block outward actions on protected branches (default main/master), on
blocked commits, disallowed author/committer identities, private regex matches or
suspected secrets. No force push, `--all` or `--mirror`. A PR requires both review
and green checks; otherwise the decision is reduced to a branch push. Existing open
PRs are updated by push. If `p_needs_user >= 0.5`, the outward step is skipped. Identify
the concrete unresolved owner decision only when one is needed; honor explicit
authorization and decisions already given in the conversation. Never treat elapsed time
as permission.

Fix blocks only within the authorized scope, then rescan. Stop and explain blocks
that require rewriting pushed/other people's history or changing private policy.
An API error is a failed decision, never permission to publish.

Private policies live ONLY in `~/.config/jev-git/policies/*.json`, outside this repo:

```json
{"remote": "github.com/<owner>/<repo>", "protected_branches": ["main", "master"],
 "allowed_author_emails": [], "blocked_shas": [], "blocked_patterns": [], "notes": ""}
```

Remote matching is exact after canonical SSH/HTTPS normalization. Without a policy,
generic rules and the heuristic text-pattern scan apply. The scan checks added lines in
outgoing commits (including merge-parent diffs) and the working-tree diff, plus commit
subjects; it cannot guarantee that secrets or private data will be detected. Review the
changes yourself. Existing repository/user rules can justify a specific local policy
(including protected branches); do not relax policy merely to make a blocked action
succeed. If a repository is renamed, update its origin and the matching policy's `remote`
while preserving its rules. Never commit private policies or identifiers. Both helpers
use only Python's standard library and read `TYPESAFE_API_KEY` or
`~/.config/jev-loop/typesafe_api_key`; neither helper performs Git mutations itself.
