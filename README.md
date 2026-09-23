# Manifest conversion

Harvest selected Compass IIIF manifests, convert their image services to
`digital.smith.edu`, validate pages, and publish static manifests on libtools2.

**Current status:** CSV batches and folder input work. Google Sheets integration is
planned. Local tests passed for **Agendas, 1976 (19 pages)**; live libtools2 publication
and hosted browser acceptance remain to be verified. No automatic ArchivesSpace
record updates are performed.

## Install and test

Requires Python 3.11+ on macOS or Linux. No pip packages are required.

```sh
git clone https://github.com/sclibraries/manifest-conversion.git
cd manifest-conversion
python3 -m unittest scripts.manifest_worker.test_worker
python3 -m scripts.manifest_worker --help
```

## Local dry run: Agendas, 1976

This sample resolves ArchivesSpace archival object 139132 / Compass PID
`smith:1348191` to **Drupal node 80405**. The PID suffix is not the node ID.
The sample was selected for local testing; this does not grant production publication
approval. The following creates private local test configuration under ignored `work/`.
It contains no publication directory and does not upload anything.

```sh
python3 - <<'PYCONFIG'
import json
from pathlib import Path
root = Path('work/agendas').resolve()
root.mkdir(parents=True, exist_ok=True)
item = 'smith_ssc_ms00697_as139132_001'
inventory = {item: {
    'node_id': '80405',
    'access': 'public',
    'policy': 'selected-sample-local-dry-run-only',
    'approved_by': 'local-test-operator',
    'original_uri': 'https://compass.fivecolleges.edu/object/smith:1348191',
    'aspace_record': '/repositories/2/archival_objects/139132'
}}
config = {
    'manifest_base': 'https://libtools2.smith.edu/digital/manifests',
    'image_base': 'https://digital.smith.edu/iiif/2',
    'source_root': str(root / 'sources'),
    'state_dir': str(root / 'state'),
    'inventory': str(root / 'inventory.json'),
    'allowed_hosts': ['compass.fivecolleges.edu', 'digital.smith.edu'],
    'allow_live': True
}
(root / 'inventory.json').write_text(json.dumps(inventory, indent=2))
(root / 'config.json').write_text(json.dumps(config, indent=2))
(root / 'jobs.csv').write_text('item_id\n' + item + '\n')
PYCONFIG

python3 -m scripts.manifest_worker \
  --config work/agendas/config.json \
  --jobs work/agendas/jobs.csv
```

On the tested Mac, Python needed the system CA bundle. If the default trust store
fails, use the maintained CA bundle for your host. For this Mac:

```sh
SSL_CERT_FILE=/etc/ssl/cert.pem python3 -m scripts.manifest_worker \
  --config work/agendas/config.json \
  --jobs work/agendas/jobs.csv
```

Do not disable TLS verification. A successful summary reports `failed: 0` and
`published_mode: false`. Converted output is:

```text
work/agendas/state/artifacts/smith_ssc_ms00697_as139132_001.json
```

Reports, original manifest bytes/checksums and page mappings stay under `work/agendas/state/`.
The manifest's libtools2 ID is its proposed destination; a dry run does not make that URL live.
Live Compass harvesting expires on 1 July 2027; retain manifests and Drupal mappings before then.

## Publication and unattended operation

Read the [operator runbook](scripts/manifest_worker/README.md) for approved inventories,
server directories, cache/CORS configuration, dry runs, `--publish`, cron and recovery.
Production access approval is separate from the sample above.

The current publisher writes to a local directory: run it **on libtools2** under an
appropriately restricted account. Running `--publish` on a Mac does not upload over
SFTP. Destination files are written atomically and verified over HTTPS. There is no
remote SFTP client in this version.

Staff CSVs contain one column, `item_id`, referring to an operator-maintained inventory.
An inbox can be polled by cron. Staff should not need to install Python once the worker
is deployed. Unsupported manifest/media structures are reported for review.

## Proposed simpler staff interface

A shared Google Sheet can replace CSV submission and display status, results and
manifest URLs. Staff add rows; the server runs the worker. This adapter is **not yet
implemented**. See the [Google Sheets workflow](docs/google-sheets-queue.md).

## Development

Source and tests: `scripts/manifest_worker/`. The initial code was exported from the
preservica project at commit `32e15ee`; local Agendas test evidence was recorded at
`809784c`. No credentials, raw manifests, local configuration or source images are
included in this repository. Source snapshots must be reconciled deliberately when
moving fixes between the two repositories.
