"""Authenticated, bounded read access for the existing NAP web server.

No background worker, database writes, network fetches, or control commands.
New public STATE fields, registered message services, diagnostic JSON snapshots,
history kinds and ordinary SQLite tables are discovered on each request.
"""
import copy
import json
import math
import re
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.parse import parse_qs, urlencode

DIAGNOSTICS = Path('/dev/shm')
MAX_BYTES = 320 * 1024
SLOT = threading.BoundedSemaphore(1)
SECRET = re.compile(r'password|passwd|secret|token|authorization|cookie|api.?key|pairing.?key|private.?key', re.I)
BULK = re.compile(r'encode|camera|thumbnail|livestream|audio|microphone|speaker|ublox|qcomGnss|gpsNMEA|^can$|sendcan|logMessage|initData', re.I)

def integer(value, low, high, name):
    if type(value) is not int or not low <= value <= high:
        raise ValueError('%s must be an integer from %s to %s' % (name, low, high))
    return value

def clean(value, depth=0):
    if depth > 32:
        raise ValueError('Data nesting exceeds read limit; choose a narrower path')
    if isinstance(value, dict):
        # Also handle parameter maps represented as key/value records.
        if isinstance(value.get('key'), str) and SECRET.search(value['key']):
            return {'key': value['key'], 'value': '[redacted]'}
        return {str(k): '[redacted]' if SECRET.search(str(k)) else clean(v, depth+1)
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v, depth+1) for v in value]
    if isinstance(value, bytes):
        return {'binary_bytes': len(value), 'available': False, 'reason': 'Binary payload excluded'}
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value

def encoded(value):
    raw = json.dumps(clean(value), allow_nan=False, separators=(',', ':')).encode()
    if len(raw) > MAX_BYTES:
        raise ValueError('Result exceeds response limit; select a narrower path, fewer rows or SQL substr()')
    return raw

def at_path(value, path):
    if not isinstance(path, str) or len(path) > 1000 or (path and not path.startswith('/')):
        raise ValueError('path must be an empty string or a JSON Pointer such as /car/vEgo')
    for key in path.split('/')[1:] if path else []:
        key = key.replace('~1', '/').replace('~0', '~')
        if isinstance(value, dict):
            if key not in value: raise ValueError('Data path unavailable; refresh nap_data_catalog')
            value = value[key]
        elif isinstance(value, list) and key.isdecimal() and int(key) < len(value):
            value = value[int(key)]
        else:
            raise ValueError('Data path unavailable; refresh nap_data_catalog')
    return value

def paged(value, args):
    offset = integer(args.get('offset', 0), 0, 1000000000, 'offset')
    limit = integer(args.get('limit', 100), 1, 500, 'limit')
    if isinstance(value, dict):
        keys = list(value); selected = keys[offset:offset+limit]
        return {k:value[k] for k in selected}, (offset+limit if offset+limit < len(keys) else None), len(keys)
    if isinstance(value, list):
        return value[offset:offset+limit], (offset+limit if offset+limit < len(value) else None), len(value)
    if offset: raise ValueError('Scalar data cannot be paged')
    return value, None, None

def databases(ctx):
    # Explicit data root, not an arbitrary path or credential/config database.
    root = Path(ctx['DATA_DIR']).resolve()
    found = {}
    for p in sorted(root.iterdir()):
        if p.suffix not in ('.sqlite', '.sqlite3', '.db') or p.is_symlink() or not p.is_file(): continue
        if SECRET.search(p.name) or re.search(r'oauth|credential|auth', p.name, re.I): continue
        name = 'history' if p.name == 'nap_history.sqlite3' else p.stem
        found[name] = p
    return found

