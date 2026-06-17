# JBTC Adhoc Pest Control Billing MVP

Automates a manual billing workflow for Jalan Besar Town Council (JBTC) adhoc pest control jobs.

```
Retrieve un-invoiced Work Orders from JBTC TCMS portal (web, no API)
        ↓
Extract fields from each WO PDF (Claude Haiku)
        ↓
Validate + duplicate-check
        ↓
Seek per-WO approval in Telegram (inline buttons)
        ↓
On approval → input into Synergix ERP (web, no API)
        ↓
Report outcome back to Telegram
```

**There is no web UI. Telegram inline buttons are the entire human interface.**
This MVP runs locally for testing before any cloud deployment.

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

There are two WO ingestion sources. DRY_RUN defaults to true — the Synergix driver does everything
EXCEPT the final submit/confirm clicks.

```bash
# Email flow — ingest Work Orders from .msg (Outlook) or .eml files (single file or a directory):
python -m src.main --emails path/to/email-dir
python -m src.main path/to/one-wo.msg          # single .msg/.eml shorthand

# TCMS scrape flow — pull un-invoiced WOs from the TCMS web portal:
python -m src.main
```

Either source feeds the same pipeline: extract fields (Claude Haiku) → validate → duplicate-check →
send each surviving WO to Telegram for approval. The bot then stays alive listening for
Approve/Reject button presses; approved WOs are written to Synergix (in DRY_RUN, the final submit is
logged only).

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
- **`SYNERGIX_TEMPLATE_QUO_ID`** — the stable template quotation to "Copy From" (set in `.env`).
- **All DOM selectors** ([config/selectors.py](config/selectors.py)).

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
