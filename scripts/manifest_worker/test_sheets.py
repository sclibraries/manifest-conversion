"""Queue tests use fake Google transport; no credentials or live Sheet edits."""

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from .core import digest, encode
from .sheets import main, process_queue, REQUEST_HEADERS, RESULT_HEADERS

URL = 'https://archives.example.org/repositories/2/archival_objects/139132'


class Sheet:
    def __init__(self):
        self.requests = [REQUEST_HEADERS, ['test-001', URL, 'Local dry run', 'TRUE']]
        self.results = [RESULT_HEADERS]
        self.fail = False
        self.ambiguous = False

    def read_requests(self):
        return copy.deepcopy(self.requests)

    def read_results(self):
        return copy.deepcopy(self.results)

    def append_result(self, row):
        if self.fail:
            raise OSError('Google unavailable')
        self.results.append(row)
        if self.ambiguous:
            self.ambiguous = False
            raise OSError('response lost after append')


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {
            'state_dir': str(self.root),
            'manifest_base': 'https://example.org/manifests',
            'image_base': 'https://images.example.org/iiif/2',
            'allow_live': True,
        }
        self.inventory = {
            'sample': {
                'node_id': '80405',
                'access': 'public',
                'policy': 'local-test',
                'approved_by': 'operator',
                'source_urls': [URL],
            }
        }
        self.sheet = Sheet()
        self.calls = 0

    def test_full_spreadsheet_url_query_is_not_part_of_document_id(self):
        inventory = self.root / 'inventory.json'
        inventory.write_text('{}')
        config = self.root / 'config.json'
        config.write_text(
            json.dumps(
                {
                    'source_root': str(self.root / 'sources'),
                    'state_dir': str(self.root / 'state'),
                    'inventory': str(inventory),
                    'manifest_base': 'https://example.org/manifests',
                    'image_base': 'https://images.example.org/iiif/2',
                }
            )
        )
        spreadsheet = 'A' * 24
        with patch.dict(os.environ, {'GOOGLE_APPLICATION_CREDENTIALS': ''}):
            with self.assertRaisesRegex(ValueError, 'Set GOOGLE_APPLICATION_CREDENTIALS'):
                main(
                    [
                        '--config',
                        str(config),
                        '--sheet',
                        'https://docs.google.com/'
                        + 'spread'
                        + 'sheets'
                        + '/d/'
                        + f'{spreadsheet}?usp=sharing',
                    ]
                )

    def worker(self, item, config, publish, fetch):
        self.calls += 1
        return {
            'status': 'published' if publish else 'validated',
            'manifest_url': 'https://example.org/manifests/sample.json',
            'pages': [{}, {}],
            'sha256': 'abc',
        }

    def execute(self, publish=False):
        return process_queue(
            self.sheet, self.config, self.inventory, publish, None, 'sheet-id', worker=self.worker
        )

    def test_ready_row_records_result_and_repeat_does_not_reprocess(self):
        original = copy.deepcopy(self.sheet.requests)
        self.assertEqual(self.execute()['processed'], 1)
        self.assertEqual(self.sheet.results[1][2], 'Validated (dry run)')
        self.assertEqual(self.execute()['skipped'], 1)
        self.assertEqual(self.calls, 1)
        self.assertEqual(self.sheet.requests, original)

    def test_approved_item_returns_complete_inventory_record(self):
        from .sheets import approved_item

        self.assertEqual(
            approved_item(URL, self.inventory, self.config),
            {
                'node_id': '80405',
                'access': 'public',
                'policy': 'local-test',
                'approved_by': 'operator',
                'source_urls': [URL],
                'item_id': 'sample',
            },
        )

    def test_process_queue_full_result_and_ledger_output_are_pinned(self):
        summary = self.execute()
        self.assertEqual(
            summary,
            {'processed': 1, 'skipped': 0, 'reconciled': 0, 'failed': 0, 'needs_review': 0},
        )
        self.assertEqual(len(self.sheet.results), 2)
        result = self.sheet.results[1]
        request = {
            'request_id': 'test-001',
            'source_url': URL,
            'notes': 'Local dry run',
            'ready': 'TRUE',
            'enabled': True,
        }
        item = dict(self.inventory['sample'], item_id='sample')
        operation_id = digest(
            encode(
                {
                    'request': request,
                    'item': item,
                    'review': '',
                    'publish': False,
                    'config': self.config,
                }
            )
        )
        self.assertEqual(
            result[:4],
            [
                'test-001',
                URL,
                'Validated (dry run)',
                'https://example.org/manifests/sample.json',
            ],
        )
        self.assertTrue(result[4].endswith('+00:00'))
        self.assertEqual(
            result[5:],
            [
                '2 pages validated. Manifest staged locally; URL is proposed, not published.',
                operation_id,
            ],
        )
        ledger_files = list(self.root.glob('sheet-*/*.json'))
        self.assertEqual(len(ledger_files), 1)
        ledger = json.loads(ledger_files[0].read_bytes())
        self.assertEqual(
            ledger,
            {
                'request': {
                    **request,
                },
                'status': 'completed',
                'result': result,
            },
        )

    def test_failed_status_write_retries_without_reconverting(self):
        self.sheet.fail = True
        with self.assertRaises(OSError):
            self.execute()
        self.sheet.fail = False
        self.assertEqual(self.execute()['reconciled'], 1)
        self.assertEqual(self.calls, 1)
        self.assertEqual(len(self.sheet.results), 2)

    def test_lost_append_response_does_not_duplicate_result(self):
        self.sheet.ambiguous = True
        with self.assertRaises(OSError):
            self.execute()
        self.execute()
        self.assertEqual(len(self.sheet.results), 2)
        self.assertEqual(self.calls, 1)

    def test_unknown_source_and_withdrawal_do_not_run_worker(self):
        self.sheet.requests[1][1] = 'https://unapproved.example/item'
        self.execute()
        self.assertEqual(self.sheet.results[1][2], 'Needs review')
        self.assertEqual(self.calls, 0)
        self.sheet.requests[1][1] = URL
        self.config['withdrawn_items'] = ['sample']
        self.execute()
        self.assertEqual(self.calls, 0)

    def test_duplicate_ids_are_rejected_before_any_work(self):
        self.sheet.requests.append(['test-001', URL, '', 'TRUE'])
        with self.assertRaises(ValueError):
            self.execute()
        self.assertEqual(self.calls, 0)

    def test_retry_token_reruns_once(self):
        self.execute()
        self.sheet.requests[1][3] = 'RETRY:1'
        self.execute()
        self.execute()
        self.assertEqual(self.calls, 2)

    def test_false_ready_is_not_processed(self):
        self.sheet.requests[1][3] = 'FALSE'
        self.assertEqual(self.execute()['processed'], 0)
        self.assertEqual(self.calls, 0)

    def test_dry_run_and_publish_are_distinct_operations(self):
        self.execute()
        self.execute(True)
        self.assertEqual(self.calls, 2)
        self.assertEqual(self.sheet.results[-1][2], 'Published')

    def test_input_changes_during_work_are_reported(self):
        def edit(item, config, publish, fetch):
            self.sheet.requests[1][1] = 'https://changed.example/item'
            return self.worker(item, config, publish, fetch)

        process_queue(self.sheet, self.config, self.inventory, False, None, 'sheet-id', worker=edit)
        self.assertIn('changed', self.sheet.results[-1][5].lower())


