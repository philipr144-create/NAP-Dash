import copy
import importlib.util
import io
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

spec=importlib.util.spec_from_file_location('read_api',Path(__file__).with_name('nap_read_api.py'))
r=importlib.util.module_from_spec(spec);spec.loader.exec_module(r)

class ReadsTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        r.DIAGNOSTICS=self.root
        self.ctx={'DATA_DIR':str(self.root),'STATE':{'car':{'vEgo':12,'newSignal':8},'_secret':'private'},'LOCK':threading.Lock()}
        self.path=self.root/'nap_history.sqlite3'
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE history(id INTEGER PRIMARY KEY,kind TEXT,created REAL,payload TEXT)')
            db.execute('CREATE TABLE segment_telemetry(segment TEXT PRIMARY KEY,updated REAL,payload TEXT)')
            db.executemany('INSERT INTO history VALUES(?,?,?,?)',[(i,'new-kind' if i%2 else 'battery',1000+i,json.dumps({'value':i,'password':'private'})) for i in range(1,12)])
        self.original=self.path.read_bytes()
    def tearDown(self):self.temp.cleanup()
    def call(self,op,**args):return json.loads(r.dispatch(self.ctx,dict(operation=op,**args)))
    def test_dynamic_discovery(self):
        with mock.patch.object(r,'services',return_value={}):
            self.assertIn('state/car',self.call('catalog')['topics'])
            self.ctx['STATE']['new_feed']={'new_field':42}
            (self.root/'nap_new_status.json').write_text('{"new_signal":7}')
            with sqlite3.connect(self.path) as db:db.execute('CREATE TABLE new_measurements(v REAL)')
            c=self.call('catalog');self.assertIn('state/new_feed',c['topics']);self.assertIn('diagnostic/nap_new_status',c['topics'])
            self.assertIn('new_measurements',[v['name'] for v in c['databases']['history']['tables']])
            self.assertEqual(self.call('read',topic='state/new_feed',path='/new_field')['data'],42)
    def test_paths_redaction_and_paging(self):
        self.ctx['STATE']['car']={'vEgo':12,'password':'secret','values':[1,2,3]}
        out=self.call('read',topic='state/car',limit=1);self.assertEqual(out['next_offset'],1)
        self.assertEqual(self.call('read',topic='state/car',path='/password')['data'],'[redacted]')
        self.assertEqual(self.call('read',topic='state/car',path='/values',offset=1,limit=1)['data'],[2])
        with self.assertRaises(ValueError):self.call('read',topic='state/_secret')
        self.assertEqual(out['freshness']['status'],'unknown')
    def test_complete_keyset_history(self):
        a=self.call('history',limit=3);ids=[v['id'] for v in a['records']];upper=a['through_id']
        with sqlite3.connect(self.path) as db:db.execute('INSERT INTO history VALUES(12,"new-kind",1012,"{}")')
        while a['next_after_id'] is not None:
            a=self.call('history',limit=3,after_id=a['next_after_id'],through_id=upper)
            ids.extend(v['id'] for v in a['records'])
        self.assertEqual(ids,list(range(1,12)))
        self.assertEqual(self.call('history',kind='new-kind')['records'][0]['payload']['password'],'[redacted]')
    def test_sql_features_paging_and_parameters(self):
        self.assertEqual(self.call('query',sql='SELECT count(*) FROM segment_telemetry')['rows'],[[0]])
        q=self.call('query',sql='SELECT kind,count(*) AS n FROM history WHERE created>? GROUP BY kind ORDER BY kind',parameters=[1000])
        self.assertEqual(sum(row[1] for row in q['rows']),11)
        q=self.call('query',sql='WITH x AS (SELECT id,json_extract(payload,\'$.value\') v FROM history) SELECT * FROM x ORDER BY id',limit=4,offset=4)
        self.assertEqual(q['rows'][0],[5,5]);self.assertEqual(q['next_offset'],8)
        self.assertEqual(self.path.read_bytes(),self.original)
    def test_sql_denies_writes_files_and_pragmas(self):
        attacks=['DELETE FROM history','WITH x AS (SELECT 1) DELETE FROM history','PRAGMA writable_schema=ON',
                 "ATTACH '/tmp/no' AS other",'SELECT 1; DELETE FROM history',"SELECT load_extension('/tmp/no')",
                 "SELECT readfile('/etc/passwd')",'SELECT * FROM pragma_table_info(\'history\')','SELECT randomblob(999999999)']
        for sql in attacks:
            with self.subTest(sql=sql),self.assertRaises((ValueError,sqlite3.Error,MemoryError)):self.call('query',sql=sql)
        self.assertEqual(self.path.read_bytes(),self.original)
    def test_query_resource_limits(self):
        start=time.monotonic()
        with self.assertRaises(sqlite3.Error):self.call('query',sql='WITH RECURSIVE x(n) AS (VALUES(1) UNION ALL SELECT n+1 FROM x WHERE n<100000000) SELECT sum(n) FROM x')
        self.assertLess(time.monotonic()-start,1.5)
        for params in [{'limit':0},{'offset':-1},{'database':'../../etc/passwd'}]:
            with self.assertRaises(ValueError):self.call('query',sql='SELECT 1',**params)
    def test_catalog_avoids_credentials_symlinks(self):
        (self.root/'oauth.sqlite').write_text('private')
        (self.root/'nap_token_status.json').write_text('private')
        (self.root/'nap_link.json').symlink_to('/etc/passwd')
        self.assertNotIn('oauth',r.databases(self.ctx));self.assertNotIn('nap_link',r.diagnostic_paths())
        self.assertNotIn('nap_token_status',r.diagnostic_paths())
    def test_busy_and_failure_release(self):
        r.SLOT.acquire()
        try:
            with self.assertRaises(ValueError):self.call('query',sql='SELECT 1')
        finally:r.SLOT.release()
        with self.assertRaises(ValueError):self.call('read',topic='state/missing')
        self.assertEqual(self.call('query',sql='SELECT 1')['rows'],[[1]])

if __name__=='__main__':unittest.main()
