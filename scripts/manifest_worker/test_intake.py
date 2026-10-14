"""Offline intake must distinguish identity, source readiness, and permission."""

import copy
import tempfile
import unittest
from pathlib import Path

from .core import encode, digest
from .test_worker import manifest
from .intake import prepare


class IntakeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = manifest()
        self.node = self.source['@id'].split('/node/')[1].split('/')[0]
        self.alias = 'https://compass.fivecolleges.edu/object/demo:123'
        self.do = 'https://findingaids.example/repositories/2/digital_objects/1'
        self.ao = 'https://findingaids.example/repositories/2/archival_objects/2'
        self.snapshot = {
            'records': [
                {'url': self.do, 'item_id': 'sample', 'source_urls': [self.alias]},
                {'url': self.ao, 'digital_object_urls': [self.do]},
            ],
            'sources': [
                {'node_id': self.node, 'source_urls': [self.alias], 'source_file': 'source.json'}
            ],
        }
        self.config = {
            'manifest_base': 'https://example.org/manifests',
            'image_base': 'https://images.example.org/iiif/2',
        }
        self.approvals = {
            'sample': {'access': 'public', 'policy': 'example-policy', 'approved_by': 'operator'}
        }
        self.save()

    def save(self):
        (self.root / 'source.json').write_bytes(encode(self.source))

    def run_intake(self, urls=None, approvals=None, existing=None):
        return prepare(
            self.snapshot,
            self.root,
            self.config,
            urls or [self.ao, self.do],
            self.approvals if approvals is None else approvals,
            existing or {},
        )

    def test_join_once_and_reuse_all_proven_aliases(self):
        inventory, report = self.run_intake()
        self.assertEqual(len(inventory), 1)
        self.assertEqual([r['status'] for r in report], ['Ready', 'Ready'])
        self.assertEqual(report[0]['item_ids'], ['sample'])
        self.assertEqual(set(inventory['sample']['source_urls']), {self.ao, self.do, self.alias})
        self.assertEqual(inventory['sample']['source_sha256'], digest(encode(self.source)))
        self.assertEqual(
            inventory['sample']['intake_snapshot_sha256'], digest(encode(self.snapshot))
        )

    def test_unknown_url_is_actionable(self):
        _, report = self.run_intake(
            ['https://findingaids.example/repositories/2/archival_objects/9']
        )
        self.assertEqual(report[0]['status'], 'Unmapped source')
        self.assertIn('export', report[0]['message'])

    def test_public_web_visibility_does_not_grant_approval(self):
        inventory, report = self.run_intake(approvals={})
        self.assertEqual(report[0]['status'], 'Awaiting access approval')
        self.assertNotIn('approved_by', inventory['sample'])

    def test_empty_manifest_blocks_even_if_approved(self):
        self.source['sequences'][0].pop('canvases')
        self.save()
        inventory, report = self.run_intake()
        self.assertEqual(report[0]['status'], 'Invalid source manifest')
        self.assertIn('no pages', report[0]['message'])
        self.assertNotIn('sample', inventory)

    def test_missing_retained_source_is_not_a_mapping_failure(self):
        (self.root / 'source.json').unlink()
        _, report = self.run_intake()
        self.assertEqual(report[0]['status'], 'Missing retained source')

    def test_multiple_digital_objects_are_explicit_not_first_match(self):
        other = copy.deepcopy(self.snapshot['records'][0])
        other.update(url=self.do + '2', item_id='other')
        self.snapshot['records'].append(other)
        self.snapshot['records'][1]['digital_object_urls'].append(other['url'])
        _, report = self.run_intake()
        self.assertEqual(report[0]['status'], 'Ambiguous source')
        self.assertEqual(report[0]['item_ids'], ['other', 'sample'])

    def test_conflicting_sources_cannot_choose_one(self):
        self.snapshot['sources'].append(dict(self.snapshot['sources'][0], node_id='999'))
        _, report = self.run_intake()
        self.assertEqual(report[0]['status'], 'Ambiguous source mapping')

    def test_mismatched_manifest_node_is_rejected(self):
        self.source['@id'] = 'https://compass.fivecolleges.edu/node/999/manifest'
        self.save()
        _, report = self.run_intake()
        self.assertEqual(report[0]['status'], 'Invalid source manifest')

    def test_existing_approval_is_preserved_but_changed_source_is_blocked(self):
        inventory, _ = self.run_intake()
        again, report = self.run_intake(approvals={}, existing=inventory)
        self.assertEqual(again['sample']['policy'], 'example-policy')
        self.assertEqual(report[0]['status'], 'Ready')
        inventory['sample']['node_id'] = '999'
        preserved, report = self.run_intake(existing=inventory)
        self.assertEqual(report[0]['status'], 'Inventory conflict')
        self.assertEqual(preserved['sample']['node_id'], '999')
        self.assertEqual(preserved['sample']['source_sha256'], inventory['sample']['source_sha256'])
        self.assertIn('intake_hold', preserved['sample'])

    def test_bad_request_does_not_discard_other_rows(self):
        _, report = self.run_intake([self.do, self.do + '?oops=1'])
        self.assertEqual([row['status'] for row in report], ['Ready', 'Invalid request'])

    def test_node_alias_cannot_bypass_ambiguity_after_child_fails(self):
        from .sheets import approved_item

        self.snapshot['records'].append(
            {'url': self.do + '2', 'item_id': 'other', 'source_urls': [self.alias]}
        )
        self.source['metadata'] = [{'label': 'Identifier', 'value': 'sample'}]
        self.save()
        url = 'https://compass.fivecolleges.edu/node/' + self.node
        inventory, report = self.run_intake([url])
        self.assertEqual(report[0]['status'], 'Ambiguous source')
        with self.assertRaisesRegex(ValueError, 'Ambiguous or incomplete source'):
            approved_item(url, inventory, self.config)

    def test_failed_replacement_is_held_in_both_entrypoints(self):
        from .sheets import approved_item
        from .runner import run_item

        inventory, _ = self.run_intake()
        inventory['sample']['node_id'] = '999'
        held, _ = self.run_intake(existing=inventory)
        with self.assertRaisesRegex(ValueError, 'Intake hold'):
            approved_item(self.do, held, self.config)
        with self.assertRaisesRegex(ValueError, 'Intake hold'):
            run_item(dict(held['sample'], item_id='sample'), self.config, True, None)

    def test_corrected_source_and_complete_export_clear_their_holds(self):
        from .sheets import approved_item

        inventory, _ = self.run_intake()
        (self.root / 'source.json').unlink()
        held, _ = self.run_intake(existing=inventory)
        self.assertIn('intake_hold', held['sample'])
        self.save()
        repaired, report = self.run_intake(existing=held)
        self.assertEqual(report[0]['status'], 'Ready')
        self.assertNotIn('intake_hold', repaired['sample'])
        self.snapshot['records'][1]['digital_object_urls'].append(self.do + '99')
        partial, _ = self.run_intake(existing=repaired)
        with self.assertRaisesRegex(ValueError, 'Ambiguous or incomplete'):
            approved_item(self.ao, partial, self.config)
        self.snapshot['records'][1]['digital_object_urls'].pop()
        complete, _ = self.run_intake(existing=partial)
        self.assertEqual(approved_item(self.ao, complete, self.config)['item_id'], 'sample')

    def test_test_host_does_not_implicitly_authorize_production_alias(self):
        _, report = self.run_intake([self.ao.replace('findingaids.example', 'other.example')])
        self.assertEqual(report[0]['status'], 'Unmapped source')

    def test_missing_child_record_prevents_partial_parent_resolution(self):
        self.snapshot['records'][1]['digital_object_urls'].append(self.do + '99')
        inventory, report = self.run_intake()
        self.assertEqual(report[0]['status'], 'Incomplete relationship export')
        self.assertNotIn(self.ao, inventory['sample']['source_urls'])

    def test_parent_cannot_silently_choose_only_convertible_child(self):
        other = {
            'url': self.do + '2',
            'item_id': 'missing-source',
            'source_urls': ['https://compass.fivecolleges.edu/node/999'],
        }
        self.snapshot['records'].append(other)
        self.snapshot['records'][1]['digital_object_urls'].append(other['url'])
        inventory, report = self.run_intake()
        self.assertEqual(report[0]['status'], 'Ambiguous source')
        self.assertNotIn(self.ao, inventory['sample']['source_urls'])

    def test_source_traversal_and_checksum_changes_are_blocked(self):
        self.snapshot['sources'][0]['source_sha256'] = 'bad'
        _, report = self.run_intake()
        self.assertEqual(report[0]['status'], 'Invalid source manifest')
        self.snapshot['sources'][0]['source_file'] = '../source.json'
        _, report = self.run_intake()
        self.assertEqual(report[0]['status'], 'Invalid source manifest')

    def test_manifest_identifier_disagreement_is_blocked(self):
        self.source['metadata'] = [{'label': 'Identifier', 'value': 'different-item'}]
        self.save()
        _, report = self.run_intake()
        self.assertEqual(report[0]['status'], 'Invalid source manifest')


