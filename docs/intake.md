# Prepare a batch before staff submit it

The original Sheet queue required prepared mappings. [Automatic URL discovery](url-discovery.md)
provides an optional public-record lookup path; bulk offline intake remains
available for retained exports and post-retirement operation. It joins a **bulk
relationship snapshot** to retained Compass manifests and produces an inventory
candidate plus actionable preflight results. Staff do not edit individual inventory
records.

Intake processes the supplied relationship snapshot; it does not export a complete
collection-wide ArchivesSpace/Drupal inventory. Treat the output as a candidate for
review rather than as a complete migration inventory.

## Inputs

1. A relationship snapshot from an API/export or a recorded public-page capture:

```json
{
  "captured_at": "YYYY-MM-DD",
  "provenance": "Describe export/capture origin and method",
  "records": [
    {
      "url": "https://archives.example.org/repositories/2/digital_objects/9",
      "item_id": "sample_item_001",
      "source_urls": ["https://compass.fivecolleges.edu/object/demo:123"]
    },
    {
      "url": "https://archives.example.org/repositories/2/archival_objects/8",
      "digital_object_urls": ["https://archives.example.org/repositories/2/digital_objects/9"]
    }
  ],
  "sources": [
    {
      "node_id": "456",
      "source_urls": ["https://compass.fivecolleges.edu/object/demo:123"],
      "source_file": "456.json"
    }
  ]
}
```

`item_id` is the ArchivesSpace digital-object identifier. `source_urls` on that
record come from its file versions. `digital_object_urls` are actual linked
records, not a guessed identifier extracted from a title. Each production/test
origin must have its own observed/exported record. Do not clone one origin's
relationships onto another without checking it. Include every linked child,
including unsupported ones, so intake can identify multi-object parents.

A source record joins observed Compass aliases to the actual Drupal node and a
retained manifest. Use one source row per node. An optional `source_sha256` checks
that the retained bytes match the export; intake always computes and records a
checksum. Compass PID numbers are **not** assumed to be Drupal node numbers.
The `/object/` and `/islandora/object/` forms normalize together; node and manifest
URLs resolve through their verified node mapping.

2. The retained source files under `--source-root`.
3. The normal worker configuration (manifest/image base URLs).
4. A CSV download of Requests with `Request ID,Source URL,Notes,Ready` headers.
5. Optionally, existing inventory and an explicit approval file:

```json
{
  "sample_item_001": {
    "access": "public",
    "policy": "example-public-material-policy",
    "approved_by": "REPLACE_WITH_APPROVING_AUTHORITY"
  }
}
```

Public web visibility and a submitted Sheet row do not supply this decision.
An existing approval is reusable only with the same source node/checksum.

## Run offline

```sh
python3 -m scripts.manifest_worker.intake \
  --snapshot work/relationships.json \
  --source-root "$PWD/work/sources" \
  --config work/config.json \
  --requests work/requests.csv \
  --existing-inventory work/inventory.json \
  --output work/intake-review-001
```

Add `--approvals work/approvals.json` after the operator records the decision.
The output directory must be new. Files are owner-private; no Google, AWS,
ArchivesSpace or publication writes occur. Exit 0 means all enabled rows passed
**structural intake and access approval**, not that images were remotely tested
or publication completed. Exit 1 preserves the report with blocked rows. Exit 2
means an input/command problem. Blank/disabled request rows are excluded.

Outputs:

- `relationships.json`: the exact snapshot used by the join.
- `inventory.candidate.json`: joined records, hashes, approvals and explicit holds.
- `preflight.json`: one outcome per enabled source URL, with item IDs and an action.

Typical outcomes are `Unmapped source`, `Incomplete relationship export`,
`Ambiguous source`, `Missing source mapping`, `Missing retained source`,
`Invalid source manifest`, `Awaiting access approval`, `Inventory conflict`,
`Withdrawn`, `Invalid request`, and `Ready`.

A parent pointing to several objects requires individual digital-object URLs in
this first release. Intake must never pick whichever child happened to convert.
Blocked URL keys survive in inventory metadata so derived node aliases cannot
bypass this rule. A failed refresh preserves the old source identity/checksum
with an `intake_hold`; both CLI and Sheet conversion refuse that held item.
A subsequent complete, corrected snapshot can clear the hold.

## Validate and release

Keep the candidate separate from the live scheduled worker. Review every
non-Ready row; missing pages must be corrected at the source or explicitly
handled as a different media type. The current converter supports paged images,
not PDF-to-IIIF synthesis. A zero-page source is never published.

For approved, structurally ready items, point a **separate dry-run configuration**
at the candidate and retained sources. Run the existing CSV worker without
`--publish` (see README) to verify every image service. Use separate private state
so this test cannot disturb publication ownership receipts. The Sheet dry-run
command also writes Results, so use it only when those result writes are intended.

After validation, install approved inventory/sources as one reviewed change while
the scheduled worker is stopped or under its outer schedule lock. Preserve
existing source files and state/receipts, back up the old inventory, then resume
and check the next job. Never copy private input, approval, or state files into
the public web root. Install this code release before using intake hold metadata;
older releases do not enforce it. Changed inventory affects operation identity,
so Ready rows are reconsidered without inventing new request IDs. Results retains
history: the newest row for a request describes the latest attempt.

Broad-scale follow-up: automate the collection-wide relationship export and
its completeness check, batch approval decisions, and bounded export refreshes.
This intake command can support later collection-wide exports; use the reviewed
relationship snapshot as the source of mappings instead of a separate spreadsheet-only database.
