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

## Notes
- The remarks format was given as a screenshot in the doc (not captured here) — confirm exact text.
- DRY_RUN performs every step EXCEPT the final Submit/Confirm/Fulfill clicks.
