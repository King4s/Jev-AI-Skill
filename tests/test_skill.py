"""Codex silently skips a skill whose frontmatter breaks its limits - catch that in CI.

Codex reads only `name` (<= 100 chars) and `description` (<= 500 chars) from SKILL.md, and
both must be single-line. Limits documented at https://developers.openai.com/codex/skills.
"""
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parents[1] / "skill" / "jev" / "SKILL.md"


@pytest.fixture(scope="module")
def frontmatter():
    text = SKILL.read_text(encoding="utf-8")
    assert text.startswith("---\n"), "SKILL.md must start with YAML frontmatter"
    block = text.split("---", 2)[1]
    fields = {}
    for line in block.strip().splitlines():
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields


def test_frontmatter_is_within_codex_limits(frontmatter):
    assert frontmatter["name"] == "jev"
    assert len(frontmatter["name"]) <= 100
    assert len(frontmatter["description"]) <= 500, "over 500 chars: Codex skips the skill"
    assert frontmatter["description"]


def test_description_names_every_supported_harness(frontmatter):
    for harness in ("Claude Code", "Codex", "Hermes"):
        assert harness in frontmatter["description"], f"{harness} missing from the description"
