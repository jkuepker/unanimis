"""Explicit source ingestion with preserved originals; fetched content is data."""
import hashlib
import http.client
import ipaddress
import json
from pathlib import Path
import socket
import ssl
from urllib.parse import urlsplit,urljoin
from html.parser import HTMLParser
from .core import UnimError,title_of

MAX_BYTES=25*1024*1024


class Article(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True);self.stack=[];self.text=[];self.article=[];self.title=[]
    def handle_starttag(self,tag,attrs):
        if tag not in ('br','img','meta','link','input','hr','source','wbr'):self.stack.append(tag)
        if tag in ('p','div','br','li','h1','h2','h3','section','article'):self.add('\n')
    def handle_endtag(self,tag):
        if tag in self.stack:
            pos=len(self.stack)-1-self.stack[::-1].index(tag);self.stack=self.stack[:pos]
        if tag in ('p','div','li','h1','h2','h3'):self.add('\n')
    def add(self,text):
        if any(t in self.stack for t in ('script','style','nav','footer','header','aside','form','noscript')):return
        if 'title' in self.stack:self.title.append(text)
        elif 'head' not in self.stack:
            self.text.append(text)
            if 'article' in self.stack or 'main' in self.stack:self.article.append(text)
    def handle_data(self,data):self.add(data)
    def result(self):
        chunks=self.article if ''.join(self.article).strip() else self.text
        text='\n'.join(line.strip() for line in ''.join(chunks).splitlines() if line.strip())
        return ''.join(self.title).strip(),text


def fetch(url):
    original=url
    for attempt in range(4):
        parsed=urlsplit(url)
        if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password:raise UnimError('invalid_url','Use an HTTP(S) URL without credentials')
        port=parsed.port or (443 if parsed.scheme=='https' else 80)
        addresses=socket.getaddrinfo(parsed.hostname,port,type=socket.SOCK_STREAM)
        ips=[x[4][0] for x in addresses]
        if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):raise UnimError('unsafe_url','Web ingestion permits public network destinations only')
        host=parsed.hostname;ip=ips[0]
        conn=http.client.HTTPSConnection(host,port,timeout=30) if parsed.scheme=='https' else http.client.HTTPConnection(host,port,timeout=30)
        def connect():
            sock=socket.create_connection((ip,port),timeout=30)
            conn.sock=ssl.create_default_context().wrap_socket(sock,server_hostname=host) if parsed.scheme=='https' else sock
        conn.connect=connect
        try:
            conn.request('GET',(parsed.path or '/')+('?' + parsed.query if parsed.query else ''),headers={'User-Agent':'unanimis/0.1 source capture','Accept-Encoding':'identity'})
            response=conn.getresponse()
            if response.status in (301,302,303,307,308):
                location=response.getheader('Location')
                if not location:raise UnimError('fetch_failed','Redirect has no location')
                url=urljoin(url,location);continue
            if response.status!=200:raise UnimError('fetch_failed','Source returned HTTP '+str(response.status))
            length=response.getheader('Content-Length')
            if length and int(length)>MAX_BYTES:raise UnimError('too_large','Source exceeds 25 MiB')
            data=response.read(MAX_BYTES+1)
            if len(data)>MAX_BYTES:raise UnimError('too_large','Source exceeds 25 MiB')
            return data,response.getheader('Content-Type',''),url
        finally:conn.close()
    raise UnimError('fetch_failed','Source redirected more than three times')


def extract(data,locator,mime=''):
    if data.startswith(b'%PDF-'):
        try:
            from pypdf import PdfReader
            import io
            reader=PdfReader(io.BytesIO(data),strict=False)
            if reader.is_encrypted:raise UnimError('encrypted_pdf','Use an unencrypted PDF copy')
            if len(reader.pages)>300:raise UnimError('too_large','PDF exceeds 300 pages')
            pages=[p.extract_text() or '' for p in reader.pages]
            if not any(p.strip() for p in pages):raise UnimError('ocr_required','PDF has no extractable text; OCR is required')
            title=str((reader.metadata or {}).get('/Title') or Path(urlsplit(locator).path).stem)
            text='\n\n'.join('## Page '+str(n+1)+'\n\n'+p.strip() for n,p in enumerate(pages))
            return title,text,dict(format='pdf',pages=len(pages),empty_pages=[n+1 for n,p in enumerate(pages) if not p.strip()],extraction='pypdf text extraction; layout, images and OCR are not interpreted'),'.pdf'
        except ImportError as e:raise UnimError('dependency_missing','PDF extraction requires pypdf; install unanimis[pdf]') from e
        except UnimError:raise
        except Exception as e:raise UnimError('invalid_pdf','PDF extraction failed ('+type(e).__name__+')') from e
    text=data.decode('utf-8-sig')
    if 'html' in mime.lower() or Path(urlsplit(locator).path).suffix.lower() in ('.html','.htm') or '<html' in text[:1000].lower():
        parser=Article();parser.feed(text);title,body=parser.result()
        if not body.strip():raise UnimError('empty_source','HTML contains no readable text')
        return title or title_of(body),body,dict(format='html',extraction='HTML article/main text; scripts and navigation excluded'),'.html'
    if '\x00' in text:raise UnimError('unsupported_source','Source is not supported text, HTML or PDF')
    return title_of(text),text,dict(format='text',extraction='verbatim UTF-8 text'),'.txt'


def ingest(core,source,title=None,fetcher=fetch):
    if core.read_only:raise UnimError('read_only','Ingestion requires write access')
    is_url=urlsplit(source).scheme in ('http','https')
    if is_url:data,mime,final=fetcher(source);locator=source
    else:
        path=Path(source).expanduser().resolve()
        if not path.is_file():raise UnimError('not_found','Source file not found')
        if path.stat().st_size>MAX_BYTES:raise UnimError('too_large','Source exceeds 25 MiB')
        data=path.read_bytes();mime='';locator=str(path);final=locator
    if len(data)>MAX_BYTES:raise UnimError('too_large','Source exceeds 25 MiB')
    detected,text,details,suffix=extract(data,locator,mime)
    if not text.strip():raise UnimError('empty_source','Source contains no text')
    if len(text)>190000:raise UnimError('too_large','Extracted text exceeds 190,000 characters; split the source')
    fingerprint=hashlib.sha256(data).hexdigest()
    raw=core.data_dir/'sources'/(fingerprint+suffix);raw.parent.mkdir(parents=True,exist_ok=True)
    if raw.exists():
        if hashlib.sha256(raw.read_bytes()).hexdigest()!=fingerprint:raise UnimError('conflict','Preserved source was modified; refusing to overwrite')
    else:
        with raw.open('xb') as f:f.write(data)
    meta=dict(status='captured',ingested_source=True,original_sha256=fingerprint,raw_path='sources/'+raw.name,original_bytes=len(data),final_source=final,**details)
    result=core.store(text,request_id='ingest:'+hashlib.sha256((locator+'\n'+fingerprint+'\n'+(title or '')+'\ningest-v1').encode()).hexdigest(),title=(title or detected)[:250],source=locator,kind='source',metadata=meta)
    return dict(**result,original=str(raw),original_sha256=fingerprint,extraction=details,characters=len(text))
