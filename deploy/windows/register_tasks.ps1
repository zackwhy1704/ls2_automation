<#
.SYNOPSIS
    Registers the two ls2_automation jobs (JBTC batch, SKTC poll) as Windows Scheduled Tasks.

.DESCRIPTION
    Run this ONCE after setup.ps1 has completed and .env is filled in with real credentials.
    Re-running is safe — existing tasks with the same name are replaced.

    Defaults: JBTC batch runs daily at 07:00; SKTC poll runs every 15 minutes. Adjust the
    -BatchTime / -PollIntervalMinutes params to change, e.g.:
        .\register_tasks.ps1 -BatchTime "06:30" -PollIntervalMinutes 10

.NOTES
    Must be run from an ELEVATED PowerShell prompt (Run as Administrator) — registering scheduled
    tasks that run whether or not a user is logged in requires admin rights.
#>

param(
    [string]$BatchTime = "07:00",
    [int]$PollIntervalMinutes = 15,
    [string]$RunAsUser = $env:USERNAME
)

$ErrorActionPreference = "Stop"

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "This script must be run as Administrator. Right-click PowerShell -> Run as administrator, then re-run."
}

$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$pwsh = (Get-Command powershell.exe).Source

Write-Host "Registering scheduled tasks for repo at $RepoRoot, running as $RunAsUser" -ForegroundColor Cyan

$cred = Get-Credential -UserName $RunAsUser -Message "Enter the Windows password for $RunAsUser (needed so the task can run even when logged out)"

# --- JBTC batch task: daily ---
$batchAction = New-ScheduledTaskAction -Execute $pwsh `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$RepoRoot\deploy\windows\run_batch.ps1`""
$batchTrigger = New-ScheduledTaskTrigger -Daily -At $BatchTime
$batchSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopOnIdleEnd `
    -ExecutionTimeLimit (New-TimeSpan -Hours 3) -RestartCount 1 -RestartInterval (New-TimeSpan -Minutes 5)

Register-ScheduledTask -TaskName "LS2Automation-JBTC-Batch" `
    -Action $batchAction -Trigger $batchTrigger -Settings $batchSettings `
    -User $cred.UserName -Password $cred.GetNetworkCredential().Password `
    -RunLevel Highest -Force | Out-Null

Write-Host "Registered LS2Automation-JBTC-Batch (daily at $BatchTime)" -ForegroundColor Green

# --- SKTC email poll task: every N minutes ---
$pollAction = New-ScheduledTaskAction -Execute $pwsh `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$RepoRoot\deploy\windows\run_poll.ps1`""
$pollTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes $PollIntervalMinutes) -RepetitionDuration ([TimeSpan]::MaxValue)
$pollSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopOnIdleEnd `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -RestartCount 1 -RestartInterval (New-TimeSpan -Minutes 2)

Register-ScheduledTask -TaskName "LS2Automation-SKTC-Poll" `
    -Action $pollAction -Trigger $pollTrigger -Settings $pollSettings `
    -User $cred.UserName -Password $cred.GetNetworkCredential().Password `
    -RunLevel Highest -Force | Out-Null

Write-Host "Registered LS2Automation-SKTC-Poll (every $PollIntervalMinutes min)" -ForegroundColor Green

Write-Host ""
Write-Host "== Done ==" -ForegroundColor Cyan
Write-Host "Verify in Task Scheduler (taskschd.msc) under Task Scheduler Library."
Write-Host "To test a task immediately: Start-ScheduledTask -TaskName 'LS2Automation-JBTC-Batch'"
Write-Host "To remove both tasks later: Unregister-ScheduledTask -TaskName 'LS2Automation-JBTC-Batch','LS2Automation-SKTC-Poll' -Confirm:`$false"
