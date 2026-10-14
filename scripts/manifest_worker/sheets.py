"""One-run Google Sheets queue. Conversion defaults to local dry-run mode."""

import argparse
import fcntl
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from .core import contained, digest, encode, https_url, load_json, safe_id
from .runner import (
    atomic_write,
    bounded_file,
    run_item,
    publication_allowed,
    validate_discovered_item,
)
from .transport import Fetcher
from .urls import normalized_source, ASPACE_HOSTS

REQUEST_HEADERS = ['Request ID', 'Source URL', 'Notes', 'Ready']
RESULT_HEADERS = [
    'Request ID',
    'Source URL',
    'Status',
    'Manifest URL',
    'Last Run',
    'Result',
    'Operation ID',
]


class UnmappedSource(ValueError):
    pass


def _spreadsheet_id(value):
    prefix = 'https://docs.google.com/'
    if value.startswith(prefix):
        parsed = urlsplit(value)
        path = parsed.path.split('/')
        if (
            parsed.netloc != 'docs.google.com'
            or path[1:3] != ['spreadsheets', 'd']
            or len(path) < 4
        ):
            raise ValueError('Invalid spreadsheet ID')
        value = path[3]
    if not re.fullmatch(r'[A-Za-z0-9_-]{20,100}', value):
        raise ValueError('Invalid spreadsheet ID')
    return value


def parse_requests(values):
    if not values or values[0] != REQUEST_HEADERS:
        raise ValueError('Requests header must be: ' + ', '.join(REQUEST_HEADERS))
    if len(values) > 1001:
        raise ValueError('Requests tab exceeds 1000 rows; archive old requests')
    rows = []
    seen = set()
    for cells in values[1:]:
        if not any(str(value).strip() for value in cells):
            continue
        if len(cells) > 4:
            raise ValueError('Unexpected request columns')
        cells = [str(value) for value in cells] + [''] * (4 - len(cells))
        name = safe_id(cells[0].strip())
        if name in seen:
            raise ValueError('Duplicate Request ID: ' + name)
        seen.add(name)
        if len(cells[1]) > 2048 or len(cells[2]) > 2000:
            raise ValueError('Source URL or Notes exceeds limit')
        ready = cells[3].strip().upper()
        if ready not in ('', 'FALSE', 'NO', 'TRUE', 'YES', 'READY') and not re.fullmatch(
            r'RETRY:[1-9][0-9]{0,5}', ready
        ):
            raise ValueError('Ready must be TRUE, FALSE, or RETRY:1 (increment for another retry)')
        rows.append(
            {
                'request_id': name,
                'source_url': cells[1].strip(),
                'notes': cells[2],
                'ready': ready,
                'enabled': ready in ('TRUE', 'YES', 'READY') or ready.startswith('RETRY:'),
            }
        )
    return rows


def _record_aliases(record):
    aliases = list(record.get('source_urls', []))
    if record.get('original_uri'):
        aliases.append(record['original_uri'])
    node = str(record.get('node_id', ''))
    if node.isascii() and node.isdigit():
        aliases += [
            'https://compass.fivecolleges.edu/node/' + node,
            'https://compass.fivecolleges.edu/node/' + node + '/manifest',
        ]
    return aliases


def _matching_items(key, inventory):
    matches = []
    for name, record in inventory.items():
        if key in record.get('blocked_source_keys', []):
            raise ValueError(
                'Ambiguous or incomplete source: reconcile the relationship export or submit '
                'an individual digital-object URL.'
            )
        if any(normalized_source(alias) == key for alias in _record_aliases(record)):
            matches.append(dict(record, item_id=safe_id(name)))
    return matches


def _require_approved(item, config):
    if item.get('intake_hold'):
        raise ValueError('Intake hold: ' + item['intake_hold'])
    if item['item_id'] in config.get('withdrawn_items', []):
        raise ValueError('Item is withdrawn; do not republish')
    if item.get('access') != 'public' or not item.get('policy') or not item.get('approved_by'):
        raise ValueError(
            item['item_id'] + ': mapping found; operator public-access approval is missing.'
        )


