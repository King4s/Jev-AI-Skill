#!/usr/bin/env python
"""Let Jev decide the next git step for the current repository.

Usage:  python git_decide.py [--repo PATH] [--summary "what the work is"]
                             [--reviewed] [--checks-green] [--scan-only]

Code gathers the facts and enforces the hard rules; Jev (TypeSafe System One) judges.
Prints one JSON decision:

    {"action": "none|commit|push_branch|open_pr",
     "blocked": [...hard-rule violations...],
     "commands": [...display guidance only...], "argv": [...argument vectors...],
     "reason": "...", "facts": {...}, "raw": {...}}

Hard rules (never overridden by Jev):
  - never push a protected branch (main) directly, never force-push, never push --all/--mirror;
  - nothing is pushed while a blocked SHA, a non-allowed author/committer e-mail, a
    private pattern or a secret appears in the outgoing commits;
  - a PR requires --reviewed and --checks-green.
Private per-repo rules live outside the repo in ~/.config/jev-git/policies/*.json.
"""
import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

import math
import urllib.request
import urllib.error
import urllib.parse

API = "https://api.typesafe.ai/v1/systemone"
JEV_MODEL = os.environ.get("JEV_GIT_MODEL", "jev-latest")
KEY_FILE = Path.home() / ".config" / "jev-loop" / "typesafe_api_key"
POLICIES = Path.home() / ".config" / "jev-git" / "policies"

PUBLISH_THRESHOLD = 0.6   # p(publishable) needed before pushing or opening a PR
NEEDS_USER_THRESHOLD = 0.5  # p(needs user) at or above this -> no outward action

SECRET_PATTERNS = [
    r"sk-[A-Za-z0-9_-]{20,}", r"ghp_[A-Za-z0-9]{30,}", r"github_pat_[A-Za-z0-9_]{30,}",
    r"AKIA[0-9A-Z]{16}", r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    r"(?i)(api[_-]?key|password|secret|token)\s*[:=]\s*['\"][^'\"\s]{12,}['\"]",
]

ACTIONS = {
    "none": "Nothing to record or share now: no changes, or the work is unfinished, "
            "experimental or waiting for a decision.",
    "commit": "Finished, coherent changes are uncommitted and should be recorded locally.",
    "push_branch": "Local commits on a feature branch are finished and worth backing up or "
                   "sharing on the remote, but are not yet ready to propose for main.",
    "open_pr": "The feature branch is finished, tested and reviewed, and should be proposed "
               "for merging into main via a pull request.",
}


def git(repo, *args, check=True, strip=True):
    r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout.strip() if strip else r.stdout


def canonical_remote(remote_url):
    """Normalize SSH/HTTPS transport without substring or owner-name matching."""
    value = remote_url.strip()
    if re.match(r"^[^/@:]+@[^/:]+:", value):
        user_host, path = value.split(":", 1)
        value = "ssh://" + user_host + "/" + path
    if "://" not in value:
        value = "https://" + value
    parsed = urllib.parse.urlsplit(value)
    path = parsed.path.rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if not parsed.hostname or not path or parsed.query or parsed.fragment:
        return None
    return parsed.hostname.lower() + (f":{parsed.port}" if parsed.port else "") + path


def load_policy(remote_url):
    norm = canonical_remote(remote_url)
    matches = []
    for p in sorted(POLICIES.glob("*.json")):
        pol = json.loads(p.read_text(encoding="utf-8"))
        if norm and pol.get("remote") and canonical_remote(pol["remote"]) == norm:
            matches.append((pol, str(p)))
    if len(matches) > 1:
        raise ValueError("Multiple local policies match the same remote")
    return matches[0] if matches else ({}, None)

def pr_state(repo, branch):
    try:
        r = subprocess.run(["gh", "pr", "view", branch, "--json", "state,url"], cwd=repo,
                           capture_output=True, text=True, timeout=30)
        return json.loads(r.stdout) if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None


