"""Jev's Loop and Gate capabilities, exposed through the ``jev-loop`` MCP server.

The harness uses the installed ``jev`` skill to execute work and request an independent
review. The server asks Jev for routing, completion and recovery decisions, runs configured
checks, enforces stop conditions and records run state and a decision tape. Route and Git
are separate skill helpers, not MCP tools.

Protocol per turn:
  loop_decide        -> next = "execute" | "review" | "stop"
  (review)           -> loop_record_review -> next = "execute" | "stop"
  (execute role)     -> loop_record_turn   -> checks run, then loop_decide again

Gate (stateless, one Jev call each, logged to runs/gate.jsonl):
  gate_triage        -> per event: ignore | known | report | act; skip_agent when no event needs one
  gate_verdict       -> per item: closed or open; review = none | narrow | full
  gate_decide        -> a choice among the harness's own options and whether the owner must decide

Needs ``TYPESAFE_API_KEY`` in the environment or ``~/.config/jev-loop/typesafe_api_key``.
Run:   python jev_mcp.py            (stdio MCP server)
"""
import json
import os
import re
import subprocess
import time
from pathlib import Path

import requests

ROOT = Path(__file__).parent
RUNS = ROOT / "runs"
API = "https://api.typesafe.ai/v1/systemone"
RETRY_STATUS = {429, 529}
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip() if (ROOT / "VERSION").exists() else "dev"


# ---------- Jev ----------

KEY_FILE = Path.home() / ".config" / "jev-loop" / "typesafe_api_key"


def api_key():
    """TYPESAFE_API_KEY from the environment, else from ~/.config/jev-loop/typesafe_api_key."""
    key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if not key and KEY_FILE.exists():
        key = KEY_FILE.read_text(encoding="utf-8").strip()
    if not key:
        raise RuntimeError(f"No TypeSafe API key: set TYPESAFE_API_KEY or write it to {KEY_FILE}.")
    return key


def jev(model, state, questions, retries=4):
    key = api_key()
    for attempt in range(retries + 1):
        r = requests.post(
            API, timeout=60,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": model, "state": state, "questions": questions},
        )
        if r.status_code in RETRY_STATUS and attempt < retries:
            time.sleep(float(r.headers.get("retry-after") or 2 ** attempt))
            continue
        if r.status_code >= 400:
            raise RuntimeError(f"Jev HTTP {r.status_code}: {r.text[:500]}")
        return r.json()


# ---------- checks ----------

def run_checks(cmds, workdir, timeout):
    results = []
    for cmd in cmds:
        try:
            r = subprocess.run(cmd, shell=True, cwd=workdir, capture_output=True,
                               text=True, encoding="utf-8", errors="replace", timeout=timeout)
            results.append({"cmd": cmd, "ok": r.returncode == 0,
                            "out": (r.stdout + r.stderr)[-2000:]})
        except subprocess.TimeoutExpired:
            results.append({"cmd": cmd, "ok": False, "out": "TIMEOUT"})
    return results


def turn_failed(prev, checks, executor_ok, files):
    """Did this turn fail (counts toward max_consecutive_failures)?

    A red check alone is not a failure: some checks (e.g. a live data verify) can only
    turn green late in the run. A turn fails when the executor says so, when a check
    that passed before now fails, or when it stalled: nothing improved, no files were
    written and the same checks fail with the same output."""
    if not executor_ok:
        return True
    if all(c["ok"] for c in checks):
        return False
    before = {c["cmd"]: c for c in prev}
    if any(before.get(c["cmd"], {}).get("ok") and not c["ok"] for c in checks):
        return True  # regression
    # No regressions from here on, so any change is movement: a check turned green,
    # or a failing check fails differently (e.g. fewer failing tests).
    moved = any(c["cmd"] not in before or c["ok"] != before[c["cmd"]]["ok"]
                or c["out"] != before[c["cmd"]]["out"] for c in checks)
    return not moved and not files


# How much of a turn note and of each review finding reaches Jev. Jev is weak on
# distractors, so the state stays short - but long enough to carry the evidence that
# a review item was addressed, which is what decides whether a round is over.
NOTE_CHARS = 600
MISSING_CHARS = 500

_VOLATILE = re.compile(r"\d+(?:\.\d+)?\s*(?:ms|s|sec|secs|seconds)\b|\d{1,2}:\d{2}(?::\d{2})?")


