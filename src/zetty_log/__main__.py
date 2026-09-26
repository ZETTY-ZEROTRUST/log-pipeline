"""Normalize bounded Nginx JSON files; no message broker or network calls."""
import argparse
from collections import Counter
import json
from pathlib import Path

from .http import MAX_LINE,canonical,digest,normalize,parse_json,utc


def produce(source,output,key,version,start,end,complete=False,max_rows=100000):
    start,end=utc(start),utc(end)
    if start>=end or not 1<=max_rows<=100000:raise ValueError('invalid_capture_limits')
    output=Path(output)
    if output.exists():raise ValueError('output_exists')
    events={};rejected=Counter();total=duplicates=0
    with Path(source).open() as stream:
        while raw:=stream.readline(MAX_LINE+1):
            total+=1
            if total>max_rows or len(raw.encode())>MAX_LINE:raise ValueError('input_resource_limit')
            try:
                event=normalize(parse_json(raw),key,version)
                if not start<=utc(event['occurred_at'])<end:raise ValueError('outside_capture')
            except (ValueError,TypeError,OverflowError,AttributeError,KeyError):
                rejected['invalid_record']+=1;continue
            old=events.get(event['event_id'])
            if old is not None:
                if old!=event:raise ValueError('event_id_content_conflict')
                duplicates+=1;continue
            events[event['event_id']]=event
    output.mkdir(parents=True)
    path=output/'events.jsonl'
    with path.open('x') as stream:
        for event in sorted(events.values(),key=lambda e:(e['occurred_at'],e['event_id'])):
            stream.write(canonical(event)+'\n')
    manifest={'schema_version':'zetty-http-capture/1','events_sha256':digest(path),
        'capture_start':start.isoformat().replace('+00:00','Z'),'capture_end':end.isoformat().replace('+00:00','Z'),
        'complete':bool(complete and not rejected),'completeness_basis':'caller assertion; no invalid records; not inferred from elapsed time',
        'key_version':version,'environment':'local-lab','raw_rows':total,'events':len(events),
        'duplicate_rows':duplicates,'rejected_rows':dict(rejected),'producer':'zetty-log-file/1'}
    (output/'capture.json').write_text(json.dumps(manifest,indent=2)+'\n')
    return manifest


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for key in ('input','output','key-file'):p.add_argument('--'+key,required=True,type=Path)
    for key in ('key-version','capture-start','capture-end'):p.add_argument('--'+key,required=True)
    p.add_argument('--complete',action='store_true',help='explicit assertion of complete local capture; default incomplete')
    p.add_argument('--max-rows',type=int,default=100000)
    a=p.parse_args()
    if a.key_file.stat().st_size!=32:raise ValueError('expected_32_byte_key_file')
    result=produce(a.input,a.output,a.key_file.read_bytes(),a.key_version,a.capture_start,a.capture_end,a.complete,a.max_rows)
    print(json.dumps(result))

if __name__=='__main__':main()
