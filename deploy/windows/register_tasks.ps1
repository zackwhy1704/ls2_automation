<#
.SYNOPSIS
    Registers the ls2_automation scheduled task(s): JBTC batch, and optionally the SKTC poll.

.DESCRIPTION
    Run this ONCE after setup.ps1 has completed and .env is filled in with real credentials.
    Re-running is safe — existing tasks with the same name are replaced.

    DEFAULT MODE (recommended): registers a task that runs as the CURRENT USER, only while that
    user is logged on. This needs NO administrator rights and NO stored Windows password.

    That is not merely convenient, it is what the pipeline requires. The TCMS scraper runs
    NON-HEADLESS (HEADLESS=false in .env — D365/Entra was not reliable headless), so it needs a real
    interactive desktop to open a browser window on. A task registered to run "whether or not the
    user is logged on" executes in a session with no desktop, where a non-headless Chromium cannot
    display. Confirmed live 2026-08-21: TCMS logs "browser launched (headless=False)".

    So the host must stay powered on AND logged in. The trade-off is reboot resilience: after a
    restart (e.g. Windows Update) the task will not run until someone logs in again. Since a task
    that never fires cannot send a crash alert, SILENCE is the failure mode — treat "no Telegram
    report by the morning" as a signal to check the host. Enabling auto-logon for the account, or
    adding a startup trigger, removes that gap if wanted.

    -RunWhenLoggedOff switches to the old behaviour (stored password, highest privileges, runs
    logged off). It requires an ELEVATED prompt and will prompt for the account password. Only use
    it if the pipeline is made fully headless first, otherwise TCMS is expected to fail.

.EXAMPLE
    .\register_tasks.ps1 -BatchTime "22:00"
    Nightly JBTC batch at 10pm as the logged-on user. No admin needed.

.EXAMPLE
    .\register_tasks.ps1 -BatchTime "22:00" -IncludePoll -PollIntervalMinutes 15
    Also register the SKTC email poll. See the warning on -IncludePoll before using it.

.NOTES
    To inspect:  Get-ScheduledTask -TaskName "LS2Automation-*"
    To test now: Start-ScheduledTask -TaskName "LS2Automation-JBTC-Batch"
    To remove:   Unregister-ScheduledTask -TaskName "LS2Automation-JBTC-Batch" -Confirm:$false
#>

param(
    [string]$BatchTime = "22:00",
    [int]$PollIntervalMinutes = 15,
    # Also register the SKTC email-poll task. OFF by default: as of 2026-08-21 the SKTC flow is far
    # less exercised than JBTC and its Synergix project code (2000073 "Pest control" vs 2000130
    # "Mosquito") is still an unconfirmed assumption, so a poll running every 15 minutes could
    # produce wrongly-coded quotations unattended. Confirm the code mapping with the client first.
    [switch]$IncludePoll,
    # Legacy mode: run whether or not the user is logged on. Needs admin + the account password, and
    # is expected to BREAK the non-headless TCMS scraper — see the description.
    [switch]$RunWhenLoggedOff,
    [string]$RunAsUser = $env:USERNAME
)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$pwsh = (Get-Command powershell.exe).Source

if (-not (Test-Path "$RepoRoot\.venv\Scripts\python.exe")) {
    throw "Virtualenv not found at $RepoRoot\.venv\Scripts\python.exe. Run deploy\windows\setup.ps1 first."
}

Write-Host "Repo:  $RepoRoot" -ForegroundColor Cyan
Write-Host "Mode:  $(if ($RunWhenLoggedOff) { 'run whether or not logged on (needs admin)' } else { 'run as logged-on user (no admin needed)' })" -ForegroundColor Cyan