def connect(ctx, name='history'):
    choices = databases(ctx)
    if not isinstance(name, str) or name not in choices:
        raise ValueError('Unknown NAP data database; refresh nap_data_catalog')
    db = sqlite3.connect(choices[name].as_uri()+'?mode=ro', uri=True, timeout=.1)
    db.enable_load_extension(False)
    db.execute('PRAGMA query_only=ON')
    db.execute('PRAGMA trusted_schema=OFF')
    db.execute('PRAGMA temp_store=MEMORY')
    db.execute('PRAGMA cache_size=-512')
    db.execute('PRAGMA mmap_size=0')
    for key, limit in [('SQLITE_LIMIT_LENGTH', 384*1024), ('SQLITE_LIMIT_SQL_LENGTH', 16000),
                       ('SQLITE_LIMIT_COLUMN', 128), ('SQLITE_LIMIT_EXPR_DEPTH', 40),
                       ('SQLITE_LIMIT_COMPOUND_SELECT', 12), ('SQLITE_LIMIT_VARIABLE_NUMBER', 100),
                       ('SQLITE_LIMIT_ATTACHED', 0), ('SQLITE_LIMIT_VDBE_OP', 50000)]:
        db.setlimit(getattr(sqlite3, key), limit)
    deadline = time.monotonic()+.65
    steps = [0]
    def progress():
        steps[0] += 1000
        return int(steps[0] > 2500000 or time.monotonic() > deadline)
    db.set_progress_handler(progress, 1000)
    return db