def checks_signature(checks):
    """What the checks said, minus durations and clock times, to tell a turn that
    changed nothing from one that moved the checks."""
    return [[c["cmd"], c["ok"], _VOLATILE.sub("#", c["out"])] for c in checks]


SKIP_DIRS = {"node_modules", "__pycache__", "venv", ".venv", ".git", ".pytest_cache"}


def list_files(workdir, limit=200):
    """Relative paths of project files, so Jev knows what already exists."""
    out = []
    for p in sorted(Path(workdir).rglob("*")):
        parts = p.relative_to(workdir).parts
        if p.is_dir() or any(x in SKIP_DIRS or x.startswith(".") for x in parts):
            continue
        out.append(p.relative_to(workdir).as_posix())
        if len(out) >= limit:
            out.append("... (more files omitted)")
            break
    return out


# ---------- a run ----------

class Run:
    """One loop run. State lives in runs/<id>.state.json, events in runs/<id>.jsonl."""

    def __init__(self, run_id):
        self.id = run_id
        self.state_path = RUNS / f"{run_id}.state.json"
        self.tape_path = RUNS / f"{run_id}.jsonl"
        if not self.state_path.exists():
            raise KeyError(f"unknown run_id '{run_id}'")
        self.s = json.loads(self.state_path.read_text(encoding="utf-8"))

    @classmethod
    def create(cls, goal_path):
        p = Path(goal_path).resolve()
        cfg = json.loads(p.read_text(encoding="utf-8"))
        for k in ("goal", "roles"):
            if k not in cfg:
                raise ValueError(f"goal file is missing '{k}'")
        workdir = (p.parent / cfg.get("workdir", "workspace")).resolve()
        workdir.mkdir(parents=True, exist_ok=True)
        cfg["workdir"] = str(workdir)
        cfg.setdefault("jev_model", "jev-latest")
        cfg.setdefault("jev_done_threshold", 0.8)
        cfg.setdefault("jev_review_threshold", 0.5)
        cfg.setdefault("stall_turns", 2)
        cfg.setdefault("max_turns", 20)
        cfg.setdefault("max_consecutive_failures", 4)
        cfg.setdefault("check_timeout", 300)

        RUNS.mkdir(exist_ok=True)
        run_id = time.strftime("%Y%m%d-%H%M%S")
        n = 1
        while (RUNS / f"{run_id}.state.json").exists():
            n += 1
            run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{n}"
        (RUNS / f"{run_id}.state.json").write_text("{}", encoding="utf-8")
        run = cls(run_id)
        checks = run_checks(cfg.get("checks", []), workdir, cfg["check_timeout"])
        run.s = {"cfg": cfg, "turn": 0, "phase": "decide", "checks": checks,
                 "checks_ok": all(c["ok"] for c in checks),  # no checks -> True
                 "fails": 0, "idle": 0, "last_role": None, "pending_role": None,
                 "history": [], "review": None, "stopped": None}
        run.save()
        run.log("start", goal=cfg["goal"], workdir=cfg["workdir"], goal_file=str(p))
        return run

    # --- persistence ---
    def save(self):
        self.state_path.write_text(json.dumps(self.s, ensure_ascii=False, indent=1), encoding="utf-8")

    def log(self, kind, **kw):
        rec = {"t": time.strftime("%H:%M:%S"), "kind": kind, **kw}
        with open(self.tape_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

    @property
    def cfg(self):
        return self.s["cfg"]

    # --- helpers ---
    def _expect(self, *phases):
        if self.s["stopped"]:
            raise RuntimeError(f"run already stopped: {self.s['stopped']}")
        if self.s["phase"] not in phases:
            raise RuntimeError(f"wrong step: run is in phase '{self.s['phase']}', expected {phases}")

    def _stop(self, reason, **kw):
        self.s["stopped"] = reason
        self.s["phase"] = "stopped"
        self.log("stop", reason=reason, turn=self.s["turn"], **kw)
        self.save()
        return {"next": "stop", "reason": reason, "turn": self.s["turn"],
                "tape": str(self.tape_path), **kw}

    def _execute(self, role):
        self.s["pending_role"] = role
        self.s["phase"] = "execute"
        self.save()
        spec = self.cfg["roles"][role]
        return {"next": "execute", "turn": self.s["turn"], "role": role, "brief": spec["brief"],
                "workdir": self.cfg["workdir"], "reviewer_missing": (self.s["review"] or {}).get("missing"),
                "failing_checks": [c for c in self.s["checks"] if not c["ok"]]}

    def summary(self):
        return {"run_id": self.id, "turn": self.s["turn"], "phase": self.s["phase"],
                "stopped": self.s["stopped"], "checks_ok": self.s["checks_ok"],
                "consecutive_fails": self.s["fails"], "last_review": self.s["review"],
                "history": self.s["history"], "tape": str(self.tape_path)}

    # --- protocol ---
    def decide(self):
        self._expect("decide")
        cfg, s = self.cfg, self.s
        if s["turn"] >= cfg["max_turns"]:
            return self._stop("max_turns")
        if s["fails"] >= cfg["max_consecutive_failures"]:
            return self._stop("escalate", why="max_consecutive_failures reached")
        s["turn"] += 1
        roles = cfg["roles"]

        # Compact, structured state. No raw agent output: Jev is weak on distractors.
        state = {
            "goal": cfg["goal"],
            "acceptance": cfg.get("acceptance", []),
            "turn": s["turn"],
            "project_files": list_files(cfg["workdir"]),
            "all_checks_pass": s["checks_ok"],
            "checks": [{"cmd": c["cmd"], "ok": c["ok"], "tail": c["out"][-300:]} for c in s["checks"]],
            # What the last review found missing, and the turns taken since, so Jev can
            # judge whether it has been addressed (a stale list alone keeps p_done low).
            "last_review": {"missing": s["review"]["missing"],
                            "turns_since_review": s["history"][s["review"]["at_history"]:]}
                           if s["review"] else None,
            "recent_turns": [{k: h[k] for k in ("turn", "role", "files", "notes", "checks_ok")}
                             for h in s["history"][-5:]],
        }
        # A fact, not a decision: the same role kept re-running with every check green and
        # nothing in the checks changing. Jev decides what that means (review_now below).
        idle = s.get("idle", 0)
        stalled = s["checks_ok"] and idle >= cfg.get("stall_turns", 2)
        # A second fact of the same kind: the executor has taken `review_turns` turns since
        # the last review - or since the start, if no reviewer has looked yet - and every
        # check passes. A reviewer is the only one who can say whether the goal is met, and
        # without offering it the run can only be reviewed by `p_done` clearing its threshold
        # or by a stall. Neither happens in a run whose executor keeps doing real work: every
        # turn changes the check output, so it never stalls, and Jev's `p_done` sits below
        # the threshold because it sees file names and notes, not the work (runs
        # 20260925-200554, 20260925-232600 and 20260926-024217 all ended by max_turns or
        # escalate without one review). Jev still decides (review_now below).
        reviewed_at = s["review"]["at_history"] if s["review"] else 0
        since_review = len(s["history"]) - reviewed_at
        revisit = (not stalled and s["checks_ok"] and since_review >= cfg.get("review_turns", 3))
        if stalled:
            state["stalled"] = {"turns_without_change": idle, "role": s["last_role"],
                                "note": "All checks passed on each of these turns and their "
                                        "output did not change. Jev only sees file names, check "
                                        "results and turn notes; the independent reviewer reads "
                                        "the actual work and is the only one who can end the run."}
        elif revisit and s["review"]:
            state["reviewed_before"] = {
                "turns_since_review": since_review,
                "note": "The reviewer has judged this run before and found items missing (see "
                        "`last_review`). The executor has taken the turns in "
                        "`last_review.turns_since_review` since. An independent reviewer is the "
                        "only one who can say whether those items are closed."}
        elif revisit:
            state["unreviewed"] = {
                "green_turns": since_review,
                "note": "No reviewer has looked at this run yet. The executor has taken "
                        f"{since_review} turns and every check passes. Jev only sees file names, "
                        "check results and turn notes; the independent reviewer reads the actual "
                        "work and is the only one who can end the run."}
        qs = {
            "route": {"type": "choice",
                      "instructions": "Which agent should take the next turn toward the `goal`?",
                      "criteria": {k: v["when"] for k, v in roles.items()}},
            "done": {"type": "noul",
                     "instructions": "Is the `goal` fully met: every check passing, every "
                                     "`acceptance` criterion satisfied, and (if `last_review` is set) "
                                     "every item in `last_review.missing` addressed by the turns in "
                                     "`last_review.turns_since_review`?"},
        }
        if stalled or revisit:
            qs["review_now"] = {"type": "noul",
                                "instructions": "See `stalled`, `reviewed_before` or `unreviewed`: the "
                                                "last turns changed nothing, or a previous review found "
                                                "items missing and the executor has worked on them since, "
                                                "or the executor has worked for several green turns and no "
                                                "reviewer has looked yet. Should an independent reviewer "
                                                "judge the `goal` and `acceptance` now?"}
        if s["fails"]:
            qs["recovery"] = {"type": "choice",
                              "instructions": "The last turn failed (see `recent_turns` and `checks`). "
                                              "What is the best recovery?",
                              "criteria": {
                                  "retry": "Same agent again; the failure looks transient or nearly fixed",
                                  "reroute": "A different agent is better suited to fix this",
                                  "escalate": "Repeated failures with no real progress; a human is needed"}}

        raw = jev(cfg["jev_model"], state, qs)
        a = raw["answers"]
        route = a["route"]
        role, p_done = route["choice"], float(a["done"]["noul"])
        decision = {"turn": s["turn"], "route": role, "route_conf": route.get("confidence"),
                    "p_done": round(p_done, 3), "model": raw.get("model")}
        p_review = float(a["review_now"]["noul"]) if (stalled or revisit) and "review_now" in a else None
        if p_review is not None:
            decision["stalled"] = idle
            decision["p_review_now"] = round(p_review, 3)

        if s["fails"]:
            rec = a["recovery"]["choice"]
            decision["recovery"] = rec
            # 'route' already saw the failure and stays the primary decision; 'retry' must not
            # override it (a failed build turn is often fixed by routing to another role).
            if rec == "reroute" and role == s["last_role"] and len(roles) > 1:
                probs = {k: v for k, v in route["probabilities"].items() if k != s["last_role"]}
                role = max(probs, key=probs.get)
            decision["role"] = role
            self.log("decide", **decision, jev_raw=raw)
            if rec == "escalate":
                return self._stop("escalate", why="Jev chose escalate")
        else:
            decision["role"] = role
            self.log("decide", **decision, jev_raw=raw)

        if role not in roles:
            self.log("warn", msg=f"unknown role '{role}', falling back")
            role = next(iter(roles))

        # Cascade: cheap Jev filter first; the review is the only way to finish.
        # Jev sends the run to review when it thinks the goal is met, or when it judges
        # that a stalled executor cannot change anything and the reviewer should look.
        why = ("done" if p_done >= cfg["jev_done_threshold"] else
               (("revisit" if s["review"] else "unreviewed") if revisit else "stalled")
               if p_review is not None and p_review >= cfg.get("jev_review_threshold", 0.5) else None)
        if s["checks_ok"] and why:
            s["pending_role"] = role
            s["phase"] = "review"
            self.save()
            return {"next": "review", "why": why, "turn": s["turn"], "p_done": round(p_done, 3),
                    "goal": cfg["goal"], "acceptance": cfg.get("acceptance", []),
                    "workdir": cfg["workdir"], "checks": s["checks"]}
        return self._execute(role)

    def record_review(self, done, missing):
        self._expect("review")
        self.log("review", done=done, missing=missing)
        self.s["idle"] = 0  # the reviewer has looked; a new stall has to build up again
        if done:
            return self._stop("goal_met", executor_turns=len(self.s["history"]))
        self.s["review"] = {"missing": [str(m)[:MISSING_CHARS] for m in missing][:10]
                                       or ["reviewer said not done (no details)"],
                            "at_history": len(self.s["history"])}
        return self._execute(self.s["pending_role"])

    def record_turn(self, notes, files, executor_ok):
        self._expect("execute")
        s, cfg = self.s, self.cfg
        role = s["pending_role"]
        checks = run_checks(cfg.get("checks", []), cfg["workdir"], cfg["check_timeout"])
        checks_ok = executor_ok and all(c["ok"] for c in checks)
        failed = turn_failed(s["checks"], checks, executor_ok, files)
        # Idle: the same role again, all checks green before and after, and nothing in
        # them changed. Written file names don't count - a re-run rewrites its own log.
        idle = (checks_ok and s["checks_ok"] and role == s["last_role"]
                and checks_signature(checks) == checks_signature(s["checks"]))
        s["idle"] = s.get("idle", 0) + 1 if idle else 0
        s["checks"], s["checks_ok"] = checks, checks_ok
        s["fails"] = s["fails"] + 1 if failed else 0
        s["last_role"], s["pending_role"], s["phase"] = role, None, "decide"
        h = {"turn": s["turn"], "role": role, "files": files, "notes": notes[:NOTE_CHARS], "checks_ok": checks_ok,
             "failed": failed}
        s["history"].append(h)
        # The state keeps the bounded view Jev sees; the tape keeps the note whole.
        self.log("turn", **{**h, "notes": notes})
        self.save()
        return {"turn": s["turn"], "checks_ok": checks_ok, "turn_failed": failed, "consecutive_fails": s["fails"],
                "checks": [{"cmd": c["cmd"], "ok": c["ok"], "out": c["out"][-1200:]} for c in checks],
                "next": "call loop_decide"}


# ---------- Gate ----------
#
# Cheap Jev judgments taken *before* a large model is run. They come from an audit of the
# owner's Hermes, Codex and Claude Code histories (2026-09-28): the largest token sinks were
# cron jobs that woke an agent to echo an exit code, idle-resume timers that replayed a whole
# coordinator thread to find no work, group-chat turns that answered "(pass)", notification
# turns that only confirmed a green result, review rounds re-run on unchanged criteria, and
# questions to the owner whose recommended answer was already known. Each tool is stateless:
# the harness passes a compact, structured state (never raw output), Jev answers in one call,
# and the decision is appended to the gate tape.

GATE_TAPE = RUNS / "gate.jsonl"
SUMMARY_CHARS = 600
ITEM_CHARS = 500
CONTEXT_CHARS = 1500
MAX_BATCH = 40

TRIAGE_ACTIONS = {
    "ignore": "Routine success or no change: the expected result; nothing to report, nothing to do",
    "known": "The same problem is already listed in `known` or was reported before; repeating it adds nothing",
    "report": "A new problem or change worth one short report to the owner; no agent work is needed",
    "act": "Something changed that an agent must investigate or work on: a failure, a request, new work",
}

REVIEW_SCOPES = {
    "none": "Every item is closed by evidence that stands on its own; no reviewer is needed now",
    "narrow": "Only the changed items need a reviewer's eyes; the rest of the work is untouched",
    "full": "The change reaches beyond the listed items, or the evidence is thin; a full independent review is needed",
}

DEFAULT_POLICY = ("The owner wants the assistant to decide and act on its own. Ask the owner only "
                  "when a step is irreversible, spends money, publishes or sends something to "
                  "others, needs credentials or an interactive login, or contradicts an explicit "
                  "instruction from the owner.")


def _clip(text, n):
    s = " ".join(str(text if text is not None else "").split())
    return s if len(s) <= n else s[:n - 3] + "..."


def _gate_log(tool, **kw):
    RUNS.mkdir(exist_ok=True)
    rec = {"t": time.strftime("%Y-%m-%d %H:%M:%S"), "tool": tool, **kw}
    with open(GATE_TAPE, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")


def _choice(answer, allowed, fallback):
    """Jev's choice, or `fallback` when it names something outside `allowed`."""
    c = answer.get("choice")
    return (c if c in allowed else fallback, answer.get("confidence"), answer.get("probabilities", {}))


def gate_triage(events, known=(), policy="", model="jev-latest"):
    """Decide per event whether an agent must run at all.

    events: [{"kind": "cron|process|notification|message|room|ticket|...", "source": "...",
              "summary": "<= 600 chars of what happened", "exit_code": int|None,
              "expected": "what a routine result looks like"}]
    known:  short fingerprints of problems already reported (so a repeat is `known`).
    Returns one action per event and `skip_agent`, true when no event needs an agent."""
    if not events:
        raise ValueError("events is empty")
    if len(events) > MAX_BATCH:
        raise ValueError(f"at most {MAX_BATCH} events per call")
    state = {"policy": _clip(policy or DEFAULT_POLICY, CONTEXT_CHARS),
             "known": [_clip(k, ITEM_CHARS) for k in list(known)[:MAX_BATCH]],
             "events": {}}
    qs = {}
    for i, e in enumerate(events, 1):
        key = f"e{i}"
        ev = {"kind": _clip(e.get("kind", "event"), 40), "source": _clip(e.get("source", ""), 120),
              "summary": _clip(e.get("summary", ""), SUMMARY_CHARS)}
        if e.get("exit_code") is not None:
            ev["exit_code"] = int(e["exit_code"])
        if e.get("expected"):
            ev["expected"] = _clip(e["expected"], ITEM_CHARS)
        state["events"][key] = ev
        qs[key] = {"type": "choice", "criteria": TRIAGE_ACTIONS,
                   "instructions": f"See `events.{key}`, `known` and `policy`. What should happen with it?"}
    raw = jev(model, state, qs)
    results = []
    for i, e in enumerate(events, 1):
        action, conf, probs = _choice(raw["answers"][f"e{i}"], TRIAGE_ACTIONS, "act")
        results.append({"event": i, "source": state["events"][f"e{i}"]["source"], "action": action,
                        "confidence": conf, "probabilities": probs,
                        "needs_agent": action == "act", "needs_report": action == "report"})
    out = {"results": results, "skip_agent": not any(r["needs_agent"] for r in results),
           "report": [r["event"] for r in results if r["needs_report"]], "model": raw.get("model")}
    _gate_log("triage", events=len(events), actions=[r["action"] for r in results], model=out["model"])
    return out


def gate_verdict(goal, items, evidence, changes="", model="jev-latest", closed_threshold=0.7):
    """Judge, per item, whether the evidence closes it, and which review scope is still needed.

    items:    reviewer findings still open, or acceptance criteria to re-check (<= 40).
    evidence: compact facts - gate results, test tails, turn notes (each <= 500 chars).
    changes:  what changed since the items were written (e.g. the commit message)."""
    if not items:
        raise ValueError("items is empty")
    if len(items) > MAX_BATCH:
        raise ValueError(f"at most {MAX_BATCH} items per call")
    state = {"goal": _clip(goal, ITEM_CHARS), "items": {}, "changes": _clip(changes, CONTEXT_CHARS),
             "evidence": [_clip(x, ITEM_CHARS) for x in list(evidence)[:MAX_BATCH]]}
    qs = {}
    for i, item in enumerate(items, 1):
        state["items"][f"i{i}"] = _clip(item, ITEM_CHARS)
        qs[f"i{i}"] = {"type": "noul", "instructions": f"Is `items.i{i}` closed: do `evidence` and "
                                                       f"`changes` show it has been addressed for the `goal`?"}
    qs["review"] = {"type": "choice", "criteria": REVIEW_SCOPES,
                    "instructions": "Given `items`, `evidence` and `changes`, what review is still needed?"}
    raw = jev(model, state, qs)
    a = raw["answers"]
    judged = []
    for i, item in enumerate(items, 1):
        p = float(a[f"i{i}"]["noul"])
        judged.append({"item": state["items"][f"i{i}"], "p_closed": round(p, 3), "closed": p >= closed_threshold})
    review, conf, probs = _choice(a["review"], REVIEW_SCOPES, "full")
    if review == "none" and any(not j["closed"] for j in judged):
        review = "narrow"  # an open item always gets a reviewer's eyes
    out = {"items": judged, "open": [j["item"] for j in judged if not j["closed"]],
           "review": review, "review_confidence": conf, "review_probabilities": probs, "model": raw.get("model")}
    _gate_log("verdict", items=len(items), open=len(out["open"]), review=review, model=out["model"])
    return out


def gate_decide(question, options, context="", recommended="", policy="", model="jev-latest",
                min_confidence=0.6, ask_threshold=0.5):
    """Answer a question the harness was about to ask the owner, from its own options.

    options: {"label": "what choosing it means"} (2-12). recommended: the label the harness
    would suggest, if any. Returns the choice and `proceed`: true when Jev is confident and
    the owner does not need to decide it themselves."""
    if not isinstance(options, dict) or not 2 <= len(options) <= 12:
        raise ValueError("options must map 2-12 labels to descriptions")
    state = {"policy": _clip(policy or DEFAULT_POLICY, CONTEXT_CHARS),
             "question": _clip(question, ITEM_CHARS),
             "options": {_clip(k, 80): _clip(v, ITEM_CHARS) for k, v in options.items()},
             "context": _clip(context, CONTEXT_CHARS)}
    if recommended:
        state["recommended"] = _clip(recommended, 80)
    qs = {"choice": {"type": "choice", "criteria": state["options"],
                     "instructions": "Which option best answers `question` for this owner, given "
                                     "`context`, `policy` and (if set) `recommended`?"},
          "ask_owner": {"type": "noul",
                        "instructions": "Must the owner decide this themselves: is the chosen step "
                                        "irreversible, does it spend money, publish or send something "
                                        "outward, need credentials or an interactive login, or "
                                        "contradict `policy` or an explicit instruction in `context`?"}}
    raw = jev(model, state, qs)
    a = raw["answers"]
    choice, conf, probs = _choice(a["choice"], state["options"], recommended or next(iter(state["options"])))
    p_ask = float(a["ask_owner"]["noul"])
    proceed = (conf is None or float(conf) >= min_confidence) and p_ask < ask_threshold
    out = {"choice": choice, "confidence": conf, "probabilities": probs, "p_ask_owner": round(p_ask, 3),
           "proceed": proceed,
           "reason": ("decide and continue" if proceed else
                      "ask the owner: the step needs their decision" if p_ask >= ask_threshold else
                      "ask the owner: Jev is not confident between the options"),
           "model": raw.get("model")}
    _gate_log("decide", question=state["question"][:120], choice=choice, proceed=proceed,
              p_ask_owner=out["p_ask_owner"], model=out["model"])
    return out


def gate_pick(task, candidates, context="", model="jev-latest", min_confidence=0.5):
    """Pick one candidate - a tool, skill, session, channel or file - for a task from one-line
    descriptions, instead of reading every candidate's full description first."""
    if not isinstance(candidates, dict) or not 2 <= len(candidates) <= MAX_BATCH:
        raise ValueError(f"candidates must map 2-{MAX_BATCH} names to one-line descriptions")
    state = {"task": _clip(task, ITEM_CHARS), "context": _clip(context, CONTEXT_CHARS),
             "candidates": {_clip(k, 80): _clip(v, 200) for k, v in candidates.items()}}
    qs = {"pick": {"type": "choice", "criteria": state["candidates"],
                   "instructions": "Which candidate fits `task` best, given `context`?"}}
    raw = jev(model, state, qs)
    pick, conf, probs = _choice(raw["answers"]["pick"], state["candidates"], None)
    confident = pick is not None and (conf is None or float(conf) >= min_confidence)
    ranked = sorted(probs.items(), key=lambda kv: -float(kv[1]))[:3]
    out = {"pick": pick if confident else None, "confidence": conf, "top": ranked,
           "reason": "use pick" if confident else "no clear fit: inspect the top candidates yourself",
           "model": raw.get("model")}
    _gate_log("pick", task=state["task"][:120], pick=out["pick"], model=out["model"])
    return out


GATE_CLI = {"triage": gate_triage, "verdict": gate_verdict, "decide": gate_decide, "pick": gate_pick}


def gate_cli(name, payload):
    """`python jev_mcp.py --gate <name> < request.json`: the same Gate decision for scripts and
    cron jobs that have no MCP host. The JSON object holds the tool's keyword arguments."""
    if name not in GATE_CLI:
        raise ValueError(f"unknown gate '{name}'; use one of {sorted(GATE_CLI)}")
    if not isinstance(payload, dict):
        raise ValueError("the request must be a JSON object of keyword arguments")
    return GATE_CLI[name](**payload)


# ---------- MCP ----------

def build_server():
    from mcp.server.mcpserver import MCPServer

    mcp = MCPServer("jev-loop", version=VERSION, instructions=(
        "Jev Loop server for the installed jev skill. Call loop_start, then repeat "
        "loop_decide -> (review -> loop_record_review) -> execute -> loop_record_turn "
        "until a response has next='stop'. Never skip a step or invent a decision. "
        "Gate tools run before expensive work: gate_triage before reasoning about a "
        "scheduled, polled or notified event; gate_verdict before another review round; "
        "gate_decide before asking the owner a question you have options for."))

    def safe(fn):
        try:
            return fn()
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}

    @mcp.tool()
    def loop_start(goal_path: str) -> dict:
        """Start a Jev Loop run from its goal JSON file and run checks once.

        Use with the installed jev skill. Returns the run_id; next call loop_decide.
        """
        def go():
            run = Run.create(goal_path)
            c = run.cfg
            return {"run_id": run.id, "workdir": c["workdir"], "goal": c["goal"],
                    "acceptance": c.get("acceptance", []), "checks": c.get("checks", []),
                    "roles": {k: v["brief"] for k, v in c["roles"].items()},
                    "max_turns": c["max_turns"], "tape": str(run.tape_path),
                    "next": "call loop_decide"}
        return safe(go)

    @mcp.tool()
    def loop_decide(run_id: str) -> dict:
        """Ask Jev for the next step. Returns next='execute' (do the role's work in workdir,
        then loop_record_turn), next='review' (run an independent review, then
        loop_record_review) or next='stop' (report the reason to the user)."""
        return safe(lambda: Run(run_id).decide())

    @mcp.tool()
    def loop_record_review(run_id: str, done: bool, missing: list[str]) -> dict:
        """Record the reviewer's verdict. done=true ends the run as goal_met; otherwise
        'missing' is fed back into the loop and next='execute' follows."""
        return safe(lambda: Run(run_id).record_review(done, missing))

    @mcp.tool()
    def loop_record_turn(run_id: str, notes: str, files: list[str], executor_ok: bool = True) -> dict:
        """Record the executed turn: notes (1-2 sentences), files written (relative to
        workdir). Set executor_ok=false if you could not complete the step. The server
        runs the checks and returns them. Next: loop_decide."""
        return safe(lambda: Run(run_id).record_turn(notes, files, executor_ok))

    @mcp.tool()
    def loop_status(run_id: str) -> dict:
        """Current phase, checks, history and tape path of a run."""
        return safe(lambda: Run(run_id).summary())

    @mcp.tool()
    def gate_triage(events: list[dict], known: list[str] = [], policy: str = "") -> dict:  # noqa: B006
        """Before reasoning about scheduled, polled or notified events, ask Jev what each needs.

        events: [{kind, source, summary (<=600 chars), exit_code?, expected?}], up to 40 in one
        call. known: fingerprints of problems already reported. Returns per event
        action = ignore | known | report | act, plus skip_agent (no event needs an agent) and
        report (events whose summary should be passed on as-is). Only 'act' justifies running
        a large model."""
        return safe(lambda: globals()["gate_triage"](events, known, policy))

    @mcp.tool()
    def gate_verdict(goal: str, items: list[str], evidence: list[str], changes: str = "") -> dict:
        """Before another review round, ask Jev which findings the evidence already closes.

        items: open reviewer findings or acceptance criteria (<=40). evidence: compact facts
        such as gate results, test tails and turn notes. changes: what changed since (commit
        message). Returns closed/open per item and review = none | narrow | full."""
        return safe(lambda: globals()["gate_verdict"](goal, items, evidence, changes))

    @mcp.tool()
    def gate_decide(question: str, options: dict[str, str], context: str = "",
                    recommended: str = "", policy: str = "") -> dict:
        """Before asking the owner a question you already have options for, ask Jev.

        options: {label: meaning} (2-12). recommended: the label you would suggest. Returns
        choice, confidence, p_ask_owner and proceed. If proceed is true, act on choice and tell
        the owner in one line; otherwise ask the owner, leading with the recommended option."""
        return safe(lambda: globals()["gate_decide"](question, options, context, recommended, policy))

    @mcp.tool()
    def gate_pick(task: str, candidates: dict[str, str], context: str = "") -> dict:
        """Before reading every tool, skill, session or file description, ask Jev which one fits.

        candidates: {name: one-line description} (2-40). Returns pick (or null when nothing
        fits clearly), confidence and the top candidates. Read only the picked candidate's
        full description."""
        return safe(lambda: globals()["gate_pick"](task, candidates, context))

    return mcp


def self_check():
    """One tiny live Jev call: proves dependencies, key and network work. Exit 0 = OK."""
    import mcp.server.mcpserver  # noqa: F401  (dependency check)
    raw = jev("jev-latest", "The build passed and all tests are green.",
              {"ok": {"type": "noul", "instructions": "Did the build succeed?"}})
    p = raw["answers"]["ok"]["noul"]
    print(f"jev-loop {VERSION}: OK (model {raw.get('model')}, p={p:.2f})")
    return 0 if p > 0.5 else 1


def serve_http(host, port):
    """Serve the same tools over streamable HTTP at http://host:port/mcp, so one always-on
    machine can host the server for every harness on the tailnet. Stateless: run state is
    on disk, Gate has none. Bind to a tailnet address; the server has no auth of its own."""
    build_server().run("streamable-http", host=host, port=port, stateless_http=True)


if __name__ == "__main__":
    import sys
    if "--check" in sys.argv:
        sys.exit(self_check())
    if "--http" in sys.argv:
        def arg(flag, default):
            return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else default
        serve_http(arg("--host", "127.0.0.1"), int(arg("--port", "8765")))
        sys.exit(0)
    if "--gate" in sys.argv:
        name = sys.argv[sys.argv.index("--gate") + 1] if len(sys.argv) > sys.argv.index("--gate") + 1 else ""
        try:
            print(json.dumps(gate_cli(name, json.load(sys.stdin)), ensure_ascii=False))
            sys.exit(0)
        except Exception as e:  # a failed decision is reported, never guessed
            print(json.dumps({"error": f"{type(e).__name__}: {e}"}, ensure_ascii=False))
            sys.exit(2)
    build_server().run()