def approved_item(source, inventory, config):
    key = normalized_source(source)
    matches = _matching_items(key, inventory)
    if not matches:
        raise UnmappedSource(
            'Unmapped source: include this URL and its linked digital objects in the '
            'relationship export, then run intake.'
        )
    if len(matches) > 1:
        raise ValueError(
            'Ambiguous source: matches '
            + str(len(matches))
            + ' inventory entries; reconcile the mapping or submit individual digital-object URLs.'
        )
    item = matches[0]
    _require_approved(item, config)
    return item


def result_keys(values):
    if not values or values[0] != RESULT_HEADERS:
        raise ValueError('Results header missing or changed; initialize/restore the Results tab')
    if len(values) > 5001:
        raise ValueError('Results exceeds 5000 rows; archive before continuing')
    keys = set()
    for row in values[1:]:
        if len(row) >= 7 and row[6]:
            if row[6] in keys:
                raise ValueError('Duplicate result Operation ID; reconcile the Results tab')
            keys.add(row[6])
    return keys


def _validate_queue_config(config):
    options = config.get('discovery', {})
    if not isinstance(options, dict) or not isinstance(options.get('enabled', False), bool):
        raise ValueError('discovery must be an object with a Boolean enabled setting')
    approval = options.get('ready_publication', {})
    if not isinstance(approval, dict) or (
        approval
        and any(
            not isinstance(approval.get(field), str) or not approval[field].strip()
            for field in ('policy', 'approved_by')
        )
    ):
        raise ValueError(
            'ready_publication must be an object with a policy and approving authority, or omitted'
        )


def _queue_resolver(resolver):
    if resolver is None:
        from .discovery import discover

        resolver = discover
    return resolver


def _resolve_queue_item(request, inventory, config, publish, fetch, resolver):
    review_error = ''
    approval_required = False
    try:
        try:
            item = approved_item(request['source_url'], inventory, config)
        except UnmappedSource:
            options = config.get('discovery', {})
            if not options.get('enabled'):
                raise
            item = resolver(
                request['source_url'], config, fetch, inventory, retry_token=request['ready']
            )
            approval = options.get('ready_publication', {})
            if (
                not publication_allowed(item)
                and approval.get('policy')
                and approval.get('approved_by')
            ):
                item = dict(
                    item,
                    access='public',
                    policy=approval['policy'],
                    approved_by=approval['approved_by'],
                )
            approval_required = not publication_allowed(item)
    except ValueError as error:
        item = None
        review_error = str(error)
    return item, review_error, approval_required


def _request_fingerprint(request, item, review_error, publish, config):
    return digest(
        encode(
            {
                'request': request,
                'item': item,
                'review': review_error,
                'publish': publish,
                'config': config,
            }
        )
    )


def _limit_reached(summary, limit):
    return summary['processed'] + summary['reconciled'] >= limit


def _start_queue_request(request, path, sheet, publish):
    # Re-read immediately before processing; do not act on a shifted/edited snapshot.
    current = parse_requests(sheet.read_requests())
    if request not in current:
        return False
    atomic_write(
        path,
        encode(
            {
                'request': request,
                'mode': 'publish' if publish else 'dry-run',
                'status': 'processing',
            }
        ),
    )

    return True


def _queue_outcome(
    item, review_error, approval_required, config, publish, fetch, worker, validator, summary
):
    if review_error:
        summary['needs_review'] += 1
        return '', 'Needs review', review_error
    try:
        operation = validator if approval_required else worker
        receipt = operation(item, config, publish and not approval_required, fetch)
        url = receipt['manifest_url']
        status = (
            'Validated (approval required)'
            if approval_required
            else ('Published' if publish else 'Validated (dry run)')
        )
        message = str(len(receipt['pages'])) + ' pages validated. '
        message += (
            'HTTPS publication verified.'
            if publish and not approval_required
            else 'Manifest staged locally; URL is proposed, not published.'
        )
        if approval_required:
            message += ' Public-publication approval is required.'
            summary['needs_review'] += 1
    except (ValueError, OSError, KeyError, TypeError) as error:
        summary['failed'] += 1
        return '', 'Failed', str(error)[:500]
    return url, status, message


