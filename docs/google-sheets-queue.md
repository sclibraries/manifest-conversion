# Google Sheets setup

The Sheets command reads staff requests, matches them to the operator's approved
inventory, runs the converter, and appends outcomes to a separate Results tab.
It leaves the Requests tab unchanged. Default mode validates and stages manifests
locally; it does not publish or update ArchivesSpace records.

## 1. Install dependencies

From the repository directory:

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements-sheets.txt
```

Complete the configuration and inventory setup in the root README first.
Use absolute paths in the configuration. A dry-run configuration may omit
`publish_dir` entirely.

## 2. Configure Google access

1. In Google Cloud, select or create a project and enable the Google Sheets API.
2. Create a dedicated service account and a JSON key, subject to your organization's policy.
3. Store the key outside the repository on the worker machine. Restrict its permissions.
4. Share the spreadsheet with the service account's email address as an Editor.

```sh
chmod 600 /absolute/private/path/google-service-account.json
export GOOGLE_APPLICATION_CREDENTIALS=/absolute/private/path/google-service-account.json
```

Do not commit the key or paste its contents into the spreadsheet. Rotate keys using
your organization's policy. The spreadsheet ID is the portion of its URL between
`/d/` and `/edit`. The command also accepts the full spreadsheet URL.

## 3. Create the spreadsheet and Requests tab

1. Create a blank Google spreadsheet. Its document title can be anything.
2. Rename the first tab to **Requests** (capitalization matters).
3. Paste the following tab-separated line into cell **A1**. It fills A1:D1:

   ```text
   Request ID	Source URL	Notes	Ready
   ```

4. Keep these four headers in this exact order. Add requests starting at row 2.
5. Share the spreadsheet with staff who submit requests and with the service account
   as an Editor. Staff use their normal Google accounts, not the service-account key.
6. Leave creation of **Results** to the initialization command in step 5 below.
   That command also runs ready requests, so leave Ready blank until setup is complete.

The document title and the tab name are different: the command looks for the tab
named `Requests`. Use `--requests-tab NAME` if you choose another name.

### Requests fields — entered by staff

| Column | Required? | What to enter | Example |
| --- | --- | --- | --- |
| **Request ID** | Yes, on every nonempty row | A unique tracking ID you assign. Use 1–64 characters, starting with a letter or number; remaining characters may also include `_` or `-`. No spaces. Keep the ID stable when retrying. | `request-001` |
| **Source URL** | Yes, before submitting | The full HTTPS ArchivesSpace or Compass URL that the operator has mapped to an approved inventory item. Paste the URL itself, without query parameters. Maximum 2,048 characters. | `https://archives.example.org/repositories/2/archival_objects/123` |
| **Notes** | No | Context for the operator, up to 2,000 characters. This does not change manifest metadata or grant publication approval. | `Requested for reading room use` |
| **Ready** | No; blank means hold | `TRUE` submits the row. `FALSE` or blank holds it. To explicitly retry, use `RETRY:1`, then `RETRY:2`, etc. | `TRUE` |

Request ID is a queue tracking ID, **not** the inventory item ID, Drupal node ID,
or output filename. The approved inventory determines the manifest filename.
Duplicate Request IDs stop the run, even if one duplicate is on hold.

Use plain text in Ready so retry values can be entered. Standard TRUE/FALSE
checkboxes are accepted for initial submission, but their validation may need to
be removed before entering a `RETRY:N` value. The parser also accepts `YES`/`READY`
for submission and `NO` for hold; `TRUE` and `FALSE` are the recommended convention.

### Example request

| Request ID | Source URL | Notes | Ready |
| --- | --- | --- | --- |
| request-001 | https://archives.example.org/repositories/2/archival_objects/123 | Requested for reading room use | TRUE |

This is a format example; the example URL will not run until replaced with a real,
approved source. Keep at most 1,000 request rows. Every nonempty row needs a valid
Request ID, including drafts; completely blank rows are ignored.

### Everyday staff workflow

1. Add a new row with a unique Request ID and an approved Source URL.
2. Add optional Notes and set Ready to `TRUE` when the row is complete.
3. Wait for the next scheduled run. The operator chooses the interval; for a
   five-minute schedule, allow at least five minutes plus processing time.
4. Open Results and find the same Request ID. Check the latest **Last Run** and
   **Status**; older attempts remain visible.
5. If a request fails, follow the Result explanation and ask the operator to resolve
   configuration or mapping issues. Then change Ready to `RETRY:1` (increment for
   each further retry). Keep the original Request ID.

The worker does not clear Ready or put status in Requests. Leaving a completed
row at `TRUE` does not continuously republish it. Changing Notes or other request
content does create a new operation, so avoid editing completed rows casually.
Setting Ready to FALSE holds future processing; it does not cancel a run already
in progress or remove a published manifest.

## 4. Map sources to approved items

Add `source_urls` to the corresponding item in the operator-maintained inventory:

```json
"source_urls": [
  "https://archives.example.org/repositories/2/archival_objects/123"
]
```

