# Automatic discovery for new Sheet URLs

This extends the existing Sheet queue. It needs no ArchivesSpace API credentials
and does not modify ArchivesSpace. It follows the submitted **public** record's
links to its digital object and Compass source. Discovery is opt-in; existing
operator inventory is checked first and its holds/withdrawals remain authoritative.

## Supported inputs

- Public ArchivesSpace archival-object and digital-object URLs on configured hosts,
  with any repository number. Production and test records are followed independently.
- Compass `/object/...`, `/islandora/object/...`, `/node/N` and `/node/N/manifest` URLs.
- URLs copied without `https://` from a configured ArchivesSpace host or Compass.
  Fragments and trailing slashes normalize; queries, credentials, HTTP and unapproved
  hosts/paths are rejected. Redirects remain restricted to configured HTTPS hosts.

Example input: `archives.example.org/repositories/4/archival_objects/592825`.
No AWS thumbnail URL, Drupal node number or manually written mapping is needed.

An archival record must link to one digital object for this first version. If it
links to several, staff submit the individual digital-object URLs. A digital
object must have its stable Identifier and one Compass File Version. The resolver
checks this identifier against Compass's `field_local_identifier` or
`field_identifier` and the source manifest. It never infers a node ID from a PID,
assumes production/test record IDs match, or substitutes a title for identity.

PDF-only and other unsupported objects receive an explanatory hold. No empty
image manifest is published. One bad source gets its own result; other rows continue.

## Configuration and publication policy

Add to the operator-controlled worker configuration:

```json
"discovery": {
  "enabled": true,
  "aspace_public_hosts": [
    "archives.example.org"
  ]
}
```

Without further policy, a newly discovered item is converted and its image-service
identifiers/dimensions are checked, but it is **not published**, even when the
scheduled command has `--publish`. Results says `Validated (approval required)`;
its URL is proposed, not public. This validation cannot create an approval or
publication ownership receipt. Existing explicitly approved items retain normal
publication behavior.

If an organization defines its Ready mark as confirmation that an authorized reviewer
checked public-access eligibility, explicitly configure that authority:

```json
"ready_publication": {
  "policy": "reviewed-public-material",
  "approved_by": "named approving authority"
}
```

Place this inside `discovery`. It authorizes only newly discovered sources;
existing restricted/held/ambiguous inventory cannot be overridden through a new
alias. Do not enable it until the operator adopts that policy. Merely reaching
an object on the public web is not treated as permission to publish it.

## Evidence, repeats and retries

The existing worker lock covers discovery and conversion. Successful discovery
retains manifests under `source_root/discovery/<sha256>.json`; mapping cache and
request URL/hash evidence live under `state_dir/discovery/`. The operator inventory
is not rewritten. Keep this private state and retained sources together when
moving or recovering a worker. Cached source checksums are verified before reuse.

A repeat run reuses saved mappings and existing Sheet operation receipts; no
network discovery or image work is needed for an unchanged completed request.
The same discovered object can also resolve through its observed node/manifest,
Compass or digital-object aliases. Discovery never fabricates aliases for a host
or record it did not inspect.

A failed lookup is retained with its Ready retry token. After fixing the source
or network route, increment Ready to `RETRY:1`, `RETRY:2`, etc. The source is then
retried. This prevents a cron loop from repeatedly fetching a blocked page or
flooding Results. The normal queue limit also bounds discovery work. Archive the
existing run evidence under the operator retention policy; no automatic deletion
is introduced.

Live discovery stops on 1 July 2027 with Compass retirement. Cached relationships
and retained manifests continue to work. A complete durable relationship export
remains necessary for sources not discovered before retirement.

## Public-page access prerequisite

The scheduled worker must be able to make HTTPS GET requests to each configured
public ArchivesSpace host and these record paths:

- `/repositories/*/archival_objects/*`
- `/repositories/*/digital_objects/*`

The worker identifies itself as `ManifestWorker/1`. Configure any bot protection to
return ordinary record HTML to the worker. No API access, staff credentials or
interactive browser service is required by this implementation. Verify the complete
public-page-to-manifest chain with an empty discovery cache before enabling scheduled
processing.

## Verification

Run `python3 -m unittest discover -s scripts/manifest_worker -t .` from the repository root.
