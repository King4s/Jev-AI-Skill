# One-time / update setup for Claude Code, Codex and Hermes on Windows: installs
# dependencies, copies the skill and registers the MCP server at user scope. Safe to
# run again after updates.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

python -m pip install -q -r requirements.txt

$skillRoot = Join-Path $HOME ".claude\skills"
python install_skill.py skill/jev $skillRoot --legacy jev-loop jev-route jev-git
if ($LASTEXITCODE -ne 0) { throw "Claude Code skill installation failed." }

$server = Join-Path $PSScriptRoot "jev_mcp.py"
# JEV_MCP_URL points every harness at one shared, always-on server (python jev_mcp.py --http)
# instead of starting a local copy; the key then lives only on that server.
$mcpUrl = $env:JEV_MCP_URL
$target = if ($mcpUrl) { $mcpUrl } else { $server }
if (Get-Command claude -ErrorAction SilentlyContinue) {
    claude mcp remove jev-loop --scope user 2>$null | Out-Null
    if ($mcpUrl) { claude mcp add --transport http jev-loop --scope user $mcpUrl }
    else { claude mcp add jev-loop --scope user -- python $server }
    Write-Host "MCP server registered: jev-loop -> $target (Claude Code)"
} else {
    Write-Host "claude not on PATH - skipping Claude Code MCP setup."
}

# Codex: skills live in ~/.agents/skills, the server in ~/.codex/config.toml.
if (Get-Command codex -ErrorAction SilentlyContinue) {
    $codexSkillRoot = Join-Path $HOME ".agents\skills"
    python install_skill.py skill/jev $codexSkillRoot --legacy jev-loop
    if ($LASTEXITCODE -ne 0) { throw "Codex skill installation failed." }

    codex mcp remove jev-loop 2>$null | Out-Null
    if ($mcpUrl) { codex mcp add jev-loop --url $mcpUrl }
    else { codex mcp add jev-loop -- python $server }
    Write-Host "MCP server registered: jev-loop -> $target (Codex)"
} else {
    Write-Host "codex not on PATH - skipping Codex setup."
}

# Hermes: same server, same loop; the tools show up as mcp_jev_loop_*.
if (Get-Command hermes -ErrorAction SilentlyContinue) {
    $hermesHome = if ($env:HERMES_HOME) { $env:HERMES_HOME } else { Join-Path $HOME ".hermes" }
    $hermesSkillRoot = Join-Path $hermesHome "skills"
    python install_skill.py skill/jev $hermesSkillRoot --legacy jev-loop
    if ($LASTEXITCODE -ne 0) { throw "Hermes skill installation failed." }

    # A Hermes stdio MCP subprocess gets a filtered environment, so the key is handed over in
    # the server's env block - but ONLY when Hermes can resolve it: an unresolved
    # ${TYPESAFE_API_KEY} would reach Jev as the key itself instead of falling back to the key
    # file. Without the env block the server reads the key file on its own.
    $hermesEnvArgs = @()
    $hermesEnvFile = Join-Path $hermesHome ".env"
    if ((Test-Path $hermesEnvFile) -and (Select-String -Path $hermesEnvFile -Pattern '^TYPESAFE_API_KEY=.' -Quiet)) {
        $hermesEnvArgs = @("--env", 'TYPESAFE_API_KEY=${TYPESAFE_API_KEY}')
    }
    hermes mcp remove jev-loop 2>$null | Out-Null
    if ($mcpUrl) { "y`n" | hermes mcp add jev-loop --url $mcpUrl }
    else { "y`n" | hermes mcp add jev-loop --command python @hermesEnvArgs --args $server }
    Write-Host "MCP server registered: jev-loop -> $target (start a new Hermes session)"
} else {
    Write-Host "hermes not on PATH - skipping Hermes setup."
}

$keyFile = Join-Path $HOME ".config\jev-loop\typesafe_api_key"
if ($mcpUrl) {
    Write-Host "Using the shared server at $mcpUrl; its host holds the TypeSafe key. Skipping the local check."
} elseif (-not [Environment]::GetEnvironmentVariable("TYPESAFE_API_KEY", "User") -and -not $env:TYPESAFE_API_KEY -and -not (Test-Path $keyFile)) {
    Write-Warning "No TypeSafe key. Set TYPESAFE_API_KEY (user env) or write it to $keyFile, then restart Claude Code."
} else {
    python $server --check
}
Write-Host "Done. Restart your harness (Claude Code / Codex), then say: 'byg med jev: <what you want>' or invoke the jev skill (/jev, or `$jev in Codex)."
