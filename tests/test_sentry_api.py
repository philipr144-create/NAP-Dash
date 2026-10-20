import importlib.util
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
spec=importlib.util.spec_from_file_location('sentry',str(Path(__file__).resolve().parents[1] / 'nap_sentry.py'))
s=importlib.util.module_from_spec(spec);spec.loader.exec_module(s)
class Handler:
 def __init__(self,path,pin=None,body=None):
  self.path='/api/sentry/'+path;self.command='POST' if body is not None else 'GET'
  raw=json.dumps(body).encode() if body is not None else b''
  self.headers={'Content-Length':str(len(raw)),'Content-Type':'application/json','Host':'localhost'}
  if pin is not None:self.headers['Authorization']='Bearer '+pin
  self.rfile=io.BytesIO(raw)
 def send_json(self,body,status=200):self.result=(status,body)
class Tests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();s.LEASE=Path(self.tmp.name)/'lease';s.TOKEN=Path(self.tmp.name)/'pin';self.obj=s.Sentry()
 def tearDown(self):self.tmp.cleanup()
 def test_auth(self):
  for pin,expected in [(None,401),('123456',401),('000000',200)]:
   h=Handler('status',pin);self.obj.handle(h);self.assertEqual(h.result[0],expected)
 def test_session_limit(self):
  self.obj.safe=True;self.obj.cooldown=0
  h=Handler('start','000000',{});self.obj.handle(h);self.assertEqual(h.result[0],200)
  deadline=self.obj.deadline
  self.obj.handle(Handler('start','000000',{}));self.obj.handle(Handler('ping','000000',{}));self.assertEqual(self.obj.deadline,deadline)
  self.obj.handle(Handler('stop','000000',{}));self.assertEqual(self.obj.deadline,0)
 def test_three_camera_proxy(self):
  self.obj.safe=True
  def proxy(req,timeout):
   body=json.loads(req.data);self.assertEqual(body['cameras'],['road','wideRoad','driver']);self.assertEqual(body['bridge_services_in'],[]);self.assertEqual(body['bridge_services_out'],[])
   return io.BytesIO(b'{"type":"answer","sdp":"test-answer"}')
  with patch.object(s,'enabled',return_value=True),patch.object(s,'urlopen',side_effect=proxy):
   h=Handler('offer','000000',{'sdp':'test-offer','cameras':['bad'],'bridge_services_in':['carControl']});self.obj.handle(h)
  self.assertEqual(h.result,(200,{'type':'answer','sdp':'test-answer'}))
 def test_page(self):
  self.assertIn("localStorage.getItem(storageKey)",s.PAGE)
  self.assertNotIn('000000',s.PAGE)
  for cam in s.CAMERAS:self.assertIn('id="video-'+cam+'"',s.PAGE)
if __name__=='__main__':unittest.main()
