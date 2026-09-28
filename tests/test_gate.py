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


def test_http_mode_serves_stateless_streamable_http(monkeypatch):
    calls = {}

    class Fake:
        def run(self, transport, **kw):
            calls["transport"], calls["kw"] = transport, kw
    monkeypatch.setattr(m, "build_server", lambda: Fake())
    m.serve_http("100.64.0.1", 8765)
    assert calls == {"transport": "streamable-http",
                     "kw": {"host": "100.64.0.1", "port": 8765, "stateless_http": True}}


# ---------- upstream forwarding ----------

def test_upstream_url_accepts_base_or_mcp_url(monkeypatch):
    monkeypatch.delenv("JEV_MCP_URL", raising=False)
    monkeypatch.delenv("JEV_UPSTREAM", raising=False)
    assert m.upstream_url() is None
    monkeypatch.setenv("JEV_UPSTREAM", "http://100.64.0.1:8765/mcp")
    assert m.upstream_url() == "http://100.64.0.1:8765/jev"
    monkeypatch.setenv("JEV_UPSTREAM", "http://100.64.0.1:8765/")
    assert m.upstream_url() == "http://100.64.0.1:8765/jev"


def test_jev_forwards_to_upstream_without_a_local_key(monkeypatch):
    monkeypatch.setenv("JEV_UPSTREAM", "http://100.64.0.1:8765/mcp")
    monkeypatch.setattr(m, "api_key", lambda: pytest.fail("no local key when forwarding"))
    sent = {}

    class R:
        status_code = 200
        def json(self):
            return {"model": "jev", "answers": {"ok": {"noul": 0.9}}}

    def post(url, timeout, json):
        sent["url"], sent["body"] = url, json
        return R()
    monkeypatch.setattr(m.requests, "post", post)
    out = m.jev("jev-latest", {"s": 1}, {"ok": {"type": "noul"}})
    assert out["answers"]["ok"]["noul"] == 0.9
    assert sent == {"url": "http://100.64.0.1:8765/jev",
                    "body": {"model": "jev-latest", "state": {"s": 1}, "questions": {"ok": {"type": "noul"}}}}


def test_jev_upstream_error_is_a_failed_decision(monkeypatch):
    monkeypatch.setenv("JEV_UPSTREAM", "http://100.64.0.1:8765")

    class R:
        status_code = 502
        text = "boom"
    monkeypatch.setattr(m.requests, "post", lambda *a, **k: R())
    with pytest.raises(RuntimeError, match="upstream HTTP 502"):
        m.jev("jev-latest", {}, {"q": {}})


def test_jev_endpoint_answers_validates_and_refuses_to_chain(monkeypatch):
    import asyncio
    monkeypatch.delenv("JEV_UPSTREAM", raising=False)
    monkeypatch.delenv("JEV_MCP_URL", raising=False)
    monkeypatch.setattr(m, "jev", lambda model, state, qs: {"model": model, "answers": {"q": state}})
    ok = asyncio.run(m.jev_forward({"model": "jev-latest", "state": "s", "questions": {"q": {}}}))
    assert ok == (200, {"model": "jev-latest", "answers": {"q": "s"}})
    assert asyncio.run(m.jev_forward({"state": "s"}))[0] == 400
    assert asyncio.run(m.jev_forward(None))[0] == 400

    def boom(*a):
        raise RuntimeError("no key")
    monkeypatch.setattr(m, "jev", boom)
    assert asyncio.run(m.jev_forward({"state": "s", "questions": {}}))[0] == 502
    monkeypatch.setenv("JEV_UPSTREAM", "http://elsewhere:8765")
    assert asyncio.run(m.jev_forward({"state": "s", "questions": {}}))[0] == 508


def test_http_app_serves_the_jev_route():
    pytest.importorskip("mcp")
    app = m.build_server().streamable_http_app(stateless_http=True)
    assert "/jev" in {getattr(r, "path", None) for r in app.routes}


# ---------- watch ----------

