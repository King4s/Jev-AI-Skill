#!/usr/bin/env python
"""Ask Jev (TypeSafe System One) how to run the next task.

Reads a JSON request on stdin (or from a file given as the first argument):

    {
      "task": "What the next task is, 1-3 sentences",
      "context": "Optional: project, what has happened, constraints",
      "skills": {"skill-name": "one-line description", ...},   # shortlist, max ~12
      "main_model": "opus"                                       # optional, default opus
    }

Prints one JSON decision on stdout:

    {"model": "haiku|sonnet|opus|fable", "skill": "<name>|none", "subagent": true|false,
     "reason": "...", "raw": {...probabilities...}}

Jev supplies the judgments; the policy that turns them into a decision is the code below.
Needs TYPESAFE_API_KEY in the environment or ~/.config/jev-loop/typesafe_api_key.
"""
import json
import os
import sys
import time
from pathlib import Path

import math
import urllib.request
import urllib.error
import urllib.parse

API = "https://api.typesafe.ai/v1/systemone"
JEV_MODEL = os.environ.get("JEV_ROUTE_MODEL", "jev-latest")
KEY_FILE = Path.home() / ".config" / "jev-loop" / "typesafe_api_key"

# Policy thresholds (explicit so they can be tuned without touching the questions).
SUBAGENT_THRESHOLD = 0.5      # same model as main session: delegate at or above this
SKILL_MIN_PROB = 0.35         # below this the top skill is not trusted -> "none"
IMPORTANT_THRESHOLD = 0.6     # p(important) at or above this -> one model tier up

TIERS = ["haiku", "sonnet", "opus", "fable"]  # cheapest -> most capable

MODELS = {
    "haiku": "Routine, mechanical work with a clear recipe: run a command and report, check "
             "numbers, rename or move files, apply a precisely specified edit, simple lookups "
             "or searches, summarising a short output.",
    "sonnet": "Ordinary engineering work: implement a well-specified feature or fix, write tests, "
              "focused debugging with a known symptom, a moderate refactor, reading several "
              "files to answer a concrete question.",
    "opus": "Judgment-heavy work: architecture or design decisions, ambiguous requirements, "
            "hard debugging with unknown cause, code review, security-sensitive changes, "
            "anything needing repo-wide understanding or careful trade-offs with the user.",
    "fable": "The hardest work only: long-horizon autonomous tasks, deep multi-step reasoning "
             "where Opus is likely to fall short, or a problem that has already defeated a "
             "strong model. This is a capability tier, not a promised model ID.",
}


def api_key():
    key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if not key and KEY_FILE.exists():
        key = KEY_FILE.read_text(encoding="utf-8").strip()
    if not key:
        sys.exit(f"No TypeSafe API key: set TYPESAFE_API_KEY or write it to {KEY_FILE}.")
    return key


def jev(state, questions, retries=4):
    """Bounded retry for transient HTTP/network errors; never echo response secrets."""
    data = json.dumps({"model": JEV_MODEL, "state": state, "questions": questions}).encode("utf-8")
    request = urllib.request.Request(API, data=data, headers={
        "Authorization": f"Bearer {api_key()}", "Content-Type": "application/json"})
    for attempt in range(retries + 1):
        delay = min(2 ** attempt, 30)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                result = json.loads(response.read().decode("utf-8"))
            if not isinstance(result, dict) or not isinstance(result.get("answers"), dict):
                raise ValueError("Jev returned no answers object")
            return result
        except urllib.error.HTTPError as exc:
            if exc.code not in (408, 429, 500, 502, 503, 504, 529) or attempt == retries:
                raise RuntimeError(f"Jev HTTP {exc.code}") from None
            try:
                requested = float(exc.headers.get("Retry-After", delay))
                if math.isfinite(requested):
                    delay = max(0, min(requested, 30))
            except (ValueError, TypeError):
                pass
            exc.close()
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt == retries:
                raise RuntimeError("Jev request failed after bounded retries") from None
        except (ValueError, UnicodeError):
            raise RuntimeError("Jev returned invalid JSON or answer structure") from None
        time.sleep(delay)


def probability(value):
    number = float(value)
    if not math.isfinite(number) or not 0 <= number <= 1:
        raise ValueError("Jev probability must be between zero and one")
    return number


