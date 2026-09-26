"""Strict, pseudonymous HTTP observation file contract shared with UBA."""
from datetime import datetime, timezone
import hashlib
import hmac
import json
from pathlib import Path
import re
import uuid

SCHEMA='zetty-http-observation/1'
KEYS={'schema_version','event_id','occurred_at','environment','client_key','path_key',
      'key_version','method','status_code','response_body_bytes'}
RAW_KEYS={'request_id','time','remote_addr','user_agent','method','uri','status','body_bytes'}
METHODS={'GET','POST','PUT','PATCH','DELETE','HEAD','OPTIONS','CONNECT','TRACE'}
MAX_LINE=65536


def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)


def unique_object(pairs):
    value={}
    for key,item in pairs:
        if key in value:raise ValueError('duplicate_json_key')
        value[key]=item
    return value


def parse_json(raw):return json.loads(raw,object_pairs_hook=unique_object)


def utc(value):
    if not isinstance(value,str) or len(value)>40:raise ValueError('invalid_time')
    try:stamp=datetime.fromisoformat(value.replace('Z','+00:00'))
    except ValueError:raise ValueError('invalid_time') from None
    if stamp.utcoffset() is None:raise ValueError('timezone_required')
    return stamp.astimezone(timezone.utc)


def validate(event):
    if not isinstance(event,dict) or set(event)!=KEYS:raise ValueError('invalid_fields')
    if event['schema_version']!=SCHEMA or event['environment']!='local-lab':raise ValueError('invalid_source')
    try:
        if str(uuid.UUID(event['event_id']))!=event['event_id']:raise ValueError('invalid_event_id')
    except (ValueError,TypeError,AttributeError):raise ValueError('invalid_event_id') from None
    if not event['occurred_at'].endswith('Z'):raise ValueError('utc_required')
    utc(event['occurred_at'])
    for key in ('client_key','path_key'):
        if not isinstance(event[key],str) or not re.fullmatch('[0-9a-f]{64}',event[key]):raise ValueError('invalid_pseudonym')
    if not isinstance(event['key_version'],str) or not re.fullmatch('[A-Za-z0-9_.-]{1,40}',event['key_version']):raise ValueError('invalid_key_version')
    if event['method'] not in METHODS:raise ValueError('invalid_method')
    if type(event['status_code']) is not int or not 100<=event['status_code']<=599:raise ValueError('invalid_status')
    size=event['response_body_bytes']
    if size is not None and (type(size) is not int or not 0<=size<=2**63-1):raise ValueError('invalid_bytes')
    return event


def normalize(raw,key,key_version):
    if not isinstance(raw,dict) or set(raw)!=RAW_KEYS:raise ValueError('invalid_raw_fields')
    if len(key)<32:raise ValueError('hmac_key_too_short')
    request=raw['request_id']
    if not isinstance(request,str) or not re.fullmatch('[0-9a-f]{32}',request):raise ValueError('invalid_request_id')
    for name,limit in (('remote_addr',128),('user_agent',2048),('uri',8192)):
        if not isinstance(raw[name],str) or not 1<=len(raw[name])<=limit:raise ValueError('invalid_context')
    if not raw['uri'].startswith('/') or '?' in raw['uri'] or '#' in raw['uri']:raise ValueError('path_without_query_required')
    def pseudonym(namespace,values):
        return hmac.new(key,canonical([namespace,*values]).encode(),hashlib.sha256).hexdigest()
    event={'schema_version':SCHEMA,'event_id':str(uuid.uuid5(uuid.NAMESPACE_URL,'zetty:edge:'+request)),
           'occurred_at':utc(raw['time']).isoformat().replace('+00:00','Z'),'environment':'local-lab',
           'client_key':pseudonym('client',[raw['remote_addr'],raw['user_agent']]),
           'path_key':pseudonym('path',[raw['uri']]),'key_version':key_version,
           'method':raw['method'],'status_code':raw['status'],'response_body_bytes':raw['body_bytes']}
    return validate(event)


def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):h.update(block)
    return h.hexdigest()
