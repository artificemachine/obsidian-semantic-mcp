#!/usr/bin/env pwsh
# Obsidian Semantic MCP - one-liner installer for Windows
#
# Usage:
#   powershell -c "irm https://raw.githubusercontent.com/celstnblacc/obsidian-semantic-mcp/main/install.ps1 | iex"
#
# Or with init flags passed through:
#   powershell -c "iex (irm https://raw.githubusercontent.com/celstnblacc/obsidian-semantic-mcp/main/install.ps1)" -- --mode 2 --vault C:\path\to\vault

$ErrorActionPreference = "Stop"

$RepoUrl = "https://github.com/celstnblacc/obsidian-semantic-mcp.git"
$DataHome = if ([string]::IsNullOrWhiteSpace($env:XDG_DATA_HOME)) {
    Join-Path $HOME ".local\share"
} else {
    $env:XDG_DATA_HOME
}
$InstallDir = Join-Path $DataHome "obsidian-semantic-mcp"
$BinDir = Join-Path $HOME ".local\bin"
$CmdPath = Join-Path $BinDir "osm.cmd"
$LauncherPath = Join-Path $InstallDir "scripts\osm.ps1"

function Write-Ok {
    param([string]$Message)
    Write-Host "  ✓  $Message"
}

function Write-Info {
    param([string]$Message)
    Write-Host "  →  $Message"
}

function Write-Warn {
    param([string]$Message)
    Write-Host "  ⚠  $Message"
}

function Write-Fail {
    param([string]$Message)
    Write-Host "  ✗  $Message"
}

Write-Host "────────────────────────────────────────────────────────────"
Write-Host ""
Write-Host "  Obsidian Semantic MCP - Installer"
Write-Host ""

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Fail "git not found - install Git for Windows and re-run"
    exit 1
}
Write-Ok "git found"

if (Test-Path (Join-Path $InstallDir ".git")) {
    Write-Info "Updating existing install at $InstallDir..."
    git -C $InstallDir pull --ff-only
    Write-Ok "Up to date"
} elseif (Test-Path $InstallDir) {
    $backup = "$InstallDir.backup-$(Get-Date -Format yyyyMMddHHmmss)"
    Write-Warn "Existing non-git install directory found"
    Write-Info "Moving it to $backup"
    Move-Item -Force $InstallDir $backup
    Write-Info "Cloning to $InstallDir..."
    git clone --depth=1 $RepoUrl $InstallDir
    Write-Ok "Cloned"
} else {
    Write-Info "Cloning to $InstallDir..."
    git clone --depth=1 $RepoUrl $InstallDir
    Write-Ok "Cloned"
}

New-Item -ItemType Directory -Force -Path $BinDir | Out-Null

@"
@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "$LauncherPath" %*
"@ | Set-Content -Encoding ASCII $CmdPath

Write-Ok "Linked: $CmdPath"

if ($env:PATH -notlike "*$BinDir*") {
    Write-Warn "$BinDir is not in your PATH."
    Write-Info "Add this to your PowerShell profile:"
    Write-Host ""
    Write-Host "    `$env:PATH = `"$BinDir;`$env:PATH`""
    Write-Host ""
}

Write-Host "────────────────────────────────────────────────────────────"
Write-Host ""

& $LauncherPath @args