def _save_queue_result(request, path, sheet, result, keys, fingerprint, summary):
    # Save result before Google I/O. A failed status write never needs reconversion.
    atomic_write(path, encode({'request': request, 'status': 'completed', 'result': result}))
    latest = parse_requests(sheet.read_requests())
    if request not in latest:
        result[5] += (
            ' Request changed or was removed during processing; this result applies to '
            'the recorded source.'
        )
        atomic_write(path, encode({'request': request, 'status': 'completed', 'result': result}))
    sheet.append_result(result)
    keys.add(fingerprint)
    summary['processed'] += 1


def _run_queue_request(
    request,
    item,
    review_error,
    approval_required,
    path,
    sheet,
    config,
    publish,
    fetch,
    worker,
    validator,
    keys,
    fingerprint,
    summary,
):
    if not _start_queue_request(request, path, sheet, publish):
        return
    url, status, message = _queue_outcome(
        item, review_error, approval_required, config, publish, fetch, worker, validator, summary
    )
    result = [
        request['request_id'],
        request['source_url'],
        status,
        url,
        datetime.now(timezone.utc).isoformat(),
        message,
        fingerprint,
    ]
    _save_queue_result(request, path, sheet, result, keys, fingerprint, summary)


def _queue_state(sheet, config, sheet_id):
    rows = parse_requests(sheet.read_requests())
    keys = result_keys(sheet.read_results())
    state = Path(config['state_dir']).resolve()
    ledger = contained(state, 'sheet-' + digest(sheet_id.encode())[:24])
    ledger.mkdir(parents=True, exist_ok=True)
    summary = {'processed': 0, 'skipped': 0, 'reconciled': 0, 'failed': 0, 'needs_review': 0}
    return rows, keys, ledger, summary


def _queue_operation(request, inventory, config, publish, fetch, resolver, ledger):
    item, review_error, approval_required = _resolve_queue_item(
        request, inventory, config, publish, fetch, resolver
    )
    # Mode/config/approval changes are new operations; row positions never identify work.
    fingerprint = _request_fingerprint(request, item, review_error, publish, config)
    path = contained(ledger, fingerprint + '.json')
    previous = load_json(bounded_file(path)) if path.exists() else None
    return item, review_error, approval_required, fingerprint, path, previous


def _skip_processed_operation(fingerprint, keys, summary):
    if fingerprint in keys:
        summary['skipped'] += 1
        return True
    return False


def _reconcile_operation(fingerprint, previous, sheet, keys, summary):
    if previous and previous.get('result'):
        sheet.append_result(previous['result'])
        keys.add(fingerprint)
        summary['reconciled'] += 1
        return True
    return False


def _process_queue_item(
    request,
    sheet,
    config,
    inventory,
    publish,
    fetch,
    resolver,
    worker,
    validator,
    limit,
    ledger,
    keys,
    summary,
):
    item, review_error, approval_required, fingerprint, path, previous = _queue_operation(
        request, inventory, config, publish, fetch, resolver, ledger
    )
    if _skip_processed_operation(fingerprint, keys, summary):
        return False
    if _limit_reached(summary, limit):
        return True
    if not _reconcile_operation(fingerprint, previous, sheet, keys, summary):
        _run_queue_request(
            request,
            item,
            review_error,
            approval_required,
            path,
            sheet,
            config,
            publish,
            fetch,
            worker,
            validator,
            keys,
            fingerprint,
            summary,
        )
    return False


