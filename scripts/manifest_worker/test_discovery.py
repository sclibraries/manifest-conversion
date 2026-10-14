import json
import tempfile
import unittest
from pathlib import Path

from .core import encode
from .test_worker import manifest
from .discovery import discover, DiscoveryError

AO = 'https://archives.example.org/repositories/4/archival_objects/592825'
DO = 'https://archives.example.org/repositories/4/digital_objects/13931'
COMPASS = 'https://compass.fivecolleges.edu/islandora/object/demo_item'
NODE = 'https://compass.fivecolleges.edu/node/123'


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {
            'source_root': str(self.root / 'sources'),
            'state_dir': str(self.root / 'state'),
            'manifest_base': 'https://example.org/manifests',
            'image_base': 'https://images.example.org/iiif/2',
            'discovery': {'enabled': True},
        }
        self.documents = {
            AO: b'<main><a href="/repositories/4/digital_objects/13931">Digital object</a></main>',
            DO: (
                '<div class="identifier"><span class="id-label">Identifier:</span>'
                '<span class="component">demo_item</span></div><li data-file-uri="'
                + COMPASS
                + '"><a href="'
                + COMPASS
                + '">File</a></li>'
            ).encode(),
            COMPASS + '?_format=json': encode(
                {'nid': [{'value': 123}], 'field_identifier': [{'value': 'demo_item'}]}
            ),
            NODE + '?_format=json': encode(
                {'nid': [{'value': 123}], 'field_identifier': [{'value': 'demo_item'}]}
            ),
            NODE + '/manifest': encode(manifest()),
        }
        self.calls = []

    def fetch(self, url):
        self.calls.append(url)
        if url not in self.documents:
            raise OSError('Unavailable')
        return self.documents[url], {}

    def run_discovery(self, url=AO, **kwargs):
        return discover(url, self.config, self.fetch, kwargs.pop('inventory', {}), **kwargs)

    def test_fresh_repository_four_url_discovers_without_inventory(self):
        item = self.run_discovery()
        self.assertEqual(item['item_id'], 'demo_item')
        self.assertEqual(item['node_id'], '123')
        self.assertEqual(self.calls, [AO, DO, COMPASS + '?_format=json', NODE + '/manifest'])
        self.assertTrue((self.root / 'sources' / item['source_file']).is_file())
        self.assertIn(AO, item['source_urls'])
        self.assertNotIn('approved_by', item)

    def test_fresh_discovery_item_and_retained_outputs_are_pinned(self):
        item = self.run_discovery()
        expected = {
            'aspace_record': AO,
            'item_id': 'demo_item',
            'node_id': '123',
            'original_uri': COMPASS,
            'source_file': (
                'discovery/f67bd611e4519bafeeb69a891986d566384aa4dc28c4146945bdcb6064c42824.json'
            ),
            'source_sha256': ('f67bd611e4519bafeeb69a891986d566384aa4dc28c4146945bdcb6064c42824'),
            'source_urls': [AO, DO, COMPASS, NODE, NODE + '/manifest'],
        }
        self.assertEqual(item, expected)

        cache = json.loads((self.root / 'state' / 'discovery' / 'cache.json').read_bytes())
        self.assertEqual(
            cache,
            {
                'errors': {},
                'items': {
                    alias.removeprefix('https://'): expected for alias in expected['source_urls']
                },
            },
        )
        source_path = self.root / 'sources' / expected['source_file']
        self.assertEqual(source_path.read_bytes(), self.documents[NODE + '/manifest'])

        evidence_files = list((self.root / 'state' / 'discovery' / 'evidence').glob('*.json'))
        self.assertEqual(len(evidence_files), 1)
        evidence = json.loads(evidence_files[0].read_bytes())
        self.assertEqual(evidence['item'], expected)
        self.assertEqual(
            [read['url'] for read in evidence['reads']],
            [AO, DO, COMPASS + '?_format=json', NODE + '/manifest'],
        )
        self.assertEqual(
            [read['sha256'] for read in evidence['reads']],
            [
                '6f5782133e2f252ba9d4928eb099bcbc9354d0b87fa289997cb8feda9bebde83',
                '9d5870cf1e76e7eade0bd1128f8b11abf31225463ac0140765992fd0fd1ebbbd',
                'cf36ecab4c5084e5dbbef33c6f7a5cff531f3864e5481cc2e92b0d4ba789d695',
                expected['source_sha256'],
            ],
        )

    def test_cached_mapping_and_compass_alias_need_no_network(self):
        original = self.run_discovery()
        self.calls.clear()
        self.assertEqual(self.run_discovery(), original)
        self.assertEqual(self.run_discovery(COMPASS)['item_id'], 'demo_item')
        self.assertEqual(self.calls, [])

    def test_bare_host_and_fragment_are_normalized(self):
        self.assertEqual(self.run_discovery(AO.removeprefix('https://') + '#top')['node_id'], '123')
        self.assertEqual(self.calls[0], AO)

    def test_node_and_manifest_urls_are_direct_inputs(self):
        self.assertEqual(self.run_discovery(NODE + '/manifest')['item_id'], 'demo_item')
        self.assertEqual(self.calls, [NODE + '?_format=json', NODE + '/manifest'])

    def test_foreign_host_or_record_path_never_fetched(self):
        for url in [
            'https://evil.example/foo',
            'https://archives.example.org/staff/users/1',
            'https://archives.example.org/repositories/4/archival_objects/1?x=1',
            'http://compass.fivecolleges.edu/node/123',
        ]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.run_discovery(url)
        self.assertEqual(self.calls, [])

    def test_multiple_children_and_cross_repository_links_are_not_guessed(self):
        self.documents[AO] = (
            b'<a href="/repositories/4/digital_objects/13931">A</a>'
            b'<a href="/repositories/4/digital_objects/13932">B</a>'
        )
        with self.assertRaisesRegex(DiscoveryError, 'multiple digital objects'):
            self.run_discovery()
        self.assertEqual(self.calls, [AO])

    def test_missing_or_challenge_page_is_not_empty_success(self):
        self.documents[AO] = b'<html>AWS WAF challenge</html>'
        with self.assertRaisesRegex(DiscoveryError, 'record links|challenge'):
            self.run_discovery()

    def test_identifier_mismatch_is_held(self):
        self.documents[COMPASS + '?_format=json'] = encode(
            {'nid': [{'value': 123}], 'field_identifier': [{'value': 'other'}]}
        )
        with self.assertRaisesRegex(DiscoveryError, 'identifier'):
            self.run_discovery()

    def test_node_input_must_match_returned_node(self):
        self.documents[NODE + '?_format=json'] = encode(
            {'nid': [{'value': 999}], 'field_identifier': [{'value': 'demo_item'}]}
        )
        with self.assertRaisesRegex(DiscoveryError, 'node'):
            self.run_discovery(NODE)

    def test_empty_manifest_is_not_published_or_cached_as_success(self):
        source = manifest()
        source['sequences'][0].pop('canvases')
        self.documents[NODE + '/manifest'] = encode(source)
        with self.assertRaisesRegex(DiscoveryError, 'no image pages.*PDF'):
            self.run_discovery()

    def test_manifest_identity_and_metadata_must_match(self):
        source = manifest()
        source['@id'] = NODE.replace('123', '999') + '/manifest'
        self.documents[NODE + '/manifest'] = encode(source)
        with self.assertRaisesRegex(DiscoveryError, 'manifest.*node'):
            self.run_discovery()

    def test_existing_approval_only_reused_for_identical_source(self):
        item = self.run_discovery(NODE)
        inventory = {
            'demo_item': dict(
                item, access='public', policy='example-policy', approved_by='operator'
            )
        }
        self.assertEqual(self.run_discovery(AO, inventory=inventory)['policy'], 'example-policy')
        inventory['demo_item']['source_sha256'] = '0' * 64
        with self.assertRaisesRegex(DiscoveryError, 'changed|conflict'):
            self.run_discovery(AO, inventory=inventory)

    def test_held_or_withdrawn_item_cannot_be_rediscovered_through_alias(self):
        item = self.run_discovery(NODE)
        with self.assertRaisesRegex(DiscoveryError, 'hold'):
            self.run_discovery(AO, inventory={'demo_item': dict(item, intake_hold='PDF')})
        self.config['withdrawn_items'] = ['demo_item']
        with self.assertRaisesRegex(DiscoveryError, 'withdrawn'):
            self.run_discovery(AO)

    def test_cached_source_corruption_is_detected(self):
        item = self.run_discovery()
        (self.root / 'sources' / item['source_file']).write_bytes(b'{}')
        with self.assertRaisesRegex(DiscoveryError, 'checksum'):
            self.run_discovery()

    def test_error_is_cached_until_explicit_retry(self):
        valid = self.documents[AO]
        self.documents[AO] = b'<html>challenge</html>'
        with self.assertRaises(DiscoveryError):
            self.run_discovery(retry_token='READY')
        self.documents[AO] = valid
        self.calls.clear()
        with self.assertRaises(DiscoveryError):
            self.run_discovery(retry_token='READY')
        self.assertEqual(self.calls, [])
        self.assertEqual(self.run_discovery(retry_token='RETRY:1')['node_id'], '123')

    def test_publication_policy_is_not_inferred_from_public_source(self):
        item = self.run_discovery()
        self.assertNotIn('policy', item)

    def test_later_conflicting_inventory_hold_is_not_skipped(self):
        item = self.run_discovery(NODE)
        approved = dict(item, access='public', policy='example-policy', approved_by='operator')
        inventory = {'demo_item': approved, 'other': dict(approved, intake_hold='operator hold')}
        with self.assertRaisesRegex(DiscoveryError, 'hold|conflict'):
            self.run_discovery(AO, inventory=inventory)

    def test_malformed_remote_shapes_are_reported_as_discovery_errors(self):
        bad_nodes = [
            {'nid': []},
            [],
            {'nid': [None]},
            {'nid': [{'value': 123}], 'field_identifier': [None]},
            {'nid': [{'value': 123}], 'field_identifier': [{'value': None}]},
        ]
        for index, node in enumerate(bad_nodes):
            self.documents[COMPASS + '?_format=json'] = encode(node)
            with self.subTest(node=node), self.assertRaises(DiscoveryError):
                self.run_discovery(retry_token='RETRY:' + str(index + 1))

    def test_malformed_manifests_are_reported_as_discovery_errors(self):
        for index, source in enumerate(
            [[], dict(manifest(), sequences=[None]), dict(manifest(), metadata=[None])]
        ):
            self.documents[NODE + '/manifest'] = encode(source)
            with self.subTest(source=source), self.assertRaises(DiscoveryError):
                self.run_discovery(retry_token='RETRY:' + str(index + 1))

    def test_bad_discovery_row_does_not_stop_next_row(self):
        from .sheets import process_queue, REQUEST_HEADERS
        from .test_sheets import Sheet

        sheet = Sheet()
        sheet.requests = [REQUEST_HEADERS, ['bad', AO, '', 'Ready'], ['good', NODE, '', 'Ready']]
        self.documents[COMPASS + '?_format=json'] = encode({'nid': []})

        def validate(item, config, publish, fetch):
            self.assertFalse(publish)
            return {
                'manifest_url': 'https://example.org/manifests/' + item['item_id'] + '.json',
                'pages': [{}],
            }

        result = process_queue(
            sheet, self.config, {}, True, self.fetch, 'mixed-discovery', validator=validate
        )
        self.assertEqual(result['processed'], 2)
        self.assertEqual(
            [row[2] for row in sheet.results[1:]], ['Needs review', 'Validated (approval required)']
        )

    def test_malformed_nested_manifest_does_not_stop_next_queue_row(self):
        from .sheets import process_queue, REQUEST_HEADERS
        from .test_sheets import Sheet

        sheet = Sheet()
        sheet.requests = [REQUEST_HEADERS, ['bad', AO, '', 'Ready'], ['good', NODE, '', 'Ready']]
        valid = self.documents[NODE + '/manifest']
        broken = manifest()
        broken['sequences'][0]['canvases'][0]['@id'] = 123
        self.documents[NODE + '/manifest'] = encode(broken)
        original_append = sheet.append_result

        def append(row):
            original_append(row)
            self.documents[NODE + '/manifest'] = valid

        sheet.append_result = append

        def validate(item, config, publish, fetch):
            return {'manifest_url': 'https://example.org/manifests/sample.json', 'pages': [{}]}

        result = process_queue(
            sheet, self.config, {}, False, self.fetch, 'nested-bad', validator=validate
        )
        self.assertEqual(result['processed'], 2)
        self.assertEqual(sheet.results[1][2], 'Needs review')
        self.assertEqual(sheet.results[2][2], 'Validated (approval required)')

    def test_existing_restriction_cannot_be_overridden_by_new_alias(self):
        item = self.run_discovery(NODE)
        inventory = {
            'demo_item': dict(
                item, access='restricted', policy='restricted', approved_by='reviewer'
            )
        }
        with self.assertRaisesRegex(DiscoveryError, 'access decision'):
            self.run_discovery(AO, inventory=inventory)


if __name__ == '__main__':
    unittest.main()
