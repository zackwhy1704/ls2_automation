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
- Type customer name to search; click on the employee.
- Click on the calendar → event details pop up.
- **Date = WO date**; choose **ECOCARE** or **INFIGO** from the dropdown.
- Check the person name, remarks, etc.; if in order, press **Submit**.

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

## Automation scope (agreed)
- **In scope now: Stage B only** — dedup check + create quotation (Copy From) + fill all header/line/
  remarks fields + verify Payment/Shipment info. Does NOT auto-submit.
- **Left to the human approver**: final Submit + Variation Order confirm (end of B), Stage C schedule
  board (most fragile), and Stage D fulfil/attach/bill.
- DRY_RUN performs every step EXCEPT the final Submit/Confirm/Fulfill clicks.
- Copy-From source quotation: TBD (decide during codegen recording).
