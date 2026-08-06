# Context brief for a new session picking up this project

Read this first if you're a fresh Claude Code (or human) session with no memory of how this repo
got here. It's the "why," not the "what" — the README and code comments cover the what. This covers
what's actually proven working, what's fragile, and what's still an open question, as of the last
working session.

## What's real and verified, not just written

- **JBTC (Jalan Besar)**: full pipeline proven live, end to end, repeatedly — TCMS scrape → PDF
  download → Claude extraction → validation → dedup → Synergix quotation draft creation (DRY_RUN).
  Multiple real WOs tested this way.
- **SKTC (Sengkang)**: same, proven live via the email-ingestion path instead of TCMS scraping.
  Synergix quotation creation for SKTC was the harder problem (see below) but is now working too.
- **Money arithmetic**: the discount-application direction is DIFFERENT between the two councils —
  JBTC's pre-GST base is `gross + discount`, SKTC's is `gross - discount` (the intuitive one). This
  was a real, live bug caught by running the extraction accuracy harness against real samples before
  going live — see `synergix-money-formula` project history and `src/models.py`'s `is_jbtc()`. If you
  ever "simplify" the two councils to share one formula, you will silently underbill JBTC.
- **The Synergix write step no longer uses "Copy From".** It used to copy an existing quotation as a
  template, but that only works if a quotation happens to be sitting in "New" status for that
  customer — which was true for JBTC (by accident, from repeated testing) and never true for SKTC,
  blocking every Sengkang WO. `_stage_b_create_quotation` in `src/synergix_driver.py` now builds every
  quotation from a blank draft instead, for both councils, via the same code path.

## What's fragile / known gaps (as of the last session)

- **Item Code auto-fill in the Synergix line item** works when tested in isolation but fails in the
  real pipeline (after Customer/Salesperson/Project Site are already selected) — root cause not fully
  traced, suspected AJAX re-render of the Details grid. Fails SAFELY: the draft is still created with
  everything else correct, just missing the item code, with a clear log warning. A human needs to
  finish that one field before Submit. Worth investigating further, not currently a blocker.
- **SKTC's Synergix project code is a reasonable assumption, not confirmed.** Sengkang's real Project
  Site options are `2000073` ("Pest control") and `2000130` ("Mosquito") — a totally different scheme
  from JBTC's Ecocare/Infigo split. Every SKTC sample seen so far is generic pest control, so the code
  defaults to `2000073`. Get client confirmation before trusting this for anything beyond generic pest
  control WOs. See `TODO(human)` in `src/synergix_driver.py`.
- **TCMS MFA is assumed bypassable** via a "use your password instead" fallback link. If JBTC's
  tenant ever enforces Conditional Access MFA with no bypass, this breaks with a clear, distinct error
  (`_detect_mfa_dead_end`), not a silent hang — but it needs a client-side fix (tenant exemption or a
  dedicated non-MFA service account), not a code fix.
- **Multi-line-item WOs** (a single WO with several distinct billable line items) are not fully
  modeled — only the first line's figures get captured. Doesn't cause a wrong bill (the money
  cross-check flags it and routes to NEEDS_REVIEW), but needs a human to fill in the rest.
- **SKTC IMAP is blocked** — Basic Auth (plain username/password) for IMAP is disabled tenant-wide on
  LS2's Microsoft 365 tenant (Microsoft's default posture since Oct 2022), and there's no admin
  available to enable app passwords or set up OAuth2. Confirmed by a live login test, not assumed. As
  a bridge, `SKTC_INTAKE_MODE=folder` (`src/sktc_folder_intake.py`) reads WO PDFs from a Power
  Automate-populated, OneDrive-synced folder instead — see
  `docs/power_automate_sktc_setup.md`. `SKTC_INTAKE_MODE=imap` remains fully functional and is a
  one-line flip back if IMAP access is ever sorted out.

## Hard-won debugging lessons (so you don't repeat these)

- **Playwright's `get_by_text(x, exact=False)` matches substrings** — searching for "Customer" also
  matches "Customer Type", and `.first` then picks non-deterministically. Cost hours of confusing,
  intermittent failures. If you need "optionally has a trailing ` *`" behavior for required-field
  labels, use a precise regex, not `exact=False`.
- **A visible UI label can be wrong while the underlying value is correct.** A dropdown showing "New
  Customer" was purely a rendering artifact; the real `<select>` already had "Existing Customer"
  selected the whole time. When something looks wrong, dump the actual DOM/form value before trusting
  what's rendered on screen.
- **Synergix's PrimeFaces autocomplete panels are easy to mis-target.** The Details grid itself is a
  `.ui-datatable` and stays visible the whole time, so a naive "last visible `.ui-datatable`" selector
  can match the wrong element. Exclude elements with `<th>` headers when hunting for a transient
  dropdown panel, and prefer stable, semantically-named classes (e.g. `add-row-button`) over
  icon-shape guessing whenever you can find one in the real DOM.
- **Test drafts in Synergix cost something real** — the driver has no delete capability, and every
  live test run leaves an orphaned `QUO...` record. Batch discovery into as few live-Synergix rounds
  as possible; don't spin up a new draft per diagnostic question if you can avoid it.

## Where to look for more detail

- `docs/operational_expectations.md` — client-facing summary of what breaks and how you'll find out.
- `docs/synergix_workflow.md` — the authoritative manual workflow this automates.
- `docs/power_automate_sktc_setup.md` — how to set up SKTC's folder-based email intake bridge.
- `deploy/windows/README.md` — how to actually install and schedule this on a Windows machine.
- Git log / commit messages — every non-obvious fix has a detailed "why," not just "what changed."
