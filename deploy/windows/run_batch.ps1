<#
.SYNOPSIS
    Scheduled job: run the JBTC (TCMS) batch pipeline end to end.

.DESCRIPTION
    Intended to be triggered by Windows Task Scheduler (see register_tasks.ps1). Logs to a dated
    file under logs\, and sends a Telegram alert if the process crashes outright (per-WO failures
    already get reported via the batch summary Telegram message from inside the app).
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
$logFile = "$RepoRoot\logs\batch_$stamp.log"

Write-Host "Starting JBTC batch run, logging to $logFile"

& $venvPython -m scripts.alert_on_crash -- -m src.main --batch *>&1 | Tee-Object -FilePath $logFile

exit $LASTEXITCODE
