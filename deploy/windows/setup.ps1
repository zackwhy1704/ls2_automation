<#
.SYNOPSIS
    One-time setup for ls2_automation on a Windows machine at LS2's office.

.DESCRIPTION
    Creates a virtualenv, installs Python + Playwright dependencies, and installs the Chromium
    browser Playwright drives. Does NOT touch .env (secrets) or register scheduled tasks — run
    register_tasks.ps1 separately after .env is filled in.

.NOTES
    Requires Python 3.11+ already installed and on PATH (winget install Python.Python.3.12).
    Run from an elevated or normal PowerShell prompt — no admin rights needed for this step.
#>

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $RepoRoot

Write-Host "== ls2_automation setup ==" -ForegroundColor Cyan
Write-Host "Repo root: $RepoRoot"

$pythonCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCmd) {
    throw "python not found on PATH. Install Python 3.11+ first (https://www.python.org/downloads/ or 'winget install Python.Python.3.12'), then re-run this script."
}

$pyVersion = & python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
Write-Host "Found Python $pyVersion"
if ([version]$pyVersion -lt [version]"3.11") {
    throw "Python 3.11+ required, found $pyVersion. Install a newer Python and re-run."
}

if (-not (Test-Path "$RepoRoot\.venv")) {
    Write-Host "Creating virtualenv..." -ForegroundColor Cyan
    python -m venv .venv
} else {
    Write-Host "Virtualenv already exists, skipping creation."
}

$venvPython = "$RepoRoot\.venv\Scripts\python.exe"

Write-Host "Installing Python dependencies..." -ForegroundColor Cyan
& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install -r requirements.txt

Write-Host "Installing Playwright's Chromium browser..." -ForegroundColor Cyan
& $venvPython -m playwright install chromium
& $venvPython -m playwright install-deps chromium

if (-not (Test-Path "$RepoRoot\.env")) {
    Write-Host "No .env found — copying .env.example to .env." -ForegroundColor Yellow
    Copy-Item "$RepoRoot\.env.example" "$RepoRoot\.env"
    Write-Host "IMPORTANT: edit .env now and fill in real credentials before running anything." -ForegroundColor Yellow
} else {
    Write-Host ".env already exists — leaving it as-is."
}

Write-Host ""
Write-Host "== Setup complete ==" -ForegroundColor Green
Write-Host "Next steps:"
Write-Host "  1. Edit .env with real credentials (see deploy/windows/README.md for the checklist)."
Write-Host "  2. Run a manual dry-run test:  .venv\Scripts\python.exe -m src.main --batch --limit 3"
Write-Host "  3. Once that looks right, register the scheduled tasks:  deploy\windows\register_tasks.ps1"