if ($RunWhenLoggedOff) {
    if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "-RunWhenLoggedOff requires an elevated prompt. Right-click PowerShell -> Run as administrator, or drop the switch to use the recommended logged-on mode."
    }
    Write-Warning "TCMS runs non-headless (HEADLESS=false), which needs an interactive desktop. In this mode it is expected to fail. Make the pipeline headless first."
    $cred = Get-Credential -UserName $RunAsUser -Message "Windows password for $RunAsUser (so the task can run while logged out)"
} else {
    # Interactive token, current user, no password stored, no elevation required.
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
        -LogonType Interactive -RunLevel Limited
}

function Register-LS2Task {
    param($Name, $ScriptFile, $Trigger, $Settings, $Description)
    $action = New-ScheduledTaskAction -Execute $pwsh `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$RepoRoot\deploy\windows\$ScriptFile`""
    if ($RunWhenLoggedOff) {
        Register-ScheduledTask -TaskName $Name -Action $action -Trigger $Trigger -Settings $Settings `
            -User $cred.UserName -Password $cred.GetNetworkCredential().Password `
            -RunLevel Highest -Description $Description -Force | Out-Null
    } else {
        Register-ScheduledTask -TaskName $Name -Action $action -Trigger $Trigger -Settings $Settings `
            -Principal $principal -Description $Description -Force | Out-Null
    }
}

# --- JBTC batch task: daily ---
# ExecutionTimeLimit 6h, raised from 3h on 2026-08-21. Measured full-list runs over ~306 WOs took
# 2h20m (live) and 3h22m (DRY_RUN), so a 3h cap would have killed the longer one mid-flight. A Task
# Scheduler kill is worse than the in-app per-WO timeout: nothing gets to clean up, so a half-created
# quotation can be left behind to mask its WO from every later dedup check. Runtime scales with how
# many WOs are genuinely NEW (each create+submit is minutes, each duplicate ~30s), so this needs
# headroom as the backlog grows rather than a limit tuned to today's mostly-duplicate list.
Register-LS2Task -Name "LS2Automation-JBTC-Batch" -ScriptFile "run_batch.ps1" `
    -Trigger (New-ScheduledTaskTrigger -Daily -At $BatchTime) `
    -Settings (New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopOnIdleEnd `
        -ExecutionTimeLimit (New-TimeSpan -Hours 6) -RestartCount 1 -RestartInterval (New-TimeSpan -Minutes 5)) `
    -Description "LS2 JBTC billing batch: scrape TCMS un-invoiced WOs, create Service Quotations in Synergix. Gated by DRY_RUN in .env."

Write-Host "Registered LS2Automation-JBTC-Batch (daily at $BatchTime)" -ForegroundColor Green

if ($IncludePoll) {
    Register-LS2Task -Name "LS2Automation-SKTC-Poll" -ScriptFile "run_poll.ps1" `
        -Trigger (New-ScheduledTaskTrigger -Once -At (Get-Date) `
            -RepetitionInterval (New-TimeSpan -Minutes $PollIntervalMinutes) -RepetitionDuration ([TimeSpan]::MaxValue)) `
        -Settings (New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopOnIdleEnd `
            -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -RestartCount 1 -RestartInterval (New-TimeSpan -Minutes 2)) `
        -Description "LS2 SKTC email intake poll."
    Write-Host "Registered LS2Automation-SKTC-Poll (every $PollIntervalMinutes min)" -ForegroundColor Green
} else {
    Write-Host "Skipped the SKTC poll task (pass -IncludePoll to register it)." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "== Done ==" -ForegroundColor Cyan
Get-ScheduledTask -TaskName "LS2Automation-*" | Select-Object TaskName, State | Format-Table -AutoSize
Write-Host "DRY_RUN in .env decides whether runs SUBMIT or only create drafts. Current value:"
Select-String -Path "$RepoRoot\.env" -Pattern "^DRY_RUN=" | ForEach-Object { "  " + $_.Line }
Write-Host ""
Write-Host "This mode needs the host powered on AND logged in. A task that never fires sends no"
Write-Host "crash alert, so treat a missing morning Telegram report as a signal to check the host."
