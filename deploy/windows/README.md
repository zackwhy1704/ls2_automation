# Deploying ls2_automation on the LS2 office machine (Windows)

This assumes LS2 has designated an always-on Windows PC physically in Singapore (required —
Synergix's Cloudflare blocks non-Singapore IPs, see project memory `synergix-cloudflare`).

## 0. Prerequisites on the machine

- Windows 10/11 (or Server), always-on, network access to the internet.
- Remote access set up (RDP or similar) so you can install/monitor/update remotely.
- Python 3.11+ installed and on PATH. Easiest: `winget install Python.Python.3.12`, or download
  from python.org — during install, tick "Add python.exe to PATH".
- Git installed (`winget install Git.Git`), or just copy the repo folder over some other way.

## 1. Get the code onto the machine

```powershell
git clone <repo-url> C:\ls2_automation
cd C:\ls2_automation
```

## 2. Run the install script

```powershell
cd C:\ls2_automation
powershell -ExecutionPolicy Bypass -File deploy\windows\setup.ps1
```

This creates a `.venv`, installs Python deps, installs Playwright's Chromium, and copies
`.env.example` to `.env` if one doesn't exist yet.

## 3. Fill in `.env` with real credentials

Edit `C:\ls2_automation\.env`. Checklist of what's required for a real (non-stub) run:

| Variable | What it is |
|---|---|
| `ANTHROPIC_API_KEY` | Claude API key, for WO field extraction |
| `TCMS_BASE_URL`, `TCMS_USERNAME`, `TCMS_PASSWORD` | JBTC TCMS portal service account login |
| `SYNERGIX_BASE_URL`, `SYNERGIX_USERNAME`, `SYNERGIX_PASSWORD` | Synergix ERP login |
| `SYNERGIX_TEMPLATE_QUO_ID` | Stable template quotation to "Copy From" (confirm with client) |
| `IMAP_HOST`, `IMAP_USERNAME`, `IMAP_PASSWORD` | SKTC intake mailbox (app password, not main password) |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Batch summary + crash alerts destination |
| `DRY_RUN` | **Leave `true` until you've watched several real runs.** Set `false` only when confident. |
| `HEADLESS`, `SYNERGIX_HEADLESS` | Set `true` for unattended scheduled runs |

See `.env.example` in the repo root for the full list with inline comments.

**Never commit `.env`.** It's already in `.gitignore`.

## 4. Manual smoke test before scheduling anything

```powershell
.venv\Scripts\python.exe -m src.main --batch --limit 3
```

Watch it run against 3 real WOs with `DRY_RUN=true`. Check the Telegram summary arrives, and that
`data\pdfs\` has the right PDFs. Only proceed once this looks correct.

## 5. Register the scheduled tasks

From an **elevated** (Run as Administrator) PowerShell prompt:

```powershell
cd C:\ls2_automation
powershell -ExecutionPolicy Bypass -File deploy\windows\register_tasks.ps1
```

You'll be prompted for the Windows account password (needed so tasks run even when logged out).
Defaults: JBTC batch daily at 07:00, SKTC poll every 15 minutes. Override with:

```powershell
.\register_tasks.ps1 -BatchTime "06:30" -PollIntervalMinutes 10
```

Verify both tasks appear in Task Scheduler (`taskschd.msc`) under
Task Scheduler Library → `LS2Automation-JBTC-Batch` / `LS2Automation-SKTC-Poll`.

To trigger one immediately for testing:

```powershell
Start-ScheduledTask -TaskName "LS2Automation-JBTC-Batch"
```

## 6. Monitoring

- Every run writes a timestamped log to `logs\batch_*.log` / `logs\poll_*.log`.
- Every completed batch run posts a summary to Telegram (counts by status: PROCESSED, PARTIAL,
  FAILED, DUPLICATE, etc.) — this is the primary place to spot-check outcomes day to day.
- If the run process itself crashes (not a per-WO failure, but the whole script dying — bad
  login, unhandled exception, etc.), you'll get a separate 🚨 Telegram alert from
  `scripts/alert_on_crash.py`. This should be rare; if it fires, check the matching log file.
- Session cookies persist in `.tcms_session\` and `.synergix_session\` so it doesn't log in from
  scratch every run — if either portal changes its login flow, these may need to be deleted to
  force a fresh login next run.

## 7. Going live (turning off DRY_RUN)

Only after you've watched enough scheduled runs to trust the output:

1. Set `DRY_RUN=false` in `.env`.
2. No need to re-register tasks — they read `.env` fresh each run.
3. Watch the next few runs closely via the Telegram summary and Synergix directly.

## Updating the code later

```powershell
cd C:\ls2_automation
git pull
.venv\Scripts\python.exe -m pip install -r requirements.txt   # in case deps changed
```

Scheduled tasks don't need to be re-registered for a code update — only re-run
`register_tasks.ps1` if you're changing the schedule itself.
