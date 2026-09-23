"""One-run Google Sheets queue. Conversion defaults to local dry-run mode."""
import argparse
import fcntl
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .core import contained, digest, encode, https_url, load_json, safe_id
from .runner import atomic_write, bounded_file, run_item
from .transport import Fetcher

REQUEST_HEADERS = ['Request ID', 'Source URL', 'Notes', 'Ready']
RESULT_HEADERS = ['Request ID', 'Source URL', 'Status', 'Manifest URL', 'Last Run', 'Result', 'Operation ID']


def normalized_source(value):
    parsed = https_url(value.strip())
    if parsed.query:
        raise ValueError('Source URL must not have query parameters')
    path = unquote(parsed.path).rstrip('/')
    if parsed.hostname == 'compass.fivecolleges.edu' and path.startswith('/object/'):
        path = '/islandora' + path
    return parsed.hostname.lower() + path


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
        cells = [str(value) for value in cells] + [''] * (4-len(cells))
        name = safe_id(cells[0].strip())
        if name in seen:
            raise ValueError('Duplicate Request ID: ' + name)
        seen.add(name)
        if len(cells[1]) > 2048 or len(cells[2]) > 2000:
            raise ValueError('Source URL or Notes exceeds limit')
        ready = cells[3].strip().upper()
        if ready not in ('', 'FALSE', 'NO', 'TRUE', 'YES', 'READY') and not re.fullmatch(r'RETRY:[1-9][0-9]{0,5}', ready):
            raise ValueError('Ready must be TRUE, FALSE, or RETRY:1 (increment for another retry)')
        rows.append({'request_id':name, 'source_url':cells[1].strip(), 'notes':cells[2],
                     'ready':ready, 'enabled':ready in ('TRUE','YES','READY') or ready.startswith('RETRY:')})
    return rows


def approved_item(source, inventory, config):
    key = normalized_source(source)
    matches = []
    for name, record in inventory.items():
        aliases = list(record.get('source_urls', []))
        if record.get('original_uri'):
            aliases.append(record['original_uri'])
        node = str(record.get('node_id', ''))
        if node.isascii() and node.isdigit():
            aliases += ['https://compass.fivecolleges.edu/node/'+node,
                        'https://compass.fivecolleges.edu/node/'+node+'/manifest']
        if any(normalized_source(alias) == key for alias in aliases):
            matches.append(dict(record, item_id=safe_id(name)))
    if len(matches) != 1:
        raise ValueError('Source must match exactly one operator-approved inventory entry')
    item = matches[0]
    if item['item_id'] in config.get('withdrawn_items', []):
        raise ValueError('Item is withdrawn; do not republish')
    if item.get('access') != 'public' or not item.get('policy') or not item.get('approved_by'):
        raise ValueError('Public access approval is missing')
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


