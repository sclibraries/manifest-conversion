"""Authenticated Sheets access; no credential values are logged."""
import time
from urllib.parse import quote
from .core import load_json
from .sheets import RESULT_HEADERS


class GoogleSheet:
    def __init__(self, sheet_id, credentials_path, requests_tab, results_tab):
        try:
            from google.oauth2 import service_account
            from google.auth.transport.requests import AuthorizedSession
        except ImportError:
            raise ImportError('Install optional dependencies: python3 -m pip install -r requirements-sheets.txt') from None
        credentials = service_account.Credentials.from_service_account_file(
            str(credentials_path), scopes=['https://www.googleapis.com/auth/spreadsheets'])
        self.session = AuthorizedSession(credentials)
        self.base = 'https://sheets.googleapis.com/v4/spreadsheets/' + sheet_id
        self.requests_tab = requests_tab
        self.results_tab = results_tab

    def request(self, method, suffix='', payload=None, params=None):
        import requests
        for attempt in range(3):
            try:
                with self.session.request(method,self.base+suffix,json=payload,params=params,
                                          timeout=(10,30),stream=True) as response:
                    if method=='GET' and response.status_code in (429,500,502,503,504) and attempt<2:
                        time.sleep(2**attempt)
                        continue
                    if response.status_code >= 400:
                        raise OSError('Google Sheets HTTP '+str(response.status_code)+
                                      '; check API enablement, spreadsheet sharing and configured tab names')
                    chunks = []
                    size = 0
                    for chunk in response.iter_content(65536):
                        size += len(chunk)
                        if size > 2*1024*1024:
                            raise ValueError('Google response exceeds 2 MiB; archive old rows')
                        chunks.append(chunk)
                    return load_json(b''.join(chunks),limit=2*1024*1024)
            except requests.RequestException:
                # Never blindly retry an ambiguous append. Next run reads operation IDs.
                raise OSError('Google Sheets connection failed; rerun to reconcile saved results') from None

    def read(self, tab, cells):
        name = "'" + tab.replace("'","''") + "'!" + cells
        return self.request('GET','/values/'+quote(name,safe=''),
                            params={'valueRenderOption':'FORMATTED_VALUE'}).get('values',[])

    def read_requests(self):
        return self.read(self.requests_tab,'A1:D1002')

    def read_results(self):
        values = self.read(self.results_tab,'A1:H5002')
        if not values:
            return []
        columns = values[0]
        if len(columns) != len(set(columns)) or set(columns) not in (set(RESULT_HEADERS), set(RESULT_HEADERS + ['Error'])):
            raise ValueError('Results columns need initialization or have unexpected names')
        self.result_columns = columns
        mapped = [RESULT_HEADERS]
        for row in values[1:]:
            record = dict(zip(columns,row))
            mapped.append([record.get(key,'') for key in RESULT_HEADERS])
        return mapped

    def append_result(self, row):
        record = dict(zip(RESULT_HEADERS,row))
        record['Error'] = record['Result'] if record['Status'] in ('Failed','Needs review') else ''
        name = "'" + self.results_tab.replace("'","''") + "'!A:H"
        return self.request('POST','/values/'+quote(name,safe='')+':append',
                            payload={'values':[[record.get(key,'') for key in self.result_columns]]},
                            params={'valueInputOption':'RAW','insertDataOption':'INSERT_ROWS'})

    def initialize_results(self):
        metadata = self.request('GET',params={'fields':'sheets.properties'})
        sheets = {entry['properties']['title']:entry['properties'] for entry in metadata['sheets']}
        if self.requests_tab not in sheets:
            raise ValueError('Requests tab does not exist: '+self.requests_tab)
        if self.results_tab not in sheets:
            created = self.request('POST',':batchUpdate',payload={'requests':[
                {'addSheet':{'properties':{'title':self.results_tab,'gridProperties':{'rowCount':1000,'columnCount':8}}}}]})
            properties = created['replies'][0]['addSheet']['properties']
        else:
            properties = sheets[self.results_tab]
        values = self.read(self.results_tab,'A1:H5002')
        columns = values[0] if values else []
        allowed = set(RESULT_HEADERS + ['Error'])
        if len(columns) != len(set(columns)) or any(key not in allowed for key in columns):
            raise ValueError('Existing Results has unexpected columns; nothing overwritten')
        if columns and not {'Request ID','Status'}.issubset(columns):
            raise ValueError('Existing Results does not look like a results table')
        missing = [key for key in RESULT_HEADERS if key not in columns]
        if missing:
            start = chr(ord('A')+len(columns))
            end = chr(ord('A')+len(columns)+len(missing)-1)
            name = "'"+self.results_tab.replace("'","''")+"'!"+start+'1:'+end+'1'
            self.request('PUT','/values/'+quote(name,safe=''),payload={'values':[missing]},
                         params={'valueInputOption':'RAW'})
        self.result_columns = columns + missing
        sheet_id = properties['sheetId']
        operation_column = self.result_columns.index('Operation ID')
        self.request('POST',':batchUpdate',payload={'requests':[
            {'updateSheetProperties':{'properties':{'sheetId':sheet_id,'gridProperties':{'frozenRowCount':1}},
                                      'fields':'gridProperties.frozenRowCount'}},
            {'updateDimensionProperties':{'range':{'sheetId':sheet_id,'dimension':'COLUMNS',
                                                   'startIndex':operation_column,'endIndex':operation_column+1},
                                          'properties':{'hiddenByUser':True},'fields':'hiddenByUser'}}]})