def facts_for(repo, summary, reviewed, checks_green):
    branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    remote = git(repo, "remote", "get-url", "origin", check=False)
    fetch_urls = git(repo, "remote", "get-url", "--all", "origin", check=False).splitlines()
    push_urls = git(repo, "remote", "get-url", "--push", "--all", "origin", check=False).splitlines()
    # Remote-tracking refs describe the fetch destination. A separate push server
    # needs its own policy and history, which this helper does not support.
    supported_destination = len(fetch_urls) == len(push_urls) == 1 and fetch_urls == push_urls
    policy, policy_path = load_policy(remote) if remote else ({}, None)
    protected = set(policy.get("protected_branches", ["main", "master"]))
    base = "origin/main" if git(repo, "rev-parse", "--verify", "-q", "origin/main",
                               check=False) else None
    upstream = git(repo, "rev-parse", "--abbrev-ref", "@{u}", check=False) or None

    status = git(repo, "status", "--porcelain", strip=False).splitlines()
    # Commands below explicitly push this branch to origin, regardless of @{u}.
    # An unrelated upstream/base cannot prove that origin already has a commit.
    origin_ref = f"refs/remotes/origin/{branch}"
    origin_tip = git(repo, "rev-parse", "--verify", "-q", origin_ref, check=False)
    outgoing_range = f"{origin_tip}..HEAD" if supported_destination and origin_tip else "HEAD"
    commits = [l for l in git(repo, "log", "--format=%h%x09%ae%x09%ce%x09%s",
                              outgoing_range, check=False).splitlines() if l]
    ahead_of_main = int(git(repo, "rev-list", "--count", f"{base}..HEAD") or 0) if base else None

    blocked = []
    if branch == "HEAD":
        blocked.append("detached HEAD: select a branch before publishing")
    if not remote:
        blocked.append("no origin remote configured")
    elif not supported_destination:
        blocked.append("origin has multiple or differing fetch/push destinations: unsupported for publication")
    if branch in protected:
        blocked.append(f"on protected branch '{branch}': work on a feature branch")
    allowed = set(policy.get("allowed_author_emails", []))
    full_shas = git(repo, "log", "--format=%H", outgoing_range, check=False).split()
    for sha in policy.get("blocked_shas", []):
        if any(s.startswith(sha) for s in full_shas):
            blocked.append(f"blocked commit {sha} is in the outgoing range")
    for c in commits:
        sha, ae, ce, subj = c.split("\t", 3)
        if allowed and (ae not in allowed or ce not in allowed):
            blocked.append(f"commit {sha} uses a non-allowed identity (author/committer)")

    # Scan additions against every merge parent too: plain log -p omits merge
    # diffs and would miss content introduced only during conflict resolution.
    added = []
    if commits:
        added += [l[1:] for l in git(repo, "log", "-p", "-m", "--format=", outgoing_range,
                                     check=False).splitlines()
                  if l.startswith("+") and not l.startswith("+++")]
    added += [l[1:] for l in git(repo, "diff", "HEAD", check=False).splitlines()
              if l.startswith("+") and not l.startswith("+++")]
    text = "\n".join(added) + "\n" + "\n".join(c.split("\t", 3)[3] for c in commits)
    for pat in policy.get("blocked_patterns", []):
        if re.search(pat, text):
            blocked.append(f"private pattern /{pat}/ appears in outgoing changes")
    for pat in SECRET_PATTERNS:
        if re.search(pat, text):
            blocked.append("something that looks like a secret appears in outgoing changes")
            break

    pr = pr_state(repo, branch) if remote and (canonical_remote(remote) or "").startswith("github.com/") else None
    facts = {
        "repo": os.path.basename(os.path.abspath(repo)),
        "branch": branch,
        "upstream": upstream,
        "has_remote": bool(remote),
        "uncommitted_files": [l[3:] for l in status][:40],
        "outgoing_commits": [c.split("\t", 3)[0] + " " + c.split("\t", 3)[3] for c in commits][:30],
        "commits_ahead_of_main": ahead_of_main,
        "existing_pr": pr,
        "work_summary": summary or "",
        "reviewed_independently": reviewed,
        "checks_green": checks_green,
        "policy_notes": policy.get("notes", ""),
    }
    return facts, blocked, policy_path


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