class LayoutTests(unittest.TestCase):
    def test_existing_result_columns_are_preserved_and_extended(self):
        from .sheets_api import GoogleSheet

        sheet = object.__new__(GoogleSheet)
        sheet.requests_tab = 'Requests'
        sheet.results_tab = 'Results'
        original = ['Request ID', 'Status', 'Manifest URL', 'Last Run', 'Error']
        calls = []

        def request(method, suffix='', payload=None, params=None):
            calls.append((method, suffix, payload, params))
            if method == 'GET' and not suffix:
                return {
                    'sheets': [
                        {'properties': {'title': 'Requests', 'sheetId': 0}},
                        {'properties': {'title': 'Results', 'sheetId': 1}},
                    ]
                }
            if method == 'GET':
                return {'values': [original]}
            return {}

        sheet.request = request
        sheet.initialize_results()
        writes = [x for x in calls if x[0] == 'PUT']
        self.assertEqual(writes[0][2]['values'], [['Source URL', 'Result', 'Operation ID']])
        self.assertIn('F1', writes[0][1])
        self.assertEqual(sheet.result_columns[:5], original)

    def test_raw_append_keeps_error_empty_on_success(self):
        from .sheets_api import GoogleSheet

        sheet = object.__new__(GoogleSheet)
        sheet.results_tab = 'Results'
        sheet.result_columns = [
            'Request ID',
            'Status',
            'Manifest URL',
            'Last Run',
            'Error',
            'Source URL',
            'Result',
            'Operation ID',
        ]
        captured = []
        sheet.request = lambda *args, **kwargs: captured.append((args, kwargs)) or {}
        sheet.append_result(
            [
                'test-001',
                URL,
                'Validated (dry run)',
                'https://example.org/x',
                'now',
                '=untrusted',
                'key',
            ]
        )
        payload = captured[0][1]
        self.assertEqual(payload['params']['valueInputOption'], 'RAW')
        self.assertEqual(payload['payload']['values'][0][4], '')
        self.assertEqual(payload['payload']['values'][0][6], '=untrusted')


