# Synergix Service Quotation & Order Workflow (from client docs)

Source: `JBTC_Service Quotation & Order Workflow.docx` (client-provided). This is the authoritative
spec for what the Synergix driver must do. JBTC and SKTC follow the same Synergix steps; they differ
only in WO intake (JBTC = TCMS scrape, SKTC = email).

## 0. Duplicate check (BEFORE creating anything)
- Go to **Servicing → General Services → Service Order Performance → HISTORY**.
- Key in the **WO number**; confirm the WO has NOT already been created as a service quotation.
  **AVOID DUPLICATION.** Only proceed once "no record found".
  (This is exactly the fail-safe `check_duplicate()` guard — Synergix HISTORY is the source of truth.)

## 1. Service Quotation (stage B)
- Go to **Servicing → General Services → Service Quotation**.
- Press **+** to generate a new quotation, then **"Copy from"** a previous service quotation (template).
- Fill the details:
  - **Customer contact** = WO "prepared by" (from the Jalan Besar / SKTC work order form).
  - **Date** = today's date; **delivery date** = same as enquiry date.
  - **Subject** = `WO-PO/XXXXXXXXX – Jalan Besar Town Council (Type of service)`.
  - **Reference No.** = paste the **GL #** (full G/L string).
  - **Project site**: if JOB SHEET has alphabets → **2000069 (Infigo)**; if JOB SHEET is numeric only →
    **2000050 (Ecocare)**.
  - **Item code**, **Qty/UOM**, **unit price** (per the job cost), and **remarks** (client format).
- Right-side column: click **Payment info** to verify; click **Shipment info** to verify (date = today).
- **Submit** if everything is in order.
- Go to **Variation Order**, retrieve the same service quotation, click it, and **Confirm** the VO.

## 2. Schedule Board (stage C — most fragile, best-effort)
- Filter Unscheduled Service Orders by the WO-PO number (WITH its `WO-PO/` prefix, per the video —
  see the detailed frame-by-frame section below); tick the row's own checkbox to select it.
- Click Employee (toggle may need clicking twice if it drifts back to Work Team).
- Click on the calendar → Event Details pop up: **Assigned** = a Work Team (e.g. `800SUPER`, not a
  person — this is the field ⚠️ this project spent a week automating the WRONG target for, see
  below), **To Pair With** = tick **ECOCARE** or **INFIGO** (alphabetic/numeric Job Sheet rule).
- **Date = WO date** (both From and To); Remarks left blank in the one confirmed example.
- Confirm the popup (tick → Yes), then click the SEPARATE **Submit** button on Order Details to
  actually commit it — the popup's own confirm does not submit by itself.
- See "Stage C, frame-by-frame from JBTC WO Synergix.mp4" further down for the full, exact,
  timestamped walkthrough and open questions for the client.

## 3. Service Order Performance — Fulfil (stage D)
- Search for the **service order no.** (or customer code if only one).
- Press **Fulfill**.
- Check the **billable**.
- **Attach the work order** PDF; use the WO number as the reference for the folder and pdf.
- **Submit the service order for billing.**

## Concrete field values (from JBTC_Adhoc_Pest_Control_Billing_Workflow.docx)
- **Nav path**: post-login → **General Service** (text link) → **Servicing** icon → the modules
  **Service Order Performance - LS2**, **Service Quotation - LS2**, **Schedule Board - LS2**.
- **Dedup (Stage B step 3)**: Service Order Performance - LS2 → search the **Enquiry/Subject** column
  by WO-PO number → confirm no existing record before proceeding.
- **Item code (adhoc pest control)**: `SE-400212A`, Type **S**, Qty **1.00 SVC**
  (SE-400212A = "Adhoc-Provision of Pest Control Services").
- **Subject**: `WO-PO/XXXXXXXXX – Jalan Besar Town Council (Type of Service)`.
- **Reference No.** = the GL number (full string).
- **Project Site**: alphabetic Job Sheet → Infigo (2000069); numeric only → Ecocare (2000050).
- **Remarks format**:
  `[TC name – address]. Remarks: [location details]. Job done on [date]. [Description of Work].
   Job Sheet: [number]. [Scope description]. WO-PO/[number].`
- **Payment Info panel**: Billing Party = the Town Council; Payment Method = Cheque; Tenor Term = 30 days.
- **Shipment Info panel**: Required Shipment Date = today.
- **Stage D tabs**: **Billables** tab (confirm Quoted/Actual Qty, Unit Price, Total) then **Attachments**
  tab (paperclip) → upload WO PDF, folder+file named by WO-PO number → **Fulfill**.
- **Outputs**: Service Quotation (e.g. QUO0006010) + Service Report (e.g. SV00008631), both PDF.

## Field source mapping (agreed with client)
Corrected nav: post-login → **General Service** → under **Enquiry Call & Order** →
**Service Quotation - LS2**. Dedup is done INSIDE this module via its **History** view (not a separate
Service Order Performance module): check History's Enquiry/Subject for the WO-PO; if it exists, SKIP
this WO. Once confirmed unique, go back to **Draft** and click **+**, then **Copy From** a template.

Inherited FROM the Copy From template (driver does NOT set these):
- **Customer / Billing Party** (one template per town council carries the right customer).
- **Item code** (varies by service type; the template already has the correct one).
- Payment/Shipment defaults (Cheque, 30-day tenor) unless they need per-WO change.

Set/overridden by the driver FROM extracted WO data:
- **Subject** = `WO-PO/<num> – <Town Council> (<type of service>)`
- **Reference No.** = `gl_number` (full string)
- **Date** / delivery / required shipment date = today
- **Project Site** = `resolve_project_code(job_sheet_number)` (alphabetic→Infigo 2000069, numeric→Ecocare 2000050)
- **Customer Contact** = `prepared_by`
- **Qty** = `quantity`, **Unit price** = `unit_price`
- **Remarks** = built per the remarks format above

Verification (step 6): after filling, open the quotation **PDF preview** to eyeball the end product
against the JBTC billing workflow BEFORE the human submits. Use existing History records as the
formatting reference (they hold correct real data).

Template quotation ids (one per council) + copy-from source: TBD during codegen recording.

## Observed from the JBTC WO Synergix.mp4 walkthrough (WO-PO/000080291, QUO0006213)
The **Service Quotation - LS2** edit form fields (confirmed against a real filled quotation):
- Left column: Customer Type (Existing Customer), **Customer** + **Customer Contact** (inherited from
  template — e.g. JALAN BESAR TOWN COUNCIL / Iswaran s/o Regunath), Contact No.,
  **Enquiry/Subject** = `WO-PO/000080291 - Jalan Besar Town Council (Rodent Treatment)`.
  ⚠️ Enquiry/Subject has a **50-character max** (validation toast fires past 50) — the subject string
  must be truncated/abbreviated to fit.
- Middle: **Enquiry Date** + **Quotation Date** = today; Validity Term/Date.
- Details grid (line item, mostly inherited from template): Qty/UOM `1.00 SVC`, Options `Regular`,
  **Unit Price** (from WO job cost, e.g. 44.00), Total Amount, per-line **Remarks**
  (e.g. `Jalan Besar Town Council - 4C ST. GEORGE'S LANE`). Totals auto-calc: Before Tax, 9% GST,
  After Tax (44.00 → 47.96, matches the WO Grand Total).
- Right panel "General": Location, Salesperson*, SBU, Currency (SGD), Sales Tax (GST Std Rt 9%),
  **Reference No.** = GL number, Customer PO No.
- Right panel "Segment" (required *): **Project Site** (e.g. `2000069-(Potong…)` = Infigo since job
  sheet A25-01086 is alphabetic), Project In-Charge (`2SC-Infigo`), Project Portfolio (`Towncouncil`),
  Project BU (`Pest Control`).
- The list/dedup view (**Service Quotation - LS2** grid) has an **Enquiry/Subject** column with a
  per-column filter box — filtering it by the WO-PO is the dedup check.

Stage C (Schedule Board) and the Fulfil view (Service Order Performance) were also shown but are OUT
of the current Stage-B automation scope.

## Stage C, frame-by-frame from JBTC WO Synergix.mp4 (2026-08-31) — corrects a multi-session mistake

