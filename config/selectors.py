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

# Login page
TCMS_USERNAME_INPUT = TODO_SELECTOR   # TODO(human): inspect portal — username/email input on TCMS login
TCMS_PASSWORD_INPUT = TODO_SELECTOR   # TODO(human): inspect portal — password input on TCMS login
TCMS_LOGIN_BUTTON = TODO_SELECTOR     # TODO(human): inspect portal — submit/sign-in button on TCMS login
TCMS_LOGIN_SUCCESS_MARKER = TODO_SELECTOR  # TODO(human): an element that only appears AFTER successful login (to confirm auth)

# Un-invoiced Work Order list
TCMS_UNINVOICED_NAV = TODO_SELECTOR   # TODO(human): nav link/menu item that opens the un-invoiced WO list/view
TCMS_WO_ROW = TODO_SELECTOR           # TODO(human): selector matching EACH WO row in the list (used with .all())
TCMS_WO_ROW_ID_ATTR = TODO_SELECTOR   # TODO(human): attribute on the row holding the WO id, OR a child selector for the WO-PO text
TCMS_WO_OPEN_LINK = TODO_SELECTOR     # TODO(human): within a row, the clickable element that opens the WO detail

# Work Order detail / PDF download
TCMS_WO_PDF_DOWNLOAD_BUTTON = TODO_SELECTOR  # TODO(human): the button/link that triggers the WO PDF download
TCMS_WO_BACK_TO_LIST = TODO_SELECTOR  # TODO(human): control to return from a WO detail back to the list (known state)


# ======================================================================================
# Synergix ERP
# ======================================================================================

# Login page
SYNERGIX_USERNAME_INPUT = TODO_SELECTOR   # TODO(human): username input on Synergix login
SYNERGIX_PASSWORD_INPUT = TODO_SELECTOR   # TODO(human): password input on Synergix login
SYNERGIX_LOGIN_BUTTON = TODO_SELECTOR     # TODO(human): submit button on Synergix login
SYNERGIX_LOGIN_SUCCESS_MARKER = TODO_SELECTOR  # TODO(human): element only present after successful Synergix login
SYNERGIX_HOME_MARKER = TODO_SELECTOR      # TODO(human): element identifying the Synergix home/known state (navigate-back target)

# --- Duplicate check (Service Order Performance or equivalent search) ---
SYNERGIX_DEDUP_NAV = TODO_SELECTOR        # TODO(human): nav to the search screen used to check if a WO already exists
SYNERGIX_DEDUP_SEARCH_INPUT = TODO_SELECTOR  # TODO(human): search field where WO-PO number is entered
SYNERGIX_DEDUP_SEARCH_SUBMIT = TODO_SELECTOR # TODO(human): search/go button
SYNERGIX_DEDUP_RESULT_ROW = TODO_SELECTOR    # TODO(human): a result row indicating a MATCH exists (presence => duplicate)
SYNERGIX_DEDUP_NO_RESULT_MARKER = TODO_SELECTOR  # TODO(human): the "no records found" indicator (presence => NOT duplicate)

# --- Stage B: Create quotation (Copy From template) ---
SYNERGIX_NEW_QUOTATION_NAV = TODO_SELECTOR   # TODO(human): nav/button to start a new quotation
SYNERGIX_COPY_FROM_BUTTON = TODO_SELECTOR    # TODO(human): the "Copy From" control
SYNERGIX_COPY_FROM_ID_INPUT = TODO_SELECTOR  # TODO(human): field to enter the template quotation id (SYNERGIX_TEMPLATE_QUO_ID)
SYNERGIX_COPY_FROM_CONFIRM = TODO_SELECTOR   # TODO(human): confirm the copy-from selection

# The ~8 fields filled on the quotation. Confirm the real field set against Synergix.
SYNERGIX_FIELD_SERVICE_LOCATION = TODO_SELECTOR  # TODO(human): service location field
SYNERGIX_FIELD_CUSTOMER_CONTACT = TODO_SELECTOR  # TODO(human): customer contact field (maps from prepared_by)
SYNERGIX_FIELD_REFERENCE_NO = TODO_SELECTOR      # TODO(human): Reference No. field (maps from gl_number)
SYNERGIX_FIELD_PROJECT_CODE = TODO_SELECTOR      # TODO(human): project code field (2000069 / 2000050)
SYNERGIX_FIELD_JOB_DATE = TODO_SELECTOR          # TODO(human): job date field
SYNERGIX_FIELD_QUANTITY = TODO_SELECTOR          # TODO(human): line quantity field
SYNERGIX_FIELD_UNIT_PRICE = TODO_SELECTOR        # TODO(human): line unit price field
SYNERGIX_FIELD_REMARKS = TODO_SELECTOR           # TODO(human): remarks field (built remarks string)

SYNERGIX_QUOTATION_SUBMIT = TODO_SELECTOR    # TODO(human): the FINAL submit/save/confirm button for the quotation (gated by DRY_RUN)

# --- Stage C: Schedule board update (MOST FRAGILE — best-effort) ---
SYNERGIX_SCHEDULE_BOARD_NAV = TODO_SELECTOR   # TODO(human): nav to the schedule board
SYNERGIX_SCHEDULE_BOARD_ENTRY = TODO_SELECTOR # TODO(human): where/how the scheduled entry is added (likely drag/drop — flag manual)
SYNERGIX_SCHEDULE_BOARD_SAVE = TODO_SELECTOR  # TODO(human): save control for the schedule board (gated by DRY_RUN)

# --- Stage D: Attach PDF + fulfil service order ---
SYNERGIX_ATTACH_PDF_BUTTON = TODO_SELECTOR    # TODO(human): control to attach a file to the order
SYNERGIX_ATTACH_PDF_INPUT = TODO_SELECTOR     # TODO(human): the <input type=file> used for the PDF upload
SYNERGIX_FULFIL_SO_BUTTON = TODO_SELECTOR     # TODO(human): the FINAL fulfil/confirm button for the service order (gated by DRY_RUN)
