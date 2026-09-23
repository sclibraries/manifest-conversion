"""One item per transaction; publication ownership and receipts survive retries."""
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from .core import VERSION, COMPASS, contained, convert, digest, encode, https_url, load_json, safe_id


def atomic_write(path, data, mode=0o600):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    if path.is_symlink(): raise ValueError('refusing symlink target')
    fd,temp=tempfile.mkstemp(prefix='.manifest-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
            os.fchmod(stream.fileno(),mode)
        os.replace(temp,path)
    finally:
        if os.path.exists(temp): os.unlink(temp)


def bounded_file(path):
    with path.open('rb') as stream: data=stream.read(8*1024*1024+1)
    if len(data)>8*1024*1024: raise ValueError('source byte limit exceeded')
    return data


def run_item(item, config, publish, fetch):
    name=safe_id(item['item_id'])
    if name in config.get('withdrawn_items',[]): raise ValueError('withdrawn item')
    if item.get('access')!='public' or not item.get('policy') or not item.get('approved_by'):
        raise ValueError('explicit public access policy and approving authority required')
    node=item.get('node_id','')
    if not str(node).isdigit(): raise ValueError('exported numeric node mapping required')
    source_url=f'https://{COMPASS}/node/{node}/manifest'
    if item.get('source_file'):
        raw=bounded_file(contained(config['source_root'],item['source_file']))
        if not item.get('source_sha256'): raise ValueError('retained source checksum required')
    else:
        if not config.get('allow_live') or datetime.now(timezone.utc).date().isoformat()>='2027-07-01':
            raise ValueError('live harvesting disabled; provide retained manifest')
        raw,_=fetch(source_url)
    if item.get('source_sha256') and digest(raw)!=item['source_sha256']:
        raise ValueError('source checksum mismatch')
    source=load_json(raw)
    if source.get('@id')!=source_url: raise ValueError('manifest does not match approved node')
    canonical=config['manifest_base'].rstrip('/')+'/'+name+'.json'
    https_url(canonical)
    converted,details=convert(source,canonical,config['image_base'])
    for page in details['pages']:
        data,_=fetch(page['service']+'/info.json'); info=load_json(data)
        if (info.get('width'),info.get('height'))!=(page['width'],page['height']):
            raise ValueError('image dimensions do not match canvas')
        if info.get('@id',info.get('id'))!=page['service']:
            raise ValueError('image service identifier mismatch')
    state=Path(config['state_dir']).resolve(); state.mkdir(parents=True,exist_ok=True)
    for directory in ('artifacts','sources','receipts','history'):
        contained(state,directory).mkdir(exist_ok=True)
    data=encode(converted); checksum=digest(data)
    artifact=contained(state,'artifacts/'+name+'.json')
    receipt_path=contained(state,'receipts/'+name+'.json')
    previous=load_json(bounded_file(receipt_path)) if receipt_path.exists() else {}
    receipt={'item_id':name,'node_id':str(node),'source_uri':source_url,
             'source_sha256':digest(raw),'sha256':checksum,'manifest_url':canonical,
             'converter_version':VERSION,'access_policy':item['policy'],'approved_by':item['approved_by'],
             'aspace_record':item.get('aspace_record',''),'original_uri':item.get('original_uri',source_url),
             'timestamp':datetime.now(timezone.utc).isoformat(), **details}
    atomic_write(contained(state,'sources/'+name+'-'+digest(raw)+'.json'),raw)
    atomic_write(artifact,data)
    if not publish:
        receipt['status']='validated'
        # A dry run must not replace a publication ownership receipt.
        atomic_write(contained(state,'artifacts/'+name+'-validation.json'),encode(receipt))
        return receipt
    destination=Path(config['publish_dir']).resolve()
    if not destination.is_dir(): raise ValueError('publish directory must already exist')
    target=contained(destination,name+'.json')
    old=bounded_file(target) if target.exists() else None
    owned={previous.get('sha256')}
    if previous.get('status')=='publishing':
        owned.add(previous.get('previous_sha256'))
    if old is not None and (digest(old) not in owned or previous.get('manifest_url')!=canonical):
        raise ValueError('destination collision or external change; manual reconciliation required')
    if old is not None and old!=data:
        atomic_write(contained(state,'history/'+name+'-'+digest(old)+'.json'),old)
    # Persist intended ownership before replacement for safe interrupted-run recovery.
    receipt['status']='publishing'
    receipt['previous_sha256']=digest(old) if old else None
    atomic_write(receipt_path,encode(receipt))
    if old!=data: atomic_write(target,data,0o644)
    receipt['status']='published_unverified'
    atomic_write(receipt_path,encode(receipt))
    remote,headers=fetch(canonical)
    headers={key.lower():value for key,value in headers.items()}
    if digest(remote)!=checksum: raise ValueError('published HTTPS checksum mismatch')
    cache=headers.get('cache-control','').lower()
    if not ('no-cache' in cache or 'no-store' in cache):
        raise ValueError('canonical manifest requires Cache-Control no-cache or no-store')
    origin=headers.get('access-control-allow-origin','')
    if origin not in ('*',config.get('pui_origin','__unset__')):
        raise ValueError('manifest CORS does not allow configured PUI')
    receipt['status']='unchanged' if old==data and previous.get('status') in ('published','unchanged') else 'published'
    atomic_write(receipt_path,encode(receipt))
    return receipt
