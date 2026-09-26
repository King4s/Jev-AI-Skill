"""Legacy standalone Loop variant: Jev decides while OpenRouter models do the work.

The maintained Loop capability uses the ``jev`` skill and ``jev-loop`` MCP server, with
the active harness as executor and reviewer. This script remains a separate alternative.

Usage:  python loop.py goal.json
Needs:  OPENROUTER_API_KEY and TYPESAFE_API_KEY in the environment, `pip install requests`.

Per turn:
  1. Jev (one batched call): route -> which role runs next, done -> p(goal met),
     recovery -> retry/reroute/escalate (only after a failed turn).
  2. If the checks passed last turn AND p(done) >= threshold: a strong model reviews.
     Only the review can end the run successfully. Jev is a cheap filter, not the judge.
  3. Otherwise the chosen role's executor writes files; checks run; everything is logged.
Hard stops: max_turns, max_consecutive_failures, or Jev choosing 'escalate'.
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).parent
API = "https://openrouter.ai/api"
KEY = os.environ.get("OPENROUTER_API_KEY")
TS_KEY = os.environ.get("TYPESAFE_API_KEY")
if not KEY or not TS_KEY:
    sys.exit("Set OPENROUTER_API_KEY (executor/reviewer) and TYPESAFE_API_KEY (Jev) first.")
H = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
SKIP_DIRS = {"node_modules", "__pycache__", "venv", ".venv", ".git", ".pytest_cache"}


# ---------- config & logging ----------

def load_cfg(path):
    p = Path(path).resolve()
    cfg = json.loads(p.read_text(encoding="utf-8"))
    cfg["workdir"] = (p.parent / cfg["workdir"]).resolve()
    cfg["workdir"].mkdir(parents=True, exist_ok=True)
    return cfg


class Tape:
    """Decision tape: one JSON line per event in runs/<timestamp>.jsonl."""

    def __init__(self):
        d = ROOT / "runs"
        d.mkdir(exist_ok=True)
        self.path = d / f"{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
        self.f = open(self.path, "a", encoding="utf-8")

    def log(self, kind, quiet=False, **kw):
        rec = {"t": time.strftime("%H:%M:%S"), "kind": kind, **kw}
        self.f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        self.f.flush()
        if not quiet:
            print(f"[{rec['t']}] {kind}: {json.dumps(kw, ensure_ascii=False, default=str)[:220]}")


# ---------- Jev ----------

def jev(cfg, state, questions, tape):
    r = requests.post(
        "https://api.typesafe.ai/v1/systemone", timeout=60,
        headers={"Authorization": f"Bearer {TS_KEY}", "Content-Type": "application/json"},
        json={"model": cfg["jev_model"], "state": state, "questions": questions},
    )
    if r.status_code >= 400:
        tape.log("jev_error", status=r.status_code, body=r.text[:1000])
        r.raise_for_status()
    raw = r.json()
    tape.log("jev_raw", quiet=True, raw=raw)
    return raw


# Verified response shape (docs.typesafe.ai/api):
#   {"answers": {"<id>": {"type": "choice", "choice": ..., "probabilities": {...}}
#                         | {"type": "noul", "noul": 0.95}}}

def _entry(raw, name):
    return raw["answers"][name]


def as_choice(e):
    return e["choice"]


def as_prob(e):
    return float(e["noul"])


# ---------- executor & reviewer ----------

def llm(model, system, user, max_tokens=8000):
    r = requests.post(
        f"{API}/v1/chat/completions", headers=H, timeout=600,
        json={"model": model, "max_tokens": max_tokens,
              "messages": [{"role": "system", "content": system},
                           {"role": "user", "content": user}]},
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def parse_json(text):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("model returned no JSON object")
    return json.loads(m.group(0))


EXEC_SYS = """You are the '{role}' agent in an automated build loop. Your role: {brief}

Do the single most useful next step for your role, based on the goal, check results,
reviewer feedback and current files.

Respond ONLY with a JSON object, no prose, no code fences:
{{"files": [{{"path": "relative/path", "content": "complete file content"}}], "notes": "one or two sentences on what you did"}}

Paths are relative to the project root. Always write complete files, never diffs.
Use "files": [] if nothing needs to change."""

REVIEW_SYS = """You are a strict reviewer. Decide whether the goal and every acceptance
criterion are FULLY met by the project below. Passing checks are necessary but not sufficient.

