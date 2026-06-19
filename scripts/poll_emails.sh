#!/usr/bin/env bash
# Scheduled poll job: pull new Work Order emails from the IMAP mailbox and run them through the
# ingest -> extract -> validate -> approve pipeline. Safe to run on a cron/launchd timer — the
# Message-ID ledger makes it idempotent, so an email is never processed twice even if runs overlap.
#
# Usage:   scripts/poll_emails.sh
# Cron:    */15 * * * *  /Users/zackwhye/ls2_automation/scripts/poll_emails.sh >> /Users/zackwhye/ls2_automation/logs/poll_cron.log 2>&1
#
# Requires the .env to be configured (IMAP_* and, for live approvals, TELEGRAM_*). When Synergix is
# not yet configured the write step is stubbed; when Telegram is not configured the console gate runs.
set -euo pipefail

# Resolve the repo root from this script's location, so cron/launchd can call it by absolute path.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

# Prefer a project virtualenv if present, else fall back to the python on PATH.
if [[ -x "${REPO_ROOT}/.venv/bin/python" ]]; then
  PY="${REPO_ROOT}/.venv/bin/python"
else
  PY="$(command -v python3)"
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] poll_emails: starting (python=${PY})"
"${PY}" -m src.main --poll
echo "[$(date '+%Y-%m-%d %H:%M:%S')] poll_emails: done"
