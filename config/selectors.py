"""ALL DOM selectors for the TCMS and Synergix portals live here as placeholders.

The author has never seen these portals. Inventing selectors would produce code that *looks*
finished but runs on guesses. So every selector below is the sentinel ``TODO_SELECTOR`` and must be
replaced with a real one discovered via `playwright codegen` (see README).

Each constant has a comment describing exactly what element it must target. Code that consumes these
calls ``require()`` / ``is_placeholder()`` so an unfilled selector produces a clear
``MISSING SELECTOR`` log line and a skipped/failed WO — never a crash or a silent wrong click.
"""
from __future__ import annotations

# Sentinel value. Any selector still equal to this has NOT been filled in.
TODO_SELECTOR = "TODO_SELECTOR"


def is_placeholder(selector: str) -> bool:
    """True if this selector has not yet been filled in by the developer."""
    return selector == TODO_SELECTOR or not selector


class MissingSelectorError(RuntimeError):
    """Raised when code reaches a portal step whose selector is still a placeholder."""


def require(name: str, selector: str) -> str:
    """Return the selector, or raise MissingSelectorError naming it.

    Callers should catch this per-WO, log ``MISSING SELECTOR: <name>``, mark the WO FAILED,
    and continue — see the per-WO error isolation in the drivers.
    """
    if is_placeholder(selector):
        raise MissingSelectorError(name)
    return selector


# ======================================================================================
# JBTC TCMS portal (Dynamics 365 based)
# ======================================================================================

# Login page — DONE. Login is the Microsoft Entra flow (login.microsoftonline.com), handled directly
# in TCMSScraper.login() with Microsoft's stable field ids (#i0116 email, #i0118 password,
# #idSIButton9 submit) plus the "Use your password instead" fallback (the account has MFA, but the
# password path bypasses the Authenticator push). Success is confirmed by landing on the D365 domain,
# so these constants are unused. Left here for reference.
TCMS_USERNAME_INPUT = "#i0116"
TCMS_PASSWORD_INPUT = "#i0118"
TCMS_LOGIN_BUTTON = "#idSIButton9"
TCMS_LOGIN_SUCCESS_MARKER = "[data-dyn-controlname=\"NavigationBar\"]"  # D365 top nav (present only post-login)

# Un-invoiced Work Order list
# Discovered on the live D365 portal (2026-07). The un-invoiced WO flow is:
#   Work order workspace tile -> "Un-Invoiced WO" tile -> grid -> click WO/PO cell -> detail ->
#   Preview/Print -> Original preview -> SSRS PDF viewer -> Export -> PDF download.
TCMS_WORKSPACE_TILE = '[data-dyn-controlname="VendVendorPortalWorkspace"]'  # dashboard -> Work order workspace
TCMS_UNINVOICED_NAV = '[data-dyn-controlname="NewUnInvoiceWO"]'   # workspace tile: "NNN Un-Invoiced WO"
# The WO/PO id lives in a grid-cell text input (D365 FixedDataTable is virtualized: only rendered
# rows are in the DOM). We read the input .value and click it to open the WO detail.
TCMS_WO_ROW = 'input[name="PurchTableAllVersions_PurchOrderId"]'  # each rendered WO/PO id cell
TCMS_WO_ROW_ID_ATTR = TODO_SELECTOR   # unused for D365 (id is the input value, read directly)
TCMS_WO_OPEN_LINK = 'input[name="PurchTableAllVersions_PurchOrderId"]'  # click the id cell to open detail

# Work Order detail / PDF download (SSRS report viewer path)
TCMS_WO_PREVIEW_PRINT = '[data-dyn-controlname="PurchPurchaseOrderShow"]'      # "Preview/Print" split button
TCMS_WO_ORIGINAL_PREVIEW = '[data-dyn-controlname="PurchPurchaseOrderOriginal"]'  # "Original preview" -> opens PDF viewer
TCMS_WO_PDF_EXPORT = '[data-dyn-controlname="PdfViewerExportMenuButton"]'      # "Export" in the SSRS PDF viewer -> downloads
TCMS_WO_PDF_DOWNLOAD_BUTTON = '[data-dyn-controlname="PdfViewerExportMenuButton"]'  # alias kept for the old name
TCMS_WO_BACK_TO_LIST = TODO_SELECTOR  # not needed: we re-navigate to the workspace between WOs for a known state


# ======================================================================================
# Synergix ERP
# ======================================================================================

