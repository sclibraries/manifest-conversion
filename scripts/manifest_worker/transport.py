"""Bounded HTTPS; redirects are checked before following them."""
import time
import urllib.error
import urllib.request
from .core import https_url


class Fetcher:
    def __init__(self, hosts, max_requests=5000, max_bytes=128*1024*1024):
        self.hosts=set(hosts); self.requests=0; self.bytes=0
        self.max_requests=max_requests; self.max_bytes=max_bytes
        self.deadline=time.monotonic()+1800
        self.opener=urllib.request.build_opener(urllib.request.HTTPRedirectHandler())
        owner=self
        class Redirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,req,fp,code,msg,headers,newurl):
                owner.check(newurl)
                return super().redirect_request(req,fp,code,msg,headers,newurl)
        self.opener=urllib.request.build_opener(Redirect())

    def check(self,url):
        if https_url(url).hostname not in self.hosts: raise ValueError('HTTP host not approved')
        self.requests+=1
        if self.requests>self.max_requests or time.monotonic()>self.deadline:
            raise ValueError('request/time budget exhausted')

    def __call__(self,url):
        for attempt in range(3):
            self.check(url)
            try:
                request=urllib.request.Request(url,headers={'Accept':'application/json','Cache-Control':'no-cache','User-Agent':'SmithManifestWorker/1'})
                with self.opener.open(request,timeout=20) as response:
                    parts=[]; size=0
                    while True:
                        chunk=response.read(65536)
                        if not chunk: break
                        size+=len(chunk); self.bytes+=len(chunk)
                        if size>8*1024*1024 or self.bytes>self.max_bytes:
                            raise ValueError('download byte limit exceeded')
                        parts.append(chunk)
                    return b''.join(parts),dict(response.headers.items())
            except urllib.error.HTTPError as error:
                if error.code not in (429,500,502,503,504) or attempt==2: raise
            except (urllib.error.URLError,TimeoutError):
                if attempt==2: raise
            time.sleep(2**attempt)