class IntakeCommandTests(unittest.TestCase):
    def test_command_writes_private_review_bundle_and_refuses_overwrite(self):
        import csv
        import json
        from .intake import main
        from .test_worker import manifest

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = manifest()
            (root / 'source.json').write_bytes(encode(source))
            do = 'https://findingaids.example/repositories/2/digital_objects/1'
            alias = 'https://compass.fivecolleges.edu/node/123'
            snapshot = {
                'records': [{'url': do, 'item_id': 'sample', 'source_urls': [alias]}],
                'sources': [{'node_id': '123', 'source_file': 'source.json'}],
            }
            (root / 'snapshot.json').write_bytes(encode(snapshot))
            (root / 'config.json').write_bytes(
                encode(
                    {
                        'manifest_base': 'https://example.org/manifests',
                        'image_base': 'https://images.example.org/iiif/2',
                    }
                )
            )
            with (root / 'requests.csv').open('w') as stream:
                writer = csv.writer(stream)
                writer.writerow(['Request ID', 'Source URL', 'Notes', 'Ready'])
                writer.writerow(['request-1', do, '', 'Ready'])
            args = [
                '--snapshot',
                str(root / 'snapshot.json'),
                '--source-root',
                str(root),
                '--config',
                str(root / 'config.json'),
                '--requests',
                str(root / 'requests.csv'),
                '--output',
                str(root / 'review'),
            ]
            self.assertEqual(main(args), 1)
            self.assertEqual((root / 'review').stat().st_mode & 0o777, 0o700)
            self.assertEqual(
                (root / 'review/inventory.candidate.json').stat().st_mode & 0o777, 0o600
            )
            report = json.loads((root / 'review/preflight.json').read_text())
            self.assertEqual(report[0]['status'], 'Awaiting access approval')
            with self.assertRaises(FileExistsError):
                main(args)


if __name__ == '__main__':
    unittest.main()
