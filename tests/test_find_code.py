"""Jev's ranking preserves every search lead for the coding model."""

import importlib.util
from pathlib import Path
import shutil

import pytest


@pytest.fixture
def finder(monkeypatch):
    skill = Path(__file__).resolve().parents[1] / "skill" / "jev"
    monkeypatch.syspath_prepend(str(skill))
    spec = importlib.util.spec_from_file_location("find_code", skill / "find_code.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep is not installed")
def test_semantic_scan_includes_files_without_literal_overlap(finder, monkeypatch, tmp_path):
    (tmp_path / "tree.py").write_text("def harvest():\n    take_wood()\n", encoding="utf-8")
    (tmp_path / "other.py").write_text("def idle():\n    pass\n", encoding="utf-8")
    def fake_jev(state, questions, retries):
        assert len(state["leads"]) == 2
        assert len(questions) == 2
        return {"model": "jev-test", "answers": {
            f"l{i}": {"type": "noul", "noul": 0.9 if lead["path"] == "tree.py" else 0.01}
            for i, lead in enumerate(state["leads"])
        }}
    monkeypatch.setattr(finder, "jev", fake_jev)
    result = finder.run(tmp_path, "Make the bot collect wood")
    assert result["mode"] == "all_text_chunks"
    assert result["lead_count"] == 2
    assert [lead["path"] for lead in result["leads"]] == ["tree.py", "other.py"]
    assert result["leads"][1]["relevance"] == 0.01  # Low scores are retained.


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep is not installed")
def test_literal_search_keeps_every_matching_line(finder, monkeypatch, tmp_path):
    (tmp_path / "a.py").write_text("old_api()\nold_api()\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("old_api()\n", encoding="utf-8")
    monkeypatch.setattr(finder, "jev", lambda state, questions, retries: {
        "answers": {name: {"type": "noul", "noul": 0.5} for name in questions}})
    result = finder.run(tmp_path, "Replace broken calls", "old_api")
    assert result["mode"] == "all_literal_matches"
    assert [(lead["path"], lead["start_line"]) for lead in result["leads"]] == [
        ("a.py", 1), ("a.py", 2), ("b.py", 1)]
    assert result["lead_count"] == 3


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep is not installed")
def test_provider_failure_preserves_every_unranked_lead(finder, monkeypatch, tmp_path):
    (tmp_path / "a.py").write_text("alpha()\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("beta()\n", encoding="utf-8")
    def unavailable(*args, **kwargs):
        raise RuntimeError("service unavailable")
    monkeypatch.setattr(finder, "jev", unavailable)
    result = finder.run(tmp_path, "Repair behavior")
    assert result["ranking_used"] is False
    assert "service unavailable" in result["error"]
    assert {lead["path"] for lead in result["leads"]} == {"a.py", "b.py"}


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep is not installed")
def test_broad_scope_limit_fails_instead_of_silent_truncation(finder, tmp_path):
    (tmp_path / "a.py").write_text("one\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("two\n", encoding="utf-8")
    with pytest.raises(ValueError, match="narrow --repo"):
        finder.run(tmp_path, "Find change", max_entries=1)


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep is not installed")
def test_long_lines_and_existing_report_are_not_lost_or_rescanned(finder, monkeypatch, tmp_path):
    source = "x" * (finder.MAX_SNIPPET_CHARS + 200)
    (tmp_path / "source.py").write_text(source + "\n", encoding="utf-8")
    report = tmp_path / "report.json"
    report.write_text("previous report", encoding="utf-8")
    monkeypatch.setattr(finder, "jev", lambda state, questions, retries: {
        "answers": {name: {"type": "noul", "noul": 0.5} for name in questions}})
    result = finder.run(tmp_path, "Find relevant code", exclude=report)
    assert result["lead_count"] == 2
    assert {lead["path"] for lead in result["leads"]} == {"source.py"}
    assert result["clipped_leads"] == 0
    assert "".join(lead["snippet"].split(": ", 1)[1] for lead in result["leads"]) == source
