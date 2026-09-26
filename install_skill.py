"""Install the complete skill and remove only recognized, local legacy folders."""
import argparse
import os
from pathlib import Path
import re
import shutil
import stat


def assert_local(path):
    """Reject symlinks and Windows junctions/reparse points along the entire path."""
    path = Path(os.path.abspath(path))
    for item in (path, *path.parents):
        if item.is_symlink():
            raise ValueError(f"Refusing linked installation path: {item}")
        if item.exists():
            attrs = getattr(item.lstat(), "st_file_attributes", 0)
            if attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
                raise ValueError(f"Refusing reparse point: {item}")
    return path


def check_tree(path):
    assert_local(path)
    if path.is_dir():
        for folder, dirs, files in os.walk(path, followlinks=False):
            for name in dirs + files:
                assert_local(Path(folder) / name)


def frontmatter_name(path):
    if not path.is_file():
        return None
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    try:
        end = lines.index("---", 1)
    except ValueError:
        return None
    values = []
    for line in lines[1:end]:
        match = re.fullmatch(r"name:\s*(?:([a-z0-9-]+)|\"([a-z0-9-]+)\"|'([a-z0-9-]+)')\s*", line)
        if match:
            values.append(next(value for value in match.groups() if value))
        elif re.match(r"name\s*:", line):
            return None
    return values[0] if len(values) == 1 else None


def install_skill(source, root, legacy_names=()):
    source = assert_local(source)
    root = assert_local(root)
    target = root / "jev"
    check_tree(source)
    if frontmatter_name(source / "SKILL.md") != "jev":
        raise ValueError("Source must be the jev skill")
    check_tree(target)
    if target.exists() and frontmatter_name(target / "SKILL.md") != "jev":
        raise ValueError(f"Refusing to overwrite an unrecognized skill: {target}")
    old_dirs = []
    for name in legacy_names:
        if name not in {"jev-loop", "jev-route", "jev-git"}:
            raise ValueError("Unrecognized legacy name")
        old = assert_local(root / name)
        # Validate all links before any recursive copy or removal.
        check_tree(old)
        if old.exists() and frontmatter_name(old / "SKILL.md") == name:
            if old.resolve().parent != root.resolve():
                raise ValueError("Legacy skill escaped installation root")
            old_dirs.append(old)
    root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target, dirs_exist_ok=True)
    for old in old_dirs:
        # Recheck immediately before deletion as well as during preflight.
        check_tree(old)
        if old.resolve().parent != root.resolve():
            raise ValueError("Legacy skill escaped installation root")
        shutil.rmtree(old)
    print(f"Skill installed: {target}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source")
    parser.add_argument("root")
    parser.add_argument("--legacy", nargs="*", default=[])
    args = parser.parse_args()
    install_skill(args.source, args.root, args.legacy)
