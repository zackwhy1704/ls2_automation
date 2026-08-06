# LS2 Billing Automation

Automates the manual billing workflow for two town councils' pest control jobs: JBTC/Jalan Besar
(scraped from the TCMS web portal) and SKTC/Sengkang (ingested via email).

```
Retrieve un-invoiced Work Orders (JBTC: TCMS portal scrape | SKTC: email poll)
        ↓
Extract fields from each WO PDF (Claude Haiku)
        ↓
Validate + duplicate-check
        ↓
Every valid, non-duplicate WO is auto-submitted to Synergix ERP (gated by DRY_RUN)
        ↓
Report every WO's outcome to Telegram (for spot-checking, not approval)
```

**There is no web UI and no per-WO approval gate.** The recommended path (`--batch`) processes
every un-invoiced WO automatically and reports outcomes to Telegram afterward for the team to
spot-check in Synergix. An older Telegram inline-button approval flow also exists in the code
(`python -m src.main` without `--batch`) but `--batch` is what actually runs in production.

For deploying this to run unattended on a Windows machine, see
[deploy/windows/README.md](deploy/windows/README.md). SKTC email intake normally uses IMAP, but if
that's blocked (as it currently is on LS2's tenant), see
[docs/power_automate_sktc_setup.md](docs/power_automate_sktc_setup.md) for the folder-based alternative.

---

## ⚠️ This code does NOT run end-to-end out of the box — by design

Every DOM selector for the TCMS and Synergix portals is a placeholder
(`"TODO_SELECTOR"`) in [config/selectors.py](config/selectors.py). The author of this MVP has
never seen those portals, so inventing selectors would produce code that *looks* finished but runs
on guesses. **You must fill the selectors** (see below) before the scrape/write steps will work.

Until then, the orchestrator runs cleanly, reaches a portal step, logs a clear
`MISSING SELECTOR: <name>` line, marks the affected WO `FAILED`, and continues — it does **not**
crash with a stack trace.

---

## Setup

Requires **Python 3.11+**.

```bash
cd jbtc-billing-mvp
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

cp .env.example .env
# edit .env and fill in real values
```

## Running

DRY_RUN defaults to true — the Synergix driver does everything EXCEPT the final submit/confirm
clicks, so it's safe to run repeatedly while testing.

```bash
# RECOMMENDED — batch pipeline, no approval gate: scrape TCMS -> extract -> validate -> dedup ->
# auto-submit every valid non-duplicate WO -> report to Telegram
python -m src.main --batch
python -m src.main --batch --limit 20             # cap to the first 20 WOs (sampling)
python -m src.main --batch --poll                 # pull new SKTC emails first, then batch-process
python -m src.main --batch --emails path/to/dir   # batch-process a dir/file of .msg/.eml/.pdf

# Older flow: per-WO Telegram approval gate (Approve/Reject buttons), no auto-submit.
# Kept in the code but --batch is what runs in production.
python -m src.main
python -m src.main --emails path/to/email-dir
```

Either source feeds the same extraction/validation/dedup pipeline. In `--batch` mode every WO's
outcome (PROCESSED, PARTIAL, FAILED, DUPLICATE, etc.) is reported to Telegram afterward for the
team to spot-check in Synergix — there's no approve/reject step blocking the run.

### Email ingestion details

`src/email_ingestor.py` parses each `.msg`/`.eml` and, based on the real SKTC samples:

- extracts **each PDF attachment as one Work Order** — a single email can carry several WOs
  (e.g. "AN Division - 3 Work Orders" → 3 PDFs → 3 WOs), each processed independently;
- filters out inline signature images (`image001.png`, …) so they are never treated as WOs;
- keeps the email body text as a fallback when an email has no PDF attachment;
- saves a copy of the original email so Synergix stage D always has a file to attach.

The extractor sends the WO **PDF** to Haiku as a document (preferred), or the **body text** when
there is no PDF. You can test ingestion + extraction in isolation, no Synergix needed:

```bash
python -m src.email_ingestor path/to/wo.msg     # show what was parsed out (no LLM, no API key)
python -m src.extractor data/pdfs/000060068.pdf  # run Haiku extraction on a WO PDF (needs ANTHROPIC_API_KEY)
```

