"""Exercise shared installer migration and filesystem safety on every platform."""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("install_skill", ROOT / "install_skill.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def skill(path, name):
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text(f"---\nname: {name}\n---\nInstructions\n", encoding="utf-8")
    (path / "keep.txt").write_text("valuable contents", encoding="utf-8")


@pytest.mark.parametrize("harness,legacy", [
    (".agents", ("jev-loop",)), (".hermes", ("jev-loop",)),
    (".claude", ("jev-loop", "jev-route", "jev-git"))])
def test_complete_install_migrates_recognized_legacy_and_is_idempotent(tmp_path, harness, legacy):
    root = tmp_path / harness / "skills"
    for name in legacy:
        skill(root / name, name)
    source = ROOT / "skill" / "jev"
    installer.install_skill(source, root, legacy)
    installer.install_skill(source, root, legacy)
    for source_file in source.rglob("*"):
        if source_file.is_file():
            assert (root / "jev" / source_file.relative_to(source)).read_bytes() == source_file.read_bytes()
    assert all(not (root / name).exists() for name in legacy)


@pytest.mark.parametrize("frontmatter", [
    "---\nname: personal-skill\n---\n",
    "Documentation\nname: jev-loop\n",
    "---\nname: jev-loop\nname: personal-skill\n---\n"])
def test_unrecognized_legacy_directory_is_preserved(tmp_path, frontmatter):
    root = tmp_path / "skills"
    old = root / "jev-loop"
    skill(old, "jev-loop")
    (old / "SKILL.md").write_text(frontmatter, encoding="utf-8")
    installer.install_skill(ROOT / "skill" / "jev", root, ("jev-loop",))
    assert (old / "SKILL.md").read_text(encoding="utf-8") == frontmatter
    assert (old / "keep.txt").read_text(encoding="utf-8") == "valuable contents"


def test_linked_legacy_directory_cannot_delete_external_contents(tmp_path):
    outside = tmp_path / "outside"
    skill(outside, "jev-loop")
    root = tmp_path / "skills"
    root.mkdir()
    link = root / "jev-loop"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Directory symlink creation is not permitted on this host")
    with pytest.raises(ValueError):
        installer.install_skill(ROOT / "skill" / "jev", root, ("jev-loop",))
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "valuable contents"
    assert not (root / "jev").exists(), "Reject unsafe paths before copying anything"
