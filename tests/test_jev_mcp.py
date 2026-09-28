"""Protocol tests for jev_mcp.Run with Jev mocked out (no network, no API key)."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import jev_mcp as m  # noqa: E402

CHECK = f"\"{sys.executable}\" -c \"import os,sys; sys.exit(0 if os.path.exists('ok.txt') else 1)\""


@pytest.fixture
def goal(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "RUNS", tmp_path / "runs")
    g = tmp_path / "goal.json"
    g.write_text(json.dumps({
        "goal": "make ok.txt", "workdir": "ws", "checks": [CHECK],
        "roles": {"build": {"when": "a", "brief": "b"}, "fix": {"when": "c", "brief": "d"}},
        "max_consecutive_failures": 2,
    }), encoding="utf-8")
    return g


def fake_jev(route="build", recovery="retry", p_done=None, seen=None, review_now=0.1):
    def jev(model, state, qs):
        if seen is not None:
            seen.append((state, qs))
        done = p_done if p_done is not None else (0.9 if state["all_checks_pass"] else 0.1)
        a = {"route": {"type": "choice", "choice": route, "confidence": 0.8,
                       "probabilities": {"build": 0.7, "fix": 0.3}},
             "done": {"type": "noul", "noul": done}}
        if "review_now" in qs:
            a["review_now"] = {"type": "noul", "noul": review_now}
        if "recovery" in qs:
            a["recovery"] = {"type": "choice", "choice": recovery, "probabilities": {}}
        return {"model": "fake", "answers": a}
    return jev


def test_full_run_to_goal_met(goal, monkeypatch):
    monkeypatch.setattr(m, "jev", fake_jev())
    run = m.Run.create(goal)
    assert run.s["checks_ok"] is False

    d = m.Run(run.id).decide()
    assert (d["next"], d["role"]) == ("execute", "build")
    Path(run.cfg["workdir"], "ok.txt").write_text("x")
    assert m.Run(run.id).record_turn("made it", ["ok.txt"], True)["checks_ok"] is True

    assert m.Run(run.id).decide()["next"] == "review"
    d = m.Run(run.id).record_review(False, ["needs more"])
    assert d["next"] == "execute" and d["reviewer_missing"] == ["needs more"]
    m.Run(run.id).record_turn("did more", [], True)

    assert m.Run(run.id).decide()["next"] == "review"
    stop = m.Run(run.id).record_review(True, [])
    assert stop["reason"] == "goal_met"
    with pytest.raises(RuntimeError, match="already stopped"):
        m.Run(run.id).decide()


def test_route_wins_over_retry(goal, monkeypatch):
    run = m.Run.create(goal)
    monkeypatch.setattr(m, "jev", fake_jev(route="build"))
    m.Run(run.id).decide()
    m.Run(run.id).record_turn("nothing", [], True)  # check fails -> fails=1, last_role=build
    monkeypatch.setattr(m, "jev", fake_jev(route="fix", recovery="retry"))
    assert m.Run(run.id).decide()["role"] == "fix"


def test_reroute_moves_away_from_last_role(goal, monkeypatch):
    monkeypatch.setattr(m, "jev", fake_jev(route="build", recovery="reroute"))
    run = m.Run.create(goal)
    m.Run(run.id).decide()
    m.Run(run.id).record_turn("nothing", [], True)
    assert m.Run(run.id).decide()["role"] == "fix"


def test_escalate_and_max_failures(goal, monkeypatch):
    monkeypatch.setattr(m, "jev", fake_jev(recovery="escalate"))
    run = m.Run.create(goal)
    m.Run(run.id).decide()
    m.Run(run.id).record_turn("nothing", [], True)
    assert m.Run(run.id).decide()["reason"] == "escalate"

    monkeypatch.setattr(m, "jev", fake_jev(recovery="retry"))
    run = m.Run.create(goal)
    for _ in range(2):
        m.Run(run.id).decide()
        m.Run(run.id).record_turn("nothing", [], False)

    def boom(*a):
        raise AssertionError("Jev must not be called after max failures")
    monkeypatch.setattr(m, "jev", boom)
    assert m.Run(run.id).decide()["reason"] == "escalate"


def exists_check(name):
    return f"\"{sys.executable}\" -c \"import os,sys; sys.exit(0 if os.path.exists('{name}') else 1)\""


@pytest.fixture
def two_checks(goal):
    """A goal with an early check (a.txt) and a late 'outcome' check (b.txt)."""
    g = json.loads(goal.read_text())
    g.update(checks=[exists_check("a.txt"), exists_check("b.txt")], max_consecutive_failures=3)
    goal.write_text(json.dumps(g))
    return goal


def turn(run_id, files=(), ok=True):
    m.Run(run_id).decide()
    return m.Run(run_id).record_turn("t", list(files), ok)


def test_progress_with_a_late_check_still_red_is_not_a_failure(two_checks, monkeypatch):
    monkeypatch.setattr(m, "jev", fake_jev(recovery="retry"))
    run = m.Run.create(two_checks)
    ws = Path(run.cfg["workdir"])
    r = turn(run.id)                      # stall: nothing changed, no files
    assert (r["turn_failed"], r["consecutive_fails"]) == (True, 1)
    (ws / "a.txt").write_text("x")
    r = turn(run.id, ["a.txt"])           # early check turned green, late check still red
    assert r["checks_ok"] is False
    assert (r["turn_failed"], r["consecutive_fails"]) == (False, 0)
    for _ in range(4):                    # more work that keeps a green, b not reachable yet
        r = turn(run.id, ["src.py"])
        assert (r["turn_failed"], r["consecutive_fails"]) == (False, 0)
    assert m.Run(run.id).decide()["next"] == "execute"  # no escalation


def test_regression_counts_as_failure(two_checks, monkeypatch):
    monkeypatch.setattr(m, "jev", fake_jev(recovery="retry"))
    run = m.Run.create(two_checks)
    ws = Path(run.cfg["workdir"])
    (ws / "a.txt").write_text("x")
    assert turn(run.id, ["a.txt"])["consecutive_fails"] == 0
    (ws / "a.txt").unlink()
    r = turn(run.id, ["a.txt"])           # files written, but a passing check broke
    assert (r["turn_failed"], r["consecutive_fails"]) == (True, 1)


def test_executor_not_ok_counts_even_when_checks_pass(two_checks, monkeypatch):
    monkeypatch.setattr(m, "jev", fake_jev(recovery="retry"))
    run = m.Run.create(two_checks)
    ws = Path(run.cfg["workdir"])
    (ws / "a.txt").write_text("x")
    (ws / "b.txt").write_text("x")
    r = turn(run.id, ["a.txt", "b.txt"], ok=False)
    assert (r["checks_ok"], r["turn_failed"], r["consecutive_fails"]) == (False, True, 1)
    r = turn(run.id, [], ok=True)
    assert (r["checks_ok"], r["turn_failed"], r["consecutive_fails"]) == (True, False, 0)


def test_turn_failed_unit():
    red = {"cmd": "c", "ok": False, "out": "3 failed"}
    assert m.turn_failed([red], [red], True, []) is True                        # stall
    assert m.turn_failed([red], [red], True, ["x.py"]) is False                 # work done
    assert m.turn_failed([red], [dict(red, out="1 failed")], True, []) is False  # moved
    assert m.turn_failed([dict(red, ok=True)], [red], True, ["x.py"]) is True   # regression
    assert m.turn_failed([red], [dict(red, ok=True)], False, []) is True         # executor failed


def test_jev_sees_files_and_review_progress(goal, monkeypatch):
    seen = []
    monkeypatch.setattr(m, "jev", fake_jev(seen=seen))
    run = m.Run.create(goal)
    Path(run.cfg["workdir"], "ok.txt").write_text("x")
    Path(run.cfg["workdir"], "__pycache__").mkdir()
    Path(run.cfg["workdir"], "__pycache__", "x.pyc").write_text("x")
    m.Run(run.id).decide()
    m.Run(run.id).record_turn("t1", ["ok.txt"], True)
    m.Run(run.id).decide()
    m.Run(run.id).record_review(False, ["add docs"])
    m.Run(run.id).record_turn("added docs", ["README.md"], True)
    m.Run(run.id).decide()

    state = seen[-1][0]
    assert state["project_files"] == ["ok.txt"]
    assert state["last_review"]["missing"] == ["add docs"]
    assert [t["notes"] for t in state["last_review"]["turns_since_review"]] == ["added docs"]


def green_run(goal, **cfg):
    g = json.loads(goal.read_text())
    g.update(cfg)
    goal.write_text(json.dumps(g))
    run = m.Run.create(goal)
    Path(run.cfg["workdir"], "ok.txt").write_text("x")
    return run


def test_stalled_green_turns_ask_jev_about_review(goal, monkeypatch):
    seen = []
    monkeypatch.setattr(m, "jev", fake_jev(p_done=0.75, review_now=0.9, seen=seen))
    run = green_run(goal)
    assert turn(run.id, ["ok.txt"])["checks_ok"] is True   # made the check green: progress
    turn(run.id, ["sync.log"])                              # idle 1 (a rewritten log is no change)
    assert "review_now" not in seen[-1][1] and "stalled" not in seen[-1][0]
    turn(run.id, ["sync.log"])                              # idle 2 -> stalled
    d = m.Run(run.id).decide()
    state, qs = seen[-1]
    assert "review_now" in qs
    assert state["stalled"]["turns_without_change"] == 2 and state["stalled"]["role"] == "build"
    assert (d["next"], d["why"]) == ("review", "stalled")
    tape = [json.loads(x) for x in Path(run.tape_path).read_text(encoding="utf-8").splitlines()]
    assert tape[-1]["kind"] == "decide" and tape[-1]["p_review_now"] == 0.9

    # The reviewer, not code, decides; a rejection resets the stall so it has to build up again.
    m.Run(run.id).record_review(False, ["x"])
    m.Run(run.id).record_turn("t", [], True)
    m.Run(run.id).decide()
    assert "review_now" not in seen[-1][1]


def test_stall_leaves_the_decision_to_jev(goal, monkeypatch):
    monkeypatch.setattr(m, "jev", fake_jev(p_done=0.75, review_now=0.2))
    run = green_run(goal)
    for _ in range(4):
        r = turn(run.id, ["ok.txt"])
        assert r["turn_failed"] is False                   # a stall is not a failure
    assert m.Run(run.id).decide()["next"] == "execute"      # Jev said keep working


def test_stall_needs_same_role_and_unchanged_checks(goal, monkeypatch):
    seen = []
    run = green_run(goal)
    monkeypatch.setattr(m, "jev", fake_jev(p_done=0.5, seen=seen))
    turn(run.id)
    monkeypatch.setattr(m, "jev", fake_jev(route="fix", p_done=0.5, seen=seen))
    turn(run.id)                                           # role switched: not idle
    monkeypatch.setattr(m, "jev", fake_jev(p_done=0.5, seen=seen))
    turn(run.id)
    m.Run(run.id).decide()
    assert "stalled" not in seen[-1][0], "a role switch is not a stall"
    assert m.Run(run.id).s["idle"] == 0

    red = {"cmd": "c", "ok": True, "out": "5 passed in 0.31s"}
    assert m.checks_signature([red]) == m.checks_signature([dict(red, out="5 passed in 1.02s")])
    assert m.checks_signature([red]) != m.checks_signature([dict(red, out="6 passed in 0.31s")])


def test_wrong_step_is_rejected(goal, monkeypatch):
    monkeypatch.setattr(m, "jev", fake_jev())
    run = m.Run.create(goal)
    with pytest.raises(RuntimeError, match="wrong step"):
        m.Run(run.id).record_turn("x", [], True)
    m.Run(run.id).decide()
    with pytest.raises(RuntimeError, match="wrong step"):
        m.Run(run.id).decide()


def test_max_turns(goal, monkeypatch):
    monkeypatch.setattr(m, "jev", fake_jev(recovery="retry"))
    g = json.loads(goal.read_text())
    g.update(max_turns=1, max_consecutive_failures=99)
    goal.write_text(json.dumps(g))
    run = m.Run.create(goal)
    m.Run(run.id).decide()
    m.Run(run.id).record_turn("x", [], True)
    assert m.Run(run.id).decide()["reason"] == "max_turns"


def test_mcp_server_exposes_tools():
    import asyncio
    names = {t.name for t in asyncio.run(m.build_server().list_tools())}
    assert names == {"loop_start", "loop_decide", "loop_record_review", "loop_record_turn", "loop_status",
                     "gate_triage", "gate_verdict", "gate_decide", "gate_pick"}


def test_api_key_env_then_file(tmp_path, monkeypatch):
    kf = tmp_path / "typesafe_api_key"
    monkeypatch.setattr(m, "KEY_FILE", kf)
    monkeypatch.setenv("TYPESAFE_API_KEY", "from-env")
    assert m.api_key() == "from-env"
    monkeypatch.delenv("TYPESAFE_API_KEY")
    with pytest.raises(RuntimeError, match="No TypeSafe API key"):
        m.api_key()
    kf.write_text("from-file\n")
    assert m.api_key() == "from-file"

def test_a_review_round_can_be_revisited(goal, monkeypatch):
    """A review round has to be reachable again without a stall.

    The executor alternates roles while working through what a reviewer found
    missing, so the same-role stall never fires; without a second path the run can
    never be reviewed again, however much it fixed.
    """
    run = m.Run.create(goal)
    Path(run.cfg["workdir"], "ok.txt").write_text("x", encoding="utf-8")
    roles = ["build", "fix", "build", "fix", "build"]

    def jev(model, state, qs):
        # A high p_done sends the run to its first review. Once a verdict is recorded the
        # run is known to be unfinished, so after that only the fact that the round has
        # had executor turns since can take it back there.
        done = 0.9 if state["last_review"] is None and state["all_checks_pass"] else 0.1
        route = roles.pop(0) if roles else "build"
        return fake_jev(route=route, p_done=done, review_now=0.9)(model, state, qs)

    monkeypatch.setattr(m, "jev", jev)

    assert m.Run(run.id).decide()["next"] == "execute"
    m.Run(run.id).record_turn("made it", ["ok.txt"], True)  # checks turn green
    assert m.Run(run.id).decide()["next"] == "review"
    assert m.Run(run.id).record_review(False, ["handle a missing cover"])["next"] == "execute"

    for n in range(3):  # three turns on the finding, alternating roles
        m.Run(run.id).record_turn(f"worked on it {n}", [], True)
        assert m.Run(run.id).s["idle"] == 0, "this must be the reviewed-before path, not a stall"
        if n < 2:
            assert m.Run(run.id).decide()["next"] == "execute"

    # Jev is asked the reviewer question again and sends the run back to review.
    assert m.Run(run.id).decide()["next"] == "review"


def test_an_unreviewed_run_of_green_turns_is_offered_to_the_reviewer(goal, monkeypatch):
    """Real work never stalls (each turn changes the check output) and Jev's p_done can sit
    below the threshold for the whole run, so without this path a run that keeps building
    is never reviewed - it ends by max_turns or escalate (runs 20260925-200554,
    20260925-232600, 20260926-024217). After `review_turns` green turns with no review yet,
    Jev is told `unreviewed` and asked `review_now`; it still decides."""
    run = m.Run.create(goal)
    Path(run.cfg["workdir"], "ok.txt").write_text("x", encoding="utf-8")
    roles = ["build", "fix", "build", "fix"]
    seen = []

    def jev(model, state, qs):
        route = roles.pop(0) if roles else "build"
        # p_done never reaches jev_done_threshold; review_now says yes when asked.
        return fake_jev(route=route, p_done=0.6, review_now=0.9, seen=seen)(model, state, qs)

    monkeypatch.setattr(m, "jev", jev)
    for n in range(3):
        assert m.Run(run.id).decide()["next"] == "execute"
        assert "review_now" not in seen[-1][1] and "unreviewed" not in seen[-1][0]
        m.Run(run.id).record_turn(f"real work {n}", [f"f{n}.txt"], True)
        assert m.Run(run.id).s["idle"] == 0, "roles alternate, so this is not a stall"

    d = m.Run(run.id).decide()
    state, qs = seen[-1]
    assert state["unreviewed"]["green_turns"] == 3 and state["last_review"] is None
    assert "review_now" in qs and "reviewed_before" not in state
    assert (d["next"], d["why"]) == ("review", "unreviewed")

    # A rejected review switches the run to the reviewed-before path, not back to unreviewed.
    assert m.Run(run.id).record_review(False, ["one more thing"])["next"] == "execute"
    for n in range(3):
        m.Run(run.id).record_turn(f"on the finding {n}", [], True)
        if n < 2:
            assert m.Run(run.id).decide()["next"] == "execute"
    d = m.Run(run.id).decide()
    assert "reviewed_before" in seen[-1][0] and "unreviewed" not in seen[-1][0]
    assert (d["next"], d["why"]) == ("review", "revisit")


def test_unreviewed_path_still_leaves_the_decision_to_jev(goal, monkeypatch):
    run = m.Run.create(goal)
    Path(run.cfg["workdir"], "ok.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(m, "jev", fake_jev(route="build", p_done=0.6, review_now=0.2))
    for n in range(4):
        d = m.Run(run.id).decide()
        assert d["next"] == "execute", "review_now below jev_review_threshold keeps executing"
        m.Run(run.id).record_turn(f"work {n}", [f"g{n}.txt"], True)