class MappingMessagesTests(unittest.TestCase):
    def test_unmapped_and_ambiguous_have_different_actions(self):
        from .sheets import approved_item

        with self.assertRaisesRegex(ValueError, 'Unmapped source.*relationship export'):
            approved_item(URL, {}, {})
        item = {
            'source_urls': [URL],
            'access': 'public',
            'policy': 'test',
            'approved_by': 'operator',
        }
        with self.assertRaisesRegex(ValueError, 'Ambiguous source.*2'):
            approved_item(URL, {'one': item, 'two': item}, {})

    def test_approval_message_identifies_resolved_item(self):
        from .sheets import approved_item

        with self.assertRaisesRegex(ValueError, 'sample.*public-access approval'):
            approved_item(URL, {'sample': {'source_urls': [URL]}}, {})


class DiscoveryQueueTests(unittest.TestCase):
    worker = QueueTests.worker

    def setUp(self):
        QueueTests.setUp(self)
        self.config['discovery'] = {'enabled': True}
        self.discovered = {
            'item_id': 'new-item',
            'node_id': '123',
            'source_urls': [URL],
            'source_sha256': 'abc',
            'source_file': 'retained.json',
        }
        self.inventory = {}
        self.discovery_calls = []

    def resolver(self, source, config, fetch, inventory, retry_token='READY'):
        self.discovery_calls.append(source)
        return copy.deepcopy(self.discovered)

    def execute(self, publish=False):
        return process_queue(
            self.sheet,
            self.config,
            self.inventory,
            publish,
            None,
            'sheet-id',
            worker=self.worker,
            resolver=self.resolver,
            validator=self.worker,
        )

    def test_new_row_discovers_and_validates_without_publication_approval(self):
        result = self.execute(True)
        self.assertEqual(self.sheet.results[-1][2], 'Validated (approval required)')
        self.assertEqual(result['needs_review'], 1)

    def test_ready_policy_can_authorize_publication_explicitly(self):
        self.config['discovery']['ready_publication'] = {
            'policy': 'staff-reviewed-public-material',
            'approved_by': 'reviewer',
        }
        self.execute(True)
        self.assertEqual(self.sheet.results[-1][2], 'Published')

    def test_existing_hold_never_enters_discovery(self):
        self.inventory = {'sample': {'source_urls': [URL], 'intake_hold': 'stop'}}
        self.execute(True)
        self.assertEqual(self.discovery_calls, [])
        self.assertEqual(self.calls, 0)

    def test_new_rows_beyond_limit_do_not_discover(self):
        self.sheet.requests.append(['next', URL + '2', '', 'Ready'])
        process_queue(
            self.sheet,
            self.config,
            self.inventory,
            False,
            None,
            'sheet-id',
            worker=self.worker,
            resolver=self.resolver,
            validator=self.worker,
            limit=1,
        )
        self.assertEqual(self.discovery_calls, [URL])

    def test_invalid_discovery_policy_is_a_setup_error_before_sheet_reads(self):
        self.config['discovery']['ready_publication'] = True
        self.sheet.read_requests = lambda: self.fail(
            'Bad config must fail before reading the queue'
        )
        with self.assertRaisesRegex(ValueError, 'ready_publication'):
            self.execute(True)


if __name__ == '__main__':
    unittest.main()
