import ast,importlib.util,io,json,sys,tempfile,threading,time,types,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if 'server' not in sys.modules:
 package=types.ModuleType('server');package.__path__=[str(ROOT)];sys.modules['server']=package
from server import sentry_common as common
from server.nap_sentry import Sentry
from server.sentry_page import PAGE

class Handler:
 def __init__(self,path,token='test-pin',command='GET',body=None):
  self.path=path;self.command=command;raw=json.dumps(body).encode()if body is not None else b''
  self.headers={'Authorization':'Bearer '+token,'Host':'nap.local','Origin':'http://nap.local','Content-Type':'application/json','Content-Length':str(len(raw))}
  self.rfile=io.BytesIO(raw);self.wfile=io.BytesIO();self.code=None;self.response_headers={}
 def send_json(self,data,code=200):self.json=data;self.code=code
 def send_response(self,code):self.code=code
 def send_header(self,k,v):self.response_headers[k]=v
 def end_headers(self):pass
class Tests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
  self.store=common.Store(self.temp.name);self.ident='a'*32
  self.store.metadata({'id':self.ident,'timestamp':time.time()-90000,'state':'ready','protected':False})
  self.store.path(self.ident,'.mp4').write_bytes(b'video-test-data')
 def test_keep_survives_age_cleanup_and_unkeep_restores_cleanup(self):
  self.store.set_protected(self.ident,True)
  self.assertIsNone(self.store.events()[0]['expires_at'])
  self.store.prune({'storage_mb':64,'reserve_mb':0});self.assertTrue(self.store.path(self.ident,'.mp4').exists())
  self.store.set_protected(self.ident,False)
  self.store.prune({'storage_mb':64,'reserve_mb':0});self.assertFalse(self.store.path(self.ident,'.mp4').exists())
 def test_strict_keep_and_active_recording(self):
  for value in ('false',1,None):
   with self.assertRaises(ValueError):self.store.set_protected(self.ident,value)
  for state in ('recording','finalizing'):
   self.store.metadata({'id':self.ident,'state':state})
   with self.assertRaises(ValueError):self.store.set_protected(self.ident,True)
 def test_keep_rejects_missing_or_invalid_event(self):
  for ident in ('../file','b'*32):
   with self.assertRaises(ValueError):self.store.set_protected(ident,True)
 def coordinator(self):
  s=Sentry.__new__(Sentry);s.token='test-pin';s.store=self.store;s.lock=threading.RLock();return s
 def test_download_preserves_auth_and_camera_bytes(self):
  s=self.coordinator();h=Handler('/api/sentry/media/'+self.ident+'/video-road',token='wrong')
  s.handle(h);self.assertEqual(h.code,401);self.assertFalse(h.wfile.getvalue())
  h=Handler('/api/sentry/media/'+self.ident+'/video-road');s.handle(h)
  self.assertEqual(h.code,200);self.assertEqual(h.wfile.getvalue(),b'video-test-data');self.assertEqual(h.response_headers['Content-Type'],'video/mp4')
 def test_keep_endpoint(self):
  s=self.coordinator();h=Handler('/api/sentry/protect',command='POST',body={'id':self.ident,'protected':True})
  s.handle(h);self.assertEqual(h.code,200);self.assertTrue(self.store.events()[0]['protected'])
  h=Handler('/api/sentry/protect',command='POST',body={'id':self.ident,'protected':'false'})
  s.handle(h);self.assertEqual(h.code,409);self.assertTrue(self.store.events()[0]['protected'])
 def test_removed_airride_routes_and_assistant_navigation(self):
  s=(ROOT/'server_v21.py').read_text();self.assertNotIn('airride',s.lower());self.assertIn('<a href="/assistant">Assistant</a>',s)
  tree=ast.parse(s);handler=next(n for n in tree.body if isinstance(n,ast.ClassDef)and n.name=='Handler')
  get=next(n for n in handler.body if isinstance(n,ast.FunctionDef)and n.name=='do_GET')
  self.assertIn('handle_assistant',ast.unparse(get))
  self.assertIn('Keep on comma',PAGE);self.assertIn('Download photo',PAGE);self.assertIn('addDownloads(buttons,event)',PAGE)
if __name__=='__main__':unittest.main()
