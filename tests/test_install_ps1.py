"""install.ps1: parses cleanly, registers an absolute interpreter, keeps the Codex block.

The script cannot be executed off Windows (it installs a real venv, calls pip and runs a
live Jev check), so this checks what a CI run can honestly check: PowerShell's own parser
plus the registration commands in all three harness blocks.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

PS1 = Path(__file__).resolve().parents[1] / "install.ps1"
PARSE = ('$e=$null;[void][System.Management.Automation.Language.Parser]::ParseFile('
         '"{}",[ref]$null,[ref]$e); if($e){{$e | ForEach-Object {{ $_.Message }}; exit 1}}')


def test_install_ps1_parses():
    pwsh = shutil.which("pwsh") or shutil.which("powershell")
    if not pwsh:
        pytest.skip("PowerShell not installed")
    r = subprocess.run([pwsh, "-NoProfile", "-Command", PARSE.format(PS1.as_posix())],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_install_ps1_sets_up_codex():
    text = PS1.read_text(encoding="utf-8")
    assert 'install_skill.py skill/jev $codexSkillRoot --legacy jev-loop' in text, "Codex skill installation command missing"
    assert '.agents\\skills' in text, "Codex skill location missing"
    assert "codex mcp add jev-loop" in text, "Codex MCP registration missing"
    assert "claude mcp add jev-loop" in text, "Claude Code registration was lost"


def test_install_ps1_supports_shared_server_url():
    text = PS1.read_text(encoding="utf-8")
    assert "$env:JEV_MCP_URL" in text
    assert 'claude mcp add jev-loop --scope user -e "JEV_UPSTREAM=$mcpUrl" -- "$python" $server' in text
    assert 'codex mcp add jev-loop --env "JEV_UPSTREAM=$mcpUrl" -- "$python" $server' in text
    assert '$hermesEnvArgs = @("--env", "JEV_UPSTREAM=$mcpUrl")' in text
    assert "--url" not in text and "--transport http" not in text


def test_install_ps1_answers_hermes_remove_prompt():
    """`hermes mcp remove` prompts [Y/n]; an unanswered prompt hangs the installer."""
    text = PS1.read_text(encoding="utf-8")
    assert '"y`n" | hermes mcp remove jev-loop' in text


def test_install_ps1_registers_an_absolute_interpreter():
    """A bare `python` is resolved by the harness at connect time.

    Hermes resolves it to its own bundled runtime, which has neither requests nor mcp, so the
    server died on `import requests` and the harness parked it as "Connection closed". The
    registration must name the project venv interpreter explicitly.
    """
    text = PS1.read_text(encoding="utf-8")
    assert '$python = Join-Path $PSScriptRoot ".venv\\Scripts\\python.exe"' in text, "venv interpreter variable missing"
    assert "--command \"$python\"" in text, "Hermes registration does not use the venv interpreter"
    assert "-- python $server" not in text, "a harness still registers the bare `python`"
    assert "& $python -m pip install -q -r requirements.txt" in text, "dependencies are not installed into the venv"


def test_install_ps1_creates_the_venv_before_installing():
    text = PS1.read_text(encoding="utf-8")
    assert "if (-not (Test-Path $python))" in text
    assert "python -m venv $venvRoot" in text