**This section exists because the automation spent roughly a week (sessions 4 through 8+) building
and repeatedly "fixing" Stage C against the WRONG target.** Every one of those sessions assumed the
final scheduling assignment is an **Employee** (specifically `SCHEDULE_EMPLOYEE = "TAN WEI YING"`,
via Schedule Board's Employee/Work Team toggle and its "Employee Job Type" checklist). Extracting
and reading the actual walkthrough video frame-by-frame (1 frame/sec, OpenCV, from 5:30 to 7:52)
shows the REAL, successful, submitted flow uses a **Work Team** assignment ("Assigned: 800SUPER")
paired with **INFIGO or ECOCARE** via a "To Pair With" checklist — never an employee name at all.
The high-level summary two sections above this one (`## 2. Schedule Board`) already said "choose
ECOCARE or INFIGO from the dropdown" — the correct answer was written down from the start; later
sessions built an entire employee-checklist automation path without cross-checking it.

Video: `~/Downloads/JBTC WO Synergix.mp4` (9.6 min, 30fps, screen recording against
`copy.taskhub.ls2.sg` — confirmed by the visible URL bar and the "NON-PRODUCTION SERVER" banner in
frame, so this is NOT a prod-only flow). WO used: `WO-PO/000080291` (JALAN BESAR TOWN COUNCIL,
Job Sheet `A25-01086`, alphabetic prefix -> Infigo project code, Grand Total $47.96). Timestamps
below are video position, extracted via `cv2.VideoCapture` + `CAP_PROP_POS_FRAMES` — reproducible
by anyone with the same file (`ffmpeg`/`cv2` were not preinstalled on this machine; opencv-python-
headless was pip-installed to do the extraction, no other tool was available).

**Step-by-step, exact clicks and waits (referenced by video timestamp):**

1. **[5:54–6:04]** From the Stage B.5 success toast (SA0005, Variation Order confirmed), navigate
   General Service -> **Schedule Board - LS2**. Page loads with the Customer Info/Search panel open
   on the right at full width — the panel is NOT collapsed first; no "Step 1 chevron" action of any
   kind happens before filtering. (An earlier session in THIS SAME investigation invented a
   "collapse the Customer Info panel first" step from a misreading of the written doc's Step-1
   screenshot and had to revert it after it caused a regression — see the commit history on
   `_schedule_stage_c_attempt`. The video proves that step doesn't exist in the real flow.)
2. **[6:04–6:12]** Click into the **Unscheduled Service Orders** grid's **Enquiry/Subject** column
   filter box. First paste is a stray, unrelated clipboard string (a PowerFX snippet — a genuine
   mistake in the recording, not part of the procedure) which correctly returns "No records found".
   Corrected to **`WO-PO/000080291` — the FULL string, WITH the `WO-PO/` prefix and slash** — which
   returns exactly one row (`SV00008851 | QUO0006213 | JALAN BESAR TOWN COUNCIL | ...`).
   **RESOLVED (2026-08-31, later same day)**: this looked like a contradiction with the driver's
   `wo_bare = wo.replace("WO-PO/", "")` filtering, but the bare-number form has since been confirmed
   working live, repeatedly, across many real test runs this same session (always narrows the grid
   to exactly one row). Both forms apparently work — the video just happened to show the with-prefix
   form because of the stray first paste being corrected to the full string, not because the bare
   form fails. Left the driver's existing bare-number filter unchanged rather than risk a regression
   on weaker (single-example) video evidence against much stronger (repeated live) evidence.
3. **[6:13–6:14]** **Tick the row's own checkbox** (the small checkbox at the start of the row, NOT
   a click on the row body/text). This is the "correct order MUST BE SELECTED (blue highlight)"
   step the user described from memory before the video was reviewed. Ticking it immediately opens
   **Order Details[SV00008851]** in the right-hand panel (replacing Customer Search), showing
   "⚠ This Service Order has not been submitted."
4. **[6:15]** Click **Employee** (top-right of the Schedule Calendar section, next to Work Team).
   Becomes active (blue) immediately.
5. **[6:15–~6:28]** A **long ajax wait** — Synergix's own footer loading spinner
   (`img.js-ajax-spinner`, see `_wait_for_ajax_spinner` in `src/synergix_driver.py`) runs
   continuously for approximately **13 seconds** here with no visible page change, then the
   calendar grid changes shape and gains a real "1 / 2" pagination footer. No "Filter" panel was
   explicitly opened in this recording before this point — the calendar itself became paginated
   directly off the Employee click. (This differs from the current driver's assumption that a
   separate Filter-panel click is required to see/select employees — worth re-testing whether
   Filter is actually optional, or whether it happened off-screen/too fast for the recording to
   show. Flagging as unconfirmed rather than asserting either way.)
6. **[6:28–6:44]** Cursor moves around the empty calendar body and the right panel, clicks page "2"
   navigation once, calendar switches from a plain date view to a **Week of 16/07/2026** view.
   Ticking the row's checkbox again (or the toggle reasserting itself) causes "Employee" to
   revert to "Work Team" being highlighted, needing "Employee" clicked a second time — the same
   "toggle can drift back" flakiness this project's earlier sessions already documented, now with
   ground truth that this drift is a real, recorded phenomenon and not a testing artifact.
7. **[6:50]** Click into the calendar body -> **Event Details** popup opens. Fields, exactly as shown:
   - **From** / **To** date pickers (both default to `15/07/2026` on open — NOT the WO date; this
     gets manually corrected in the next step).
   - **Assigned** — a dropdown, defaulted to **`800SUPER`**. This is a WORK TEAM, not a person.
     ⚠️ **Nothing on the WO PDF or in the workflow doc names "800SUPER" or explains how to choose
     it.** No field on the source WO (`Contractor Code: 530`, `Schedule Type: KB-000202`, GL No.,
     etc.) obviously maps to it. Treating `800SUPER` as the default/only value used to date is an
     ASSUMPTION, not a confirmed rule — see "Open question for the client" below.
   - **Paired With** (read-only display, populated once a "To Pair With" box is ticked).
   - **To Pair With** — a checklist of 12 names/teams: CADILLAX, ECOCARE, GREENCARE, HERBERT LIM PIN
     HENG, INFIGO, NEWS, SUSAN LEE SUN SUN, TAN WEI YING, TAN ZHEN YUAN BENEDICT, TOMMY TOH GIM POR,
     VINCENT TEO BOON WAH, VISHAL ANAND SINGH. **`TAN WEI YING` appears here too — as one of 12
     possible pairings, not as the "Assigned" party.** This is very likely why every earlier session
     assumed she was the target: her name genuinely does appear on this exact popup, just in the
     wrong field.
   - **Remarks** — a free-text box, left EMPTY in this recording (not filled with the
     nature-of-work + WO-number string the current driver code composes for the employee-flow
     equivalent).
8. **[6:52]** Ticks **INFIGO** in "To Pair With" (not ECOCARE) — consistent with this WO's Job Sheet
   `A25-01086` having an alphabetic prefix, which the existing `resolve_project_code()` logic (and
   the doc's Project Site rule) already maps to Infigo/2000069. This is the FIRST live confirmation
   that the "To Pair With" choice follows the SAME alphabetic/numeric Job Sheet rule as Project
   Site — genuinely likely but not independently proven from a second example in this recording.
9. **[7:00]** **From** date manually changed to `03/07/2026` — the WO Date (top of the PDF), not the
   job/service date shown elsewhere on the form. **Paired With** field now shows `INFIGO`,
   confirming the checkbox tick committed.
10. **[7:03]** **To** date also changed to `03/07/2026` — same-day event, both fields equal.
11. **[7:10–7:20]** Cursor lingers near the checklist and Remarks box but Remarks is never filled in
    this recording, and no other checkbox is ticked.
12. **[7:30]** Clicks the popup's own confirm (checkmark) button -> a **"Confirmation: Are you
    sure?"** dialog appears -> clicks **Yes**.
13. **[7:32–7:40]** Popup closes. The Unscheduled Service Orders grid reverts to its unfiltered
    default view (all 45 rows) — the filter is NOT preserved after the popup closes. Order
    Details[SV00008851] remains open on the right, still showing "⚠ This Service Order has not been
    submitted" — the popup's own confirm does NOT submit the Service Order by itself, matching this
    project's existing documented understanding ("a SECOND, separate Submit action" — this part of
    the prior understanding was correct).
14. **[7:40–7:50]** Clicks **Submit** (top of the Order Details panel, next to "Order
    Details[SV00008851]"). No visible confirmation dialog for this specific click in the recording.
15. **[7:50]** **Confirmed success**: the "not submitted" warning is gone, and the **Upcoming
    Service** tab (top-right of the page, next to Customer Info) now shows a real populated entry —
    `16/07/2026, SV00008851, Jalan Besar Town Council - 4C ST. GEORGE'S LANE, ... WO-PO/000080291` —
    with the full remarks/description text. The bottom of the Order Details panel shows a
    **"Schedule"** section listing **`INFIGO`**, confirming the Work Team + Pair-With assignment is
    what actually persisted, not an employee name.

**Assumptions this session is making, to be clarified with the client (per the user's explicit
instruction, 2026-08-31: "i dont know just follow the WO, state your assumptions and i will clarify
with the client"):**

- **`Assigned = 800SUPER` is assumed to be a fixed default for JBTC**, since nothing on the WO or in
  the doc ties it to any WO-specific field. This may be wrong — it could vary by council, by date
  (whoever's on duty that week), by job type, or be a dropdown the human operator picks from
  domain knowledge not captured anywhere in the automation's inputs. **Needs client confirmation.**
- **"To Pair With" (INFIGO vs ECOCARE) is assumed to follow the same alphabetic/numeric Job Sheet
  rule as Project Site** (`resolve_project_code()`), since the one example in the video is
  consistent with that rule. Only one data point — **not independently proven, needs client
  confirmation or a second video/example.**
- **Filtering the grid by the WO-PO WITH its "WO-PO/" prefix** is what's proven to work in the
  video; whether the bare-number form (what the driver currently sends) also works, or whether that
  was the actual latent bug this whole time, is not yet re-tested live as of this writing.
- **Remarks was left blank** in the one recorded example — assumed optional/not required for a
  successful Submit, since Submit succeeded without it. Not explicitly confirmed as intentional
  vs. an omission in the recording.

**Next step once these are confirmed:** rewrite `_schedule_stage_c_attempt` in
`src/synergix_driver.py` around the row-checkbox selection + Work Team "Assigned" dropdown +
"To Pair With" checklist flow instead of the Employee/`SCHEDULE_EMPLOYEE` checklist flow it
currently targets. This is a structural rewrite, not a small patch — nearly every helper in that
method (`_mouse_click_ui_button` targeting Employee/Work Team, the `employee_checkbox_js` lookup,
`SCHEDULE_EMPLOYEE` itself) was built for the wrong target field and will need to be re-pointed or
replaced.

### Rewrite done and live-tested (2026-08-31, same day) — real progress, one blocker remains

Per the user's explicit instruction ("do it first we will make the changes over at the client
side"), `_schedule_stage_c_attempt` was rewritten around the corrected target before the open
assumptions above were confirmed. `SCHEDULE_EMPLOYEE` is retired (kept only because Stage B's
Salesperson field happens to use the same literal string); a new `ASSIGNED_WORK_TEAM = "800SUPER"`
constant was added with the same "unconfirmed assumption" framing as above.

**What's now proven working, live, across multiple test runs:**
- Row checkbox selection, Employee toggle click, opening Event Details via a (now name-agnostic)
  `newEventButton` overlay click.
- The "Assigned" dropdown correctly defaults to `ASSIGNED_WORK_TEAM` and is left alone when it
  matches (only warns, doesn't guess, if it defaults to something else).
- **The "To Pair With" checkbox click required its own fix**: initially assumed a plain native
  `<input type="checkbox">` was directly clickable (based on how the popup LOOKS in a screenshot),
  but a raw DOM dump showed the real `<input>` is wrapped in `div.ui-helper-hidden-accessible` --
  the same PrimeFaces accessibility pattern the now-retired employee-checklist code had already
  found and worked around for ITS OWN checkbox. Fixed by clicking the sibling `.ui-chkbox-box` div
  instead of the hidden input directly, exactly mirroring that older, already-solved pattern. Found
  the correct checkbox by its `value` attribute (encodes `oa_code=INFIGO`/`oa_code=ECOCARE`), not
  by label text, since the label/input `for`-attribute pairing on this specific popup was not
  confirmed.
- From/To date fields, and the popup's own Confirm click (with the already-fixed SV9104 self-
  collision guard) all work correctly through to the confirmation dialog.

**RESOLVED (2026-08-31, later same day): the `ui-dialog-mask`/disabled-Submit blocker was a SESSION
TIMEOUT artifact, not a missing UI step.** Extensive investigation (frame-by-frame video review,
live DOM polling for 30s straight, force-clicking the "disabled" Submit button) could not find any
client-side action that enables Submit, because there wasn't one to find -- the automated test runs
that hit this were sitting idle for extended periods (the test machine went to sleep mid-run
multiple times that same session, freezing the browser process for many minutes at a stretch),
almost certainly long enough for Synergix's own session/state to go stale server-side. When the
user manually drove the SAME flow through by hand (no automation, no idle gaps) after seeing this
exact error overlay once, **Submit worked correctly on the very next attempt** -- confirming the
rewritten Work-Team + To-Pair-With flow (row select -> Employee -> Event Details -> To Pair With ->
dates -> Confirm -> Submit) is correct and complete as implemented in `_schedule_stage_c_attempt`.
No further UI/selector change was needed here. The real lesson: an idle/slept browser session
mid-Stage-C can produce exactly this symptom (a button that looks disabled and never activates, a
mask that never clears) -- treat a stuck Submit as a possible stale-session issue first (re-login,
retry from a fresh page load) before assuming a missing click sequence.

## ⚠️ Cloudflare bot protection
`taskhub.ls2.sg` sits behind **Cloudflare bot protection**. HEADLESS Chromium gets blocked
("Sorry, you have been blocked"), and rapid repeated automated hits trigger an **IP-level block**
that persists for a while (minutes–hours) even for a visible browser. Mitigations in the driver:
- Run **non-headless** (`SYNERGIX_HEADLESS=false`, the default) with a realistic user-agent and
  `--disable-blink-features=AutomationControlled`.
- **Persistent context** (`.synergix_session/`) to reuse the login cookie and minimise hits.
- Avoid tight retry loops against the server.
For reliable production automation, the best fix is to have the client/vendor **allowlist the runner
IP** or relax Cloudflare bot protection for this app.

## ⚠️ Selector strategy: label-based, NOT id-based
Synergix TH6 is JSF/PrimeFaces. Element ids are AUTO-GENERATED and unstable, e.g.
`syn:j_id-803968509_59bfea21:summaryTabs:j_id695566453_56c810d0:...:serviceQuotationTable:j_id-207568224_1ce7fad8:filter`
— the `j_id...` fragments regenerate across view changes/sessions, so hard-coding them is brittle.

What IS stable: the visible **column header labels** ("Enquiry/Subject", "Quotation No.", "Reference
No.", …) and structural class/role names (`ui-column-title`, `serviceQuotationTable`, `:filter`,
`ui-inputfield`). So the driver must locate fields RELATIONALLY: find the column/label by its text,
then target the input/filter within it (Playwright get_by_role / has-text / xpath-by-label), rather
than by absolute id. Login ids (`loginForm:*`) are the exception — those are stable.

### Fields the driver actually SETS (everything else inherited from the Copy From template)
Per client decisions: Customer, Customer Contact, item code, and all four Segment fields
(Project Site/In-Charge/Portfolio/BU) are INHERITED from the template. The driver only overrides:
- **Enquiry/Subject** = `WO-PO/<num> - <Town Council>` (DROP the service-type suffix), truncated to
  the **50-char** max.
- **Enquiry Date** + **Quotation Date** = today.
- **Reference No.** = `gl_number` (full string).
- **Unit Price** = `unit_price`; line **Remarks** = built remarks.
(Qty is 1.00 SVC from the template; totals auto-calc.)

## Automation scope (agreed)
- **In scope now: Stage B only** — dedup check + create quotation (Copy From) + fill all header/line/
  remarks fields + verify Payment/Shipment info. Does NOT auto-submit.
- **Left to the human approver**: final Submit + Variation Order confirm (end of B), Stage C schedule
  board (most fragile), and Stage D fulfil/attach/bill.
- DRY_RUN performs every step EXCEPT the final Submit/Confirm/Fulfill clicks.
- Copy-From source quotation: TBD (decide during codegen recording).

## Stage C (Schedule Board) selector discovery — 2026-08-25, session 1 (INCOMPLETE)

The client asked for an email-to-finance notification tied to workflow-doc **step 28** ("Press
Fulfil to submit the service order for billing" — the very last action of Stage D). Since neither
Stage C nor D has ANY automation yet (config/selectors.py has TODO_SELECTOR placeholders for both,
and the original work order marked both out of scope), the agreed plan is to automate Stage C+D
first, then fire the finance email on the real Fulfil action. This section records the first live
discovery pass, run against `copy.taskhub.ls2.sg` (the NON-PRODUCTION copy environment) with
`scripts/synergix_discover_schedule_fulfil.py` (new, read-only discovery REPL modelled on
`scripts/tcms_discover.py`, reusing `SynergixDriver` for login/session).

**Confirmed nav path**: General Service → Servicing tile → **Schedule Board - LS2** (same tile menu
Stage B already uses for Service Quotation - LS2).

**Confirmed real, working selectors** (label-relational, per this doc's existing selector strategy):
- **Unscheduled Service Orders grid** — a `.ui-datatable` with per-column filter inputs. The
  "Filter by Customer" column filter (`id` contains `unscheduledHdrs:...:filter`, one auto-generated
  suffix per column — anchor on the **"Filter by Customer" `<label for=...>` text**, not the raw id,
  since ids regenerate) filters the grid on **Enter** (not live/debounced). Filtering by "Jalan Besar"
  against this copy environment returned exactly one real row: `SV00008850`, Order Date 16/07/2026,
  `QUO0006212`, "Jalan Besar Town Council - 15 ST GEORGE ROAD".
- **Customer Search panel** (top right) is a SEPARATE widget from the grid filter above — filling its
  "Customer" field (relational label lookup, `.synfaces-grid-item` scope; must exact-match the label
  text and exclude `th`/`.ui-datatable` or it collides with the grid's own "Customer" column header)
  and pressing Enter populates "Customer Search Results" with the matched Town Council's address and
  a list of associated staff names — informational, does NOT appear to drive the calendar.
- **Schedule Calendar**: an Employee/Work Team `ui-selectonebutton` toggle (real visible text
  "Employee"/"Work Team" inside a `ui-button-text` span — a PLAIN `text=Employee` locator will instead
  match a hidden, unrelated "Global Employee" nav link elsewhere on the page; must filter to
  `visible=true`). The date field (`...1b3c433a_input`, a PrimeFaces datepicker input) accepts a typed
  `DD/MM/YYYY` + Enter. The grid itself (`.schedule_container`, `<table>` with a `Service Personnel`
  row-header column and hourly `08:00`..`22:00` column headers) renders ONLY after a real settle delay
  post-navigation (~8s; a 3-4s wait was NOT enough and left the table element entirely absent from the
  DOM, not just empty — same ajax-timing class of issue already documented throughout
  `synergix_driver.py` for Stage B). A "Filter" panel (collapsed by default, `display:none`) toggles
  open to reveal "Employee Job Type" / "Work Team" checkbox lists (`ui-selectmanycheckbox ui-grid`).

**Where this session stopped**: on `copy.taskhub.ls2.sg`, BOTH the "Employee Job Type" and "Work
Team" checkbox lists render permanently empty (no options at all, not a loading state — confirmed
after an 8s+ wait and a fresh Filter-panel toggle), and the calendar shows "This schedule is empty."
for every date/customer combination tried, including the WO's own real order date (16/07/2026) for
the one real matched order (`SV00008850`). Clicking the matched order row in the Unscheduled Service
Orders grid highlights/selects it but leaves the "Order Details" panel on the right completely empty
(no ajax content loaded) — clicking the row itself may be the wrong target (Stage B hit an identical
"clicked the row, nothing happened" trap that turned out to require clicking the `<a>` INSIDE the
row's cell, not the row — see `_click_panel_row_by_text`'s docstring — not yet tried here).

**Working theory**: the copy/non-production environment likely lacks Work Team/Job Type master data
or real personnel-to-order assignments, so there is nothing for the Employee-view calendar to
render regardless of selector correctness. This has NOT been confirmed against production
(`taskhub.ls2.sg`, no `copy.` prefix) — genuinely unknown whether production has the same gap or
whether this is copy-environment-only.

**Next steps** (not yet done):
1. Confirm whether production Synergix has real Work Team/Job Type data and populated calendar rows
   for at least one real scheduled JBTC order — needed before any driver code can be written, since
   there is currently no confirmed selector for the actual employee/date-cell click → Event Details
   popup → Ecocare/Infigo dropdown → Submit sequence the doc describes.
2. Try clicking the `<a>` inside the matched order row's first cell (not the `<tr>`) to see if that's
   what populates "Order Details" — untried this session.
3. Once an Event Details popup is reachable, repeat this same discovery process for it, then for
   Stage D (Service Order Performance → Billables → Attachments → Fulfil).
4. Only after Stage C+D driver code exists and is live-verified: wire the finance-notification email
   to fire immediately after the real Fulfil click succeeds (client's request, step 28).

## Stage C discovery — 2026-08-25, session 2: root cause found (a real, previously-undocumented gap)

The client corrected session 1's assumption: Schedule Board only shows orders whose Stage B
quotation has actually been **submitted** — nothing created in DRY_RUN (which never clicks Submit)
would ever appear there, which fully explains session 1's permanently-empty calendars. Per the
client's instruction, ran 5 real JBTC WOs end to end with a real Submit (`DRY_RUN=false`, dedup
intentionally bypassed for this test since every sample WO already had a prior test-run draft in
Synergix — "self reported and testing", not real duplicates) via a new one-off script,
`scripts/run_5_stage_b_submit_test.py`. All 5 succeeded: `WO-PO/000076625→QUO0006749`,
`000076627→QUO0006750`, `000076639→QUO0006751`, `000076640→QUO0006752`, `000078228→QUO0006753`.

**Root cause of the empty Schedule Board, confirmed live**: a submitted quotation does NOT
immediately become a schedulable Service Order. It lands in the **"Under Variation"** status tab
(the "89" count seen in session 1) — this doc's own Stage B section already said as much ("Go to
Variation Order, retrieve the same service quotation, click it, and Confirm the VO") but that step
had NEVER been automated or even given a placeholder selector, and its actual necessity for Stage C
had not been connected until now. Opening `QUO0006749` from the "Under Variation" tab shows the
quotation read-only with a toolbar (`undo, +, envelope icon, pencil/edit, check-double/Confirm`).
Clicking the **Confirm** button (`title="Confirm"`, icon `fa-check-double`, fires a real
`PrimeFaces.confirm({message:"Are you sure?"})` dialog — a genuine state-changing action, done live
here only after explicit user sign-off) → click **Yes** → Synergix shows
**"SA0005: Service Order No.: SV00008852 is created successfully."** and the quotation disappears
from "Under Variation". That new Service Order (`SV00008852`) then immediately appeared in the
**Unscheduled Service Orders** grid on Schedule Board (count went 44→45) — confirming the full,
real chain end to end:

**Submit (Stage B) → Under Variation → Confirm the VO ("Are you sure?" → Yes) → real Service Order
(`SV0000...`) created → appears in Schedule Board's Unscheduled Service Orders grid.**

This VO-confirm step is a **third automation gap**, distinct from Stage C (Schedule Board) and
Stage D (Fulfil) — call it **Stage B.5**. It sits structurally at the END of Stage B in this doc's
own numbering (already listed under Stage B: "Go to Variation Order... Confirm the VO") but has
zero code, selectors, or even a TODO placeholder today. Any real Stage C automation is blocked on
this being done first, since without it there is simply no Service Order for Schedule Board to show.

**Clicking the new order row** (`SV00008852`) in Unscheduled Service Orders, after also clicking the
calendar's "Filter" toggle, revealed a previously-unseen **"Order Details[SV00008852]"** panel
(distinct from the smaller "Customer Search" panel used in session 1) with a real, actionable
**"⚠️ This Service Order has not been submitted."** banner, and read-only fields: Enquiry/Subject
(full remarks text), Order Date, Service for, Customer Job/PO No., SBU, Servicing Period, Project
Site, Project In-Charge, Project Portfolio, Project BU. The calendar itself also switched from a
daily to a **weekly view** (`Week of 25/08/2026`, days 23-29 Aug as columns) once this panel was
open — a new calendar mode not seen in session 1. No "Assign employee" control was found in the
captured HTML of this panel (~417KB dumped, searched for Assign/Personnel/Employee/Work Team/
"Schedule Now"/"Add Schedule" — only the same pre-existing Employee/Work Team toggle and the
still-empty Filter checkboxes turned up). The actual employee-assignment interaction is most likely
a direct click/drag on the Service Personnel calendar row itself (per the original doc: "click on
the employee... click on the calendar → event details pop up") — but this STILL requires the
Employee Job Type / Work Team filter lists to be non-empty, and they remain empty even now, with a
real, genuine, unscheduled Service Order in the system. This rules out session 1's "maybe there's
just no real order yet" theory — the copy environment's Work Team/Job Type master data gap looks
increasingly like the actual blocker, independent of having real orders to schedule.

**Updated next steps**:
1. **Stage B.5 (Variation Order confirm) is real, scoped, and ready to automate** — selector is
   known (`title="Confirm"` button on an opened "Under Variation" quotation, then `role=button
   name="Yes"` on the resulting confirm dialog). This should be added to `synergix_driver.py` (e.g.
   `_confirm_variation_order(quo_id)`) and wired into `write()` right after `_submit_quotation()`,
   gated by `DRY_RUN` like every other write step, before Stage C automation is attempted — Stage C
   has nothing to act on without it.
2. Confirm whether production Synergix has real Work Team/Job Type master data — this remains the
   open blocker for seeing the actual employee-assignment interaction, now confirmed independent of
   having a real order present.
3. Try directly clicking/dragging on a Service Personnel calendar row cell once Work Team data is
   available, rather than clicking the order row (which opens "Order Details" but has no visible
   assign action of its own).
4. Test WOs created this session that still need cleanup/decision: `QUO0006749` (confirmed → now
   `SV00008852`, sitting unscheduled) and `QUO0006750`-`QUO0006753` (still sitting in "Under
   Variation", not yet confirmed) — all against `copy.taskhub.ls2.sg` only, no production impact.

## Stage B.5 implemented and live-tested — 2026-08-25, session 3

`_confirm_variation_order(quotation_no)` added to `synergix_driver.py`, wired into `write()` right
after `_submit_quotation()`, gated by `DRY_RUN` via the existing `_dry_guard` helper. A failed or
skipped confirm reports `WOStatus.PARTIAL` (not `PROCESSED`) with the quotation id in the detail
message — the quotation is real and submitted, but a human must finish the confirm manually before
it becomes a schedulable Service Order. 5 new regression tests in
`tests/test_variation_order_confirm.py`, verified non-tautological (4 of 5 genuinely fail against
the pre-fix code, confirmed by reverting via `git stash` and re-running). 111 passed, 22 skipped.

**Two real bugs found and fixed during live verification** (both the same root cause already
documented elsewhere in this file — a plain `.first` locator grabbing a hidden/wrong copy of a
status-tab-scoped element, the exact class of bug `check_duplicate` and the Schedule Board discovery
session both hit): the initial version's `page.locator("th", has_text="Quotation No.")` and
`page.get_by_text(quotation_no, exact=True)` calls were not scoped to `:visible`/`visible=true`, so
they intermittently matched the DRAFT tab's hidden copy of the same grid instead of the active
"Under Variation" tab's visible one, causing spurious 30s timeouts and false "still under Under
Variation" verify failures. Fixed by scoping both to the visible element, and by adding a
recheck-before-declaring-failure on the verify step (same flicker-tolerance pattern used throughout
this file). Also added: treating "quotation not found under Under Variation at all" as ALREADY
CONFIRMED (success) rather than failure, since re-running the confirm on an already-confirmed
quotation is not an error — confirmed live this happens naturally on a retry.

**Live verification results, `copy.taskhub.ls2.sg`**: after the fix, re-ran against `QUO0006750`-
`QUO0006753` (left over from session 2) and separately ran the FULL `write()` path end-to-end on a
fresh WO (`WO-PO/000076646` → `QUO0006761`). Ground truth checked directly in Synergix after each
run (not just trusting the returned status):
- `QUO0006750`, `751`, `752`, `753` all confirmed successfully at some point across the session —
  verified live as real Service Orders (`SV00008852`-`SV00008855`) sitting in Schedule Board's
  Unscheduled Service Orders grid (count 44→49 across the session).
- `QUO0006761` (the fresh full-`write()` run) reported `PARTIAL` — verified this was a GENUINE
  failure, not a false negative: opening `QUO0006761` from "Under Variation" shows its toolbar has
  only 4 icons (undo, +, envelope, paperclip) with NO pencil/edit and NO Confirm (check-double) icon
  at all, unlike `QUO0006749`'s successful open which had 5 icons including Confirm. So the Confirm
  button is not always present/available on an "Under Variation" record — the write() code correctly
  detected this (`no Confirm button on %s` logged) and reported PARTIAL rather than silently
  succeeding or crashing. **Root cause of WHY the Confirm button is sometimes missing is still
  unknown** — candidates not yet investigated: an approval-workflow gate, a required-field check
  Synergix enforces before showing Confirm, a per-record permission state, or simply a longer settle
  delay needed after opening the record than the current 4s wait allows. This needs another live
  session specifically diffing a "has Confirm" vs "missing Confirm" record's full state (Payment
  Method, Customer Contact, GST, etc.) to find what differs.

**Still blocked, unchanged from session 2**: Employee Job Type / Work Team filter lists remain
empty on this copy environment even with 5+ real confirmed Service Orders now sitting unscheduled,
so the actual employee-assignment calendar interaction has still never been reached.

## Stage C unblocked — 2026-08-25, session 4: real sequence found, Event Details popup reached

Both blockers from sessions 2-3 turned out to be MY mistakes, not real environment gaps:

1. **"Employee Job Type" checkboxes were never actually empty.** They were rendering correctly the
   whole time; my screenshots were just cropped before the (further-down) real "Employee" checkbox
   section, and I was toggling "Work Team" (genuinely empty on this environment) instead of
   "Employee" (has real names: 800SUPER, CADILLAX, ECOCARE, GREENCARE, HERBERT LIM PIN HENG, INFIGO,
   NEWS, SUSAN LEE SUN SUN, TAN WEI YING, TAN ZHEN YUAN BENEDICT, TOMMY TOH GIM POR, VINCENT TEO BOON
   WAH, VISHAL ANAND SINGH). Confirmed by dumping the raw HTML instead of trusting a cropped
   screenshot -- the checkboxes were always there in the DOM.
2. **The click sequence was wrong.** Clicking directly on a calendar cell (even with an employee
   checked) does nothing -- confirmed live, repeatedly, no popup ever opened this way. The user
   corrected this: the real sequence is
     (a) filter "Unscheduled Service Orders" by Customer (e.g. "jalan"),
     (b) click the SPECIFIC ORDER ROW to select it (it highlights blue),
     (c) THEN click a calendar cell in the employee's row.
   Only with an order selected first does the click open anything -- the calendar cell click is
   scoped to "schedule THIS selected order", not a bare "create an event" action. This matches the
   workflow doc's own phrasing more literally than earlier sessions gave it credit for: "Type
   customer name to search... click on the employee... click on the calendar" is describing this
   same select-an-order-first flow, just abbreviated.

**The Event Details popup, confirmed live** (screenshot from the user, real click on a real order):
- Title "Event Details"
- **From** / **To** date fields (both defaulted to a date already, e.g. 27/08/2026 -- NOT necessarily
  today or the WO's job date; needs to be verified/set to the WO's actual job date per the workflow
  doc's "Date should be WO date")
- **Assigned** -- a dropdown, pre-filled with the employee whose row was clicked (TAN WEI YING here)
- **Paired With** (read-only/display) and **To Pair With** (a picker, empty/greyed in this screenshot
  -- this is almost certainly where ECOCARE/INFIGO gets selected per the workflow doc's "choose
  ECOCARE or INFIGO from the dropdown", not yet confirmed live)
- **Remarks** -- a free-text box, empty
- A checkmark (✓) button (confirm/submit) and an X button (cancel), bottom-left of the dialog

**Not yet done**: actually filling and submitting this popup (setting To Pair With, remarks, clicking
the checkmark) -- next session should pick up here, filling and submitting on one of the confirmed
test Service Orders (SV00008852-SV00008855) to see the real post-submit state, then move to Stage D
(Service Order Performance -> Billables -> Attachments -> Fulfil).

## Stage C: the real click target found, Event Details popup opened programmatically — session 4 cont'd

Two automated click attempts on the calendar cell (`div_new_event`, both plain and `force=True`)
silently did nothing -- no popup, no error on the forced one, a "`<td> intercepts pointer events`"
error on the plain one. The user found the missing step by hand: collapsing the RIGHT-SIDE PANEL
(the "»" double-arrow toggle, top-right of the screen, which hides "Customer Info"/"Order Details")
before the calendar becomes properly clickable -- that panel was narrowing/interfering with the
calendar's layout in a way that broke click targeting, even though it visually looked like it
wasn't overlapping the calendar cells.

With the user's real click as a reference, dumping the DOM at that moment revealed the ACTUAL click
target: each calendar cell's visible `div_new_event` (`cursor:pointer`, but NOT the real target) has
a full-size INVISIBLE overlay sitting on top of it --
`<button ... title="Click to add event" style="all: unset; position: absolute; width: 100%; height:
100%; ...">`, with id suffix `...newEventButton`. This button, not the div, is what PrimeFaces binds
the "open Event Details" ajax action to. Clicking `[id*="...newEventButton"]` directly (ordinary
click, NO force needed) opened the Event Details popup cleanly and immediately -- confirming this
is the real, correct selector. The earlier "`<td> intercepts pointer events`" error was Playwright
correctly detecting this exact overlay button sitting on top of the div, just misidentified as an
obstacle rather than the actual target.

**Event Details popup, full field set confirmed live** (opened on a real row, "800SUPER", not
cancelled without submitting -- verified after re-opening the browser that the Unscheduled Service
Orders count was unchanged at 50, so nothing leaked through):
- **From** date + time, **To** date + time (each a separate date input + time input, not one
  combined field) -- defaulted to the clicked cell's own date/hour (25/08/2026 08:00-08:30 for the
  08:00 column cell clicked here).
- **Duration** (hours, numeric, e.g. `0.5`) -- auto-computed from From/To, editable.
- **Assigned** -- a dropdown, pre-filled with whichever employee's ROW was clicked (confirms the
  workflow doc's "click on the employee" is about picking the ROW, then the cell within that row).
- **Paired With** (label only, greyed out, no value here) and **To Pair With** -- a full checkbox
  list of ALL employee names from the Employee filter (CADILLAX, ECOCARE, GREENCARE, HERBERT LIM PIN
  HENG, INFIGO, NEWS, SUSAN LEE SUN SUN, TAN WEI YING, TAN ZHEN YUAN BENEDICT, TOMMY TOH GIM POR,
  VINCENT TEO BOON WAH, VISHAL ANAND SINGH). **This confirms ECOCARE and INFIGO are real, live
  checkbox options here** -- exactly matching the workflow doc's "choose ECOCARE or INFIGO from the
  dropdown" (it's a checkbox list, not a literal dropdown, but same selection intent).
- **Remarks** -- free-text box, empty by default.
- **✓** (confirm/submit) and **✗** (cancel) icon buttons at the bottom, no visible labels -- their
  exact selectors not yet captured (need a fresh DOM dump while the popup is open, scoped to this
  button pair specifically; the dialog's title-bar red X (`aria-label="Close"`, class
  `ui-dialog-titlebar-close`) is a THIRD, separate way to dismiss without submitting -- confirmed
  used for this session's own cleanup by just closing the whole browser/page instead).

**Confirmed working automation sequence for Stage C, up to (not including) filling+submitting**:
1. Filter "Unscheduled Service Orders" by Customer.
2. Click the specific order's ROW to select it (highlights blue) -- REQUIRED before anything else
   works; clicking a calendar cell with no order selected does nothing.
3. Click "Employee" toggle (NOT "Work Team" -- that list is genuinely empty on this environment).
4. Click "Filter" to expand the Employee Job Type / Work Team checkbox panel.
5. Check the target employee's checkbox (their calendar row then appears).
6. Click "Filter" again to collapse the panel.
7. **Collapse the right-side info panel via the "»" toggle** (top-right of the whole screen) --
   without this, calendar clicks silently fail to register even though nothing visually blocks them.
8. Click the specific cell's `[id*="newEventButton"]` (NOT the visible `div_new_event` inside it) in
   the target employee's row, at the desired date/hour column.

**Not yet done**: filling in Date=WO date (currently defaults to the clicked cell's date, which may
not match the WO's actual job date -- per the workflow doc "Date = WO date" this likely needs
explicit correction, not just accepting the default), checking the correct To Pair With box
(ECOCARE vs INFIGO, per `resolve_project_code`'s existing alphabetic/numeric Job Sheet logic already
used for Stage B's Project Site), filling Remarks, and clicking the real ✓ submit button -- then
verifying the resulting state (does the order move out of "Unscheduled"? does a new status appear?).
Also not yet done: Stage D (Service Order Performance -> Billables -> Attachments -> Fulfil) and the
finance-notification email the client originally asked for, which per earlier discussion should fire
after Stage D's real Fulfil action (workflow-doc step 28), not before.

## Stage C completed end-to-end, live-verified — session 4 cont'd (2026-08-25)

Filled and submitted the Event Details popup fully, on the real test order `SV00008852`
(`WO-PO/000076625`, `QUO0006749`), against `copy.taskhub.ls2.sg`, with explicit user sign-off before
the final Submit click:

**Field-filling notes (real quirks worth remembering)**:
- The generic label-based `fill` helper works fine for **Remarks** (a plain textarea) but NOT for
  **From**/**To**, which are split into a DATE sub-field (its own PrimeFaces datepicker, opens a
  calendar popup on interaction) and a separate TIME sub-field with a different, unrelated id
  fragment. Typing into the date input directly (bypassing the calendar UI) DOES stick, but opens
  the calendar popup as a side effect -- close it via the picker's own "Close" button, which does
  NOT clear the typed value (confirmed safe, unlike pressing Escape which was untested).
- **The time field resets to `00:00` every time a DIFFERENT field in the same popup is filled after
  it** (confirmed live, repeatedly) -- the whole form re-renders on each individual field's ajax
  round-trip, and the time input doesn't survive that reprint. Practical implication for automation:
  fill the TIME field LAST, immediately before clicking the submit checkmark, not earlier in the
  sequence -- filling it early is wasted work.
- **"To Pair With" is EMPTY for some employees** (confirmed live: TAN WEI YING's popup had zero
  checkboxes) but FULL for others (800SUPER's popup listed all 12 names including ECOCARE/INFIGO).
  Not yet understood why -- candidates: employee-specific config, or it's simply not applicable when
  the order's own Project Site already encodes Ecocare/Infigo (as it does here via
  `resolve_project_code`), making the popup's own pairing redundant for a JBTC/SKTC adhoc order.
  Confirmed live that submitting with "To Pair With" left empty (no boxes even existed to check)
  works fine.

**Real selectors captured**:
- Event popup's real submit ("✓ checkmark"): NOT a `role=button name=Yes`-style dialog button --
  it's `<button ... class="ui-button-icon-only"><span class="fa-check">` with a captured id fragment
  `..._379045a4` this session (regenerates per session; locate via the "Remarks" textarea's nearby
  `<div class="synfaces-button-bar">` sibling, first button = submit/check, second = cancel/✗ which
  explicitly calls `PF('widgetEditSchedule').hide()`).
- After the Event Details ✓ submit, the underlying **"Order Details[<SV number>]" panel** gains a
  new **"Schedule"** section showing the assigned employee + confirmed From/To times, AND a new blue
  **"Submit"** header bar appears above "Order Details" -- this is the ACTUAL Stage C submit action,
  separate from the Event Details popup's own checkmark. It requires the order to be re-selected
  (click its row again) if the grid was reset by an intervening ajax refresh -- confirmed live that
  after filling Event Details, the Unscheduled Service Orders grid silently reset to page 1 with the
  customer filter cleared, and the Submit button read `disabled="disabled"` until the order was
  re-selected.
- This "Submit" bar's button fires a `PrimeFaces.confirm({message:"Are you sure?"})` dialog (Yes/No,
  same shape as the Stage B.5 Variation Order confirm) -- clicking Yes is the real, final Stage C
  submit.

**Live verification of success** (not just trusting a status message): after clicking Yes,
(1) the "⚠️ This Service Order has not been submitted" banner disappeared from Order Details,
(2) the panel header reverted from "Submit | Order Details[...]" back to plain "Order Details[...]"
(no more pending submit action), (3) the "Schedule" section persisted (TAN WEI YING, 23/04/2026
08:00-08:30), and (4) most conclusively, the top-right **"Upcoming Service"** panel (previously
always "No records found." all session) now shows a real entry: "25/08/2026, SV00008852 -- JALAN
BESAR TOWN COUNCIL - Blk 113 MCNAIR ROAD ... WO-PO/000076625." This is independent, cross-panel
confirmation that the schedule genuinely persisted server-side, not just a client-side form state.

**Confirmed full working sequence, Stage C end to end**:
1. Filter "Unscheduled Service Orders" by Customer; click the target order's row to select it.
2. Employee toggle -> Filter -> check the target employee via `label[for*="<id>"]` CSS click (NOT
   the text= locator -- confirmed live it can report "clicked" successfully while the checkbox
   stays unchecked; verify via the checkbox's `ui-icon-check`/`ui-icon-blank` class in the DOM, don't
   trust the click's own success message) -> Filter again to collapse.
3. **Set the browser viewport wide (1920x1080 confirmed working)** -- this achieves the same
   practical effect the user's manual "collapse the right panel via the »/toggler icon" trick did
   (freeing enough calendar width that clicks register), without needing to fight that toggler
   element itself (two automated attempts on it failed: a plain click reported "not visible", a
   forced click reported success but never changed the panel's CSS classes -- likely a
   resizer-handle widget that needs real mousedown/drag semantics, not investigated further since
   the viewport-resize workaround was simpler and confirmed sufficient).
4. Click `[id*="...newEventButton"]` (NOT `div_new_event`) in the target employee's row/column.
5. In the Event Details popup: fill Remarks and the From/To DATE fields via the generic label-fill
   helper; leave TIME fields for last (they reset on every other field's ajax refresh); check a
   "To Pair With" box only if any exist for this employee; click the ✓ button.
6. Re-select the order row (the grid likely reset) and click the now-enabled blue "Submit" bar above
   Order Details; confirm the "Are you sure?" dialog with Yes.
7. Verify via the "Upcoming Service" panel showing a new dated entry for the order, NOT just the
   absence of the "not submitted" warning (that alone was already gone one step earlier at the Event
   Details ✓, before the real Stage C Submit had even been clicked -- it is not sufficient evidence
   on its own).

**Left in Synergix from this session** (`copy.taskhub.ls2.sg` only): `SV00008852` /
`WO-PO/000076625` is now a fully scheduled AND submitted Service Order (TAN WEI YING, 23/04/2026
08:00-08:30). `SV00008853`-`SV00008855` remain unscheduled (Stage C not yet run on them). `QUO0006761`
(`WO-PO/000076646`) remains stuck at Stage B.5 (missing Confirm button, per session 4's earlier
finding -- unrelated to Stage C, not touched this session).

**Not yet done**: Stage D (Service Order Performance -> Billables -> Attachments -> Fulfil) --
`SV00008852` is now a real candidate to try this on, being the first order in this whole project
that both a submitted quotation AND a submitted schedule exist for. Also not yet done: writing this
newly-confirmed sequence into `synergix_driver.py` as real driver code (everything so far has been
manual discovery-script commands, not committed automation) -- and the finance-notification email
the client asked for, still correctly deferred until Stage D's real Fulfil action exists (per
earlier discussion, workflow-doc step 28).

## Stage B.5 + Stage C committed as real driver code; B.5 "missing Confirm button" investigated further

`_schedule_stage_c()` (Stage C) written into `synergix_driver.py` from the confirmed sequence above,
wired into `write()` right after `_confirm_variation_order()` (Stage B.5), same PARTIAL-on-failure
safety pattern as every other stage. Also set the browser's viewport explicitly to 1920x1080 at
launch in `start()` (previously unset, defaulting to Playwright's 1280x720) -- this is what makes
Stage C's calendar clicks work reliably without depending on the right-panel-collapse toggle, which
two separate automated attempts failed to trigger. 8 new regression tests across
`tests/test_variation_order_confirm.py` and `tests/test_schedule_stage_c.py`, all verified
non-tautological (8/9 relevant tests genuinely fail against the pre-fix code via `git stash`).
115 passed, 22 skipped.

Live end-to-end test of the newly-committed code (not just discovery-script commands) on a fresh WO
(`WO-PO/000078229` -> `QUO0006769`) hit the SAME "missing Confirm button" issue found earlier on
`QUO0006761` -- confirming this is a real, reproducible gap independent of any of today's Stage C
work, not a one-off. Investigated further this session:
- Compared `QUO0006769`'s full visible record (Customer, Contact, Details row, Payment/Segment
  fields, Project Site/In-Charge/Portfolio/BU) against a working record -- found NO visible
  difference. Everything looks completely normal and fully filled.
- Confirmed via raw DOM search that `fa-check-double` (the Confirm icon's class) and
  `title="Confirm"` are ABSENT from the page entirely, not just hidden/disabled -- this is not a
  visibility/timing bug, the button element itself does not exist in this record's rendered toolbar.
- A long in-place wait (8s) did not make it appear.
- A full re-navigation (fresh page load, not just a re-render) did not make it appear either.
- Added a reload-and-retry loop to `_confirm_variation_order` (reload the record fresh, up to 2
  extra attempts) on the theory that a transient render glitch might resolve on a clean reload --
  tested live against the still-stuck `QUO0006769`: the retry loop ran correctly (2 attempts logged,
  clean give-up) but did NOT fix it, confirming this is a genuinely persistent per-record state, not
  a timing issue. The retry is kept anyway as a safety net for whatever DOES turn out to be a
  transient case, at low cost, but it is not, by itself, a fix.

**Root cause remains unknown.** Candidates not yet investigated: an approval-workflow gate keyed off
something not visible on the General/Segment tabs (e.g. a hidden validation rule), a per-record
permission/ownership check, or a difference in how the quotation was created (all stuck records so
far were created via the same automated from-scratch flow as working ones, so this seems unlikely
but hasn't been ruled out by comparing e.g. exact creation timestamps or session state). Given the
client's immediate need to test the pipeline, this was deliberately NOT blocked on further
root-causing -- the pipeline already handles this failure safely (real submitted quotation, clear
PARTIAL status naming the exact quotation id, human finishes manually), so shipping now with this
known, safely-contained gap was the agreed tradeoff over continued investigation.

## Stage D discovery — 2026-08-26: Billables/Actual-Qty found and fixed a real under-billing risk

Navigated to **General Service -> Service Order Performance - LS2** (a plain grid list, no
duplicate-status-tab complexity like Stage B/C) and found the completed test order from Stage C,
`SV00008852` (`WO-PO/000076625`), sitting at the top with status **"Pending For Performance"** and a
**"Fulfil"** button directly in the grid row -- much simpler nav than Stage C.

Opening it shows a detail page with three tabs matching the workflow doc exactly: **Item Serviced**,
**Billables**, **Service Personnel Details**. On the **Billables** tab, the row showed **Quoted Qty
1.00** (correct, from the quotation) but **Actual Qty 0.00**, and every total (Total Amount, Sales
Tax Amount, Total After Tax Amount) read **0.00** -- despite the quotation itself being correctly
priced at 44.00 + GST = 47.96. This is a real, previously-undocumented risk matching the doc's own
"Check the billable" instruction literally: **fulfilling without first setting Actual Qty would bill
the customer $0.00**, not the correct amount. Filling Actual Qty to match Quoted Qty (1.00) and
pressing Enter (to fire the recalculation ajax) correctly recomputed the totals to 44.00 / 3.96 /
**47.96** -- exactly matching the original WO's authorised total, confirmed live.

**Selectors found**:
- Actual Qty input: relationally, it's the editable (`aria-readonly="false"`) text input with class
  `synfaces-align-right qty` immediately preceding the row's "Options" dropdown cell -- NOT the
  adjacent Quoted Qty cell (which looks identical but is genuinely readonly) and NOT the Unit
  Price/Exchange Rate inputs elsewhere on the page which also default to similar-looking values.
- Attachments: a `title="Attachments"` tab in the same right-rail icon-tab pattern used throughout
  this app (General/Payment/Shipment/Remarks/Attachments), NOT a paperclip icon on this particular
  page (unlike the Stage B quotation form, which does have a paperclip icon for the same purpose).
  Opened it live: shows an empty "Attachments" panel with a "+" to add a file and "No records
  found." -- NOT yet wired up (needs an actual file upload flow, not attempted this session).
- The real Fulfil/Submit action: `title="Submit"` button (id ending `submitButton`, icon
  `fa-vote-yea` -- the SAME icon class already used for Stage B's Submit and Stage C's Submit),
  triggering the same `PrimeFaces.confirm({message:"Are you sure?"})` Yes/No dialog pattern used at
  every other stage-ending confirm in this app.

**Attempted live submit, with explicit user sign-off, WITHOUT an attachment** (deliberately skipping
that step for this test, to be wired up separately) -- clicked Submit, confirmed "Are you sure?" with
Yes, and the page hung on its loading indicator, then the Synergix session expired outright
("You've left your browser idle for too long and your page has expired"). This is a genuine
uncertain-outcome case: rather than assume either success or failure, re-logged in fresh and
checked `SV00008852` directly in Service Order Performance's list. **Ground truth: still showing
"Pending For Performance" with the Fulfil button still present, list count unchanged at 41** -- the
Submit did NOT go through before the session died. Nothing was lost or corrupted; the record is
exactly where it was before the attempt, still fully fulfillable. This was NOT investigated further
this session (why the session expired specifically during this action, whether it's related to the
same session-timeout-during-a-long-operation class of issue already documented elsewhere in this
project for Stage B) -- left as a clean, safe stopping point.

**Not yet done**: retry the Fulfil submit (the record is untouched and ready); wire up the
Attachments file-upload step before submitting "for real" per the doc's literal instructions; write
Stage D into `synergix_driver.py` as committed driver code (everything this session was manual
discovery-script commands only, matching how Stage C started before being committed); and the
finance-notification email, still correctly gated on a real, successful Fulfil action existing.

**Test WOs left in Synergix from this session** (all `copy.taskhub.ls2.sg`, no production impact):
`QUO0006749`-`QUO0006753` confirmed → `SV00008852`-`SV00008855` (unscheduled, real). `QUO0006761`
(`WO-PO/000076646`) submitted but NOT confirmed — missing Confirm button, needs the investigation
above before it can be retried.

## Session 5 (2026-08-26): re-testing the committed Stage C code found real bugs; one genuine
## intermittent Synergix widget issue remains open

Per the user's request, re-ran the newly-committed `_schedule_stage_c` end to end against real WOs
(`scripts/run_stage_c_direct_test.py`, calling the driver method directly against an already
Variation-Order-confirmed Service Order) to verify the code actually works, not just that it was
written correctly from session notes. It did not, on the first attempt — writing the function from
memory of the live discovery session had drifted from what actually worked. Nine live iterations
found and fixed the following real bugs, each confirmed against actual DOM captures rather than
guessed:

1. **`_fill_labeled_input`'s `<tr>`-only lookup couldn't find the Event Details popup's fields at
   all** (it uses a div-based `.synfaces-grid-item` layout, no `<tr>` ancestor exists in that
   dialog). Fixed by widening the shared helper to try `.synfaces-grid-item` first, falling back to
   `<tr>` — additive, the two pre-existing `<tr>`-based callers (Enquiry/Subject, Reference No.) are
   unaffected.
2. **The time-sub-field JS used `.closest('td')`**, but the popup has no `<td>` at all (same
   div-based layout as #1). Rewritten to use `Locator.fill()` (not a raw JS value-setter — this
   file's own established rule, see `_fill_grid_field`'s docstring) scoped to `.synfaces-grid-item`.
3. **The Employee/Work Team toggle click can report success via a Playwright Locator while the
   button visibly stays on "Work Team"** — confirmed repeatedly, not a one-off, across both
   `get_by_text(exact=True)` and generic `text=` forms. Fixed by clicking the underlying `.ui-button`
   div via `page.evaluate(...).click()` directly (bypassing Playwright's click/event simulation
   entirely) and verifying `ui-state-active` before proceeding, retrying up to 3 times.
4. **The toggle can drift back to "Work Team" between passing that check and the checkbox lookup**,
   with no single action caught doing it. Fixed by re-asserting Employee is active on every poll
   iteration of the checkbox search, not just once up front.
5. **The Event Details popup's own confirm button (`button:has(span.fa-check)`) and the time-field
   lookup both queried the WHOLE page**, and `fa-check`/label-text can collide with an unrelated,
   hidden dialog's own button elsewhere on the page (confirmed live: a real crash where Playwright
   waited 10s for a hidden `id="j_idt969"` "Yes" button from a completely different confirm dialog to
   become visible). Both are now scoped to inside the Event Details dialog specifically, found via
   `[role="dialog"]:has-text("Event Details")`.
6. **The confirm click can leave the dialog open** (its modal overlay then blocks every subsequent
   click for a full 30s timeout each). Now waits for the dialog to actually close, retrying the
   confirm click once before giving up.
7. **The Order Details "Submit" button can stay `disabled` immediately after re-selecting the order
   row.** Now polls for it to become enabled, re-selecting the row once more if it doesn't.

After all seven fixes, a live run got all the way through Employee-toggle, checklist, calendar
click, and Event Details fill/confirm on `WO-PO/000076627` — the one failure at that point was
Synergix's OWN validation, correctly rejecting a double-booking ("SV9104: 800SUPER: You can only
book one task on the same Timeslot 23/04/2026 under current service order") against an
already-scheduled record left over from an earlier session. That is the pipeline working correctly,
not a bug.

**One genuine, still-open issue**: re-testing immediately afterward against a completely clean WO
(`WO-PO/000078228`, no prior booking on its date) failed again at the SAME employee-checklist step
— this time with the Employee toggle correctly active (confirmed via screenshot) but the checklist
itself still rendering empty. This means fix #3/#4 (verifying/re-asserting the TOGGLE's active
state) is necessary but not sufficient — the checklist's own population is a separate, apparently
still-intermittent failure mode not yet fixed. It has now been observed empty in at least four
distinct live sessions across this project (session 2, session 4, and twice in session 5), each time
resolving eventually after some combination of waiting, re-toggling, or a fresh page load, without a
single reliable trigger identified. This is the same category of unexplained intermittency as
Stage B.5's missing Confirm button (see above) — both are now known, open, safely-contained gaps
(the pipeline reports PARTIAL with a clear reason rather than silently failing or crashing) rather
than fully solved.

**Not yet done**: root-cause the checklist-population intermittency (candidates: a slower ajax
backend call than any wait tried so far, a session/browser-state factor, or a genuine Synergix
front-end bug); wire up the Attachments upload; write Stage D into `synergix_driver.py` as committed
code once Stage C's remaining gap is resolved or explicitly accepted as a known limit.

## Stage D Fulfil submit: completed live (2026-08-26)

Per the user's explicit request, retried the Fulfil submit on `SV00008852` from a fresh session
(the earlier attempt this pull had died mid-click when the Synergix session expired, leaving the
record untouched -- see the "Ground truth" note above). This time: opened the record, confirmed
Actual Qty was still 0.00 (the Billables edit from the earlier attempt had NOT persisted, consistent
with the session having expired before anything committed), re-filled it to 1.00 (recalculating
correctly to 44.00 / 3.96 GST / 47.96 total, matching the WO's authorised amount), clicked the real
Submit button (`title="Submit"`, id ending `submitButton`, icon `fa-vote-yea`), confirmed the
resulting "Are you sure?" dialog with Yes, and waited a full 8s (longer than the previous attempt's
wait, on the theory that impatience contributed to hitting the session-expiry window before).

**Verified via ground truth, not just the absence of an error**: searched Service Order Performance
directly for `SV00008852` with the Service Order Status filter set to **"All"** (not just the
default "Pending For Performance" view) -- returned **"No records found."** The record has left this
list entirely, in any status, and the list's own count dropped from 41 to 40. This is the strongest
available signal within this screen that the Fulfil action genuinely completed and the order moved
downstream (out of "requires performance action" -- consistent with progressing toward billing,
though this session did not chase down which exact screen/status it landed in next, e.g. a Sales
Order or Invoice view outside Service Order Performance's own scope).

This is the first work order in this entire project to be confirmed live through all four stages:
Stage A (WO retrieval) -> Stage B (quotation create+submit) -> Stage B.5 (Variation Order confirm)
-> Stage C (Schedule Board assign+submit) -> Stage D (Fulfil submit) -- `WO-PO/000076625` /
`QUO0006749` / `SV00008852`.

**Not yet done**: Attachments (the WO PDF) were deliberately skipped again this attempt, per the
user's earlier explicit sign-off to test Fulfil without them first; write Stage D into
`synergix_driver.py` as committed driver code (this was manual discovery-script commands only, same
gap noted for Stage C before it was committed); confirm exactly which downstream screen/status
`SV00008852` landed in after leaving Service Order Performance's own list.

## Session 6 (2026-08-26): Stage C fails 8/9 live attempts across 4 different WOs -- root cause is
## deeper than any single selector

Attempting to get a SECOND WO through Stage C (to unblock testing Stage D on a fresh order rather
than the one already-completed `SV00008852`) surfaced that the employee-checklist/calendar issue is
far more persistent than session 5's fixes addressed. Across `WO-PO/000078228`, `000076639`,
`000076640` (multiple attempts each) and a re-run of `000078228`, only ONE Stage C attempt has ever
succeeded in this project's history (`WO-PO/000076625`, the very first one tried). All 8 further
attempts today failed, in three distinct failure shapes -- fixing each one uncovered the next:

1. **Checklist stays empty** even through the forced Work-Team<->Employee refresh and a full
   re-navigation (session 5's fixes) -- confirmed this is not always transient; it can persist
   through the ENTIRE ~20s+ retry budget on a given attempt.
2. Once the checklist error was fixed to move past that: the click on `[id*="newEventButton"]`
   (unscoped, `.first`) was found to land on a DIFFERENT employee's row than the one just checked
   (confirmed live via a "SV9104: 800SUPER: You can only book one task..." Synergix validation error
   naming the wrong employee). Fixed by scoping the click to the specific `<tr>`s under the checked
   employee's own `rowspan`-ed name cell, plus a new post-click check that the popup's own "Assigned"
   dropdown actually shows the target employee, aborting cleanly (closing the dialog, returning
   `False`) if not.
3. Even with that fix in place: found live that the "Service Personnel" calendar section can render
   with literally NO rows at all -- not even the wrong employee's row, nothing -- while the Employee
   toggle sits correctly active and the checkbox search successfully finds and checks the target
   employee. This is a THIRD, independent ajax call (checklist population, row rendering, and
   apparently a click target that can still resolve to something even when no row is visibly
   present) that can fail on its own. Added a dedicated poll for the employee's name cell to actually
   appear in the calendar DOM before attempting any click on it.

After all three fixes, re-tested three more times across two different WOs: one attempt still hit
the original "wrong employee" abort (meaning the row-scoping fix's own row-boundary detection is
not yet fully reliable either), and one hit the original empty-checklist failure again. **Net
result: 0 successful Stage C completions in 9 live attempts this session**, despite each fix being a
real, verifiable improvement over what came before (confirmed via distinct failure signatures at
each stage, not the same crash recurring).

**Conclusion**: this is very likely a genuine, non-deterministic issue in Synergix's own Schedule
Board widget -- three separate, independently-flaky ajax populations (checklist, calendar rows, and
whatever the click handler resolves to) stacked in sequence, each with its own failure window. No
amount of additional client-side polling/retry logic tried so far has converged on a reliable fix.
Per the user's explicit decision this session, further live debugging of Stage C was paused here in
favour of writing Stage D as committed code and verifying it standalone (against the one Service
Order that IS confirmed scheduled, `SV00008852`, already fulfilled -- so a fresh target will be
needed) rather than continuing to burn time on Stage C convergence.

**Recommendation for whoever picks this up next**: consider whether Stage C should have a
much longer overall timeout with fewer, larger-grained retries (e.g. re-navigate + wait 15-20s
fresh, rather than many short polls within one page load), since the fresh-navigation fallback was
only tried ONCE per failure in this session's code, not looped. Also worth asking the client
directly whether Synergix's Schedule Board is known internally to be slow/flaky under their own
manual use -- if their own staff also see this, it may not be fixable from the automation side at
all, and Stage C may need to stay a manual, best-effort step indefinitely (matching the original
work order's own description of it as "most fragile").

## Session 6 continued: Stage C checkbox fix + Stage D live end-to-end success

While manually driving toward a real Stage D submit (since Stage C's automation could not be relied
upon that day), found that `SV00008856` (WO-PO/000076627) had been left in a real, partially
scheduled state (`800SUPER`, 23/04/2026) by an earlier aborted "wrong employee" attempt -- a
previously undocumented gap: the abort path returns `False` but does not roll back whatever the
Synergix backend already committed server-side. Not fixed this session, only noted here for
whoever picks up Stage C's abort-cleanup next.

Manually completed `SV00008856`'s Stage C submit live, discovering along the way (via the discovery
REPL's `js` command, watching `document.querySelector("[id*=submitButton]")?.disabled` flip
`True` -> `False`) that clicking the order row's own `ui-chkbox` checkbox -- NOT the row or its link
text -- is what actually enables the Order Details Submit button. This is now fixed in
`_schedule_stage_c` (`src/synergix_driver.py`): the re-selection step ticks the row's checkbox via
`.ui-chkbox-box`, both on the first attempt and the retry-once-more fallback.

Then drove `SV00008856` through a full, real Stage D Fulfil live: Billables' Actual Qty defaulted to
0.00 (same as `SV00008852` earlier), filled to match Quoted Qty (1.00), Total After Tax Amount
recalculated correctly to 47.96. Attachments: found the "+" icon next to the file panel is actually
"New Folder" (clicking it opens an inline rename row, NOT a file picker) -- confirmed the real
upload path is the panel's own `input[type=file]`, already present in the DOM once the Attachments
tab opens, usable directly via `set_input_files()` with no button click needed at all. Clicked
Submit, confirmed "Are you sure?" with Yes, and verified via ground truth: filtering Service Order
Performance's Order No column for `SV00008856` with Service Order Status left at "All" returned "No
records found" -- the strongest available signal, since the record disappears from the list
entirely once Fulfilled rather than merely changing status within it.

**Both fixes are now committed** in `_fulfil_stage_d` and `_schedule_stage_c`
(`src/synergix_driver.py`), and Stage D is wired into `write()`'s call chain after Stage C succeeds
-- `write()` no longer stops at "Fulfil still manual"; a fully successful WO now returns
`WOStatus.PROCESSED` only after Stage D's own Submit is confirmed via the same ground-truth check.
The Order No filter lookup used for that ground-truth check was also fixed: the original
`th:visible has-text("Order No")` -> descendant-input lookup timed out live (30s, no match); fixed
to `page.get_by_label("Filter by Order No")`, which matches the input's real accessible label and is
more robust to this datatable's specific header markup.

Net result this session: full pipeline A -> B -> B.5 -> C -> D proven live end-to-end on a real
Service Order (`SV00008856` / `SV00008852` earlier), not a draft, with every step's completion
verified via ground truth rather than an absence of errors. Stage C's underlying flakiness
(documented above) is unchanged and still best-effort; the checkbox fix here only addresses the
Submit-button-stays-disabled failure mode, not the checklist/calendar-row rendering intermittency.

## Session 7 (2026-08-26/27): Stage B.5 false-negative root-caused and fixed; full A-D re-verified

After the session 6 fixes above, two full 5-WO batches were run live (`DRY_RUN=false`) to test
routine batch volume. **Both batches went 5/5 PARTIAL, every WO stuck at Stage B.5** -- a regression
from session 6's Stage B.5 working reliably. Every failure had the identical signature:
`_confirm_variation_order` found the Confirm button, clicked it, found and clicked Yes, but its own
post-click verification (re-opening "Under Variation" and checking the quotation was no longer
listed) still found the quotation present, so it returned `False`.

**Root-caused by comparing against the client's own screen recording** (`JBTC WO Synergix.mp4`,
frame-by-frame via OpenCV) and by adding diagnostic screenshots at each step of the confirm
sequence (`vo_confirm_button_found`, `vo_after_confirm_click`, `vo_after_yes_click` -- since removed
after the fix landed). The screenshot taken immediately after the Yes click on `QUO0006787` showed
the real toast: **"SA0005: Service Order No.: SV00008873 is created successfully."** -- the exact
same success signal documented in session 2 as proof of a genuine confirm. Manually checking
Schedule Board confirmed a real Service Order (`SV00008874` for a different WO tested the same way)
existed and was fully functional. Yet the quotation genuinely, reproducibly remained visible under
"Under Variation" (badge count even increased, 102->103) on every recheck afterward, including
after a full fresh navigate-away-and-back cycle -- not a rendering delay, a real and permanent
UI state in this environment.

**Conclusion**: "the quotation leaves Under Variation" is NOT a reliable signal that Confirm
succeeded in this Synergix environment, despite being exactly what session 2 originally observed
and built the check around. The SA0005 toast is the correct signal instead. **Fixed** in
`_confirm_variation_order` (`src/synergix_driver.py`): the verification step now waits for and reads
the SA0005 toast text directly, rather than re-querying "Under Variation". This was a client-code
bug (a false negative), not a Synergix bug -- Stage B.5 itself was never actually broken.

Separately, while manually finishing Stage C+D on a WO already confirmed via the fixed logic
(`WO-PO/000077662` -> `QUO0006788` -> `SV00008874`), `_schedule_stage_c`'s own order-row lookup also
returned a false negative once ("no Schedule Board order found") even though the row was proven,
moments later via the identical filter, to genuinely exist. **Fixed**: the row lookup now polls up
to ~6s past the initial 3s wait before giving up, same flicker-tolerance pattern used elsewhere in
this file, rather than a single fixed wait.

**Full pipeline re-verified live end-to-end after both fixes**, entirely manually (Stage C's
checklist/calendar-row flakiness from session 6 is unrelated and unchanged, so this WO was driven
manually rather than via `_schedule_stage_c` directly): `WO-PO/000077662` -> `QUO0006788` (Stage B)
-> `SV00008874` created via Confirm+Yes, SA0005 toast (Stage B.5) -> scheduled to TAN WEI YING on
08/05/2026, appeared in "Upcoming Service" (Stage C) -> Billables Actual Qty set 3.00, Total After
Tax recalculated to $215.82 matching the WO's Grand Total exactly, Submit confirmed, verified gone
from Service Order Performance under "All" status (Stage D). Every step confirmed via ground truth,
not absence-of-error.

**Still open**: Stage C's checklist/calendar-row rendering intermittency (documented session 6) is
unchanged -- `_schedule_stage_c` as committed code has not been re-tested end-to-end since these
fixes; only the manual path was re-verified this session.

## Session 8 (2026-08-28): scheduler wrapper fixed; seven more Stage C bugs found on WO-PO/000080788

**Infrastructure, unrelated to the Synergix driver**: the 22:00 scheduled task had reported
`LastTaskResult 1` every night for a week with the underlying batch actually succeeding, and no
`batch_*.log` had ever been written. Root cause: `run_batch.ps1`/`run_poll.ps1` piped the child via
`*>&1 | Tee-Object`, which under PowerShell 5.1 wraps every stderr line in a terminating
`NativeCommandError` (Python's `logging` writes to stderr, so the wrapper died at the child's very
first log line -- before any log file was written and before Task Scheduler could see the real exit
code). Worse: Task Scheduler marked the task "completed" seconds after 22:00 while the orphaned
child ran on for hours, silently defeating `ExecutionTimeLimit`. Fixed via `Start-Process` with
OS-level stream redirection instead of a pipeline (`c405dc1`). `alert_on_crash.py`'s stderr capture
had the same "buffers everything, streams nothing" bug -- fixed to read line-by-line with a bounded
tail deque (`c405dc1`).

**Stage C, live A-D run on a genuinely new WO** (`WO-PO/000080788` -> `QUO0006805` ->
`SV00008875`): Stages A, B, and B.5 all verified correct from a fresh session (total, customer,
reference, remarks all match the source WO). Stage C then surfaced seven more real bugs across three
commits, found by instrumenting network traffic and comparing against raw ajax responses rather than
guessing from screenshots:

1. **Order-grid pagination trap** (`091c9f4`) -- filtering Unscheduled Service Orders by Customer
   sorts a freshly-created order onto the LAST page at the grid's 5-rows/page default. Fixed by
   filtering the grid's own Enquiry/Subject column by the WO number instead, which needs no
   page-size workaround at all.
2. **Decoy Filter-link element** (`091c9f4`) -- a generic `span, a` query for the "Filter" link found
   an unrelated look-alike span at the same screen position first; `.closest()` on it silently found
   no button and clicked nothing while reporting success. Fixed by querying only
   `span.ui-button-text`.
3. **The Employee/Work Team toggle fires ZERO ajax requests on its own** (`d830c0b`) -- confirmed via
   captured network traffic: clicking it (JS-dispatched or real mouse) only flips a CSS class
   client-side; nothing reaches the server until a LATER action (clicking Filter) does, by which
   point the toggle's state may not have survived. This is why forcing "Employee" mode before
   Filter was unreliable.
4. **The employee checkbox's real click target is `.ui-chkbox-box`, not its `<label>`** (`d830c0b`)
   -- same visual-state-vs-real-handler mismatch as bug 2/3. `label[for=...]` does not reliably fire
   the real `onchange="PrimeFaces.ab(...)"` handler; the sibling `.ui-chkbox-box` div does.
5. **A second "Filter" click (meant to close the panel) was actively breaking the checkbox**
   (`d830c0b`) -- it fires the SAME ajax call that opens the panel, which can re-render the checkbox
   section from stale server state and overwrite the just-checked box before its own round-trip
   lands. Removed; the calendar row never needed the panel closed to appear.
6. **A `blockUI` overlay intercepts the `newEventButton` click** (`4b4cc03`) --
   `document.elementFromPoint` at the button's own coordinates resolved to a pending-ajax mask, not
   the button; Playwright's click can report success while landing on the mask. Fixed by routing
   through the existing `_click_when_clear` helper (already used elsewhere in this file).
7. **The Event Details dialog takes ~8s to render, not the ~3s previously assumed, and the
   wrong-employee check must be scoped to the dialog itself** (`4b4cc03`) -- a page-wide
   `.ui-selectonemenu-label` search can match an unrelated dropdown (e.g. the header's company
   switcher) while the real dialog is still loading. Fixed with a poll (up to ~10s) and a
   dialog-scoped query.

Also implemented session 6's own standing recommendation: the full-re-navigation fallback for an
empty employee checklist now loops up to 3 times instead of running once (`d830c0b`) -- confirmed
live this sometimes succeeds where a single attempt doesn't.

**Net result for `WO-PO/000080788`**: further than this automation has ever reached via the fully
automated `_schedule_stage_c` -- checklist populates reliably, checkbox checks and stays checked,
calendar row renders, Event Details dialog opens for the right employee, and the schedule **commits
server-side** (`SV00008875` shows "TAN WEI YING, 14/07/2026" under its own Order Details/Schedule
section, confirmed live). It still does not complete: **Submit does not enable even with the
checkbox checked**, contradicting session 6's own documented finding that ticking this exact
checkbox is what enables Submit. Not yet resolved -- left open in `4b4cc03`'s own commit message as
either a new, additional gate (an empty "To Service"/"Billables" section was visible) or one more
instance of the same non-deterministic flakiness. `SV00008875` was deliberately left in a safe state
(scheduled, not submitted, not duplicated) rather than forced further.

`_schedule_stage_c` as committed code has **still never completed Stage C end-to-end on its own** --
every real A-D success in this project's history (`SV00008852`, `SV00008856`, `SV00008874`) required
a human or a manual step-by-step session to finish Stage C. This session's fixes are real,
independently verified progress (the automated function now gets to a materially later point of
failure each time), but the underlying Schedule Board widget's flakiness -- flagged as likely
genuinely non-deterministic since session 6 -- has still not been fully defeated.

115 passed, 22 skipped after every commit this session.
