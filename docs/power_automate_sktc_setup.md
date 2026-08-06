# SKTC intake via Power Automate — setup guide

This is a **cloud configuration task**, not code — nothing here lives in this repo except the Python
adapter that reads the resulting folder (`src/sktc_folder_intake.py`). Use this while IMAP access to
`automationworkflow@ls2.sg` is blocked (see `docs/automation_context.md` for why). Whoever owns that
mailbox can typically set this up themselves with **Power Automate**, no tenant admin consent needed
for the standard trigger/actions used here — but if your organization has locked down connector usage,
you may need to check with IT first.

## What this flow needs to do

For every new Work Order email, save its PDF attachment(s) **and** a small JSON "sidecar" file
describing the email, into a folder that syncs to the Windows machine running the automation.

The sidecar is not optional. The Python side independently re-verifies the sender against an
allowlist using the sidecar's data — it does not trust Power Automate's own sender filter as the
only gate, since that's a cloud setting a future person could loosen without anyone here noticing.

## Step-by-step

### 1. Create the destination folder

In OneDrive (or a SharePoint document library), create a folder, e.g. `SKTC-WO-Intake`. Inside it,
Power Automate will need to write directly into the folder (for PDFs) and into a `_meta` subfolder
(for sidecars) — you can pre-create `SKTC-WO-Intake/_meta` or let the flow create it on first run.

### 2. Sync that folder to the Windows machine

Install the OneDrive desktop app (or confirm it's already running) on the machine that will run the
automation, and make sure `SKTC-WO-Intake` is set to **"Always keep on this device"** — NOT
Files-On-Demand / "Free up space" mode. A cloud-only placeholder file looks present in a folder
listing but has zero bytes locally, which the Python adapter specifically detects and refuses to
treat as ready (it'll just wait and retry on the next poll) — but it will never become a real file
until it's actually downloaded, so Files-On-Demand would silently stall every WO forever.

Note the **local path** this folder syncs to (e.g. `C:\Users\<user>\OneDrive - LS2\SKTC-WO-Intake`)
— you'll put this in `.env` as `SKTC_INTAKE_FOLDER`.

### 3. Build the Power Automate flow

Go to [make.powerautomate.com](https://make.powerautomate.com) → **Create** → **Automated cloud flow**.

**Trigger:** *When a new email arrives (V3)* (Outlook connector)
- **Folder:** the SKTC intake mailbox's inbox (or wherever SKTC's WO emails currently land)
- **From:** the known SKTC sender address(es) — `# TODO(human): confirm SKTC's actual sending
  address(es) with the client before filling this in.` This is a first line of defense only; the
  Python side re-checks independently regardless of what's set here.
- **Has Attachment:** Yes

**Action 1 — Initialize a variable** (optional but recommended): a `MessageId` variable set to
`triggerOutputs()?['body/internetMessageId']`, so you're not repeating that expression everywhere.

**Action 2 — Apply to each** attachment in `triggerBody()?['attachments']`:
- **Condition:** attachment's `name` ends with `.pdf` (skip inline signature images etc.)
- If true → **Create file** (OneDrive/SharePoint connector), in the `SKTC-WO-Intake` folder, file
  name = the attachment's own name (or prefix with a timestamp if duplicate filenames across
  different emails are a concern — e.g. `@{utcNow('yyyyMMdd_HHmmss')}_@{items('Apply_to_each')?['name']}`),
  file content = the attachment's content.

**Action 3 — Create the sidecar JSON** (after the loop, once per email — not per attachment): another
**Create file** action, in `SKTC-WO-Intake/_meta`, file name = something derived from the message id
(e.g. `@{replace(replace(variables('MessageId'), '<', ''), '>', '')}.json`), file content:

```json
{
  "message_id": "@{variables('MessageId')}",
  "sender": "@{triggerOutputs()?['body/from']?['emailAddress']?['address']}",
  "subject": "@{triggerOutputs()?['body/subject']}",
  "received_at": "@{triggerOutputs()?['body/receivedDateTime']}",
  "attachments": @{...an array of the PDF filenames saved in Action 2...}
}
```

The `attachments` array is what lets the Python adapter match a PDF back to its sidecar without
assuming any particular filename convention between the two — build this by collecting each saved
filename into an array variable during the "Apply to each" loop (Initialize an `Attachments` array
variable before the loop, `Append to array variable` inside it), then reference that array here.

### 4. Point the automation at it

In `.env` on the machine running the automation:

```
SKTC_INTAKE_MODE=folder
SKTC_INTAKE_FOLDER=C:\Users\<user>\OneDrive - LS2\SKTC-WO-Intake
SKTC_SENDER_ALLOWLIST=jenny.ang@sktc.sg,other-sktc-sender@sktc.sg
```

(`SKTC_SENDER_ALLOWLIST` is comma-separated; each entry is matched as a case-insensitive substring
against the sidecar's `sender` field — so `@sktc.sg` alone would match any sender at that domain, or
list specific addresses for tighter control.)

### 5. Test it before trusting it

`# TODO(human): once built, send one real SKTC test email (with a PDF attachment) through the actual
inbox this flow watches, and confirm BOTH the PDF and the sidecar JSON land correctly in the synced
local folder — check the file's actual byte size isn't 0 (a stuck Files-On-Demand placeholder), and
that the sidecar's "attachments" array lists the exact PDF filename that was saved.`

Then run the adapter standalone (no LLM, no Synergix) to confirm it picks the WO up cleanly:

```powershell
python -m src.sktc_folder_intake
```

This prints what it ingested and anything it flagged for review — an orphaned PDF or an
unallowlisted sender at this stage means either the flow's sidecar step or the `SKTC_SENDER_ALLOWLIST`
config needs a second look before relying on this for a live run.

## Known limitations of this approach vs. IMAP

- Depends on OneDrive sync staying healthy on the specific machine running the automation — if
  someone signs that OneDrive account out, or sync gets paused, `poll_folder_once()` raises a loud,
  distinct error (surfaced via the batch report / crash alert) rather than silently reporting "0 new
  WOs," but it still means no new SKTC WOs get processed until sync is restored.
- Filenames must be unique within the intake folder — if SKTC ever reuses the same PDF filename
  across two different unrelated WOs, the second would overwrite the first before either is scanned.
  Not currently observed in real samples, but worth knowing if it ever comes up.
- This is a bridge, not necessarily the final state — `SKTC_INTAKE_MODE=imap` remains fully
  functional and is a one-line config flip back to it if/when M365 admin access to enable Basic Auth
  (or set up OAuth2) becomes available.