def schema(db):
    result = []
    for name, sql in db.execute("SELECT name,sql FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
        if SECRET.search(name) or (sql or '').upper().startswith('CREATE VIRTUAL'): continue
        quoted = '"'+name.replace('"','""')+'"'
        columns = [{'name':r[1], 'type':r[2], 'primary_key':bool(r[5])} for r in db.execute('PRAGMA table_info('+quoted+')')]
        result.append({'name':name,'columns':columns,'definition':sql})
    return result

def query(ctx, args):
    sql = args.get('sql')
    if not isinstance(sql, str) or not 1 <= len(sql) <= 16000:
        raise ValueError('A single read-only SQL query is required (maximum 16000 characters)')
    # The authorizer, read-only connection and single execute are the enforcement;
    # this prefix check only supplies a clearer error for non-query operations.
    if not re.match(r'^\s*(SELECT|WITH)\b', sql, re.I):
        raise ValueError('Only SELECT or WITH queries are supported')
    parameters = args.get('parameters', [])
    if not isinstance(parameters, (list, dict)) or len(parameters) > 100:
        raise ValueError('parameters must be a list or named-parameter object (maximum 100)')
    if any(not (v is None or type(v) in (str,int,float,bool)) or (isinstance(v,float) and not math.isfinite(v))
           for v in (parameters.values() if isinstance(parameters,dict) else parameters)):
        raise ValueError('SQL parameters must be finite scalar values')
    limit = integer(args.get('limit',100),1,500,'limit')
    offset = integer(args.get('offset',0),0,1000000000,'offset')
    with closing(connect(ctx,args.get('database','history'))) as db:
        tables = {r['name'] for r in schema(db)} | {'sqlite_schema','sqlite_master'}
        def authorize(action, a, b, database, trigger):
            if action in (sqlite3.SQLITE_SELECT,sqlite3.SQLITE_RECURSIVE): return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_READ:
                # SQLite's optimized count(*) can report an empty column and
                # no database name. It still names the authorized table.
                main_read = database == 'main' or (database is None and b == '')
                return sqlite3.SQLITE_OK if main_read and a in tables and not SECRET.search(b or '') else sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_FUNCTION:
                return sqlite3.SQLITE_DENY if (b or '').lower() in ('load_extension','readfile','writefile','randomblob','zeroblob','fts3_tokenizer') else sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY
        db.set_authorizer(authorize)
        # Wrapping gives reliable row pagination even if the caller omitted LIMIT.
        cur = db.execute('SELECT * FROM ('+sql.rstrip().removesuffix(';')+') LIMIT %d OFFSET %d' % (limit+1,offset), parameters)
        columns = [d[0] for d in cur.description]
        rows = []; size = 0; more = False
        for row in cur:
            if len(rows) == limit: more = True; break
            row = list(row); n = len(encoded(row))
            if size+n > MAX_BYTES-12000:
                if not rows: raise ValueError('SQL row too large; choose columns or SQL substr()')
                more = True; break
            size += n; rows.append(clean(row))
        return {'database':args.get('database','history'),'columns':columns,'rows':rows,'row_count':len(rows),
                'next_offset':offset+len(rows) if more else None,
                'read_only':True,'scope':'All retained records; no implicit date filter. Use ORDER BY with a unique key for stable paging. Live inserts/deletions can change offset pages.'}

def history(ctx,args):
    limit=integer(args.get('limit',100),1,500,'limit')
    after=integer(args.get('after_id',0),0,2**63-1,'after_id')
    upper=args.get('through_id')
    with closing(connect(ctx)) as db:
        if upper is None: upper=db.execute('SELECT coalesce(max(id),0) FROM history').fetchone()[0]
        integer(upper,0,2**63-1,'through_id')
    clauses=['id>?','id<=?'];params=[after,upper]
    kind=args.get('kind')
    if kind:
        if not isinstance(kind,str) or len(kind)>100: raise ValueError('Invalid history kind')
        clauses.append('kind=?');params.append(kind)
    for field,op in [('since','>='),('before','<')]:
        if field in args:
            v=args[field]
            if type(v) not in (int,float) or not math.isfinite(v): raise ValueError('History times must be Unix seconds')
            clauses.append('created'+op+'?');params.append(v)
    out=query(ctx,{'sql':'SELECT id,kind,created,payload FROM history WHERE '+' AND '.join(clauses)+' ORDER BY id','parameters':params,'limit':limit})
    records=[]
    for ident,kind,created,payload in out['rows']:
        try: payload=json.loads(payload)
        except (ValueError,TypeError): pass
        records.append({'id':ident,'kind':kind,'created_at_unix':created,'payload':payload})
    return {'records':records,'through_id':upper,'next_after_id':records[-1]['id'] if records and out['next_offset'] is not None else None,
            'read_only':True,'retention_note':'Pages cover all retained kinds and dates, not deleted or never-recorded data. Keep through_id unchanged for the same scan.'}

def diagnostic_paths():
    return {p.stem:p for p in DIAGNOSTICS.glob('nap_*.json')
            if p.is_file() and not p.is_symlink() and not SECRET.search(p.name) and p.stat().st_size <= MAX_BYTES}

def services():
    try:
        import sys
        for root in ('/data', '/data/openpilot'):
            if Path(root).is_dir() and root not in sys.path: sys.path.insert(0,root)
        from cereal.services import SERVICE_LIST
        return {k:v for k,v in SERVICE_LIST.items() if not BULK.search(k)}
    except ImportError:
        return {}

def service(name):
    choices=services()
    if name not in choices: raise ValueError('Unknown or bulk/private service; refresh nap_data_catalog')
    from cereal import messaging
    # One bounded on-demand subscription, only while handling an explicit read.
    sm=messaging.SubMaster([name])
    deadline=time.monotonic()+1.2
    while not sm.seen[name] and time.monotonic()<deadline:
        sm.update(max(1,min(200,int((deadline-time.monotonic())*1000))))
    if not sm.seen[name]: return {'available':False,'status':'unavailable','reason':'No retained message was available during this read'}
    mono=sm.logMonoTime[name]/1e9;age=time.monotonic()-mono
    freq=getattr(choices[name],'frequency',0)
    fresh_limit=min(30,max(2,3/freq)) if freq else None
    message=sm[name]
    value=message.to_dict() if hasattr(message,'to_dict') else message.to_list()
    return {'available':True,'data':value,'producer_mono':mono,'age_seconds':round(age,3),'valid':bool(sm.valid[name]),
            'status':('fresh' if sm.valid[name] and 0<=age<=fresh_limit else 'stale') if fresh_limit else 'event_timestamp_only',
            'fresh_limit_s':fresh_limit,'note':'Status describes producer publication. Nested sensors may have older timestamps; GPS unixTimestampMillis is the sensor time.'}

REPLAY_HELP = {
    'topic_format': 'replay/<segment>?source=rlog&start_s=12&end_s=17&sample_hz=20&include_model_path=true',
    'example': 'replay/0000041c--17737e41c1--78?source=rlog&start_s=12&end_s=17&sample_hz=20&include_model_path=true',
    'limits': 'Offroad only; 0-65 seconds, 0.5-20 Hz; at most 100 samples with paths or 120 without.',
    'paging': 'Use the normal JSON Pointer path and offset/limit fields to select or page samples/events.',
    'read_only': True,
}

def replay_topic(name):
    segment, _, query_string = name.partition('?')
    if not re.fullmatch(r'[0-9a-fA-F]{8}--[0-9a-fA-F]{10,40}--[0-9]{1,4}', segment):
        raise ValueError('Choose a recorded segment; read replay/help for the topic format')
    args = parse_qs(query_string, keep_blank_values=True)
    if set(args)-{'source','start_s','end_s','sample_hz','include_model_path'} or any(len(v)!=1 for v in args.values()):
        raise ValueError('Unknown or repeated replay options')
    values = {k: v[0] for k, v in args.items()}
    if values.get('source', 'qlog') not in ('qlog', 'rlog'):
        raise ValueError('Invalid replay source')
    if values.get('include_model_path', 'false') not in ('true', 'false'):
        raise ValueError('include_model_path must be true or false')
    # Only this fixed local read endpoint is reachable. Its existing offroad
    # guard and resource/window validation remain authoritative.
    url = 'http://127.0.0.1:7070/api/control/replay?' + urlencode(dict(values, segment=segment))
    with OPENER.open(Request(url, method='GET'), timeout=18) as response:
        raw = response.read(MAX_BYTES+1)
    if len(raw) > MAX_BYTES:
        raise ValueError('Replay response too large; narrow the window')
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get('version') != 2 or 'error' in value:
        raise ValueError('Expanded diagnostic replay is not active; reload the diagnostic server')
    return value

def read(ctx,args):
    topic=args.get('topic');path=args.get('path','')
    if not isinstance(topic,str) or '/' not in topic: raise ValueError('Choose a catalog topic')
    group,name=topic.split('/',1)
    if group=='state':
        with ctx['LOCK']:
            if name.startswith('_') or name not in ctx['STATE']: raise ValueError('Unknown public state group')
            value=copy.deepcopy(ctx['STATE'][name])
            metadata=copy.deepcopy(ctx['STATE'].get('_assistant_sources',{}))
        freshness={'status':'unknown','note':'Raw dashboard state can retain old values. Use source_metadata and nap_live_state per-signal freshness; retrieval time is not freshness.','source_metadata':metadata}
    elif group=='diagnostic':
        files=diagnostic_paths()
        if name not in files: raise ValueError('Diagnostic not available; refresh nap_data_catalog')
        p=files[name]
        with p.open('rb') as f: raw=f.read(MAX_BYTES+1)
        if len(raw)>MAX_BYTES: raise ValueError('Diagnostic exceeds read limit')
        value=json.loads(raw)
        freshness={'status':'unknown','file_modified_at_unix':p.stat().st_mtime,'note':'File modification is not CAN freshness. Interpret producer timestamps and status in the data.'}
        if name in ('nap_city_turn_intent', 'nap_turn_diagnostics', 'nap_turn_longitudinal', 'nap_driving_evidence', 'nap_driving_evidence_recent') and isinstance(value, dict):
            stamp = value.get('producer_mono')
            age = time.monotonic()-stamp if type(stamp) in (int, float) and math.isfinite(stamp) else None
            boot = value.get('boot_id')
            try:
                same_boot = boot == Path('/proc/sys/kernel/random/boot_id').read_text().strip() if boot else False
            except OSError:
                same_boot = False
            freshness.update(status=('fresh' if age is not None and 0 <= age <= .5 and same_boot else 'stale'),
                             producer_mono=stamp, age_seconds=round(age, 4) if age is not None else None,
                             boot_verified=same_boot,
                             note='Snapshot age describes collection/publication; nested sources retain their own validity and producer age.')
            if name == 'nap_driving_evidence':
                freshness['sources'] = value.get('sources', {})

    elif group=='replay':
        value = REPLAY_HELP if name == 'help' else replay_topic(name)
        freshness = {'status':'historical','note':'Recorded data; retrieval time does not refresh the original source timestamps.'}
    elif group=='service':
        envelope=service(name);value=envelope.pop('data',None);freshness=envelope
    elif group=='navigation' and name=='full':
        value=ctx['PHONE_NAV'].tracker.status()
        freshness={'status':'per_field','note':'Use gps_ready, route_context GPS age and model_decision timestamps; controls/influence are unchanged.'}
    elif group=='recorder' and name=='full':
        value=ctx['RECORDER'].status(False)
        freshness={'status':'per_field','note':'Recording status includes historical or active-session values; check its timestamps.'}
    else: raise ValueError('Unknown data topic; refresh nap_data_catalog')
    selected=at_path(clean(value),path)
    selected,next_offset,total=paged(selected,args)
    return {'topic':topic,'path':path,'data':selected,'next_offset':next_offset,'total_items':total,'freshness':freshness,'read_only':True}

def catalog(ctx,args):
    topic=args.get('topic')
    if topic:
        view=read(ctx,{'topic':topic,'path':args.get('path',''),'limit':args.get('limit',100),'offset':args.get('offset',0)})
        value=view['data']
        fields=[{'name':str(k),'type':type(v).__name__} for k,v in value.items()] if isinstance(value,dict) else [{'type':type(value).__name__,'items':view['total_items']}]
        return {'topic':topic,'path':args.get('path',''),'fields':fields,'next_offset':view['next_offset'],'freshness':view['freshness']}
    with ctx['LOCK']: topics=['state/'+k for k in ctx['STATE'] if not k.startswith('_')]
    topics+=['navigation/full','recorder/full','replay/help']
    topics+=['diagnostic/'+k for k in diagnostic_paths()]
    topics+=['service/'+k for k in services()]
    dbs={}
    for name in databases(ctx):
        with closing(connect(ctx,name)) as db: dbs[name]={'tables':schema(db)}
    return {'version':2,'topics':sorted(topics),'databases':dbs,'read_only':True,
            'replay':REPLAY_HELP,
            'history':'nap_history_records reads every retained kind/date with keyset pagination; SQL supports JSON extraction, joins, aggregates and window functions.',
            'discovery':'Refresh this catalog on a new question about unfamiliar data or after a missing field. New public STATE fields, diagnostic JSON files, cereal services and data tables appear automatically.',
            'limits':{'response_bytes':MAX_BYTES,'rows_per_page':500,'sql_seconds':.65,'sql_vm_steps':2500000},
            'excluded':'Credentials/OAuth databases, file/shell access, raw CAN, media/binary streams and control execution. No source is made fresh by reading it.'}

def dispatch(ctx,args):
    if not isinstance(args,dict): raise ValueError('JSON object required')
    operation=args.get('operation')
    fields={'catalog':{'topic','path','offset','limit'},'read':{'topic','path','offset','limit'},
            'query':{'database','sql','parameters','offset','limit'},'history':{'kind','after_id','through_id','since','before','limit'}}
    if operation not in fields or set(args)-({'operation'}|fields[operation]): raise ValueError('Unknown operation or arguments')
    if not SLOT.acquire(False): raise ValueError('Read service busy; retry shortly')
    try:
        out={'catalog':catalog,'read':read,'query':query,'history':history}[operation](ctx,args)
        out['retrieved_at_unix']=time.time()
        return encoded(out)
    finally: SLOT.release()

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None

OPENER=build_opener(NoRedirect)

def local_api(path,body=None):
    # All calls are fixed local read endpoints. Never accept an arbitrary URL.
    if path not in ('/api/state?view=battery','/api/lab/state','/phone-nav/api/status'):
        raise ValueError('Unsupported internal read')
    headers={}
    if path.startswith('/phone-nav/'):
        headers['X-NAP-Phone-Key']=Path('/data/server/phone_navigation/.pairing-key').read_text().strip()
        body={}
    req=Request('http://127.0.0.1:7070'+path,None if body is None else json.dumps(body).encode(),headers)
    with OPENER.open(req,timeout=2) as response:raw=response.read(2*1024*1024+1)
    if len(raw)>2*1024*1024:raise ValueError('NAP state snapshot too large')
    value=json.loads(raw)
    if not isinstance(value,dict) or 'error' in value:raise ValueError('NAP source unavailable')
    return value