def process_queue(
    sheet,
    config,
    inventory,
    publish,
    fetch,
    sheet_id,
    worker=run_item,
    limit=10,
    resolver=None,
    validator=validate_discovered_item,
):
    _validate_queue_config(config)
    resolver = _queue_resolver(resolver)
    rows, keys, ledger, summary = _queue_state(sheet, config, sheet_id)
    for request in rows:
        if not request['enabled']:
            continue
        if _limit_reached(summary, limit):
            break
        if _process_queue_item(
            request,
            sheet,
            config,
            inventory,
            publish,
            fetch,
            resolver,
            worker,
            validator,
            limit,
            ledger,
            keys,
            summary,
        ):
            break
    return summary


def _parse_args(argv):
    parser = argparse.ArgumentParser(
        description='Read Ready Sheet rows, convert locally, and append results. Default: dry run.'
    )
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--sheet', required=True, help='Spreadsheet ID or URL')
    parser.add_argument('--requests-tab', default='Requests')
    parser.add_argument('--results-tab', default='Results')
    parser.add_argument(
        '--credentials',
        type=Path,
        help='Service-account JSON; otherwise GOOGLE_APPLICATION_CREDENTIALS',
    )
    parser.add_argument('--initialize-results', action='store_true')
    parser.add_argument('--publish', action='store_true')
    parser.add_argument('--limit', type=int, default=10)
    return parser.parse_args(argv)


def _validate_args(args):
    if not 1 <= args.limit <= 100:
        raise ValueError('--limit must be between 1 and 100')
    if args.requests_tab == args.results_tab:
        raise ValueError('Requests and Results must be separate tabs')
    sheet_id = _spreadsheet_id(args.sheet)
    credential_path = args.credentials or os.environ.get('GOOGLE_APPLICATION_CREDENTIALS')
    if not credential_path:
        raise ValueError('Set GOOGLE_APPLICATION_CREDENTIALS or pass --credentials')
    return sheet_id, credential_path


def _load_config(args):
    config = load_json(bounded_file(args.config))
    for name in ('source_root', 'state_dir', 'inventory'):
        if not Path(config[name]).is_absolute():
            raise ValueError(name + ' must be absolute')
    for name in ('manifest_base', 'image_base'):
        if https_url(config[name]).query:
            raise ValueError(name + ' cannot include query parameters')
    inventory = load_json(bounded_file(Path(config['inventory'])))
    if not isinstance(inventory, dict):
        raise ValueError('Inventory must be an object')
    return config, inventory


def _prepare_state(config, publish):
    state = Path(config['state_dir']).resolve()
    state.mkdir(parents=True, exist_ok=True)
    os.chmod(state, 0o700)
    if publish:
        destination = Path(config['publish_dir']).resolve()
        if state.is_relative_to(destination) or destination.is_relative_to(state):
            raise ValueError('Public destination and private state must be separate')
    return state


def _process_locked(args, sheet_id, credential_path, config, inventory, state):
    from .sheets_api import GoogleSheet

    with contained(state, 'worker.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError('Another worker holds the state lock') from error
        sheet = GoogleSheet(sheet_id, credential_path, args.requests_tab, args.results_tab)
        if args.initialize_results:
            sheet.initialize_results()
        hosts = set(config['allowed_hosts'])
        if config.get('discovery', {}).get('enabled'):
            hosts.update(config['discovery'].get('aspace_public_hosts', ASPACE_HOSTS))
        fetch = Fetcher(hosts)
        result = process_queue(
            sheet, config, inventory, args.publish, fetch, sheet_id, limit=args.limit
        )
    return result, fetch


def main(argv=None):
    args = _parse_args(argv)
    sheet_id, credential_path = _validate_args(args)
    config, inventory = _load_config(args)
    state = _prepare_state(config, args.publish)
    result, fetch = _process_locked(args, sheet_id, credential_path, config, inventory, state)
    result.update(published_mode=args.publish, requests=fetch.requests, bytes=fetch.bytes)
    print(json.dumps(result))
    return 1 if result['failed'] or result['needs_review'] else 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, OSError, KeyError, TypeError, ImportError) as error:
        print('manifest-sheets: ' + str(error), file=sys.stderr)
        sys.exit(2)