The item still needs the source manifest/node information and access approval
described in the README. The command also recognizes the inventory's `original_uri`
and Compass node/manifest URLs derived from its node ID. Source URLs must use HTTPS
and have no query parameters. Unknown, ambiguous, unapproved or withdrawn items
receive `Needs review`. Adding a Sheet row does not establish an identity mapping
or grant publication approval. General automatic record resolution is not included.

## 5. Initialize and run locally

```sh
python3 -m scripts.manifest_worker.sheets \
  --config work/config.json \
  --sheet YOUR_SPREADSHEET_ID \
  --initialize-results
```

This creates or extends a separate `Results` tab and processes up to ten ready
requests. Existing recognized result columns are preserved. Results contain request
ID, source URL, status, manifest URL, timestamp, explanation and a hidden Operation
ID. An existing Error column is supported. Do not edit Operation IDs.

A successful dry run says **Validated (dry run)**. Its manifest URL is proposed,
not published. Converted files are under `state_dir/artifacts/`. Run the same
command again without `--initialize-results`: unchanged completed requests should
be skipped. `--limit N` sets the per-run maximum, from 1 to 100.

On macOS, if your Python installation lacks a working certificate bundle, use your
system's trusted CA file rather than disabling TLS verification:

```sh
export SSL_CERT_FILE=/etc/ssl/cert.pem
export REQUESTS_CA_BUNDLE=/etc/ssl/cert.pem
```

## Results fields — written by the worker

Initialization creates the required columns and hides Operation ID. If a Results
sheet already contains recognized columns, their order is preserved and missing
columns are appended. Staff should read this tab without editing its headers or
result rows. If you protect the tab, allow the service account to edit it.

| Field | Meaning |
| --- | --- |
| **Request ID** | Connects this result to the submitted request. Multiple attempts can share this ID. |
| **Source URL** | The source submitted for this attempt, so the history stays meaningful if Requests changes later. |
| **Status** | The outcome described in the table below. |
| **Manifest URL** | The output's canonical address. For a dry run it is only a proposed address; it does not establish that a file was published. |
| **Last Run** | When the result was recorded, in UTC. A timestamp ending `+00:00` is UTC, not local time. |
| **Result** | A short explanation, such as the number of pages validated or the reason processing failed. |
| **Operation ID** | Hidden internal identifier used to skip completed attempts and recover interrupted result writes. Do not edit or delete it. |
| **Error** | Optional compatibility column if already present. Contains the explanation for Failed or Needs review; blank for successful results. New Results tabs use Result instead. |

### Status meanings

| Status | Meaning and next step |
| --- | --- |
| **Validated (dry run)** | Conversion and image checks passed. The manifest is staged privately; it was not published by this attempt. |
| **Published** | The manifest was written or confirmed unchanged, and its HTTPS checksum, cache and CORS checks passed. The Manifest URL is ready for use. |
| **Needs review** | No unique approved mapping was found, access approval is missing, or the item is withdrawn. Ask the operator to review the inventory. |
| **Failed** | Conversion, network access, publication or verification failed. Read Result and retry after correction. Publication verification can fail after a file was written, so Failed does not prove the output is absent. |

There are no Queued or Processing result rows. Until a run finishes, a submitted
request may have no result or only an older result. If nothing appears after the
expected interval, ask the operator to check the schedule and logs.

### Where the files go

Published manifests are JSON files under the configured `publish_dir`, served at
`manifest_base/<inventory-item-id>.json`. A retry uses the same canonical filename;
it does not create another public copy. Images stay in their existing storage.
Private artifacts, receipts and source copies remain under `state_dir`.
Publication does not update an ArchivesSpace File Version automatically; use the
published URL in the separate ArchivesSpace update workflow when appropriate.

## Retries and recovery

- Correct the underlying problem, then change Ready to `RETRY:1`. Increment to
  `RETRY:2`, etc. for further attempts. Setting TRUE repeatedly is not a retry.
- Changes to request content, mapped approval, configuration or publication mode
  create a new operation. Avoid casual edits to completed requests.
- Failed result writes can be retried by rerunning the command. Completed local
  receipts are reconciled without converting again.
- Keep the state directory and Results operation IDs. Protect Results against
  routine staff edits and back up state. Archive Results before 5,000 rows.
- Exit codes: 0 for a successful run, 1 for newly processed Failed/Needs review
  outcomes, 2 for setup or transport errors. Previously recorded failures are
  skipped until explicitly retried; monitor Results as well as exit codes.

## Scheduling and publication

After a successful manual test, schedule the same command with cron using absolute
paths to the virtualenv Python and configuration. Set credential and certificate
environment variables explicitly in the scheduled job. Capture output in a private
log and arrange failure monitoring. Run one worker host with one shared state
directory; the file lock does not coordinate separate hosts.

Add `--publish` only after configuring and testing the publication directory and
HTTPS/CORS/cache headers described in the README. Publication writes to a local
filesystem directory; it does not upload over SFTP. Publishing is a different
operation from a dry run, so a previously validated request will be processed again.
No scheduler is installed by these commands. Requests remain staff-editable and
results are appended as an audit history, not written over the request rows.
