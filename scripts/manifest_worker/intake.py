"""Prepare an inventory from retained relationship evidence, without remote writes."""

import argparse
import copy
import csv
import json
import sys
from pathlib import Path

from .core import contained, convert, digest, encode, load_json, safe_id
from .runner import atomic_write, bounded_file
from .sheets import normalized_source, parse_requests


def aliases(source):
    node = str(source['node_id'])
    if not node.isascii() or not node.isdigit():
        raise ValueError('Source node_id must be numeric')
    return list(source.get('source_urls', [])) + [
        'https://compass.fivecolleges.edu/node/' + node,
        'https://compass.fivecolleges.edu/node/' + node + '/manifest',
    ]


def prepare(snapshot, source_root, config, urls, approvals, existing):  # noqa: C901, PLR0915
    """Join exact exported relationships; no guessed aliases or access policies.

    records contain either item_id/source_urls or digital_object_urls. Each URL
    is the origin-qualified record actually exported; test and production records
    are independent evidence. sources link those Compass aliases to retained JSON.
    """
    records = {}
    for record in snapshot['records']:
        key = normalized_source(record['url'])
        if key in records:
            raise ValueError('Duplicate record URL in relationship export: ' + record['url'])
        records[key] = record
    source_index = {}
    for number, source in enumerate(snapshot['sources']):
        for url in aliases(source):
            source_index.setdefault(normalized_source(url), set()).add(number)

    items = {}
    url_items = {}
    incomplete = set()
    for key, record in records.items():
        if record.get('item_id'):
            name = safe_id(record['item_id'])
            item = items.setdefault(name, {'source_urls': set(), 'record_urls': set()})
            item['source_urls'].update(record.get('source_urls', []))
            item['record_urls'].add(record['url'])
            url_items.setdefault(key, set()).add(name)
    for key, record in records.items():
        for child in record.get('digital_object_urls', []):
            target = records.get(normalized_source(child), {})
            if not target.get('item_id'):
                incomplete.add(key)
                continue
            name = safe_id(target['item_id'])
            url_items.setdefault(key, set()).add(name)
            items[name]['record_urls'].add(record['url'])

    inventory = copy.deepcopy(existing)
    outcomes = {}
    snapshot_hash = digest(encode(snapshot))
    for name, evidence in sorted(items.items()):
        matches = set()
        for url in evidence['source_urls']:
            matches.update(source_index.get(normalized_source(url), set()))
            url_items.setdefault(normalized_source(url), set()).add(name)
        for url in evidence['record_urls']:
            url_items.setdefault(normalized_source(url), set()).add(name)
        if not matches:
            outcomes[name] = (
                'Missing source mapping',
                'Export the Compass alias-to-node relationship and retain its manifest.',
            )
            continue
        if len(matches) != 1:
            outcomes[name] = (
                'Ambiguous source mapping',
                'Conflicting Compass source records match this object; reconcile the export.',
            )
            continue
        source = snapshot['sources'][next(iter(matches))]
        for url in aliases(source):
            url_items.setdefault(normalized_source(url), set()).add(name)
        try:
            raw = bounded_file(contained(source_root, source['source_file']))
        except FileNotFoundError:
            outcomes[name] = (
                'Missing retained source',
                'Retain the Compass manifest in source_root, then rerun intake.',
            )
            continue
        except (OSError, ValueError, KeyError) as error:
            outcomes[name] = ('Invalid source manifest', str(error))
            continue
        try:
            checksum = digest(raw)
            if source.get('source_sha256') and checksum != source['source_sha256']:
                raise ValueError('Retained source checksum differs from export')
            manifest = load_json(raw)
            node = str(source['node_id'])
            if manifest.get('@id') != 'https://compass.fivecolleges.edu/node/' + node + '/manifest':
                raise ValueError('Manifest identity differs from exported Compass node')
            identifiers = [
                m.get('value')
                for m in manifest.get('metadata', [])
                if m.get('label') == 'Identifier'
            ]
            if identifiers and any(value != name for value in identifiers):
                raise ValueError(
                    'Manifest Identifier disagrees with ArchivesSpace digital-object identifier'
                )
            sequences = manifest.get('sequences', [])
            if len(sequences) == 1 and not sequences[0].get('canvases'):
                raise ValueError(
                    'Compass manifest has no pages; repair or export the source before conversion'
                )
            _, details = convert(
                manifest,
                config['manifest_base'].rstrip('/') + '/' + name + '.json',
                config['image_base'],
            )
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            outcomes[name] = ('Invalid source manifest', str(error))
            continue
        previous = existing.get(name, {})
        if previous and (
            str(previous.get('node_id')) != node or previous.get('source_sha256') != checksum
        ):
            outcomes[name] = (
                'Inventory conflict',
                'Existing source identity or checksum changed; review replacement before '
                'installing inventory.',
            )
            continue
        candidate = copy.deepcopy(previous)
        candidate.pop('intake_hold', None)
        candidate.update(
            node_id=node,
            source_file=source['source_file'],
            source_sha256=checksum,
            source_urls=sorted(
                set(previous.get('source_urls', []))
                | evidence['source_urls']
                | evidence['record_urls']
                | set(source.get('source_urls', []))
            ),
            intake_snapshot_sha256=snapshot_hash,
        )
        candidate.setdefault('original_uri', sorted(evidence['source_urls'])[0])
        candidate.setdefault('aspace_record', sorted(evidence['record_urls'])[0])
        approval = approvals.get(name, {})
        if approval:
            for field in ('access', 'policy', 'approved_by'):
                candidate[field] = approval.get(field, '')
        inventory[name] = candidate
        if name in config.get('withdrawn_items', []):
            outcomes[name] = ('Withdrawn', 'Item is withdrawn; do not publish.')
        elif (
            candidate.get('access') != 'public'
            or not candidate.get('policy')
            or not candidate.get('approved_by')
        ):
            outcomes[name] = (
                'Awaiting access approval',
                'Mapping and manifest structure verified; operator public-access approval '
                'is required.',
            )
        else:
            outcomes[name] = (
                'Ready',
                str(len(details['pages']))
                + ' pages structurally validated; run the worker dry run to verify image '
                'services before publication.',
            )

    # A parent with missing/ambiguous children must never become a one-item alias
    # merely because only one child survived conversion/approval checks.
    blocked_aliases = incomplete | {key for key, names in url_items.items() if len(names) > 1}
    for name, candidate in inventory.items():
        if name in outcomes and outcomes[name][0] not in ('Ready', 'Awaiting access approval'):
            candidate['intake_hold'] = outcomes[name][0] + ': ' + outcomes[name][1]
        known_aliases = {normalized_source(url) for url in aliases(candidate)}
        if candidate.get('original_uri'):
            known_aliases.add(normalized_source(candidate['original_uri']))
        # Keep old unresolved holds outside this snapshot. A new complete export
        # can clear its own holds. Include derived node URLs used by Sheets.
        previous_blocks = set(candidate.get('blocked_source_keys', [])) - set(url_items)
        candidate['blocked_source_keys'] = sorted(
            previous_blocks | (known_aliases & blocked_aliases)
        )
        candidate['source_urls'] = [
            url
            for url in candidate.get('source_urls', [])
            if normalized_source(url) not in blocked_aliases
        ]
        if (
            candidate.get('original_uri')
            and normalized_source(candidate['original_uri']) in blocked_aliases
        ):
            candidate.pop('original_uri')

    report = []
    for url in urls:
        try:
            key = normalized_source(url)
        except ValueError as error:
            report.append(
                {
                    'source_url': url,
                    'item_ids': [],
                    'status': 'Invalid request',
                    'message': str(error),
                }
            )
            continue
        names = sorted(url_items.get(key, set()))
        if key in incomplete:
            status, message = (
                'Incomplete relationship export',
                'Export every linked digital object before resolving this parent record.',
            )
        elif not names:
            status, message = (
                'Unmapped source',
                'Include this URL and its linked digital objects in the relationship export.',
            )
        elif len(names) > 1:
            status, message = (
                'Ambiguous source',
                'This URL refers to several digital objects; submit their individual URLs.',
            )
        else:
            status, message = outcomes[names[0]]
        report.append({'source_url': url, 'item_ids': names, 'status': status, 'message': message})
    return inventory, report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', type=Path, required=True)
    parser.add_argument('--source-root', type=Path, required=True)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument(
        '--requests', type=Path, required=True, help='Requests CSV with the Sheet column headers'
    )
    parser.add_argument('--approvals', type=Path, help='Explicit per-item public-access decisions')
    parser.add_argument('--existing-inventory', type=Path)
    parser.add_argument(
        '--output', type=Path, required=True, help='New private directory; never the live inventory'
    )
    args = parser.parse_args(argv)

    def read(path):
        return load_json(bounded_file(path))

    with args.requests.open(newline='', encoding='utf-8-sig') as stream:
        requests = parse_requests(list(csv.reader(stream)))
    snapshot = read(args.snapshot)
    inventory, report = prepare(
        snapshot,
        args.source_root,
        read(args.config),
        [r['source_url'] for r in requests if r['enabled']],
        read(args.approvals) if args.approvals else {},
        read(args.existing_inventory) if args.existing_inventory else {},
    )
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    atomic_write(args.output / 'inventory.candidate.json', encode(inventory))
    atomic_write(args.output / 'preflight.json', encode(report))
    # Store provenance needed to reproduce the join without live services.
    atomic_write(args.output / 'relationships.json', encode(snapshot))
    summary = {}
    for row in report:
        summary[row['status']] = summary.get(row['status'], 0) + 1
    print(json.dumps({'requests': len(report), 'outcomes': summary, 'output': str(args.output)}))
    return 1 if any(row['status'] != 'Ready' for row in report) else 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, OSError, KeyError, TypeError) as error:
        print('manifest-intake: ' + str(error), file=sys.stderr)
        sys.exit(2)
