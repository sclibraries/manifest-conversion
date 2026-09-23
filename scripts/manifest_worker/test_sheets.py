"""Queue tests use fake Google transport; no credentials or live Sheet edits."""
import copy
import tempfile
import unittest
from pathlib import Path
from .sheets import process_queue, REQUEST_HEADERS, RESULT_HEADERS

URL='https://findingaids-test.smith.edu/repositories/2/archival_objects/139132'


class Sheet:
    def __init__(self):
        self.requests=[REQUEST_HEADERS, ['test-001',URL,'Local dry run','TRUE']]
        self.results=[RESULT_HEADERS]
        self.fail=False
        self.ambiguous=False
    def read_requests(self): return copy.deepcopy(self.requests)
    def read_results(self): return copy.deepcopy(self.results)
    def append_result(self,row):
        if self.fail: raise OSError('Google unavailable')
        self.results.append(row)
        if self.ambiguous:
            self.ambiguous=False
            raise OSError('response lost after append')


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.config={'state_dir':str(self.root),'manifest_base':'https://example.org/manifests',
                     'image_base':'https://images.example.org/iiif/2','allow_live':True}
        self.inventory={'sample':{'node_id':'80405','access':'public','policy':'local-test',
                                  'approved_by':'operator','source_urls':[URL]}}
        self.sheet=Sheet();self.calls=0
    def worker(self,item,config,publish,fetch):
        self.calls+=1
        return {'status':'published' if publish else 'validated','manifest_url':'https://example.org/manifests/sample.json',
                'pages':[{},{}], 'sha256':'abc'}
    def execute(self,publish=False):
        return process_queue(self.sheet,self.config,self.inventory,publish,None,'sheet-id',worker=self.worker)

    def test_ready_row_records_result_and_repeat_does_not_reprocess(self):
        original=copy.deepcopy(self.sheet.requests)
        self.assertEqual(self.execute()['processed'],1)
        self.assertEqual(self.sheet.results[1][2],'Validated (dry run)')
        self.assertEqual(self.execute()['skipped'],1)
        self.assertEqual(self.calls,1)
        self.assertEqual(self.sheet.requests,original)

    def test_failed_status_write_retries_without_reconverting(self):
        self.sheet.fail=True
        with self.assertRaises(OSError): self.execute()
        self.sheet.fail=False
        self.assertEqual(self.execute()['reconciled'],1)
        self.assertEqual(self.calls,1)
        self.assertEqual(len(self.sheet.results),2)

    def test_lost_append_response_does_not_duplicate_result(self):
        self.sheet.ambiguous=True
        with self.assertRaises(OSError): self.execute()
        self.execute()
        self.assertEqual(len(self.sheet.results),2)
        self.assertEqual(self.calls,1)

    def test_unknown_source_and_withdrawal_do_not_run_worker(self):
        self.sheet.requests[1][1]='https://unapproved.example/item'
        self.execute()
        self.assertEqual(self.sheet.results[1][2],'Needs review')
        self.assertEqual(self.calls,0)
        self.sheet.requests[1][1]=URL
        self.config['withdrawn_items']=['sample']
        self.execute()
        self.assertEqual(self.calls,0)

    def test_duplicate_ids_are_rejected_before_any_work(self):
        self.sheet.requests.append(['test-001',URL,'','TRUE'])
        with self.assertRaises(ValueError): self.execute()
        self.assertEqual(self.calls,0)

    def test_retry_token_reruns_once(self):
        self.execute()
        self.sheet.requests[1][3]='RETRY:1'
        self.execute();self.execute()
        self.assertEqual(self.calls,2)

    def test_false_ready_is_not_processed(self):
        self.sheet.requests[1][3]='FALSE'
        self.assertEqual(self.execute()['processed'],0)
        self.assertEqual(self.calls,0)

    def test_dry_run_and_publish_are_distinct_operations(self):
        self.execute();self.execute(True)
        self.assertEqual(self.calls,2)
        self.assertEqual(self.sheet.results[-1][2],'Published')

    def test_input_changes_during_work_are_reported(self):
        def edit(item,config,publish,fetch):
            self.sheet.requests[1][1]='https://changed.example/item'
            return self.worker(item,config,publish,fetch)
        process_queue(self.sheet,self.config,self.inventory,False,None,'sheet-id',worker=edit)
        self.assertIn('changed',self.sheet.results[-1][5].lower())



class LayoutTests(unittest.TestCase):
    def test_existing_result_columns_are_preserved_and_extended(self):
        from .sheets_api import GoogleSheet
        sheet=object.__new__(GoogleSheet)
        sheet.requests_tab='Requests';sheet.results_tab='Results'
        original=['Request ID','Status','Manifest URL','Last Run','Error']
        calls=[]
        def request(method,suffix='',payload=None,params=None):
            calls.append((method,suffix,payload,params))
            if method=='GET' and not suffix:
                return {'sheets':[{'properties':{'title':'Requests','sheetId':0}},
                                  {'properties':{'title':'Results','sheetId':1}}]}
            if method=='GET': return {'values':[original]}
            return {}
        sheet.request=request
        sheet.initialize_results()
        writes=[x for x in calls if x[0]=='PUT']
        self.assertEqual(writes[0][2]['values'],[['Source URL','Result','Operation ID']])
        self.assertIn('F1',writes[0][1])
        self.assertEqual(sheet.result_columns[:5],original)

    def test_raw_append_keeps_error_empty_on_success(self):
        from .sheets_api import GoogleSheet
        sheet=object.__new__(GoogleSheet);sheet.results_tab='Results'
        sheet.result_columns=['Request ID','Status','Manifest URL','Last Run','Error','Source URL','Result','Operation ID']
        captured=[]
        sheet.request=lambda *args,**kwargs: captured.append((args,kwargs)) or {}
        sheet.append_result(['test-001',URL,'Validated (dry run)','https://example.org/x','now','=untrusted','key'])
        payload=captured[0][1]
        self.assertEqual(payload['params']['valueInputOption'],'RAW')
        self.assertEqual(payload['payload']['values'][0][4],'')
        self.assertEqual(payload['payload']['values'][0][6],'=untrusted')


if __name__ == "__main__":
    unittest.main()
