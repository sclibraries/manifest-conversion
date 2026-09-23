"""Pure, deliberately narrow IIIF Presentation 2 conversion."""
import copy
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

VERSION = '1'
COMPASS = 'compass.fivecolleges.edu'


def safe_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', value):
        raise ValueError('invalid item identifier')
    return value


def safe_cell(value):
    value = str(value)
    return "'" + value if value.lstrip().startswith(('=', '+', '-', '@')) else value


def contained(root, name):
    root = Path(root).resolve()
    path = root / name
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise ValueError('path escapes configured root or is a symlink')
    return path


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encode(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + '\n').encode()


def load_json(data, limit=8*1024*1024):
    if len(data) > limit:
        raise ValueError('JSON byte limit exceeded')
    # Check nesting before invoking the recursive JSON decoder, respecting strings.
    depth = 0; quoted = False; escaped = False
    for char in data.decode('utf-8'):
        if quoted:
            if escaped: escaped = False
            elif char == '\\': escaped = True
            elif char == '"': quoted = False
        elif char == '"': quoted = True
        elif char in '[{':
            depth += 1
            if depth > 32: raise ValueError('JSON nesting limit exceeded')
        elif char in ']}': depth -= 1
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result: raise ValueError('duplicate JSON key')
            result[key] = value
        return result
    def invalid(value): raise ValueError('non-finite JSON number')
    return json.loads(data, object_pairs_hook=pairs, parse_constant=invalid)


def https_url(url):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None,443) or parsed.fragment:
        raise ValueError('expected HTTPS URL without credentials or fragment')
    return parsed


def image_key(service):
    parsed = https_url(service)
    if parsed.hostname != COMPASS or '/iiif/2/' not in parsed.path or parsed.query:
        raise ValueError('unsupported source image service')
    source = urlsplit(unquote(parsed.path.split('/iiif/2/',1)[1]))
    if source.scheme not in ('http','https') or source.hostname != COMPASS or not source.path.startswith('/system/files/') or source.query or source.fragment:
        raise ValueError('image source is not an explicit Compass file mapping')
    key = unquote(source.path[len('/system/files/'):])
    if not key or any(part in ('', '.', '..') for part in key.split('/')) or any(ord(c)<32 for c in key):
        raise ValueError('invalid image key')
    return key


def check_fields(obj, allowed):
    if not isinstance(obj, dict) or set(obj)-set(allowed.split()):
        raise ValueError('unsupported manifest fields; review required')


def convert(source, canonical, image_base):
    https_url(canonical); https_url(image_base)
    result = copy.deepcopy(source)
    check_fields(result, '@context @id @type label description attribution license metadata logo thumbnail sequences service behavior')
    if result.get('@type') != 'sc:Manifest' or result.get('@context') != 'http://iiif.io/api/presentation/2/context.json':
        raise ValueError('only IIIF Presentation 2 image manifests are supported')
    sequences=result.get('sequences',[])
    if len(sequences)!=1: raise ValueError('expected exactly one sequence')
    sequence=sequences[0]
    check_fields(sequence, '@id @type @context label canvases viewingHint viewingDirection')
    canvases=sequence.get('canvases',[])
    if not 1 <= len(canvases) <= 2000: raise ValueError('page count outside 1–2000')
    result['@id']=canonical
    sequence['@id']=canonical+'/sequence/normal'
    receipt={'pages':[], 'omitted_hocr_links':[], 'omitted_search_services':[]}
    if 'service' in result:
        services=result['service']
        if not isinstance(services,list) or any(not isinstance(v,dict) or
                v.get('profile')!='http://iiif.io/api/search/0/search' for v in services):
            raise ValueError('unsupported manifest service; review required')
        receipt['omitted_search_services']=result.pop('service')
    seen=set()
    for canvas in canvases:
        check_fields(canvas, '@id @type label width height images thumbnail metadata seeAlso')
        identity=canvas.get('@id',''); https_url(identity)
        if identity in seen: raise ValueError('duplicate canvas')
        seen.add(identity)
        if canvas.get('@type')!='sc:Canvas': raise ValueError('invalid canvas type')
        for dimension in ('width','height'):
            if type(canvas.get(dimension)) is not int or not 0 < canvas[dimension] <= 100000:
                raise ValueError('invalid canvas dimensions')
        annotations=canvas.get('images',[])
        if len(annotations)!=1: raise ValueError('expected one painting per page')
        annotation=annotations[0]
        check_fields(annotation,'@id @type motivation resource on')
        if annotation.get('on')!=identity or annotation.get('motivation')!='sc:painting':
            raise ValueError('annotation target or motivation mismatch')
        resource=annotation['resource']
        check_fields(resource,'@id @type format width height service')
        if resource.get('@type')!='dctypes:Image': raise ValueError('unsupported media type')
        service=resource.get('service')
        check_fields(service,'@id @context profile')
        key=image_key(service['@id'])
        # Same encoded-key convention as workbench_lite.manifest.build_cantaloupe_service_id.
        target=image_base.rstrip('/')+'/'+quote(key,safe='')
        resource.update({'@id':target+'/full/full/0/default.jpg','format':'image/jpeg',
                         'width':canvas['width'],'height':canvas['height'],
                         'service':{'@id':target,'@context':'http://iiif.io/api/image/2/context.json',
                                    'profile':'http://iiif.io/api/image/2/level2.json'}})
        canvas['thumbnail']={'@id':target+'/full/!300,300/0/default.jpg','format':'image/jpeg'}
        if 'seeAlso' in canvas:
            link=canvas['seeAlso']
            if not isinstance(link,dict) or link.get('format')!='text/vnd.hocr+html':
                raise ValueError('unsupported companion link; review required')
            receipt['omitted_hocr_links'].append({'canvas':identity,'source':canvas.pop('seeAlso')})
        receipt['pages'].append({'canvas':identity,'image_key':key,'service':target,
                                  'width':canvas['width'],'height':canvas['height']})
    result['thumbnail']=canvases[0]['thumbnail']
    # Metadata is text, never trusted HTML. Reject markup rather than silently altering it.
    def inspect(value, key=''):
        if isinstance(value,dict):
            for k,v in value.items(): inspect(v,k)
        elif isinstance(value,list):
            for v in value: inspect(v,key)
        elif isinstance(value,str):
            if '<' in value or '>' in value: raise ValueError('markup requires metadata review')
            if key not in ('@id','@context','on','profile') and ('javascript:' in value.lower() or 'data:' in value.lower()):
                raise ValueError('unsafe metadata URI')
    inspect(result)
    if 'logo' in result: raise ValueError('logo delivery requires explicit review')
    return result,receipt
