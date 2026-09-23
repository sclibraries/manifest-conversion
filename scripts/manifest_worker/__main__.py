"""Run with python3 -m scripts.manifest_worker from the repository root."""
import argparse
import csv
import fcntl
import io
import json
import os
import pwd
import sys
from datetime import datetime, timezone
from pathlib import Path
from .core import contained, digest, encode, load_json, safe_cell, safe_id, https_url
from .runner import atomic_write, bounded_file, run_item
from .transport import Fetcher


def jobs(path):
    if path.is_symlink() or not path.is_file(): raise ValueError('job must be a regular CSV file')
    data=bounded_file(path)
    reader=csv.DictReader(io.StringIO(data.decode('utf-8-sig')))
    if reader.fieldnames!=['item_id']: raise ValueError('job CSV must have exactly one column: item_id')
    rows=[]; seen=set()
    for row in reader:
        name=safe_id(row.get('item_id'))
        if None in row: raise ValueError('extra CSV fields')
        if name in seen: raise ValueError('duplicate item within job')
        seen.add(name); rows.append(name)
        if len(rows)>1000: raise ValueError('job exceeds 1000 items; split inventory into batches')
    if not rows: raise ValueError('empty job')
    return rows,digest(data),pwd.getpwuid(path.stat().st_uid).pw_name


def main(argv=None):
    parser=argparse.ArgumentParser(description='Validate/harvest image manifests; publish only with --publish.')
    parser.add_argument('--config',type=Path,required=True)
    inputs=parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument('--jobs',type=Path)
    inputs.add_argument('--inbox',type=Path)
    parser.add_argument('--publish',action='store_true')
    args=parser.parse_args(argv)
    config=load_json(bounded_file(args.config))
    # Config and approval inventory are operator-managed, never read from the staff drop folder.
    for key in ('manifest_base','image_base'):
        url=https_url(config[key])
        if url.query: raise ValueError('base URL cannot have query parameters')
    for key in ('source_root','state_dir','inventory'):
        if not Path(config[key]).is_absolute(): raise ValueError(key+' must be absolute')
    inventory=load_json(bounded_file(Path(config['inventory'])))
    if not isinstance(inventory,dict): raise ValueError('inventory must map item IDs to approved metadata')
    state=Path(config['state_dir']).resolve(); state.mkdir(parents=True,exist_ok=True)
    os.chmod(state,0o700)
    if args.publish:
        destination=Path(config['publish_dir']).resolve()
        if state.is_relative_to(destination) or destination.is_relative_to(state):
            raise ValueError('state and public destination must be separate directories')
    fetch=Fetcher(config['allowed_hosts'])
    results=[]
    # The shared private state directory is the lock scope for all cron/operator invocations.
    with contained(state,'worker.lock').open('a') as lock:
        try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: raise ValueError('another worker holds the state lock')
        paths=[args.jobs] if args.jobs else sorted(args.inbox.glob('*.csv'))
        if len(paths)>100: raise ValueError('more than 100 ready job files; archive processed jobs')
        for path in paths:
            try:
                names,job_hash,submitter=jobs(path)
                for name in names:
                    try:
                        if name not in inventory: raise ValueError('item is not in approved inventory')
                        item=dict(inventory[name],item_id=name)
                        receipt=run_item(item,config,args.publish,fetch)
                        result={k:receipt.get(k,'') for k in ('item_id','status','manifest_url','aspace_record','original_uri','sha256')}
                        result.update(job_sha256=job_hash,submitter=submitter,error='')
                    except (ValueError,OSError,KeyError,TypeError) as error:
                        result={'item_id':name,'status':'failed','error':str(error),
                                'job_sha256':job_hash,'submitter':submitter}
                    results.append(result)
            except (ValueError,OSError,KeyError,TypeError) as error:
                results.append({'item_id':'','status':'failed','error':str(error)})
        run_id=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        reports=contained(state,'reports'); reports.mkdir(exist_ok=True)
        atomic_write(contained(reports,run_id+'.json'),encode(results))
        stream=io.StringIO(); writer=csv.writer(stream)
        fields=['item_id','status','manifest_url','aspace_record','original_uri','sha256','submitter','job_sha256','error']
        writer.writerow(fields)
        for row in results: writer.writerow([safe_cell(row.get(key,'')) for key in fields])
        atomic_write(contained(reports,run_id+'.csv'),stream.getvalue().encode())
    failed=sum(row['status']=='failed' for row in results)
    print(json.dumps({'items':len(results),'failed':failed,'requests':fetch.requests,'bytes':fetch.bytes,
                      'report':str(reports/(run_id+'.csv')),'published_mode':args.publish}))
    return 1 if failed else 0


if __name__=='__main__':
    try: sys.exit(main())
    except (ValueError,OSError,KeyError,TypeError) as error:
        print('manifest-worker: '+str(error),file=sys.stderr); sys.exit(2)
