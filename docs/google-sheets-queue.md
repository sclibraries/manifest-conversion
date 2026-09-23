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

## 3. Prepare Requests

Create a tab named `Requests` with these exact headers in A1:D1:

| Request ID | Source URL | Notes | Ready |
| --- | --- | --- | --- |
| request-001 | https://archives.example.org/repositories/2/archival_objects/123 | Optional notes | TRUE |

Use unique, stable request IDs containing letters, numbers, underscores or hyphens.
Enter `TRUE` to submit, or `FALSE`/blank to hold. A checkbox also works.
Keep at most 1,000 request rows. Use `--requests-tab NAME` for a different tab name.

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
