# Manifest conversion

A Python command-line tool that reads IIIF manifests, rewrites their image-service
URLs, validates page dimensions, and optionally publishes the converted JSON files
to a web directory.

The current converter supports Compass IIIF Presentation 2 manifests with one image
per page. The destination image service and manifest host are configurable.

## Requirements

- Python 3.11 or newer on macOS or Linux. No additional Python packages are required.
- HTTPS access to the source and destination image services.
- For publication, filesystem write access to the directory serving the manifests.

## Install

```sh
git clone https://github.com/sclibraries/manifest-conversion.git
cd manifest-conversion
mkdir -p work/sources work/incoming
cp scripts/manifest_worker/examples/config.json work/config.json
cp scripts/manifest_worker/examples/inventory.json work/inventory.json
```

Run all commands below from the repository directory.

## Configure

Edit `work/config.json`. Use absolute filesystem paths and your destination URLs:

```json
{
  "manifest_base": "https://collections.example.org/manifests",
  "image_base": "https://images.example.org/iiif/2",
  "publish_dir": "/absolute/path/to/web/manifests",
  "source_root": "/absolute/path/to/manifest-conversion/work/sources",
  "state_dir": "/absolute/path/to/manifest-conversion/work/state",
  "inventory": "/absolute/path/to/manifest-conversion/work/inventory.json",
  "allowed_hosts": [
    "compass.fivecolleges.edu",
    "images.example.org",
    "collections.example.org"
  ],
  "pui_origin": "https://archives.example.org",
  "allow_live": false,
  "withdrawn_items": []
}
```

| Setting | Purpose |
| --- | --- |
| `manifest_base` | Public URL prefix for converted manifests. |
| `image_base` | Destination IIIF Image API 2 base URL. |
| `publish_dir` | Existing local web directory to write when using `--publish`. |
| `source_root` | Directory containing retained source manifests. |
| `state_dir` | Private directory for artifacts, receipts, history, and reports. Keep outside the web root. |
| `inventory` | Operator-maintained JSON file mapping item IDs to source and approval information. |
| `allowed_hosts` | Hostnames the worker may request over HTTPS, including redirect destinations. |
| `pui_origin` | Viewer origin allowed by the manifest server's CORS header; wildcard CORS is also accepted. |
| `allow_live` | Allow fetching source manifests from Compass instead of retained files. |
| `withdrawn_items` | Item IDs that must not be processed or republished. |

## Add items

Edit `work/inventory.json`. Each item needs a unique ID and its actual Drupal node
ID. A legacy object identifier is not necessarily the Drupal node ID.

```json
{
  "item-001": {
    "node_id": "123",
    "source_file": "123-manifest.json",
    "source_sha256": "REPLACE_WITH_SOURCE_FILE_SHA256",
    "access": "public",
    "policy": "REPLACE_WITH_APPLICABLE_ACCESS_POLICY",
    "approved_by": "REPLACE_WITH_APPROVING_AUTHORITY"
  }
}
```

Place the original manifest under `source_root` and record its SHA-256 checksum.
Set `access` to `public` only after confirming publication eligibility. Unknown or
restricted items are rejected. Optional `original_uri` and `aspace_record` fields
are included in reports.

For live harvesting, omit `source_file` and enable `allow_live`. The worker fetches
`https://compass.fivecolleges.edu/node/{node_id}/manifest` and retains its bytes and
checksum. Live harvesting is disabled on and after 1 July 2027; retained-file
processing continues to work.

Create `work/incoming/batch.csv` with exactly one column:

```csv
item_id
item-001
```

CSV rows reference approved inventory entries; they cannot supply access approval.

## Run a dry run

```sh
python3 -m scripts.manifest_worker \
  --config work/config.json \
  --jobs work/incoming/batch.csv
```

This validates sources and image services, then saves converted manifests under
`state_dir/artifacts/`. It does not write to the publication directory. The summary
reports the number of failures and the path to a CSV report.

## Publish

Create the publication directory and configure its web server to serve JSON with:

- `Cache-Control: no-cache` or `no-store`.
- `Access-Control-Allow-Origin` matching `pui_origin`, or `*`.

Then run:

```sh
python3 -m scripts.manifest_worker \
  --config work/config.json \
  --jobs work/incoming/batch.csv \
  --publish
```

Publication writes atomically to `publish_dir` and verifies the resulting HTTPS
response. Run on the publication host: this command does not transfer files over
SSH or SFTP. It refuses to overwrite existing files it does not own.

A failed verification can occur after a file has been written. Inspect the receipt
and report, then rerun the job to retry. No ArchivesSpace records are updated.

## Process a folder

```sh
python3 -m scripts.manifest_worker \
  --config work/config.json \
  --inbox work/incoming \
  --publish
```

Schedule this command with cron if needed. Upload jobs with a temporary extension,
then rename them to `.csv` when complete. Jobs remain in the folder and are rechecked
on each run; archive completed CSVs to avoid repeated checks. Keep one shared state
directory for all runs targeting the same publication directory.

See the [operator runbook](scripts/manifest_worker/README.md) for limits, recovery,
permissions, and TLS troubleshooting.