def argv_for(action, facts):
    """Argument vectors only; placeholders must be replaced before execution."""
    branch = facts["branch"]
    if action == "commit":
        return [["git", "add", "--", "<the finished files>"],
                ["git", "commit", "-m", "<message>"]]
    if action == "push_branch":
        return [["git", "push", "-u", "origin", branch]]
    if action == "open_pr":
        push = ["git", "push", "origin", branch] if facts["upstream"] else [
            "git", "push", "-u", "origin", branch]
        return [push, ["gh", "pr", "create", "--base", "main", "--head", branch,
                       "--title", "<title>", "--body-file", "<body file>"]]
    return []


def commands_for(action, facts):
    """POSIX-quoted display guidance, never input for shell evaluation."""
    return [shlex.join(argv) for argv in argv_for(action, facts)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=".")
    ap.add_argument("--summary", default="")
    ap.add_argument("--reviewed", action="store_true")
    ap.add_argument("--checks-green", action="store_true")
    ap.add_argument("--scan-only", action="store_true", help="facts and blocks, no Jev call")
    a = ap.parse_args()

    facts, blocked, policy_path = facts_for(a.repo, a.summary, a.reviewed, a.checks_green)
    out = {"facts": facts, "blocked": blocked, "policy": policy_path}
    if a.scan_only:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return

    questions = {
        "action": {"type": "choice",
                   "instructions": "Given the repository `branch`, `uncommitted_files`, "
                                   "`outgoing_commits`, `existing_pr`, `work_summary`, "
                                   "`reviewed_independently` and `checks_green`, what is the "
                                   "right next git step?",
                   "criteria": ACTIONS},
        "publishable": {"type": "noul",
                        "instructions": "Is this work finished and verified enough to share "
                                        "on the remote (checks green, reviewed, not an "
                                        "experiment or half-done change)?"},
        "needs_user": {"type": "noul",
                       "instructions": "Does publishing this work depend on a decision only "
                                       "the repository owner can make (e.g. work the owner "
                                       "paused, unclear whether the branch should be public, "
                                       "`policy_notes` restrictions, or a history rewrite)?"},
    }
    raw = jev(facts, questions)
    ans = raw["answers"]
    action = ans["action"]["choice"]
    if action not in ACTIONS:
        raise ValueError("Jev returned an unknown Git action")
    p_pub = probability(ans["publishable"]["noul"])
    p_user = probability(ans["needs_user"]["noul"])

    reasons = []
    outward = action in ("push_branch", "open_pr")
    if outward and blocked:
        reasons.append("hard rule: outward action blocked (see `blocked`)")
        action = "commit" if facts["uncommitted_files"] else "none"
    elif outward and p_user >= NEEDS_USER_THRESHOLD:
        reasons.append(f"owner decision needed (p={p_user:.2f}): ask before pushing")
        action = "commit" if facts["uncommitted_files"] else "none"
    elif outward and p_pub < PUBLISH_THRESHOLD:
        reasons.append(f"not publishable yet (p={p_pub:.2f})")
        action = "commit" if facts["uncommitted_files"] else "none"
    elif action == "open_pr" and not (a.reviewed and a.checks_green):
        reasons.append("PR needs --reviewed and --checks-green; pushing the branch instead")
        action = "push_branch"
    elif action == "open_pr" and facts["existing_pr"] and facts["existing_pr"].get("state") == "OPEN":
        reasons.append("a PR is already open; just push the branch to update it")
        action = "push_branch"
    if action == "commit" and not facts["uncommitted_files"]:
        reasons.append("nothing uncommitted")
        action = "none"

    out.update({
        "action": action,
        "commands": commands_for(action, facts),
        "argv": argv_for(action, facts),
        "reason": "; ".join(reasons) or "direct from Jev",
        "raw": {"action_probs": ans["action"].get("probabilities"),
                "p_publishable": round(p_pub, 3), "p_needs_user": round(p_user, 3),
                "jev_model": raw.get("model")},
    })
    sys.stdout.buffer.write(json.dumps(out, ensure_ascii=False, indent=2).encode("utf-8") + b"\n")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        sys.exit(f"Jev decision failed: {exc}")