# Login page — DONE (Synergix Taskhub TH6, a PrimeFaces/JSF app; ids are stable "formId:componentId").
SYNERGIX_USERNAME_INPUT = "#loginForm\\:username"
SYNERGIX_PASSWORD_INPUT = "#loginForm\\:password"
SYNERGIX_LOGIN_BUTTON = "#loginForm\\:loginButton"
SYNERGIX_LOGIN_SUCCESS_MARKER = "text=Operations"   # main menu item, present only after login
SYNERGIX_HOME_MARKER = "text=Operations"            # home/known-state marker

# --- Duplicate check — DONE (implemented directly in SynergixDriver.check_duplicate) ---
# The dedup runs in Service Quotation - LS2: filter the Enquiry/Subject column by WO-PO, then read the
# grid for a WO match vs a "No records found" row. Because the JSF ids are auto-generated, the driver
# locates the column by its header text + the stable `input.ui-column-filter` class rather than by id,
# so these id-based constants are unused. Left for reference.
SYNERGIX_DEDUP_NAV = TODO_SELECTOR        # unused — nav is "General Service" -> "Service Quotation - LS2" by text
SYNERGIX_DEDUP_SEARCH_INPUT = TODO_SELECTOR  # unused — Enquiry/Subject column filter (input.ui-column-filter)
SYNERGIX_DEDUP_SEARCH_SUBMIT = TODO_SELECTOR # unused — filter applies on Enter
SYNERGIX_DEDUP_RESULT_ROW = TODO_SELECTOR    # unused — read grid body for the WO-PO text
SYNERGIX_DEDUP_NO_RESULT_MARKER = TODO_SELECTOR  # unused — "No records found." in the grid body

# --- Stage B: Create quotation — DONE (implemented in SynergixDriver._stage_b_create_quotation) ---
# Flow (all located by icon class / label text, since JSF ids are auto-generated):
#   + new draft   = button:has(span.fa-plus)
#   Copy From     = role=button name "Copy From" (span.fa-copy)
#   copy source   = filter the modal Customer column by town council -> click top quotation link
#                   -> confirm "Yes" on the "override current data" dialog
#   fields set    = Enquiry/Subject + Reference No. (label-anchored), line Unit Price + Remarks (grid)
#   Submit        = NOT clicked — Stage B leaves the draft for a human to review + submit
# These id-based constants are therefore unused; left for reference.
SYNERGIX_NEW_QUOTATION_NAV = TODO_SELECTOR   # unused — button:has(span.fa-plus)
SYNERGIX_COPY_FROM_BUTTON = TODO_SELECTOR    # unused — role=button "Copy From"
SYNERGIX_COPY_FROM_ID_INPUT = TODO_SELECTOR  # unused — modal filters by customer, not by id
SYNERGIX_COPY_FROM_CONFIRM = TODO_SELECTOR   # unused — "Yes" on the override dialog

SYNERGIX_FIELD_SERVICE_LOCATION = TODO_SELECTOR  # unused — inherited from Copy From template
SYNERGIX_FIELD_CUSTOMER_CONTACT = TODO_SELECTOR  # unused — inherited from template
SYNERGIX_FIELD_REFERENCE_NO = TODO_SELECTOR      # unused — set via label "Reference No."
SYNERGIX_FIELD_PROJECT_CODE = TODO_SELECTOR      # unused — inherited (Segment) from template
SYNERGIX_FIELD_JOB_DATE = TODO_SELECTOR          # unused — dates come from template/today
SYNERGIX_FIELD_QUANTITY = TODO_SELECTOR          # unused — 1.00 SVC from template
SYNERGIX_FIELD_UNIT_PRICE = TODO_SELECTOR        # unused — set in the Details grid by column
SYNERGIX_FIELD_REMARKS = TODO_SELECTOR           # unused — set in the Details grid Remarks column

SYNERGIX_QUOTATION_SUBMIT = TODO_SELECTOR    # intentionally NOT used — human submits (Stage B stops before submit)

# --- Stage C: Schedule board — OUT OF SCOPE (manual; the human approver does this) ---
SYNERGIX_SCHEDULE_BOARD_NAV = TODO_SELECTOR   # out of scope
SYNERGIX_SCHEDULE_BOARD_ENTRY = TODO_SELECTOR # out of scope
SYNERGIX_SCHEDULE_BOARD_SAVE = TODO_SELECTOR  # out of scope

# --- Stage D: Attach PDF + fulfil service order ---
SYNERGIX_ATTACH_PDF_BUTTON = TODO_SELECTOR    # TODO(human): control to attach a file to the order
SYNERGIX_ATTACH_PDF_INPUT = TODO_SELECTOR     # TODO(human): the <input type=file> used for the PDF upload
SYNERGIX_FULFIL_SO_BUTTON = TODO_SELECTOR     # TODO(human): the FINAL fulfil/confirm button for the service order (gated by DRY_RUN)
