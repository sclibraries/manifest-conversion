# Manifest harvester and libtools2 publisher

MC-001 implementation: standard-library Python 3.11+ command for Linux/macOS.
Run from the repository root. No Python packages, SSH keys or AWS credentials are
required when the worker runs on the publication host. Local dry runs work on a Mac.

## What it does

- Reads operator-reviewed node mappings and retained manifest checksums.
- Optionally harvests `/node/N/manifest` over HTTPS before 1 July 2027.
- Converts supported Compass IIIF Presentation 2 image manifests to
  digital.smith.edu services, preserving canvas IDs, order, dimensions and metadata.
- Verifies each image's info.json dimensions and service identifier.
- Stages converted JSON and receipts; publishes only with `--publish`.
- Writes directly to a configured local web directory on libtools2 with atomic rename.
  A remote SFTP client is not included: run this worker on libtools2, or use a
  separately managed transfer of staged artifacts if operations requires another host.
- Verifies canonical HTTPS bytes, CORS and revalidation cache headers after writing.
- Keeps old artifacts privately, prevents overwriting unowned/externally changed files,
  resumes interrupted publication and records unchanged repeats.
- Handles individual jobs or a folder, with process locking and per-item failure reports.

No ArchivesSpace record updates, OCR indexing, scan uploads, cron installation or
remote server configuration happen automatically. New scans still use workbench-lite.

## Operator setup (once)

Start with `examples/config.json`; the libtools2 path and URL follow the existing
pilot documentation, but must be confirmed on the execution host. Configure absolute
paths and create the publication directory separately. Keep state, retained sources,
approval inventory and configuration outside the public web root. The worker user
needs write access only to its private state and the manifest directory. Restrict
config/inventory edits to operators; staff submit jobs, not approval metadata.
Do not use different state directories for workers sharing one publication directory.

Configure the web server, through its owner, for JSON, TLS, CORS and:

    Cache-Control: no-cache

This worker deliberately requires `no-cache` or `no-store` and checks that CORS is
`*` or the configured PUI origin. It does not edit Apache/nginx configuration.
No-cache permits storage but requires revalidation. Validate browser behavior and
withdrawal/cache invalidation on the hosted origin before production acceptance.

Prepare the operator inventory, keyed by a stable output ID. It contains numeric
Drupal node IDs, not Islandora PIDs. Resolve PIDs from the reviewed Drupal export;
never assume the number in `smith:1348191` is a node ID. Source fields:

- `source_file`: relative retained-manifest path under source_root, with `source_sha256`.
- Or omit source_file and explicitly set `allow_live: true` in trusted configuration.
  Live responses and their checksums are saved locally. The source must identify
  exactly the approved node. Live harvesting is disabled on/after 1 July 2027.
- `access: public`, nonempty `policy` and `approved_by` are mandatory. This is a
  recorded operator decision, not an automatic inference from Drupal terms or URLs.
  Establish the collection access policy/export outside this command before approving.
- Optional `original_uri` and `aspace_record` are copied into reports for review.

The example inventory is intentionally unapproved and cannot publish.
The operator inventory supplies authorization; staff CSVs contain **only item_id**.
Node/source changes belong in the reviewed inventory, not untrusted jobs.

## Run

Validate, fetch image metadata and stage files without writing the web directory:

```sh
python3 -m scripts.manifest_worker --config /etc/manifest-worker/config.json \
  --jobs /var/lib/manifest-worker/incoming/batch.csv
```

After inspecting artifacts and receipts, run the same command with `--publish`.
For a staff drop folder:

```sh
python3 -m scripts.manifest_worker --config /etc/manifest-worker/config.json \
  --inbox /var/lib/manifest-worker/incoming --publish
```

An example cron entry, after installation and access review:

```cron
*/15 * * * * cd /opt/preservica && /usr/bin/python3 -m scripts.manifest_worker --config /etc/manifest-worker/config.json --inbox /var/lib/manifest-worker/incoming --publish >> /var/log/manifest-worker.log 2>&1
```

Upload as `.part` and rename to `.csv` only when complete. Use authenticated accounts;
reports record the filesystem owner. With a shared upload account this identifies only
that account: an authenticated upload gateway/audit trail is required to attribute
individual staff. The folder must not be writable by unauthenticated users.

Ready CSVs remain in place and are rechecked on each run; no automatic source-file
moves occur. Archive completed jobs operationally to avoid repeated metadata requests.
Repeated items keep stable URLs, but still validate current input/images/public bytes.
A full-migration operator can split its approved inventory into bounded CSV batches;
the staff folder is not the bulk migration inventory or a replacement for Drupal exports.

On this Mac, Python required the system CA bundle for HTTPS verification:

```sh
SSL_CERT_FILE=/etc/ssl/cert.pem python3 -m scripts.manifest_worker --config CONFIG --jobs JOBS
```

Use the execution host's maintained CA bundle; never disable certificate verification.

## Reports, recovery and limits

Private `state/` contains sources, artifacts, receipts, history and reports. CSVs are
spreadsheet-safe, while JSON preserves exact machine-readable values. Reports include
the job checksum, submitting OS account, result URL, original URI, checksum and status.
`published_unverified`/`publishing` receipts remain after verification/interruption;
rerun the same approved item to retry. A failed report does not always mean no file
was written: inspect the receipt. A verified publish is not a hosted browser acceptance.

The first publish refuses any pre-existing file without an ownership receipt. Do not
fabricate a receipt to bypass this; choose an unused ID or reconcile the existing
publication through a reviewed adoption procedure. Changed owned files are archived
before replacement; external edits cause a collision error. No automatic destructive
cleanup, arbitrary overwrite, withdrawal or rollback command is provided.

For withdrawal: add the ID to `withdrawn_items` first to prevent retries republishing,
then have the publication owner remove/block the canonical URL and invalidate caches.
For rollback: review current access first, use the retained artifact through the
publication owner's controlled procedure, and reconcile the receipt before resuming.
Do not restore old restricted content merely because a historical artifact exists.

Bounds: 100 ready CSV files/run, 1,000 IDs/job, 8 MiB per source/config/inventory/HTTP
response, nesting depth 32, 2,000 pages/manifest, 5,000 requests and 128 MiB per run,
30-minute request deadline, 20-second network timeout and at most three attempts
for transient HTTP/network failures. Split large migrations; do not load million-page
inventories into one JSON file. Individual failures do not stop other jobs.

Supported shape: one IIIF v2 sequence, one image painting per canvas, Compass image
services embedding a `/system/files/` URL. Unsupported formats/fields, multiple
paintings, markup-bearing metadata, PDFs/renderings and other companions are held
for review rather than discarded. This intentionally narrow implementation does not
claim to convert every Compass object.

Known legacy hOCR seeAlso links and Compass Search API services are omitted from the
public output and explicitly preserved in receipts/original manifests. They are not
replaced by an unimplemented OCR endpoint. Originals are never deleted. Image metadata
is checked; downloading every image tile/thumbnail and hosted CSP/browser verification
remain deployment acceptance tasks. Metadata is untrusted; consumers must still escape it.

## Tests

```sh
python3 -m unittest scripts.manifest_worker.test_worker
```

Tests use local files and injected HTTP responses, including atomic-write interruption,
public verification failure, repeat runs, source integrity, path containment and
unauthorized jobs. Live dry-run evidence is recorded separately in the completion ledger.
