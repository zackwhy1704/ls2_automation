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
$errFile = "$RepoRoot\logs\poll_$stamp.err.log"

# NOT '& python ... *>&1 | Tee-Object'. In PowerShell 5.1 that wraps every stderr line the child
# writes in a NativeCommandError ErrorRecord, and under $ErrorActionPreference = 'Stop' the first
# one is a TERMINATING error. Python's logging writes to stderr, so the wrapper died at the child's
# very first log line: no batch log was ever written, the task reported exit 1 on runs that had
# actually succeeded, and Task Scheduler believed the task had finished seconds after 22:00 while
# the orphaned child ran on for hours (so ExecutionTimeLimit no longer bounded the real run).
# Start-Process redirects at the OS level, so PowerShell never parses the child's streams at all.
$psi = @{
    FilePath               = $venvPython
    ArgumentList           = @("-u", "-m", "scripts.alert_on_crash", "--", "-m", "src.main", "--batch", "--poll")
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
