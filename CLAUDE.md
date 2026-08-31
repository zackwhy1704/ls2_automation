# ls2_automation — project instructions

## Commit messages: document to client-handoff standard

This project's commits and docs are read by two audiences beyond us: the client (who will follow
these steps literally, "to a T") and our own future sessions (as long-term memory / "second
brain"). A vague commit is a liability in both directions. Every commit that changes driver
behavior (`src/synergix_driver.py`, `src/tcms_scraper.py`, or any Stage A–D logic) must include:

- **Every UI step that changed, at click/button granularity** — name the exact element (label text,
  CSS class, or id fragment), not just "fixed the Employee toggle." If a fix involves a specific
  button the human clicks (e.g. "the layout toggle, a right-pointing triangle, class
  `ui-layout-unit-header-icon`, top-right of the Unscheduled Service Orders panel — NOT the panel's
  own titlebar minimize icon"), say so precisely enough that someone with no code access could
  follow it in the live UI and land on the same element.
- **Every wait/pause and why** — if a fix replaces a fixed delay with a real signal (e.g. the
  `js-ajax-spinner`), say what the old behavior was, why it was wrong, and what the new wait
  condition actually checks.
- **What was tried and failed**, not just what worked. If three approaches were tested live before
  the one that worked, name what failed and why — a future session (or a human debugging by hand)
  needs to know the dead ends too, so they aren't re-walked.
- **Whether it was verified live**, and against what (a real WO, a synthetic test WO, a specific
  Service Order number) — "confirmed live 2026-08-31 on WO-PO/99999m2" is a claim someone can check;
  "should work" is not.
- **What's still broken**, explicitly, if the commit doesn't fully close the loop. Don't let a
  partial fix read as a complete one. Say exactly where the next failure happens and what's been
  ruled out already, so the next session picks up past the dead ends instead of re-discovering them.

This is already the style used in `src/synergix_driver.py`'s own inline comments (e.g. the
`_wait_for_ajax_spinner` docstring) — the standard is to hold **every** commit message to that same
level of specificity, not just the code comments.

## Live-testing discipline

- Confirm `SYNERGIX_BASE_URL` points at `copy.taskhub.ls2.sg` (non-prod) before any `DRY_RUN=false`
  run — the driver itself refuses to run live scripts against anything without `copy.` in the URL,
  but double-check anyway.
- Prefer synthetic test WOs (`scripts/run_synthetic_stage_c_test.py` and siblings) over hunting for
  fresh real WOs when the goal is testing driver code, not validating against real client data —
  it's faster and the real TCMS queue is frequently exhausted of non-duplicate WOs.
- When a live test fails, capture a screenshot before concluding anything about the failure mode.
  Several sessions' worth of wrong conclusions (e.g. the retracted "row-selection state" theory for
  the Stage C checklist bug) came from reasoning about a failure without looking at the actual
  screen state first.
- **Synthetic test WO job dates must always be in the past (or today), never in the future.** A
  Work Order bills for work already performed — every real WO this project has seen has a job date
  at or before the WO date. Caught live (2026-08-31): a fix for a date-collision bug in
  `run_synthetic_stage_c_test.py` offset test dates FORWARD from a fixed 2026-09-01 base, landing
  several synthetic WOs in 2027 without anyone noticing until the user spotted it on screen. Wrong
  in two ways: it's not what a real WO looks like, and it risks silently masking date-dependent
  Synergix behavior (validation rules, business logic) that only real past/current dates would ever
  exercise. When a test needs unique dates to avoid collisions, offset BACKWARD from
  `date.today()`, never forward.