def process_queue(sheet, config, inventory, publish, fetch, sheet_id, worker=run_item, limit=10):
    rows = parse_requests(sheet.read_requests())
    keys = result_keys(sheet.read_results())
    state = Path(config['state_dir']).resolve()
    ledger = contained(state, 'sheet-' + digest(sheet_id.encode())[:24])
    ledger.mkdir(parents=True, exist_ok=True)
    summary = {'processed':0, 'skipped':0, 'reconciled':0, 'failed':0, 'needs_review':0}
    for request in rows:
        if not request['enabled']:
            continue
        review_error = ''
        try:
            item = approved_item(request['source_url'], inventory, config)
        except ValueError as error:
            item = None
            review_error = str(error)
        # Mode/config/approval changes are new operations; row positions never identify work.
        fingerprint = digest(encode({'request':request, 'item':item, 'review':review_error,
                                     'publish':publish, 'config':config}))
        path = contained(ledger, fingerprint + '.json')
        previous = load_json(bounded_file(path)) if path.exists() else None
        if fingerprint in keys:
            summary['skipped'] += 1
            continue
        if summary['processed'] + summary['reconciled'] >= limit:
            break
        if previous and previous.get('result'):
            sheet.append_result(previous['result'])
            keys.add(fingerprint)
            summary['reconciled'] += 1
            continue
        # Re-read immediately before processing; do not act on a shifted/edited snapshot.
        current = parse_requests(sheet.read_requests())
        if request not in current:
            continue
        atomic_write(path, encode({'request':request, 'mode':'publish' if publish else 'dry-run',
                                   'status':'processing'}))
        url = ''
        if review_error:
            status = 'Needs review'
            message = review_error
            summary['needs_review'] += 1
        else:
            try:
                receipt = worker(item, config, publish, fetch)
                url = receipt['manifest_url']
                status = 'Published' if publish else 'Validated (dry run)'
                message = str(len(receipt['pages'])) + ' pages validated. '
                message += 'HTTPS publication verified.' if publish else 'Manifest staged locally; URL is proposed, not published.'
            except (ValueError, OSError, KeyError, TypeError) as error:
                status = 'Failed'
                message = str(error)[:500]
                summary['failed'] += 1
        result = [request['request_id'], request['source_url'], status, url,
                  datetime.now(timezone.utc).isoformat(), message, fingerprint]
        # Save result before Google I/O. A failed status write never needs reconversion.
        atomic_write(path, encode({'request':request, 'status':'completed', 'result':result}))
        latest = parse_requests(sheet.read_requests())
        if request not in latest:
            result[5] += ' Request changed or was removed during processing; this result applies to the recorded source.'
            atomic_write(path, encode({'request':request, 'status':'completed', 'result':result}))
        sheet.append_result(result)
        keys.add(fingerprint)
        summary['processed'] += 1
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description='Read Ready Sheet rows, convert locally, and append results. Default: dry run.')
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--sheet', required=True, help='Spreadsheet ID or URL')
    parser.add_argument('--requests-tab', default='Requests')
    parser.add_argument('--results-tab', default='Results')
    parser.add_argument('--credentials', type=Path, help='Service-account JSON; otherwise GOOGLE_APPLICATION_CREDENTIALS')
    parser.add_argument('--initialize-results', action='store_true')
    parser.add_argument('--publish', action='store_true')
    parser.add_argument('--limit', type=int, default=10)
    args = parser.parse_args(argv)
    if not 1 <= args.limit <= 100:
        raise ValueError('--limit must be between 1 and 100')
    if args.requests_tab == args.results_tab:
        raise ValueError('Requests and Results must be separate tabs')
    sheet_id = args.sheet
    if sheet_id.startswith('https://docs.google.com/spreadsheets/d/'):
        sheet_id = sheet_id.split('/d/',1)[1].split('/',1)[0]
    if not re.fullmatch(r'[A-Za-z0-9_-]{20,100}', sheet_id):
        raise ValueError('Invalid spreadsheet ID')
    credential_path = args.credentials or os.environ.get('GOOGLE_APPLICATION_CREDENTIALS')
    if not credential_path:
        raise ValueError('Set GOOGLE_APPLICATION_CREDENTIALS or pass --credentials')
    config = load_json(bounded_file(args.config))
    for name in ('source_root','state_dir','inventory'):
        if not Path(config[name]).is_absolute():
            raise ValueError(name+' must be absolute')
    for name in ('manifest_base','image_base'):
        if https_url(config[name]).query:
            raise ValueError(name+' cannot include query parameters')
    inventory = load_json(bounded_file(Path(config['inventory'])))
    if not isinstance(inventory,dict):
        raise ValueError('Inventory must be an object')
    state = Path(config['state_dir']).resolve()
    state.mkdir(parents=True,exist_ok=True)
    os.chmod(state,0o700)
    if args.publish:
        destination = Path(config['publish_dir']).resolve()
        if state.is_relative_to(destination) or destination.is_relative_to(state):
            raise ValueError('Public destination and private state must be separate')
    from .sheets_api import GoogleSheet
    with contained(state,'worker.lock').open('a') as lock:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Another worker holds the state lock')
        sheet = GoogleSheet(sheet_id, credential_path, args.requests_tab, args.results_tab)
        if args.initialize_results:
            sheet.initialize_results()
        fetch = Fetcher(config['allowed_hosts'])
        result = process_queue(sheet,config,inventory,args.publish,fetch,sheet_id,limit=args.limit)
    result.update(published_mode=args.publish, requests=fetch.requests, bytes=fetch.bytes)
    print(json.dumps(result))
    return 1 if result['failed'] or result['needs_review'] else 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError,OSError,KeyError,TypeError,ImportError) as error:
        print('manifest-sheets: '+str(error),file=sys.stderr)
        sys.exit(2)