To go live (only after explicit approval): set `DRY_RUN=false` in `.env`.

## Running the unit tests

The pure-logic modules (`models`, `validator` incl. remarks builder, and the `.eml` ingestor) have no
external dependencies and are unit-testable on their own:

```bash
pytest tests/ -v
```

## Filling in the selectors (`playwright codegen`)

The fastest way to discover real selectors is Playwright's recorder:

```bash
# TCMS
playwright codegen "$TCMS_BASE_URL"
# Synergix
playwright codegen "$SYNERGIX_BASE_URL"
```

Click through the workflow in the opened browser; codegen prints selectors as you go. Copy each
into [config/selectors.py](config/selectors.py), replacing the matching `"TODO_SELECTOR"` constant.
Each constant has a comment describing exactly what element it targets.

**Notes for the JBTC TCMS portal (Dynamics 365):** D365 re-renders late and lazily. The scraper
uses `wait_for_load_state("networkidle")` and explicit `wait_for_selector` — never fixed sleeps.
Prefer stable selectors (`data-id`, ARIA roles, label text) over auto-generated CSS paths.

## What you must confirm with the client / by inspecting real WO samples

Search the codebase for `TODO(human)` — every real-world unknown is marked there. The key ones:

- **Project codes + prefix rule** ([src/validator.py](src/validator.py)): letter-prefix → `2000069`
  (Infigo), digit-prefix → `2000050` (Ecocare). Confirm against real WO samples.
- **Remarks template** ([src/validator.py](src/validator.py)): confirm exact wording with the client.
- **MFA / IP whitelist** for the TCMS service account ([src/tcms_scraper.py](src/tcms_scraper.py)).
- **Synergix duplicate-search screen** and that `WO-PO` is the correct search key
  ([src/synergix_driver.py](src/synergix_driver.py)).
- **SKTC's real Synergix project code** — currently defaults to "Pest control" (`2000073`); confirm
  whether any SKTC WOs should map elsewhere ([src/synergix_driver.py](src/synergix_driver.py)).
- **All DOM selectors** ([config/selectors.py](config/selectors.py)).

Before relying on this for live billing, also read
[docs/operational_expectations.md](docs/operational_expectations.md) — what breaks, how you'll find
out, and the known current gaps (as of writing: SKTC has no Synergix template yet, TCMS MFA is
unconfirmed, multi-line-item WOs aren't fully modeled).

## Production notes (out of scope for MVP, documented in code)

- Decouple into (1) a scheduled scrape+notify job and (2) a separate approval-executor triggered by
  the Telegram callback, both sharing the SQLite state — so nothing holds a browser session open for
  hours. See the comment block in [src/main.py](src/main.py).
- Replace local `.env` with a real secret manager.
- The schedule-board update (Synergix stage C) is the most fragile step — implemented best-effort.

## Architecture

| Module | Responsibility |
|---|---|
| [config/settings.py](config/settings.py) | Load `.env`, constants (TZ, DRY_RUN, model, paths) |
| [config/selectors.py](config/selectors.py) | ALL DOM selectors as `TODO_SELECTOR` placeholders |
| [src/models.py](src/models.py) | `WOPayload`, `WOStatus`, project-code mapping |
| [src/db.py](src/db.py) | SQLite schema + CRUD for WO state (resumable, auditable) |
| [src/email_ingestor.py](src/email_ingestor.py) | Parse `.msg`/`.eml`: one WO per PDF attachment + body fallback |
| [src/tcms_scraper.py](src/tcms_scraper.py) | Playwright: login, list un-invoiced WOs, download PDFs |
| [src/extractor.py](src/extractor.py) | Claude Haiku: PDF *or* email text → structured JSON + confidence |
| [src/validator.py](src/validator.py) | Validation rules + project-code resolution + remarks builder |
| [src/synergix_driver.py](src/synergix_driver.py) | Playwright: dedup check + create/fulfil (DRY_RUN aware) |
| [src/telegram_gate.py](src/telegram_gate.py) | Bot, inline approve/reject buttons, callback → execute |
| [src/notifier.py](src/notifier.py) | Batch summary + per-WO result messages |
| [src/main.py](src/main.py) | Orchestrator / entry point |