Respond ONLY with a JSON object, no prose:
{"done": true or false, "missing": ["concrete missing or wrong item", ...]}"""


def snapshot(workdir, budget=60000):
    parts, used = [], 0
    for p in sorted(workdir.rglob("*")):
        rel_parts = p.relative_to(workdir).parts
        if p.is_dir() or any(x in SKIP_DIRS or x.startswith(".") for x in rel_parts):
            continue
        rel = p.relative_to(workdir).as_posix()
        try:
            txt = p.read_text(encoding="utf-8")
        except Exception:
            parts.append(f"--- {rel} (binary/unreadable)")
            continue
        if used + len(txt) > budget:
            parts.append(f"--- {rel} ({len(txt)} chars, omitted for size)")
            continue
        parts.append(f"--- {rel}\n{txt}")
        used += len(txt)
    return "\n".join(parts) or "(empty project)"


def apply_files(workdir, files):
    written = []
    for f in files:
        p = (workdir / f["path"]).resolve()
        if workdir != p and workdir not in p.parents:
            raise ValueError(f"path escapes workdir: {f['path']}")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f["content"], encoding="utf-8")
        written.append(f["path"])
    return written


def run_checks(cfg):
    results = []
    for cmd in cfg.get("checks", []):
        try:
            r = subprocess.run(cmd, shell=True, cwd=cfg["workdir"], capture_output=True,
                               text=True, timeout=cfg.get("check_timeout", 300))
            results.append({"cmd": cmd, "ok": r.returncode == 0,
                            "out": (r.stdout + r.stderr)[-2000:]})
        except subprocess.TimeoutExpired:
            results.append({"cmd": cmd, "ok": False, "out": "TIMEOUT"})
    return results


def context_block(cfg, checks, history, feedback):
    acc = "\n".join(f"- {a}" for a in cfg.get("acceptance", [])) or "(none given)"
    chk = "\n".join(f"[{'OK' if c['ok'] else 'FAIL'}] {c['cmd']}\n{c['out'][-1200:]}"
                    for c in checks) or "(no checks configured)"
    hist = "\n".join(f"turn {h['turn']} [{h['role']}] ok={h['checks_ok']}: {h['notes']}"
                     for h in history[-6:]) or "(first turn)"
    return (f"GOAL:\n{cfg['goal']}\n\nACCEPTANCE CRITERIA:\n{acc}\n\n"
            f"REVIEWER FEEDBACK:\n{feedback or '(none yet)'}\n\n"
            f"LAST CHECK RESULTS:\n{chk}\n\nRECENT TURNS:\n{hist}\n\n"
            f"CURRENT PROJECT FILES:\n{snapshot(cfg['workdir'])}")


# ---------- the loop ----------

def main(cfg_path):
    cfg = load_cfg(cfg_path)
    tape = Tape()
    roles = cfg["roles"]
    history, feedback = [], ""
    fails, last_role = 0, None
    checks = run_checks(cfg)
    checks_ok = all(c["ok"] for c in checks)
    tape.log("start", goal=cfg["goal"], workdir=cfg["workdir"], tape=tape.path)

    for turn in range(1, cfg["max_turns"] + 1):
        # Compact, structured state for Jev. No raw agent output: Jev is weak on distractors.
        state = json.dumps({
            "goal": cfg["goal"],
            "turn": turn,
            "all_checks_pass": checks_ok,
            "checks": [{"cmd": c["cmd"], "ok": c["ok"], "tail": c["out"][-300:]} for c in checks],
            "reviewer_missing": feedback[:800],
            "recent_turns": history[-5:],
        }, ensure_ascii=False, indent=1)

        qs = {
            "route": {"type": "choice",
                      "instructions": "Which agent should take the next turn toward the goal?",
                      "criteria": {k: v["when"] for k, v in roles.items()}},
            "done": {"type": "noul",
                     "instructions": "Is the goal fully met, with every check passing and nothing the reviewer listed as missing left undone?"},
        }
        if fails:
            qs["recovery"] = {"type": "choice",
                              "instructions": "The last turn failed. What is the best recovery?",
                              "criteria": {
                                  "retry": "Same agent again; the failure looks transient or nearly fixed",
                                  "reroute": "A different agent is better suited to fix this",
                                  "escalate": "Repeated failures with no real progress; a human is needed"}}

        raw = jev(cfg, state, qs, tape)
        role = as_choice(_entry(raw, "route"))
        p_done = as_prob(_entry(raw, "done"))
        tape.log("decide", turn=turn, route=role, p_done=round(p_done, 3))

        # Cascade: cheap Jev filter first, expensive review only when it could end the run.
        if checks_ok and p_done >= cfg["jev_done_threshold"]:
            try:
                v = parse_json(llm(cfg["review_model"], REVIEW_SYS,
                                   context_block(cfg, checks, history, feedback), 2000))
            except Exception as e:
                v = {"done": False, "missing": [f"review failed: {e}"]}
            tape.log("review", done=v.get("done"), missing=v.get("missing"))
            if v.get("done") is True:
                tape.log("stop", reason="goal_met", turns=turn)
                print(f"\n>> Done in {turn - 1} executor turns. Tape: {tape.path}")
                return 0
            feedback = "\n".join(f"- {m}" for m in v.get("missing", []))

        if fails:
            rec = as_choice(_entry(raw, "recovery"))
            tape.log("recovery", decision=rec, consecutive_fails=fails)
            if rec == "escalate" or fails >= cfg["max_consecutive_failures"]:
                tape.log("stop", reason="escalate", turns=turn)
                print(f"\n>> Stopped: needs a human. Tape: {tape.path}")
                return 2
            # 'route' already saw the failure and stays the primary decision; only a
            # 'reroute' that landed on the same role is redirected.
            if rec == "reroute" and role == last_role and len(roles) > 1:
                probs = {k: v for k, v in _entry(raw, "route")["probabilities"].items() if k != last_role}
                role = max(probs, key=probs.get)

        if role not in roles:
            tape.log("warn", msg=f"unknown role '{role}', falling back")
            role = next(iter(roles))

        spec = roles[role]
        try:
            out = parse_json(llm(spec.get("model", cfg["executor_model"]),
                                 EXEC_SYS.format(role=role, brief=spec["brief"]),
                                 context_block(cfg, checks, history, feedback)))
            written = apply_files(cfg["workdir"], out.get("files", []))
            notes, exec_ok = str(out.get("notes", ""))[:300], True
        except Exception as e:
            written, notes, exec_ok = [], f"executor error: {e}"[:300], False

        checks = run_checks(cfg)
        checks_ok = exec_ok and (not checks or all(c["ok"] for c in checks))
        fails = 0 if checks_ok else fails + 1
        last_role = role
        history.append({"turn": turn, "role": role, "files": written,
                        "notes": notes, "checks_ok": checks_ok})
        tape.log("turn", **history[-1])

    tape.log("stop", reason="max_turns")
    print(f"\n>> Hit max_turns without finishing. Tape: {tape.path}")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "goal.json"))