def decide(req):
    if not isinstance(req, dict):
        raise ValueError("Request must be a JSON object")
    task = (req.get("task") or "").strip()
    if not task:
        sys.exit("Request needs a non-empty 'task'.")
    skills = dict(req.get("skills") or {})
    main_model = req.get("main_model", "opus")
    if main_model not in TIERS:
        raise ValueError("main_model must be a tier: haiku, sonnet, opus or fable")
    review = req.get("review", False)
    if not isinstance(review, bool):
        raise ValueError("review must be a boolean")

    state = {
        "task": task,
        "context": req.get("context", ""),
        "main_session_model": main_model,
        "delegation_policy": "The user conserves usage: routine or mechanical work should run in "
                             "a subagent on a cheaper model; design decisions, review and work "
                             "that needs the conversation's context stay in the main session.",
    }
    skill_criteria = {name: desc for name, desc in skills.items()}
    skill_criteria["none"] = "No listed skill fits; the task is best done directly without a skill."

    questions = {
        "model": {"type": "choice",
                  "instructions": "Which is the cheapest model that can do the `task` well?",
                  "criteria": MODELS},
        "subagent": {"type": "noul",
                     "instructions": "Should the `task` be handed to a subagent with a "
                                     "self-contained prompt instead of being done in the main "
                                     "session? Yes when it is well-scoped, can be described "
                                     "without the conversation history, and its result can be "
                                     "summarised back. No when it needs back-and-forth with the "
                                     "user, the main session's accumulated context, or is a "
                                     "tiny step cheaper to just do. Consider `delegation_policy`."},
        "important": {"type": "noul",
                      "instructions": "Is the `task` important: high stakes where a mistake is "
                                      "costly or hard to undo (production, data, security, "
                                      "releases, core architecture, user-facing results), or "
                                      "has the user said it matters? Routine work is not."},
        "skill": {"type": "choice",
                  "instructions": "Which skill's instructions best fit how the `task` should be "
                                  "done? Pick `none` if no skill clearly applies.",
                  "criteria": skill_criteria},
    }

    raw = jev(state, questions)
    a = raw["answers"]
    model = a["model"]["choice"]
    if model not in TIERS:
        raise ValueError("Jev returned an unknown model tier")
    p_sub = probability(a["subagent"]["noul"])
    skill = a["skill"]["choice"]
    if skill not in skill_criteria:
        raise ValueError("Jev returned an unknown skill")
    skill_p = probability(a["skill"].get("probabilities", {}).get(skill, 1.0))

    reasons = []
    p_imp = probability(a["important"]["noul"])
    if p_imp >= IMPORTANT_THRESHOLD and model in TIERS[:-1]:
        up = TIERS[TIERS.index(model) + 1]
        reasons.append(f"important (p={p_imp:.2f}): {model} -> {up}")
        model = up
    if review and TIERS.index(model) < TIERS.index(main_model):
        reasons.append("reviewer raised to main session tier")
        model = main_model
    if skill != "none" and skill_p < SKILL_MIN_PROB:
        reasons.append(f"top skill '{skill}' only p={skill_p:.2f}; using none")
        skill = "none"

    # A model other than the main session's can only run as a subagent, so it always delegates.
    # The main session keeps its history; the subagent only needs a self-contained brief.
    subagent = review or p_sub >= SUBAGENT_THRESHOLD
    if model != main_model:
        if not subagent:
            reasons.append(f"model '{model}' differs from main session -> delegate "
                           f"(p(subagent)={p_sub:.2f})")
        subagent = True
    elif subagent:
        reasons.append("delegated for context isolation, same model as main session")

    return {
        "model": model,
        "skill": skill,
        "subagent": subagent,
        "reason": "; ".join(reasons) or "direct from Jev",
        "raw": {
            "model_probs": a["model"].get("probabilities"),
            "model_conf": a["model"].get("confidence"),
            "p_subagent": round(p_sub, 3),
            "p_important": round(p_imp, 3),
            "skill_probs": a["skill"].get("probabilities"),
            "jev_model": raw.get("model"),
        },
    }


def main():
    if len(sys.argv) > 1 and sys.argv[1] != "-":
        req = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    else:
        req = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    out = json.dumps(decide(req), ensure_ascii=False, indent=2)
    sys.stdout.buffer.write(out.encode("utf-8") + b"\n")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        sys.exit(f"Jev decision failed: {exc}")
