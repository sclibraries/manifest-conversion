"""Bounded public-record discovery with retained sources and per-request evidence.

Call under the worker lock. Neither an accessible URL nor discovery grants
publication approval. Failed lookups are retried with a changed Ready retry token.
"""
import copy
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from .core import contained, convert, digest, encode, load_json, safe_id
from .runner import atomic_write, bounded_file, publication_allowed
from .urls import ASPACE_HOSTS, COMPASS_HOST, normalized_source, source_url

RECORD = re.compile(r'/repositories/([0-9]+)/(archival_objects|digital_objects)/([0-9]+)$')
NODE = re.compile(r'/node/([0-9]+)(?:/manifest)?$')
OBJECT = re.compile(r'/(?:islandora/)?object/[^/]+$')


class DiscoveryError(ValueError):
    pass


class InventoryConflict(DiscoveryError):
    pass


class RecordPage(HTMLParser):
    """Read public links and the labelled identifier, without running page scripts."""
    VOID = {'area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr'}

    def __init__(self, raw):
        super().__init__(convert_charrefs=True)
        self.links = set()
        self.identifiers = []
        self.stack = []
        self.feed(raw.decode('utf-8'))

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'a' and attrs.get('href'):
            self.links.add(attrs['href'])
        if attrs.get('data-file-uri'):
            self.links.add(attrs['data-file-uri'])
        if tag not in self.VOID:
            self.stack.append((tag, attrs.get('class', '').split()))

    def handle_endtag(self, tag):
        for index in range(len(self.stack)-1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        classes = {name for _, names in self.stack for name in names}
        if 'identifier' in classes and 'component' in classes and data.strip():
            self.identifiers.append(data.strip())


def classify(url, hosts):
    parsed = urlsplit(url)
    if parsed.hostname == COMPASS_HOST and (NODE.fullmatch(parsed.path) or OBJECT.fullmatch(parsed.path)):
        return 'compass'
    if parsed.hostname in hosts and RECORD.fullmatch(parsed.path):
        return 'aspace'
    raise DiscoveryError('Unsupported URL: use an ArchivesSpace archival/digital object or a Compass object/node/manifest URL.')


def _compatible(item, inventory, config, key):
    """A different alias must not bypass an operator hold or change identity."""
    name = item['item_id']
    merged = None
    keys = {normalized_source(alias) for alias in item['source_urls']} | {key}
    if name in config.get('withdrawn_items', []):
        raise InventoryConflict('Item is withdrawn; do not republish.')
    for other_name, previous in inventory.items():
        if keys.intersection(previous.get('blocked_source_keys', [])):
            raise InventoryConflict('Source has an unresolved ambiguity or hold in the operator inventory.')
        same_node = str(previous.get('node_id')) == item['node_id']
        if other_name == name or same_node:
            if previous.get('intake_hold'):
                raise InventoryConflict('Intake hold: ' + previous['intake_hold'])
            if other_name != name or not same_node or previous.get('source_sha256') != item['source_sha256']:
                raise InventoryConflict('Discovered source changed or conflicts with an existing identity; operator review required.')
            if not publication_allowed(previous):
                raise InventoryConflict('Existing inventory access decision requires operator approval; discovery cannot override it.')
            merged = copy.deepcopy(previous)
            merged.update(item_id=name)
            # Preserve approved source/receipt identity; only add observed aliases.
            merged['source_urls'] = sorted(set(merged.get('source_urls', [])) | set(item['source_urls']))
    return merged if merged is not None else copy.deepcopy(item)


def discover(value, config, fetch, inventory, retry_token='READY'):
    url = source_url(value)
    options = config.get('discovery', {})
    hosts = options.get('aspace_public_hosts', ASPACE_HOSTS)
    classify(url, hosts)  # Reject unapproved hosts/paths before any network request.
    key = normalized_source(url)
    root = contained(config['state_dir'], 'discovery')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    cache_path = contained(root, 'cache.json')
    cache = load_json(bounded_file(cache_path)) if cache_path.exists() else {'items': {}, 'errors': {}}
    error_key = digest(encode([key, retry_token, hosts]))
    if key in cache['items']:
        item = cache['items'][key]
        if digest(bounded_file(contained(config['source_root'], item['source_file']))) != item['source_sha256']:
            raise DiscoveryError('Retained discovery source checksum mismatch; inspect the private source files.')
        return _compatible(item, inventory, config, key)
    if error_key in cache['errors']:
        raise DiscoveryError(cache['errors'][error_key])
    if datetime.now(timezone.utc).date().isoformat() >= '2027-07-01':
        raise DiscoveryError('Live Compass discovery has retired; import a retained relationship export.')
    evidence = []
    aliases = {url}

    def read(target):
        try:
            raw, _ = fetch(target)
            evidence.append({'url': target, 'sha256': digest(raw)})
            return raw
        except (OSError, ValueError) as error:
            raise DiscoveryError('Source unavailable: ' + str(error)[:250] + '; increment Ready to RETRY:n after correction.') from error

    def record(target):
        page = RecordPage(read(target))
        links = {source_url(urljoin(target, link)) for link in page.links
                 if link and not link.startswith(('javascript:', 'mailto:', 'tel:', '#'))
                 and urlsplit(urljoin(target, link)).scheme == 'https'
                 and not urlsplit(urljoin(target, link)).query}
        return page, links

    try:
        expected_id = None
        aspace_record = ''
        if classify(url, hosts) == 'aspace':
            aspace_record = url
            page, links = record(url)
            match = RECORD.fullmatch(urlsplit(url).path)
            if match[2] == 'archival_objects':
                children = {link for link in links if urlsplit(link).hostname == urlsplit(url).hostname
                            and re.fullmatch('/repositories/'+match[1]+r'/digital_objects/[0-9]+', urlsplit(link).path)}
                if len(children) > 1:
                    raise DiscoveryError('Record links to multiple digital objects; submit each digital-object URL separately.')
                if not children:
                    raise DiscoveryError('No digital record links returned; public-page access may be blocked by a challenge. Ask the host to allow the worker, or submit the Compass URL.')
                digital = next(iter(children))
                aliases.add(digital)
                page, links = record(digital)
            if len(set(page.identifiers)) != 1:
                raise DiscoveryError('Digital-object identifier missing or ambiguous; check public-page access and record metadata.')
            expected_id = safe_id(page.identifiers[0])
            compass = {link for link in links if urlsplit(link).hostname == COMPASS_HOST
                       and (NODE.fullmatch(urlsplit(link).path) or OBJECT.fullmatch(urlsplit(link).path))}
            if len(compass) != 1:
                raise DiscoveryError('Digital object needs exactly one Compass File Version; other media or multiple sources need review.')
            url = next(iter(compass))
            aliases.add(url)
        parsed = urlsplit(url)
        node_match = NODE.fullmatch(parsed.path)
        node_url = 'https://' + COMPASS_HOST + '/node/' + node_match[1] if node_match else url
        node = load_json(read(node_url+'?_format=json'))
        if not isinstance(node, dict):
            raise DiscoveryError('Compass node metadata must be an object.')
        node_ids = node.get('nid')
        if not isinstance(node_ids, list) or len(node_ids) != 1 or not isinstance(node_ids[0], dict):
            raise DiscoveryError('Compass node identifier is missing or malformed.')
        node_id = str(node['nid'][0]['value'])
        if not node_id.isascii() or not node_id.isdigit() or (node_match and node_match[1] != node_id):
            raise DiscoveryError('Returned Compass node does not match the requested node.')
        fields = node.get('field_local_identifier') or node.get('field_identifier', [])
        if not isinstance(fields, list) or any(not isinstance(entry, dict) or not isinstance(entry.get('value'), str) for entry in fields):
            raise DiscoveryError('Compass stable identifier metadata is malformed.')
        identifiers = {entry['value'] for entry in fields}
        if len(identifiers) != 1:
            raise DiscoveryError('Compass stable identifier missing or ambiguous; source metadata needs review.')
        name = safe_id(next(iter(identifiers)))
        if expected_id and expected_id != name:
            raise DiscoveryError('ArchivesSpace and Compass identifier disagree; no mapping was guessed.')
        canonical_node = 'https://' + COMPASS_HOST + '/node/' + node_id
        manifest_url = canonical_node + '/manifest'
        raw = read(manifest_url)
        manifest = load_json(raw)
        if not isinstance(manifest, dict):
            raise DiscoveryError('Source manifest must be an object.')
        if manifest.get('@id') != manifest_url:
            raise DiscoveryError('Source manifest identity does not match the discovered node.')
        metadata = manifest.get('metadata', [])
        if not isinstance(metadata, list) or any(not isinstance(entry, dict) for entry in metadata):
            raise DiscoveryError('Source manifest metadata is malformed.')
        manifest_ids = [m.get('value') for m in metadata if m.get('label') == 'Identifier']
        if manifest_ids and any(identifier != name for identifier in manifest_ids):
            raise DiscoveryError('Manifest identifier disagrees with the discovered object.')
        sequences = manifest.get('sequences', [])
        if not isinstance(sequences, list) or any(not isinstance(sequence, dict) for sequence in sequences):
            raise DiscoveryError('Source manifest sequence is malformed.')
        if len(sequences) == 1 and not sequences[0].get('canvases'):
            raise DiscoveryError('Source has no image pages; this may be a PDF-only object. Use its media delivery path, not image-manifest conversion.')
        convert(manifest, config['manifest_base'].rstrip('/')+'/'+name+'.json', config['image_base'])
        aliases.update((canonical_node, manifest_url))
        checksum = digest(raw)
        item = {'item_id': name, 'node_id': node_id, 'source_sha256': checksum,
                'source_file': 'discovery/'+checksum+'.json', 'source_urls': sorted(aliases),
                'aspace_record': aspace_record, 'original_uri': url}
        result = _compatible(item, inventory, config, key)
        for alias in aliases:
            previous = cache['items'].get(normalized_source(alias))
            if previous and any(previous.get(field) != item[field] for field in ('item_id','node_id','source_sha256')):
                raise DiscoveryError('Discovered alias conflicts with retained mapping; no cached identity was replaced.')
        atomic_write(contained(config['source_root'], item['source_file']), raw)
        evidence_id = digest(encode(item))
        atomic_write(contained(root, 'evidence/'+evidence_id+'.json'), encode({'item':item,'reads':evidence}))
        for alias in aliases:
            cache['items'][normalized_source(alias)] = item
        atomic_write(cache_path, encode(cache))
        return result
    except (ValueError, KeyError, TypeError, UnicodeError) as error:
        if isinstance(error, InventoryConflict):
            raise
        message = str(error)[:500]
        if not isinstance(error, DiscoveryError):
            message = 'Source metadata could not be validated: ' + message
        cache['errors'][error_key] = message
        atomic_write(cache_path, encode(cache))
        raise DiscoveryError(message) from error
