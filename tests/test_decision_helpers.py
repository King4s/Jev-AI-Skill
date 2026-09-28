"""Offline policy checks against real temporary Git history and mocked Jev judgments."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "skill" / "jev" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def route(monkeypatch):
    module = load("route")
    answers = {"model": {"choice": "sonnet"}, "important": {"noul": 0},
               "subagent": {"noul": 0},
               "skill": {"choice": "testing", "probabilities": {"testing": 0.9}}}
    monkeypatch.setattr(module, "jev", lambda *args: {"answers": answers})
    return module, answers


def request(**kwargs):
    return {"task": "Verify the feature", "skills": {"testing": "Write tests"},
            "main_model": "opus", **kwargs}


@pytest.mark.parametrize("selected,expected", [
    ("haiku", "sonnet"), ("sonnet", "opus"), ("opus", "fable"), ("fable", "fable")])
def test_importance_bumps_exactly_one_tier_and_caps_at_fable(route, selected, expected):
    module, answers = route
    answers["model"]["choice"] = selected
    answers["important"]["noul"] = 0.6
    assert module.decide(request())["model"] == expected


@pytest.mark.parametrize("selected", ["haiku", "sonnet", "fable"])
def test_different_model_forces_subagent_despite_jev_vote(route, selected):
    module, answers = route
    answers["model"]["choice"] = selected
    assert module.decide(request())["subagent"] is True


def test_low_probability_skill_becomes_none(route):
    module, answers = route
    answers["skill"]["probabilities"]["testing"] = 0.34
    assert module.decide(request())["skill"] == "none"


@pytest.mark.parametrize("selected,expected", [("haiku", "opus"), ("fable", "fable")])
def test_review_never_runs_below_main_tier(route, selected, expected):
    module, answers = route
    answers["model"]["choice"] = selected
    decision = module.decide(request(review=True))
    assert (decision["model"], decision["subagent"]) == (expected, True)


def git(repo, *args):
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                            text=True, encoding="utf-8", check=True)
    return result.stdout.strip()


def commit(repo, text):
    (repo / "change.txt").write_text(text, encoding="utf-8")
    git(repo, "add", "change.txt")
    git(repo, "commit", "-m", "Record change")
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repository(tmp_path, monkeypatch):
    # Isolate author identity, signing, hooks and default branch from host config.
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "empty.gitconfig"))
    for key in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(key, raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Test Author")
    git(repo, "config", "user.email", "allowed@example.invalid")
    git(repo, "remote", "add", "origin", "https://example.invalid/team/project.git")
    baseline = commit(repo, "public baseline\n")
    git(repo, "update-ref", "refs/remotes/origin/main", baseline)
    git(repo, "checkout", "-b", "feature")
    return repo


@pytest.fixture
def git_decider(tmp_path, monkeypatch):
    module = load("git_decide")
    policies = tmp_path / "policies"
    policies.mkdir()
    monkeypatch.setattr(module, "POLICIES", policies)
    monkeypatch.setattr(module, "pr_state", lambda *args: None)
    return module


def policy(module, **fields):
    (module.POLICIES / "repo.json").write_text(json.dumps({
        "remote": "https://example.invalid/team/project.git", **fields}), encoding="utf-8")


def decision(module, repo, monkeypatch, capfdbinary, action="open_pr", needs_user=0, flags=()):
    monkeypatch.setattr(module, "jev", lambda *args: {"answers": {
        "action": {"choice": action}, "publishable": {"noul": 1},
        "needs_user": {"noul": needs_user}}})
    monkeypatch.setattr(sys, "argv", ["git_decide.py", "--repo", str(repo), *flags])
    module.main()
    return json.loads(capfdbinary.readouterr().out)


@pytest.mark.parametrize("action", ["push_branch", "open_pr"])
def test_protected_branch_blocks_outward_action(repository, git_decider, monkeypatch, capfdbinary, action):
    git(repository, "checkout", "main")
    result = decision(git_decider, repository, monkeypatch, capfdbinary, action)
    assert result["action"] == "none"
    assert result["argv"] == []
    assert any("protected" in block for block in result["blocked"])


@pytest.mark.parametrize("violation", ["pattern", "identity"])
def test_private_policy_blocks_publication(repository, git_decider, monkeypatch, capfdbinary, violation):
    policy(git_decider, blocked_patterns=["PRIVATE_MARKER"],
           allowed_author_emails=["allowed@example.invalid"])
    if violation == "identity":
        git(repository, "config", "user.email", "other@example.invalid")
    commit(repository, "PRIVATE_MARKER\n" if violation == "pattern" else "public change\n")
    result = decision(git_decider, repository, monkeypatch, capfdbinary)
    assert result["action"] == "none"
    assert result["blocked"]


@pytest.mark.parametrize("flags", [(), ("--reviewed",), ("--checks-green",)])
def test_pr_requires_both_review_and_green_checks(repository, git_decider, monkeypatch, capfdbinary, flags):
    commit(repository, "ready feature\n")
    result = decision(git_decider, repository, monkeypatch, capfdbinary, flags=flags)
    assert result["action"] == "push_branch"
    assert result["argv"] == [["git", "push", "-u", "origin", "feature"]]


@pytest.mark.parametrize("needs_user,expected", [(0.49, "open_pr"), (0.5, "none"), (1, "none")])
def test_owner_decision_threshold(repository, git_decider, monkeypatch, capfdbinary, needs_user, expected):
    commit(repository, "ready feature\n")
    result = decision(git_decider, repository, monkeypatch, capfdbinary,
                      needs_user=needs_user, flags=("--reviewed", "--checks-green"))
    assert result["action"] == expected


@pytest.mark.parametrize("remote,matches", [
    ("git@example.invalid:team/project.git", True),
    ("https://example.invalid/team/project-extra.git", False),
    ("https://other.invalid/team/project.git", False),
    ("https://example.invalid/other/project.git", False)])
def test_policy_matches_exact_repository_across_transports(git_decider, remote, matches):
    policy(git_decider, notes="sentinel")
    loaded, path = git_decider.load_policy(remote)
    assert bool(path) is matches
    assert loaded.get("notes") == ("sentinel" if matches else None)


@pytest.mark.parametrize("known_origin", [True, False])
@pytest.mark.parametrize("violation", ["secret", "blocked_sha"])
def test_local_upstream_cannot_hide_history_from_origin_scan(
        repository, git_decider, monkeypatch, capfdbinary, known_origin, violation):
    """A local tracking branch at HEAD does not mean origin has these commits."""
    sha = commit(repository, "sk-" + "a" * 24 if violation == "secret" else "feature\n")
    policy(git_decider, blocked_shas=[sha] if violation == "blocked_sha" else [])
    git(repository, "branch", "local-upstream")
    git(repository, "branch", "--set-upstream-to=local-upstream", "feature")
    if not known_origin:
        git(repository, "update-ref", "-d", "refs/remotes/origin/main")
    result = decision(git_decider, repository, monkeypatch, capfdbinary,
                      action="push_branch")
    assert result["action"] == "none", "Publishing to origin must scan its outgoing history"
    assert result["argv"] == []
    expected = "secret" if violation == "secret" else sha
    assert any(expected in block for block in result["blocked"])


@pytest.mark.parametrize("multiple", [False, True])
def test_unsupported_push_destinations_block_publication(
        repository, git_decider, monkeypatch, capfdbinary, multiple):
    commit(repository, "ready feature\n")
    if multiple:
        git(repository, "config", "--add", "remote.origin.pushurl",
            "https://example.invalid/team/project.git")
    git(repository, "config", "--add", "remote.origin.pushurl",
        "https://other.invalid/team/project.git")
    result = decision(git_decider, repository, monkeypatch, capfdbinary,
                      action="push_branch")
    assert result["action"] == "none"
    assert result["argv"] == []
    assert any("destinations" in block for block in result["blocked"])


def test_known_origin_branch_defines_outgoing_range(
        repository, git_decider):
    published = commit(repository, "already published\n")
    git(repository, "update-ref", "refs/remotes/origin/feature", published)
    outgoing = commit(repository, "not published\n")
    git(repository, "branch", "local-upstream")
    git(repository, "branch", "--set-upstream-to=local-upstream", "feature")
    policy(git_decider, blocked_shas=[published, outgoing])
    facts, blocked, _ = git_decider.facts_for(str(repository), "", True, True)
    assert any(outgoing in block for block in blocked)
    assert not any(published in block for block in blocked)
    assert len(facts["outgoing_commits"]) == 1


def test_new_branch_is_scanned_from_where_it_left_origin(repository, git_decider):
    """Public history on origin must not block a new branch that origin lacks."""
    public = commit(repository, "token = 'mock-jwt-token-for-tests'\n")
    git(repository, "update-ref", "refs/remotes/origin/main", public)
    outgoing = commit(repository, "clean change\n")
    policy(git_decider, blocked_shas=[public])
    facts, blocked, _ = git_decider.facts_for(str(repository), "", True, True)
    assert blocked == [], blocked
    assert len(facts["outgoing_commits"]) == 1
    assert facts["outgoing_commits"][0].startswith(outgoing[:7])


@pytest.mark.parametrize("violation", ["secret", "pattern"])
def test_merge_only_additions_are_scanned(repository, git_decider, violation):
    git(repository, "checkout", "-b", "side")
    (repository / "side.txt").write_text("public side\n", encoding="utf-8")
    git(repository, "add", "side.txt")
    git(repository, "commit", "-m", "Public side")
    git(repository, "checkout", "feature")
    commit(repository, "public feature\n")
    git(repository, "merge", "--no-commit", "--no-ff", "side")
    marker = "sk-" + "a" * 24 if violation == "secret" else "PRIVATE_MERGE_MARKER"
    (repository / "merge-only.txt").write_text(marker, encoding="utf-8")
    git(repository, "add", "merge-only.txt")
    git(repository, "commit", "-m", "Merge side")
    policy(git_decider, blocked_patterns=["PRIVATE_MERGE_MARKER"])
    facts, blocked, _ = git_decider.facts_for(str(repository), "", True, True)
    assert any("Merge side" in item for item in facts["outgoing_commits"])
    expected = "secret" if violation == "secret" else "private pattern"
    assert any(expected in block for block in blocked)
