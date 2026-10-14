# AGENTS.md

Guidance for coding agents working in this repository. `CLAUDE.md` imports this file; edit
here only.

## What this is

The manifest-conversion worker is a Python command-line tool under
`scripts/manifest_worker/`. It validates retained or approved source manifests, converts
supported IIIF Presentation 2 image manifests, and can publish converted JSON to a configured
web directory.

## Test

Run the unit suite from the repository root:

```sh
python3 -m unittest discover -s scripts/manifest_worker -t .
```

Install the development tools and run Ruff lint and format checks from the repository root:

```sh
python3 -m pip install -r requirements-dev.txt
python3 -m ruff check .
python3 -m ruff format --check .
```

Tests use synthetic fixtures and local HTTP doubles. They must not need credentials, network
access, production services, or staff inputs.

## Code standards

- Follow DRY and SOLID; keep modules and functions focused and readable.
- Keep functions lean (about 40 lines, complexity at most 10, and no more than 40 statements).
  Track existing metric baselines with per-function suppressions for only the exceeded rules.
- Keep Ruff lint and formatting checks clean.

## Interfaces other systems depend on

Report changes to these in the handoff.

| Interface | Defined in | Who depends on it |
|---|---|---|
| Worker configuration: `manifest_base`, `image_base`, `publish_dir`, `source_root`, `state_dir`, `inventory`, `allowed_hosts`, `pui_origin`, `allow_live`, `withdrawn_items`; optional `discovery.enabled`, `discovery.aspace_public_hosts`, and `discovery.ready_publication.{policy,approved_by}` | README, URL-discovery guide and `scripts/manifest_worker/` | Operators and scheduled jobs |
| Inventory is a JSON object keyed by item ID. Records use `node_id`, `source_file`, `source_sha256`, `access`, `policy`, `approved_by`; optional fields include `source_urls`, `original_uri`, `aspace_record`, `intake_hold`, and `blocked_source_keys` | README, intake guide and `scripts/manifest_worker/` | CSV/Sheets queue and publication worker |
| Published manifest URL: `<manifest_base>/<item_id>.json`; image services use `image_base` | `scripts/manifest_worker/runner.py` | ArchivesSpace viewer and OCR tooling |
| CSV and Sheets request/result fields and status text | README and `docs/google-sheets-queue.md` | Staff requesters and operators |

## Public repository rules

This repository is public and dedicated to the public domain under CC0 1.0 Universal. Keep
fixtures synthetic. Do not commit tickets, planning records, pilot/evidence logs, staff or vendor
contact details, credentials, private hostnames, internal server paths, or production inputs.
Use `example.org` placeholders. The public hosts `compass.fivecolleges.edu`,
`findingaids.smith.edu`, and `digital.smith.edu` are allowed when needed by the documented
interfaces.

## Working rules

- Work on a task branch based on the requested upstream branch. Do not push or merge to `main`.
- Keep changes to the task's named scope. If a component dependency needs a change, stop and
  report it.
- End with the branch and commit, commands and results, deviations and why, and open questions.
