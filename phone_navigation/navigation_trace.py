"""Bounded local timestamped route/GPS/model/signal evidence, no new worker."""
import json
import os
import sqlite3
import time
from pathlib import Path

ROOT=Path('/data/bms/nap_dash')


class NavigationTrace:
    def __init__(self,root=ROOT):
        self.root=Path(root);self.db=None;self.last=-1e9;self.state=None
        self.count=0;self.error='';self.route_id=None
        self.retained_bytes=0
        self.clock_offset=None;self.clock_epoch=0

    def connect(self):
        if self.db is not None:return self.db
        self.root.mkdir(parents=True,exist_ok=True)
        path=self.root/'nap_navigation.sqlite3'
        # Created lazily in the existing navigation worker, never in supervisor.
        db=sqlite3.connect(path,timeout=.01)
        self.db=db
        db.execute('PRAGMA journal_mode=WAL');db.execute('PRAGMA synchronous=NORMAL')
        db.execute('PRAGMA wal_autocheckpoint=64');db.execute('PRAGMA max_page_count=8192')
        db.execute('CREATE TABLE IF NOT EXISTS navigation_trace(id INTEGER PRIMARY KEY,created REAL NOT NULL,mono REAL NOT NULL,route_id TEXT,event TEXT,payload TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS navigation_routes(route_id TEXT PRIMARY KEY,created REAL NOT NULL,payload TEXT NOT NULL)')
        self.retained_bytes=db.execute('SELECT coalesce(sum(length(payload)+128),0) FROM navigation_trace').fetchone()[0]
        db.commit()
        for p in (path,Path(str(path)+'-wal'),Path(str(path)+'-shm')):
            if p.exists():os.chmod(p,0o600)
        self.db=db;return db

    def write(self,event,now,route_id='',data=None,route=None,continuous=False):
        wall=time.time();offset=wall-now
        if self.clock_offset is not None and abs(offset-self.clock_offset)>2:self.clock_epoch+=1
        self.clock_offset=offset
        data=dict(data or {})
        data['clock']={'observed_wall_unix':wall,'wall_minus_mono_s':offset,
                       'synchronized':Path('/run/systemd/timesync/synchronized').exists(),
                       'epoch':self.clock_epoch}
        model=data.get('model',{}).get('data',{})
        signal=data.get('signal',{}).get('data',{})
        key=(event,route_id,data.get('generation'),self.clock_epoch,model.get('reason'),model.get('desire'),
             signal.get('active'),signal.get('sent_recent'),signal.get('block_reason'))
        if not continuous and key==self.state:return
        if continuous and now-self.last<.5 and key==self.state:return
        self.state=key;self.last=now
        try:
            db=self.connect()
            if route is not None and route_id!=self.route_id:
                raw=json.dumps(route,separators=(',',':'),allow_nan=False)
                if len(raw.encode())<=512*1024:
                    db.execute('INSERT OR REPLACE INTO navigation_routes VALUES(?,?,?)',(route_id,wall,raw))
                    db.execute('DELETE FROM navigation_routes WHERE route_id NOT IN (SELECT route_id FROM navigation_routes ORDER BY created DESC LIMIT 8)')
                self.route_id=route_id
            payload=json.dumps(data,separators=(',',':'),allow_nan=False)
            if len(payload.encode())>12000:raise ValueError('Trace record too large')
            row=db.execute('INSERT INTO navigation_trace(created,mono,route_id,event,payload) VALUES(?,?,?,?,?)',(wall,now,route_id,event,payload))
            self.retained_bytes+=len(payload)+128
            self.count+=1
            if self.count%60==0 or self.retained_bytes>16*1024*1024:
                cutoff=row.lastrowid-12000;needed=max(0,self.retained_bytes-16*1024*1024)
                removed=0;last=None
                for ident,size in db.execute('SELECT id,length(payload)+128 FROM navigation_trace ORDER BY id'):
                    if ident>cutoff and removed>=needed:break
                    removed+=size;last=ident
                if last is not None:
                    db.execute('DELETE FROM navigation_trace WHERE id<=?',(last,));self.retained_bytes-=removed
            db.commit();self.error=''
        except (OSError,ValueError,sqlite3.Error) as exc:
            self.error=type(exc).__name__
            if self.db is not None:
                try:self.db.rollback();self.db.close()
                except sqlite3.Error:pass
                self.db=None


def snapshot(path,now,stamp,max_age=3):
    try:
        with open(path) as f:raw=f.read(8193)
        d=json.loads(raw) if len(raw)<=8192 else None
        age=now-d[stamp]
        return {'data':d,'age_s':round(age,4),'fresh':0<=age<=max_age}
    except (OSError,ValueError,KeyError,TypeError):return {'fresh':False}
