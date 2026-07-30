# Thin wrapper around scripts\apply_win_compat.py - see that file for what this
# does and why. Kept ASCII-only and CRLF-terminated on purpose: Windows
# PowerShell 5.1 reads a BOM-less script as ANSI (mangling non-ASCII) and
# mis-parses here-strings in LF-only files, so all real logic and all Chinese
# text live in the Python script instead.
#
# Usage:
#   .\repair_miloco_win.ps1
#   .\repair_miloco_win.ps1 -VenvDir "D:\path\to\miloco_runtime\venv"

param(
    [string]$VenvDir = (Join-Path $env:USERPROFILE ".miot_cache\miloco_runtime\venv")
)

$ErrorActionPreference = "Stop"

$py = Join-Path $VenvDir "Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Error "python.exe not found: $py  (Miloco not installed yet?)"
}

& $py (Join-Path $PSScriptRoot "apply_win_compat.py") --venv $VenvDir
exit $LASTEXITCODE