def _cmd(code, text):
    # UTF-8 explicitly: a Windows console encoding cannot print the emoji real checks use.
    return [sys.executable, "-c",
            f"import sys; sys.stdout.reconfigure(encoding='utf-8'); print({text!r}); sys.exit({code})"]


def test_watch_is_silent_on_success_and_clears_known(tmp_path, monkeypatch):
    known = tmp_path / "known.txt"
    known.write_text("old problem\n", encoding="utf-8")
    monkeypatch.setattr(m, "gate_triage", lambda *a, **k: pytest.fail("Jev must not be asked on exit 0"))
    assert m.watch("smoke", _cmd(0, "10/10 passed"), known) == ("", 0)
    assert not known.exists()


def test_watch_reports_new_problem_once(tmp_path, monkeypatch):
    known = tmp_path / "known.txt"
    asked = []

    def triage(events, known=()):
        asked.append(events[0]["summary"])
        return {"results": [{"action": "report"}]}
    monkeypatch.setattr(m, "gate_triage", triage)
    run1 = "Report 2026-09-28 03:03 UTC\n  ok Email: HTTP 200\n  🔴 Suite Landing: HTTP 404\ndone in 1.2s"
    run2 = "Report 2026-09-28 04:03 UTC\n  ok Email: HTTP 200\n  🔴 Suite Landing: HTTP 404\ndone in 3.4s"
    text, code = m.watch("freshness", _cmd(1, run1), known)
    assert code == 0 and text == "freshness: exit 1. 🔴 Suite Landing: HTTP 404"
    assert m.watch("freshness", _cmd(1, run2), known) == ("", 0), "same problem, new clock time"
    assert asked == ["🔴 Suite Landing: HTTP 404"], "an identical repeat must not reach Jev"
    assert m.watch("freshness", _cmd(1, "🔴 Auth: HTTP 500"), known)[0].endswith("Auth: HTTP 500")


def test_watch_remembers_what_jev_calls_known(tmp_path, monkeypatch):
    known = tmp_path / "known.txt"
    monkeypatch.setattr(m, "gate_triage", lambda events, known=(): {"results": [{"action": "known"}]})
    assert m.watch("x", _cmd(1, "ERROR: same as before, reworded"), known) == ("", 0)
    assert "reworded" in known.read_text(encoding="utf-8")


def test_issue_summary_prefers_problem_lines_and_drops_clock_times():
    out = "start 2026-09-28T02:35:31.668521+00:00\nA ok\nB FAILED at 12:01:02\nC ok\n"
    assert m._issue_summary(out) == "B FAILED at #"
    assert m._issue_summary("line1\nline2") == "line1 | line2"


def test_watch_reports_when_jev_fails(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no key")
    monkeypatch.setattr(m, "gate_triage", boom)
    text, code = m.watch("x", _cmd(2, "MONITOR BROKEN"), tmp_path / "k.txt")
    assert code == 0 and "MONITOR BROKEN" in text and "Jev unavailable" in text
    assert m.watch("x", _cmd(2, "MONITOR BROKEN"), tmp_path / "k.txt") == ("", 0), "reported once"


def test_unwritable_gate_tape_does_not_break_a_decision(tmp_path, monkeypatch):
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(m, "GATE_TAPE", blocker / "gate.jsonl")  # parent is a file
    jev, _ = answers(lambda k, q: choice("ignore"))
    monkeypatch.setattr(m, "jev", jev)
    assert m.gate_triage([{"summary": "ok"}])["skip_agent"] is True


def test_watch_known_list_is_bounded(tmp_path, monkeypatch):
    known = tmp_path / "known.txt"
    known.write_text("\n".join(f"p{i}" for i in range(m.KNOWN_MAX)) + "\n", encoding="utf-8")
    monkeypatch.setattr(m, "gate_triage", lambda events, known=(): {"results": [{"action": "act"}]})
    m.watch("x", _cmd(1, "new"), known)
    lines = known.read_text(encoding="utf-8").splitlines()
    assert len(lines) == m.KNOWN_MAX and lines[-1] == "x: exit 1: new" and lines[0] == "p1"
