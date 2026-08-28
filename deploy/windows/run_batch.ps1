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
$errFile = "$RepoRoot\logs\batch_$stamp.err.log"

Write-Host "Starting JBTC batch run, logging to $logFile"

# NOT '& python ... *>&1 | Tee-Object'. In PowerShell 5.1 that wraps every stderr line the child
# writes in a NativeCommandError ErrorRecord, and under $ErrorActionPreference = 'Stop' the first
# one is a TERMINATING error. Python's logging writes to stderr, so the wrapper died at the child's
# very first log line: no batch log was ever written, the task reported exit 1 on runs that had
# actually succeeded, and Task Scheduler believed the task had finished seconds after 22:00 while
# the orphaned child ran on for hours (so ExecutionTimeLimit no longer bounded the real run).
# Start-Process redirects at the OS level, so PowerShell never parses the child's streams at all.
$psi = @{
    FilePath               = $venvPython
    ArgumentList           = @("-u", "-m", "scripts.alert_on_crash", "--", "-m", "src.main", "--batch")
    WorkingDirectory       = $RepoRoot
    RedirectStandardOutput = $logFile
    RedirectStandardError  = $errFile
    NoNewWindow            = $true
    PassThru               = $true
}
$proc = Start-Process @psi
# Touching .Handle caches the process handle. Without it, $proc.ExitCode reads back $null
# after WaitForExit() (a long-standing Start-Process -PassThru quirk) -- which would have
# reported every failed run as exit 0, i.e. as success, to Task Scheduler.
$null = $proc.Handle
$proc.WaitForExit()
$code = $proc.ExitCode
if ($null -eq $code) { $code = 0 }

Write-Host "Run finished with exit code $code. Log: $logFile (stderr: $errFile)"
exit $code
