# osm.ps1 — Obsidian Semantic MCP CLI wrapper (Windows)
#
# Usage:
#   .\scripts\osm.ps1 init       Interactive setup wizard
#   .\scripts\osm.ps1 status     Check service health
#   .\scripts\osm.ps1 rebuild    Rebuild Docker images
#
# If uv is available, uses the project venv automatically.
# Otherwise falls back to system python3.

$ErrorActionPreference = "Stop"

$ScriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
$Wizard      = Join-Path $ProjectRoot "osm_init.py"

if (Get-Command uv -ErrorAction SilentlyContinue) {
    & uv run --project $ProjectRoot python3 $Wizard @args
} else {
    & python3 $Wizard @args
}

exit $LASTEXITCODE
