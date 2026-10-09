import unittest
from unittest.mock import Mock
from .transport import Fetcher

class Response:
    status=202
    headers={}
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def read(self,size):return b''

class TransportTests(unittest.TestCase):
    def test_challenge_status_is_not_successful_empty_data(self):
        fetch=Fetcher(['archives.example.org'])
        fetch.opener=Mock();fetch.opener.open.return_value=Response()
        with self.assertRaisesRegex(ValueError,'HTTP 202'):
            fetch('https://archives.example.org/repositories/4/archival_objects/592825')
    def test_unapproved_host_and_http_rejected_before_open(self):
        fetch=Fetcher(['compass.fivecolleges.edu']);fetch.opener=Mock()
        for url in ['https://other.example/x','http://compass.fivecolleges.edu/x']:
            with self.assertRaises(ValueError):fetch(url)
        fetch.opener.open.assert_not_called()
