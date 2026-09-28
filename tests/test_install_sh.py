"""install.sh: the Codex block installs the skill and registers the MCP server.

The real script runs in a sandbox HOME with stubbed harness CLIs (`codex`, `claude`,
`hermes`) and a no-op venv python, so no dependency, skill or MCP server is installed on
the machine running the tests. POSIX only - install.sh is a shell script.
"""
import shutil
import shlex
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="install.sh is a POSIX script")


def _stub(path, log):
    """A CLI that records how it was called and always succeeds."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'#!/bin/sh\necho "$0 $*" >> "{log}"\nexit 0\n', encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def sandbox(tmp_path):
    """Repo copy + fake HOME + stubbed codex/claude/hermes + no-op venv python."""
    home = tmp_path / "home"
    home.mkdir()
    shutil.copy(ROOT / "install.sh", tmp_path / "install.sh")
    shutil.copy(ROOT / "install_skill.py", tmp_path / "install_skill.py")
    shutil.copytree(ROOT / "skill", tmp_path / "skill")
    log = tmp_path / "calls.log"
    venv_python = tmp_path / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True, exist_ok=True)
    real_python = shlex.quote(sys.executable)
    venv_python.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        f'  install_skill.py) exec {real_python} "$@" ;;\n'
        '  -m) exit 0 ;;\n'
        '  jev_mcp.py) exit 0 ;;\n'
        'esac\n'
        'exit 0\n', encoding="utf-8"
    )
    venv_python.chmod(venv_python.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    for cli in ("codex", "claude", "hermes"):
        _stub(tmp_path / "bin" / cli, log)
    return tmp_path, home, log


def run(tmp_path, home, log, with_codex=True):
    binaries = tmp_path / "bin"
    path = f"{tmp_path}/.venv/bin:{binaries}:/usr/bin:/bin" if with_codex else f"{binaries}:/usr/bin:/bin"
    env = {"HOME": str(home), "PATH": path, "TYPESAFE_API_KEY": "dummy"}
    r = subprocess.run(["bash", "install.sh"], cwd=tmp_path, env=env,
                       capture_output=True, text=True)
    return r, log.read_text(encoding="utf-8") if log.exists() else ""


def test_skill_and_mcp_server_are_installed_for_codex(sandbox):
    tmp_path, home, log = sandbox
    r, calls = run(tmp_path, home, log)

    assert r.returncode == 0, r.stderr
    skill = home / ".agents" / "skills" / "jev"
    assert (skill / "SKILL.md").is_file()
    assert (skill / "agents" / "openai.yaml").is_file(), "Codex skill metadata missing"

    venv_python = f"{tmp_path}/.venv/bin/python"
    assert "codex mcp remove jev-loop" in calls
    assert f"codex mcp add jev-loop -- {venv_python} {tmp_path}/jev_mcp.py" in calls


def test_missing_codex_is_not_an_error(sandbox):
    """No codex on PATH: skip that block, still set up the other harnesses, exit 0."""
    tmp_path, home, log = sandbox
    (tmp_path / "bin" / "codex").unlink()
    r, calls = run(tmp_path, home, log, with_codex=False)

    assert r.returncode == 0, r.stderr
    assert "codex not on PATH" in r.stdout
    assert "codex " not in calls, "the Codex block must not run when codex is missing"
    assert "claude mcp add jev-loop" in calls
    assert "hermes mcp add jev-loop" in calls
    assert not (home / ".agents").exists()


def test_installer_migrates_legacy_skills_for_all_harnesses(sandbox):
    tmp_path, home, log = sandbox
    legacy_dirs = []
    for harness in (".agents", ".claude", ".hermes"):
        names = ("jev-loop", "jev-route", "jev-git") if harness == ".claude" else ("jev-loop",)
        for name in names:
            old = home / harness / "skills" / name
            old.mkdir(parents=True)
            (old / "SKILL.md").write_text(f"---\nname: {name}\n---\n", encoding="utf-8")
            legacy_dirs.append(old)
    result, _ = run(tmp_path, home, log)
    assert result.returncode == 0, result.stderr
    assert all(not old.exists() for old in legacy_dirs)
    for harness in (".agents", ".claude", ".hermes"):
        installed = home / harness / "skills" / "jev"
        for relative in ("SKILL.md", "route.py", "git_decide.py", "agents/openai.yaml"):
            assert (installed / relative).read_bytes() == (ROOT / "skill" / "jev" / relative).read_bytes()


def test_shared_server_url_registers_http_for_every_harness(sandbox):
    """JEV_MCP_URL: every harness points at the shared server; no local server, no local check."""
    tmp_path, home, log = sandbox
    url = "http://100.64.0.1:8765/mcp"
    env = {"HOME": str(home), "PATH": f"{tmp_path}/.venv/bin:{tmp_path}/bin:/usr/bin:/bin", "JEV_MCP_URL": url}
    r = subprocess.run(["bash", "install.sh"], cwd=tmp_path, env=env, capture_output=True, text=True)
    calls = log.read_text(encoding="utf-8")
    assert r.returncode == 0, r.stderr
    assert f"claude mcp add --transport http jev-loop --scope user {url}" in calls
    assert f"codex mcp add jev-loop --url {url}" in calls
    assert f"hermes mcp add jev-loop --url {url} --connect-timeout 20" in calls
    assert "jev_mcp.py" not in calls and "Skipping the local check" in r.stdout


def test_install_sh_answers_hermes_remove_prompt():
    """`hermes mcp remove` prompts [Y/n]; run from a terminal it would wait forever."""
    text = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert r"printf 'y\n' | hermes mcp remove jev-loop" in text


def test_shared_server_url_works_with_hermes_without_connect_timeout(sandbox):
    """Hermes v0.17 has no --connect-timeout: fall back to registering without it."""
    tmp_path, home, log = sandbox
    old = tmp_path / "bin" / "hermes"
    old.write_text('#!/bin/sh\necho "$0 $*" >> "' + str(log) + '"\n'
                   'case "$*" in *--connect-timeout*) exit 2 ;; esac\nexit 0\n', encoding="utf-8")
    url = "http://100.64.0.1:8765/mcp"
    env = {"HOME": str(home), "PATH": f"{tmp_path}/.venv/bin:{tmp_path}/bin:/usr/bin:/bin", "JEV_MCP_URL": url}
    r = subprocess.run(["bash", "install.sh"], cwd=tmp_path, env=env, capture_output=True, text=True)
    calls = log.read_text(encoding="utf-8").splitlines()
    assert r.returncode == 0, r.stderr
    assert any(line.endswith(f"hermes mcp add jev-loop --url {url}") for line in calls)
