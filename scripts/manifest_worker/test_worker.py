import copy
import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from .core import convert, load_json, safe_id, safe_cell, contained, encode, digest
from .runner import run_item

SOURCE = 'https://compass.fivecolleges.edu/node/123/manifest'
BASE = 'https://libtools2.smith.edu/digital/manifests'
IMAGE = 'https://digital.smith.edu/iiif/2'
SERVICE = 'https://compass.fivecolleges.edu/cantaloupe/iiif/2/https%3A%2F%2Fcompass.fivecolleges.edu%2Fsystem%2Ffiles%2F2024-09%2Fpage%2520one.tif'


def manifest():
    return {'@context': 'http://iiif.io/api/presentation/2/context.json', '@type': 'sc:Manifest',
            '@id': SOURCE, 'label': 'Example', 'license': 'https://example.org/rights',
            'sequences': [{'@id': SOURCE+'/sequence', '@type': 'sc:Sequence', 'canvases': [
                {'@id': 'https://compass.fivecolleges.edu/node/123/canvas/1', '@type': 'sc:Canvas',
                 'label': 'Page 1', 'width': 100, 'height': 200,
                 'seeAlso': {'@id': 'https://compass.fivecolleges.edu/system/files/page.html', 'format': 'text/vnd.hocr+html'},
                 'images': [{'@type': 'oa:Annotation', 'motivation': 'sc:painting',
                             'on': 'https://compass.fivecolleges.edu/node/123/canvas/1',
                             'resource': {'@type': 'dctypes:Image', '@id': SERVICE+'/full/full/0/default.jpg',
                                          'service': {'@id': SERVICE}}}]}]}]}


