<#
.SYNOPSIS
    Scheduled job: poll the SKTC intake mailbox and run any new Work Order emails through the batch
    pipeline.

.DESCRIPTION
    Intended to be triggered by Windows Task Scheduler (see register_tasks.ps1), on a shorter
    interval than run_batch.ps1 (email arrival is not on a daily schedule). Idempotent — the
    Message-ID ledger means an email already processed is never re-processed even if runs overlap.
#>

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $RepoRoot

$venvPython = "$RepoRoot\.venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    throw "Virtualenv not found at $venvPython. Run deploy\windows\setup.ps1 first."
}

if (-not (Test-Path "$RepoRoot\logs")) {
    New-Item -ItemType Directory -Path "$RepoRoot\logs" | Out-Null
}
$stamp = Get-Date -Format "yyyy-MM-dd_HHmmss"
$logFile = "$RepoRoot\logs\poll_$stamp.log"

& $venvPython -m scripts.alert_on_crash -- -m src.main --batch --poll *>&1 | Tee-Object -FilePath $logFile

exit $LASTEXITCODE
