"""Gate tests with Jev mocked out: what reaches Jev, and what the harness gets back."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import jev_mcp as m  # noqa: E402


@pytest.fixture(autouse=True)
def tape(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(m, "GATE_TAPE", tmp_path / "runs" / "gate.jsonl")
    return tmp_path / "runs" / "gate.jsonl"


def answers(fn):
    """Install a fake Jev that builds one answer per question with `fn(key, question)`."""
    seen = []

    def jev(model, state, qs):
        seen.append((state, qs))
        return {"model": "fake", "answers": {k: fn(k, q) for k, q in qs.items()}}
    return jev, seen


def choice(c, conf=0.9, probs=None):
    return {"type": "choice", "choice": c, "confidence": conf, "probabilities": probs or {c: conf}}


# ---------- triage ----------

def test_triage_batches_events_and_skips_agent_when_nothing_needs_one(monkeypatch, tape):
    jev, seen = answers(lambda k, q: choice({"e1": "ignore", "e2": "known", "e3": "report"}[k]))
    monkeypatch.setattr(m, "jev", jev)
    out = m.gate_triage([
        {"kind": "cron", "source": "smoke", "summary": "10/10 passed", "exit_code": 0, "expected": "exit 0"},
        {"kind": "cron", "source": "freshness", "summary": "Suite Landing 404", "exit_code": 1},
        {"kind": "process", "source": "backup", "summary": "new warning: disk 91%"},
    ], known=["Suite Landing: HTTP 404"])
    state, qs = seen[0]
    assert list(qs) == ["e1", "e2", "e3"] and all(q["criteria"] == m.TRIAGE_ACTIONS for q in qs.values())
    assert state["events"]["e1"]["exit_code"] == 0 and "exit_code" not in state["events"]["e3"]
    assert state["known"] == ["Suite Landing: HTTP 404"] and state["policy"] == m.DEFAULT_POLICY
    assert [r["action"] for r in out["results"]] == ["ignore", "known", "report"]
    assert out["skip_agent"] is True and out["report"] == [3]
    rec = json.loads(tape.read_text(encoding="utf-8").splitlines()[-1])
    assert rec["tool"] == "triage" and rec["actions"] == ["ignore", "known", "report"]


def test_triage_act_wakes_agent_and_unknown_choice_is_treated_as_act(monkeypatch):
    jev, _ = answers(lambda k, q: choice("nonsense" if k == "e1" else "ignore"))
    monkeypatch.setattr(m, "jev", jev)
    out = m.gate_triage([{"summary": "x"}, {"summary": "y"}])
    assert out["results"][0]["action"] == "act" and out["skip_agent"] is False


def test_triage_clips_and_limits(monkeypatch):
    jev, seen = answers(lambda k, q: choice("ignore"))
    monkeypatch.setattr(m, "jev", jev)
    m.gate_triage([{"summary": "long\n" * 500, "expected": "e" * 900}], known=["k" * 900])
    ev = seen[0][0]["events"]["e1"]
    assert len(ev["summary"]) == m.SUMMARY_CHARS and len(ev["expected"]) == m.ITEM_CHARS
    assert len(seen[0][0]["known"][0]) == m.ITEM_CHARS and "\n" not in ev["summary"]
    with pytest.raises(ValueError):
        m.gate_triage([])
    with pytest.raises(ValueError):
        m.gate_triage([{"summary": "x"}] * (m.MAX_BATCH + 1))


# ---------- verdict ----------

def test_verdict_judges_each_item_and_keeps_open_items_in_review(monkeypatch):
    jev, seen = answers(lambda k, q: {"type": "noul", "noul": {"i1": 0.95, "i2": 0.2}[k]}
                        if k.startswith("i") else choice("none"))
    monkeypatch.setattr(m, "jev", jev)
    out = m.gate_verdict("goal", ["host is validated", "redirects refused"],
                         ["tests: 176 passed", "clippy exit 0"], changes="fix: validate host")
    state, qs = seen[0]
    assert set(qs) == {"i1", "i2", "review"} and qs["i1"]["type"] == "noul"
    assert state["items"] == {"i1": "host is validated", "i2": "redirects refused"}
    assert [i["closed"] for i in out["items"]] == [True, False] and out["open"] == ["redirects refused"]
    assert out["review"] == "narrow"  # Jev said none, but an item is still open


def test_verdict_none_when_everything_is_closed(monkeypatch):
    jev, _ = answers(lambda k, q: {"type": "noul", "noul": 0.9} if k.startswith("i") else choice("none"))
    monkeypatch.setattr(m, "jev", jev)
    out = m.gate_verdict("goal", ["a"], ["evidence"])
    assert out["review"] == "none" and out["open"] == []
    with pytest.raises(ValueError):
        m.gate_verdict("goal", [], [])


# ---------- decide ----------

def test_decide_proceeds_on_confident_choice_that_needs_no_owner(monkeypatch, tape):
    jev, seen = answers(lambda k, q: choice("keep", 0.85, {"keep": 0.85, "move": 0.15})
                        if k == "choice" else {"type": "noul", "noul": 0.1})
    monkeypatch.setattr(m, "jev", jev)
    out = m.gate_decide("Where should the data stay?", {"keep": "leave it on Thor", "move": "move it to Odin"},
                        context="disk on Odin is 90% full", recommended="keep")
    state, qs = seen[0]
    assert qs["choice"]["criteria"] == state["options"] and state["recommended"] == "keep"
    assert out["choice"] == "keep" and out["proceed"] is True and out["p_ask_owner"] == 0.1
    assert json.loads(tape.read_text(encoding="utf-8"))["proceed"] is True


def test_decide_asks_owner_when_step_needs_them_or_jev_is_unsure(monkeypatch):
    jev, _ = answers(lambda k, q: choice("move", 0.9) if k == "choice" else {"type": "noul", "noul": 0.8})
    monkeypatch.setattr(m, "jev", jev)
    out = m.gate_decide("q", {"keep": "a", "move": "b"})
    assert out["proceed"] is False and "their decision" in out["reason"]

    jev, _ = answers(lambda k, q: choice("move", 0.3) if k == "choice" else {"type": "noul", "noul": 0.1})
    monkeypatch.setattr(m, "jev", jev)
    out = m.gate_decide("q", {"keep": "a", "move": "b"}, recommended="keep")
    assert out["proceed"] is False and "not confident" in out["reason"]

    jev, _ = answers(lambda k, q: choice("elsewhere", 0.9) if k == "choice" else {"type": "noul", "noul": 0.0})
    monkeypatch.setattr(m, "jev", jev)
    assert m.gate_decide("q", {"keep": "a", "move": "b"}, recommended="keep")["choice"] == "keep"
    with pytest.raises(ValueError):
        m.gate_decide("q", {"only": "one"})


# ---------- pick ----------

def test_pick_returns_candidate_or_null(monkeypatch):
    jev, seen = answers(lambda k, q: choice("rg", 0.8, {"rg": 0.8, "grep": 0.15, "find": 0.05}))
    monkeypatch.setattr(m, "jev", jev)
    out = m.gate_pick("search literal text", {"rg": "ripgrep", "grep": "grep", "find": "find files"})
    assert out["pick"] == "rg" and out["top"][0][0] == "rg" and seen[0][1]["pick"]["criteria"]["rg"] == "ripgrep"

    jev, _ = answers(lambda k, q: choice("rg", 0.2, {"rg": 0.2}))
    monkeypatch.setattr(m, "jev", jev)
    assert m.gate_pick("t", {"rg": "a", "grep": "b"})["pick"] is None
    with pytest.raises(ValueError):
        m.gate_pick("t", {"rg": "a"})


# ---------- CLI ----------

def test_gate_cli_dispatches_and_rejects_unknown(monkeypatch):
    jev, _ = answers(lambda k, q: choice("ignore"))
    monkeypatch.setattr(m, "jev", jev)
    assert m.gate_cli("triage", {"events": [{"summary": "ok"}]})["skip_agent"] is True
    with pytest.raises(ValueError):
        m.gate_cli("nope", {})
    with pytest.raises(ValueError):
        m.gate_cli("triage", [])


def test_gate_cli_process_reports_errors_as_json(tmp_path):
    env = {"TYPESAFE_API_KEY": "", "HOME": str(tmp_path), "USERPROFILE": str(tmp_path)}
    r = subprocess.run([sys.executable, str(Path(m.__file__)), "--gate", "triage"], input='{"events": []}',
                       capture_output=True, text=True, env={**dict(__import__("os").environ), **env})
    assert r.returncode == 2 and json.loads(r.stdout)["error"].startswith("ValueError")


def test_mcp_server_exposes_gate_tools():
    pytest.importorskip("mcp")
    import asyncio
    names = {t.name for t in asyncio.run(m.build_server().list_tools())}
    assert {"gate_triage", "gate_verdict", "gate_decide", "gate_pick"} <= names