class CoreTests(unittest.TestCase):
    def test_conversion_preserves_identity_and_reports_ocr_omission(self):
        original = manifest()
        output, receipt = convert(original, BASE+'/sample.json', IMAGE)
        canvas = output['sequences'][0]['canvases'][0]
        self.assertEqual(canvas['@id'], original['sequences'][0]['canvases'][0]['@id'])
        self.assertEqual(output['license'], original['license'])
        self.assertEqual(receipt['pages'][0]['image_key'], '2024-09/page one.tif')
        self.assertEqual(canvas['images'][0]['resource']['service']['@id'], IMAGE+'/2024-09%2Fpage%20one.tif')
        self.assertNotIn('seeAlso', canvas)
        self.assertEqual(len(receipt['omitted_hocr_links']), 1)
        self.assertIn('seeAlso', original['sequences'][0]['canvases'][0])

    def test_legacy_search_service_is_removed_and_recorded(self):
        source=manifest()
        source['service']=[{'@id':'https://compass.fivecolleges.edu/paged-content-search/123',
                            'profile':'http://iiif.io/api/search/0/search'}]
        source['behavior']=['individuals','paged']
        output,receipt=convert(source,BASE+'/x.json',IMAGE)
        self.assertNotIn('service',output)
        self.assertEqual(receipt['omitted_search_services'],source['service'])
        self.assertEqual(output['behavior'],source['behavior'])

    def test_rejects_non_image_or_multiple_paintings(self):
        for change in ('video', 'multiple', 'duplicate'):
            source = manifest(); canvas = source['sequences'][0]['canvases'][0]
            if change == 'video': canvas['images'][0]['resource']['@type'] = 'dctypes:MovingImage'
            if change == 'multiple': canvas['images'] *= 2
            if change == 'duplicate': source['sequences'][0]['canvases'].append(copy.deepcopy(canvas))
            with self.assertRaises(ValueError): convert(source, BASE+'/x.json', IMAGE)

    def test_rejects_unsupported_companion_instead_of_dropping_it(self):
        source = manifest(); source['rendering'] = {'@id': 'https://compass.fivecolleges.edu/a.pdf'}
        with self.assertRaises(ValueError): convert(source, BASE+'/x.json', IMAGE)

    def test_rejects_dangerous_ids_and_paths(self):
        for item in ('../x', 'a/b', '.', 'x.json', 'a\n', '-bad'):
            with self.assertRaises(ValueError): safe_id(item)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)/'root'; root.mkdir()
            (root/'escape').symlink_to(Path(temp), target_is_directory=True)
            with self.assertRaises(ValueError): contained(root, 'escape/file')

    def test_json_limits_duplicate_keys_and_formulas(self):
        for data in (b'{"a":1,"a":2}', b'['*40+b'0'+b']'*40, b'{"a":NaN}'):
            with self.assertRaises(ValueError): load_json(data)
        with self.assertRaises(ValueError): load_json(b' '*101, limit=100)
        for cell in ('=A1', '  +cmd', '\t@x', '-1'):
            self.assertTrue(safe_cell(cell).startswith("'"))


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for directory in ('sources','state','published'): (self.root/directory).mkdir()
        self.data = encode(manifest()); (self.root/'sources/source.json').write_bytes(self.data)
        self.config = {'manifest_base': BASE, 'image_base': IMAGE,
                       'source_root': str(self.root/'sources'), 'state_dir': str(self.root/'state'),
                       'publish_dir': str(self.root/'published'), 'allowed_hosts': ['digital.smith.edu','libtools2.smith.edu','compass.fivecolleges.edu']}
        self.item = {'item_id':'sample','node_id':'123','source_file':'source.json','source_sha256':digest(self.data),
                     'access':'public','policy':'review-1','approved_by':'collection-review'}
        self.calls=[]

    def fetch(self,url):
        self.calls.append(url)
        if url.endswith('/info.json'): return encode({'@id':url[:-10], 'width':100,'height':200}), {}
        return (self.root/'published/sample.json').read_bytes(), {'cache-control':'no-cache','access-control-allow-origin':'*'}

    def test_dry_run_stages_without_publishing(self):
        result = run_item(self.item,self.config,False,self.fetch)
        self.assertEqual(result['status'],'validated')
        self.assertFalse((self.root/'published/sample.json').exists())
        self.assertTrue((self.root/'state/artifacts/sample.json').exists())

    def test_publish_verify_repeat_and_collision(self):
        result=run_item(self.item,self.config,True,self.fetch)
        self.assertEqual(result['status'],'published')
        self.assertEqual(run_item(self.item,self.config,True,self.fetch)['status'],'unchanged')
        (self.root/'published/sample.json').write_text('{}')
        with self.assertRaises(ValueError): run_item(self.item,self.config,True,self.fetch)

    def test_unknown_access_and_changed_checksum_never_publish(self):
        for field,value in [('access','unknown'),('source_sha256','0'*64),('approved_by','')]:
            item=dict(self.item); item[field]=value
            with self.assertRaises(ValueError): run_item(item,self.config,True,self.fetch)
        self.assertEqual(list((self.root/'published').iterdir()),[])

    def test_failed_image_check_prevents_publication(self):
        def wrong(url): return encode({'width':99,'height':200}),{}
        with self.assertRaises(ValueError): run_item(self.item,self.config,True,wrong)
        self.assertFalse((self.root/'published/sample.json').exists())

    def test_unverified_publication_is_retried(self):
        def stale(url):
            if url.endswith('info.json'): return self.fetch(url)
            return b'{}', {'cache-control':'no-cache','access-control-allow-origin':'*'}
        with self.assertRaises(ValueError): run_item(self.item,self.config,True,stale)
        self.assertEqual(run_item(self.item,self.config,True,self.fetch)['status'],'published')

    def test_resume_after_failure_before_replacement(self):
        from . import runner
        run_item(self.item,self.config,True,self.fetch)
        updated=manifest(); updated['label']='Updated'
        data=encode(updated); (self.root/'sources/source.json').write_bytes(data)
        item=dict(self.item,source_sha256=digest(data))
        original=runner.atomic_write
        def interrupted(path,data,mode=0o600):
            if Path(path).resolve()==(self.root/'published/sample.json').resolve(): raise OSError('interrupted')
            return original(path,data,mode)
        with patch.object(runner,'atomic_write',interrupted):
            with self.assertRaises(OSError): run_item(item,self.config,True,self.fetch)
        self.assertEqual(run_item(item,self.config,True,self.fetch)['status'],'published')

    def test_replacement_preserves_previous_artifact(self):
        run_item(self.item,self.config,True,self.fetch)
        previous=(self.root/'published/sample.json').read_bytes()
        updated=manifest(); updated['label']='Updated'
        data=encode(updated); (self.root/'sources/source.json').write_bytes(data)
        item=dict(self.item,source_sha256=digest(data))
        self.assertEqual(run_item(item,self.config,True,self.fetch)['status'],'published')
        self.assertTrue(any(p.read_bytes()==previous for p in (self.root/'state/history').glob('*.json')))

if __name__ == '__main__': unittest.main()

class CommandTests(unittest.TestCase):
    setUp = RunnerTests.setUp
    fetch = RunnerTests.fetch
    def test_command_reports_success_and_failure_without_aborting_batch(self):
        from .__main__ import main
        inventory=self.root/'inventory.json'; inventory.write_bytes(encode({'sample':self.item}))
        config=dict(self.config,inventory=str(inventory))
        config_path=self.root/'config.json'; config_path.write_bytes(encode(config))
        job=self.root/'job.csv'; job.write_text('item_id\nsample\nunknown\n')
        class FakeFetcher:
            requests=0; bytes=0
            def __call__(unused,url): return self.fetch(url)
        with patch('scripts.manifest_worker.__main__.Fetcher',return_value=FakeFetcher()):
            self.assertEqual(main(['--config',str(config_path),'--jobs',str(job)]),1)
        report=next((self.root/'state/reports').glob('*.json'))
        rows=load_json(report.read_bytes())
        self.assertEqual([row['status'] for row in rows],['validated','failed'])
        self.assertTrue(rows[0]['submitter'])

    def test_withdrawn_item_cannot_be_republished(self):
        run_item(self.item,self.config,True,self.fetch)
        self.config['withdrawn_items']=['sample']
        with self.assertRaises(ValueError): run_item(self.item,self.config,True,self.fetch)

    def test_job_cannot_supply_its_own_access_approval(self):
        from .__main__ import jobs
        job=self.root/'job.csv'; job.write_text('item_id,access\nsample,public\n')
        with self.assertRaises(ValueError): jobs(job)
