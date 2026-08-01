# Operational expectations for LS2 billing automation

This is not a "set and forget" system. It automates two portals (JBTC's TCMS/Dynamics 365, and
Synergix ERP) that LS2 does not control and that were not designed with automation in mind. Both can
change their wording, layout, or DOM structure at any time without notice, and when that happens the
part of the automation touching that specific screen will break until someone updates the matching
selector or click sequence in this codebase.

## What this means in practice

- **This needs a break-fix retainer, not a one-time build.** Budget for the automation author (or
  someone familiar with this codebase) to be reachable when a run starts failing, not just at launch.
- **Fixes are usually fast once diagnosed** — most breakage so far has been a single selector or wait
  condition going stale, not a structural rewrite. The cost is in *noticing* quickly and diagnosing
  correctly, which is why the reporting below matters as much as the automation itself.
- **The riskiest single step is Synergix Stage B (quotation creation)** — see
  [synergix_workflow.md](synergix_workflow.md). It clicks through several JSF/PrimeFaces dialogs by
  visible text and label (`"Copy From"`, `"General Service"`, etc.) because the underlying element ids
  are auto-generated and unstable. A Synergix UI update that renames a button or reorders a dialog can
  break this step specifically.

## How you'll find out something broke

- Every scheduled run posts a summary to Telegram, whether it succeeded or not — see
  [deploy/windows/README.md](../deploy/windows/README.md) for what that looks like day to day. A
  WO marked FAILED or NEEDS_REVIEW there is the normal, expected way this system tells you it hit
  something it couldn't handle safely — check the per-WO reason and fix in Synergix directly.
- If the *entire run* crashes (not just one WO) — e.g. a portal login flow changed shape entirely —
  you get a distinct 🚨 (or 🔐 for a login-specific failure) Telegram alert from
  `scripts/alert_on_crash.py`, separate from the normal per-run summary. This should be rare; treat it
  as "call for a fix," not "wait for tomorrow's run."
- Nothing in this system silently mis-bills. Every failure mode that couldn't be resolved with
  confidence (a portal change, a low-confidence extraction, an inconclusive duplicate check) routes to
  FAILED or NEEDS_REVIEW rather than guessing — see the trust-gate/dedup design in
  [synergix_workflow.md](synergix_workflow.md). The tradeoff is throughput, not correctness: a broken
  selector means WOs pile up as NEEDS_REVIEW/FAILED until fixed, not that a wrong invoice goes out.

## Known current gaps (as of 2026-08-01)

- **SKTC has no Synergix "Copy From" template yet** — Stage B will fail for every Sengkang WO until a
  template quotation exists for that customer in Synergix (or the exact expected Customer name is
  confirmed). JBTC is unaffected. See project notes for the exact error and fix options.
- **Multi-line-item WOs are not fully modeled.** The billing payload holds one quantity/unit-price/
  discount line; a WO with several distinct line items (seen on at least one real SKTC sample) only
  captures the first line's figures. This does not cause a wrong bill (the money-consistency check
  will flag the mismatch and route it to NEEDS_REVIEW), but it does mean those WOs need a human to
  fill in the rest by hand.
- **TCMS login assumes a password-fallback path is always available.** If JBTC's tenant ever enforces
  Conditional Access MFA with no bypass, TCMS runs cannot proceed unattended — this fails with a
  distinct, diagnosable error (see `_detect_mfa_dead_end` in `src/tcms_scraper.py`) rather than a
  silent hang, but resolving it needs a tenant-side exemption or a dedicated non-MFA service account,
  which is a conversation with JBTC, not a code fix.
