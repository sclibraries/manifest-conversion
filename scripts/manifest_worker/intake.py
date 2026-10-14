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


def _record_index(snapshot):
    records = {}
    for record in snapshot['records']:
        key = normalized_source(record['url'])
        if key in records:
            raise ValueError('Duplicate record URL in relationship export: ' + record['url'])
        records[key] = record
    return records


def _source_index(snapshot):
    source_index = {}
    for number, source in enumerate(snapshot['sources']):
        for url in aliases(source):
            source_index.setdefault(normalized_source(url), set()).add(number)
    return source_index


def _relationship_index(records):
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
    return items, url_items, incomplete


def _add_evidence_aliases(name, evidence, url_items):
    for urls in ('source_urls', 'record_urls'):
        for url in evidence[urls]:
            key = normalized_source(url)
            url_items.setdefault(key, set()).add(name)


def _match_source(name, evidence, source_index, snapshot, url_items):
    matches = set()
    for url in evidence['source_urls']:
        matches.update(source_index.get(normalized_source(url), set()))
    _add_evidence_aliases(name, evidence, url_items)
    if not matches:
        return None, (
            'Missing source mapping',
            'Export the Compass alias-to-node relationship and retain its manifest.',
        )
    if len(matches) != 1:
        return None, (
            'Ambiguous source mapping',
            'Conflicting Compass source records match this object; reconcile the export.',
        )

    source = snapshot['sources'][next(iter(matches))]
    for url in aliases(source):
        url_items.setdefault(normalized_source(url), set()).add(name)
    return source, None


def _convert_source_manifest(name, source, raw, config):
    checksum = digest(raw)
    if source.get('source_sha256') and checksum != source['source_sha256']:
        raise ValueError('Retained source checksum differs from export')
    manifest = load_json(raw)
    node = str(source['node_id'])
    expected_id = 'https://compass.fivecolleges.edu/node/' + node + '/manifest'
    if manifest.get('@id') != expected_id:
        raise ValueError('Manifest identity differs from exported Compass node')
    identifiers = [
        field.get('value')
        for field in manifest.get('metadata', [])
        if field.get('label') == 'Identifier'
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
    return node, checksum, details


def _load_source_manifest(name, source, source_root, config):
    try:
        raw = bounded_file(contained(source_root, source['source_file']))
    except FileNotFoundError:
        return None, (
            'Missing retained source',
            'Retain the Compass manifest in source_root, then rerun intake.',
        )
    except (OSError, ValueError, KeyError) as error:
        return None, ('Invalid source manifest', str(error))
    try:
        return _convert_source_manifest(name, source, raw, config), None
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        return None, ('Invalid source manifest', str(error))


def _candidate_status(name, candidate, details, config):
    if name in config.get('withdrawn_items', []):
        return 'Withdrawn', 'Item is withdrawn; do not publish.'
    if (
        candidate.get('access') != 'public'
        or not candidate.get('policy')
        or not candidate.get('approved_by')
    ):
        return (
            'Awaiting access approval',
            'Mapping and manifest structure verified; operator public-access approval is required.',
        )
    message = str(len(details['pages'])) + (
        ' pages structurally validated; run the worker dry run to verify image '
        'services before publication.'
    )
    return 'Ready', message


def _make_candidate(
    name, evidence, source, source_data, snapshot_hash, existing, approvals, config
):
    node, checksum, details = source_data
    previous = existing.get(name, {})
    if previous and (
        str(previous.get('node_id')) != node or previous.get('source_sha256') != checksum
    ):
        return None, (
            'Inventory conflict',
            'Existing source identity or checksum changed; review replacement before '
            'installing inventory.',
        )

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
    return candidate, _candidate_status(name, candidate, details, config)


def _prepare_item(
    name,
    evidence,
    source_index,
    snapshot,
    source_root,
    config,
    existing,
    approvals,
    url_items,
    snapshot_hash,
):
    source, outcome = _match_source(name, evidence, source_index, snapshot, url_items)
    if outcome:
        return None, outcome
    source_data, outcome = _load_source_manifest(name, source, source_root, config)
    if outcome:
        return None, outcome
    return _make_candidate(
        name,
        evidence,
        source,
        source_data,
        snapshot_hash,
        existing,
        approvals,
        config,
    )


def _prepare_items(
    items, source_index, snapshot, source_root, config, existing, approvals, url_items
):
    inventory = copy.deepcopy(existing)
    outcomes = {}
    snapshot_hash = digest(encode(snapshot))
    for name, evidence in sorted(items.items()):
        candidate, outcome = _prepare_item(
            name,
            evidence,
            source_index,
            snapshot,
            source_root,
            config,
            existing,
            approvals,
            url_items,
            snapshot_hash,
        )
        outcomes[name] = outcome
        if candidate is not None:
            inventory[name] = candidate
    return inventory, outcomes


def _blocked_aliases(url_items, incomplete):
    return incomplete | {key for key, names in url_items.items() if len(names) > 1}


def _apply_inventory_holds(inventory, outcomes, url_items, blocked_aliases):
    for name, candidate in inventory.items():
        if name in outcomes and outcomes[name][0] not in ('Ready', 'Awaiting access approval'):
            candidate['intake_hold'] = outcomes[name][0] + ': ' + outcomes[name][1]
        known_aliases = {normalized_source(url) for url in aliases(candidate)}
        if candidate.get('original_uri'):
            known_aliases.add(normalized_source(candidate['original_uri']))
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


def _build_report(urls, url_items, incomplete, outcomes):
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
    return report


def prepare(snapshot, source_root, config, urls, approvals, existing):
    """Join exact exported relationships; no guessed aliases or access policies.

    records contain either item_id/source_urls or digital_object_urls. Each URL
    is the origin-qualified record actually exported; test and production records
    are independent evidence. sources link those Compass aliases to retained JSON.
    """
    records = _record_index(snapshot)
    source_index = _source_index(snapshot)
    items, url_items, incomplete = _relationship_index(records)
    inventory, outcomes = _prepare_items(
        items, source_index, snapshot, source_root, config, existing, approvals, url_items
    )
    blocked = _blocked_aliases(url_items, incomplete)
    _apply_inventory_holds(inventory, outcomes, url_items, blocked)
    return inventory, _build_report(urls, url_items, incomplete, outcomes)


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
