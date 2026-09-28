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
# JEV_MCP_URL names a shared, always-on server (jev_mcp.py --http) that holds the TypeSafe
# key. The server registered here still runs locally, next to the projects whose goal files
# and checks Loop needs; it only forwards its Jev calls to that server (JEV_UPSTREAM).
$mcpUrl = $env:JEV_MCP_URL
$target = if ($mcpUrl) { "$server (Jev calls via $mcpUrl)" } else { $server }
if (Get-Command claude -ErrorAction SilentlyContinue) {
    claude mcp remove jev-loop --scope user 2>$null | Out-Null
    if ($mcpUrl) { claude mcp add jev-loop --scope user -e "JEV_UPSTREAM=$mcpUrl" -- python $server }
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
    if ($mcpUrl) { codex mcp add jev-loop --env "JEV_UPSTREAM=$mcpUrl" -- python $server }
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
    if ($mcpUrl) {
        $hermesEnvArgs = @("--env", "JEV_UPSTREAM=$mcpUrl")
    } elseif ((Test-Path $hermesEnvFile) -and (Select-String -Path $hermesEnvFile -Pattern '^TYPESAFE_API_KEY=.' -Quiet)) {
        $hermesEnvArgs = @("--env", 'TYPESAFE_API_KEY=${TYPESAFE_API_KEY}')
    }
    # `hermes mcp remove` asks "Remove server? [Y/n]" and waits on a console stdin.
    "y`n" | hermes mcp remove jev-loop 2>$null | Out-Null
    "y`n" | hermes mcp add jev-loop --command python @hermesEnvArgs --args $server
    Write-Host "MCP server registered: jev-loop -> $target (start a new Hermes session)"
} else {
    Write-Host "hermes not on PATH - skipping Hermes setup."
}

$keyFile = Join-Path $HOME ".config\jev-loop\typesafe_api_key"
if ($mcpUrl) {
    $env:JEV_UPSTREAM = $mcpUrl
    python $server --check
} elseif (-not [Environment]::GetEnvironmentVariable("TYPESAFE_API_KEY", "User") -and -not $env:TYPESAFE_API_KEY -and -not (Test-Path $keyFile)) {
    Write-Warning "No TypeSafe key. Set TYPESAFE_API_KEY (user env) or write it to $keyFile, then restart Claude Code."
} else {
    python $server --check
}
Write-Host "Done. Restart your harness (Claude Code / Codex), then say: 'byg med jev: <what you want>' or invoke the jev skill (/jev, or `$jev in Codex)."
