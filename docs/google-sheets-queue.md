# Google Sheets staff queue — proposed

Status: design only. The existing worker reads CSV jobs and an operator-approved
inventory. A Sheets adapter and node/record-resolution intake stage must be built.
Use a Google Sheet, not a free-form Google Doc: one row represents one request.
Google's API supports reading cells and writing results back:
https://developers.google.com/workspace/sheets/api/guides/values

## Staff workflow

1. Add an ArchivesSpace or Compass link and optional notes to a Requests sheet.
2. Mark the request Ready when complete.
3. A scheduled server worker resolves its identity, checks inclusion approval and
   processes the manifest using the existing converter.
4. Results show Validated (dry run), Published (verified upload), Needs review or
   Failed, with the manifest URL and a useful explanation.
5. Staff correct failed requests and explicitly request retry; prior results remain
   in the audit log. No terminal or Python installation is needed for staff.

Adding a row is submission, not content-access approval. Publication still uses the
operator-approved policy/inventory. Automatically updating ArchivesSpace File Versions
is a separate opt-in workflow, not implied by Published.

## Columns

| Column | Editor | Purpose |
| --- | --- | --- |
| Request ID | Intake automation | Stable unique ID, independent of row order |
| Record/source URL | Staff | ArchivesSpace or Compass item |
| Notes | Staff | Context for review |
| Ready / Retry | Staff | Explicit submission state |
| Status | Worker | Queued, Processing, Validated, Published, Needs review, Failed |
| Manifest URL | Worker | Canonical URL; label proposed versus published |
| Last attempt | Worker | Timestamp |
| Result | Worker | Short actionable explanation |

Prefer separate Requests and Results tabs, with the Results tab protected against
routine edits. Staff can continuously append requests. Use filter views instead of
sorting the underlying queue while the worker runs. Durable receipts on the server
remain authoritative; the Sheet is a submission/status interface, not a transaction log.

## Adapter requirements

- Use a dedicated authorized Google identity with access to the specific spreadsheet.
  Confirm Smith Workspace sharing/service-account policy. No credentials in the Sheet,
  repository or staff laptops; configure secrets on the worker host.
- Poll at an agreed interval (for example five minutes), in bounded batches. One
  active worker holds the local lock. A Sheet status cell is not a distributed lock.
- Identify work by immutable request ID plus an input fingerprint, never row number
  alone. Detect duplicate IDs, changed input, row deletion and reordering. A robust
  Results tab keyed by request ID avoids overwriting shifted request rows.
- Persist claim, source mapping, approval and results in durable local receipts.
  If upload succeeds but the Google update fails, retry the status write without
  republishing. Reconcile status from receipts after restarts; do not lose requests.
- Use RAW cell writes so source text is not interpreted as spreadsheet formulas.
  Do not overwrite staff-entered fields. Back off on API quotas and outages.
- Recheck authorization/withdrawals before publication or retries. Restricted and
  unknown-access requests must not become public through the Sheet.
- A self-entered submitter field is not verified identity. Use organizational audit
  facilities or authenticated intake if person-level attribution is required.
- Before retirement, resolve through retained Drupal mappings first, with bounded
  Compass HTTP fallback. An ArchivesSpace URL is not accepted by the existing worker
  directly: intake must establish the correct digital object, PID and node mapping.
- Distinguish publication, ArchivesSpace update and OCR indexing states. Missing OCR
  is not an error in image-manifest conversion unless that stage explicitly requires it.

## Setup decisions

Confirm the spreadsheet owner/URL, request editors, Google authentication method,
execution host, polling interval, access-approval owner and failure notifications.
Start with read/write status in dry-run mode, then enable publication after hosted
verification. No live spreadsheet or Google credentials have been configured yet.
