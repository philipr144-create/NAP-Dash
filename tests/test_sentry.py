import importlib.util
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
spec=importlib.util.spec_from_file_location('sentry', str(Path(__file__).resolve().parents[1] / 'nap_sentry.py'))
s=importlib.util.module_from_spec(spec); spec.loader.exec_module(s)
class SM(dict): pass
class Tests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(); s.LEASE=Path(self.tmp.name)/'lease'; s.TOKEN=Path(self.tmp.name)/'token'
  self.sm=SM(deviceState=NS(started=False,thermalStatus='green',maxTempC=40), pandaStates=[NS(ignitionLine=False,ignitionCan=False,pandaType='tres',harnessStatus='normal')],peripheralState=NS(pandaType='tres',voltage=12500))
  self.sm.valid={k:True for k in self.sm}; self.sm.recv_time={k:100 for k in self.sm}
 def tearDown(self): self.tmp.cleanup()
 def test_safety(self):
  self.assertEqual(s.safety(self.sm,101),'')
  self.sm['pandaStates'][0].ignitionCan=True; self.assertTrue(s.safety(self.sm,101))
  self.sm['pandaStates'][0].ignitionCan=False
  for voltage in (0,4950,11999,16001):
   self.sm['peripheralState'].voltage=voltage; self.assertTrue(s.safety(self.sm,101))
  self.sm['peripheralState'].voltage=12500
  self.assertTrue(s.safety(self.sm,104))
  self.sm['deviceState'].maxTempC=70; self.assertTrue(s.safety(self.sm,101))
 def test_bench_supply(self):
  self.sm['peripheralState'].voltage=4951
  self.assertTrue(s.safety(self.sm,101))
  self.sm['pandaStates'][0].harnessStatus='notConnected'
  for voltage in (4750,4951,5000,5250):
   self.sm['peripheralState'].voltage=voltage; self.assertEqual(s.safety(self.sm,101),'')
  for voltage in (0,4749,5251,10000,11999):
   self.sm['peripheralState'].voltage=voltage; self.assertTrue(s.safety(self.sm,101))
  self.sm['peripheralState'].voltage=5000
  self.sm['pandaStates'][0].ignitionCan=True; self.assertTrue(s.safety(self.sm,101))
  self.sm['pandaStates'][0].ignitionCan=False
  self.sm['deviceState'].thermalStatus='yellow'; self.assertTrue(s.safety(self.sm,101))
  self.sm['deviceState'].thermalStatus='green'; self.assertTrue(s.safety(self.sm,104))
 def test_lease(self):
  self.assertFalse(s.enabled()); now=time.monotonic()
  s.LEASE.write_text(json.dumps({'until':now+3,'deadline':now+100}))
  self.assertTrue(s.enabled()); self.assertFalse(s.enabled(True))
  s.LEASE.write_text(json.dumps({'until':now-1,'deadline':now+100})); self.assertFalse(s.enabled())
  s.LEASE.write_text('bad'); self.assertFalse(s.enabled())
 def test_reset_and_key(self):
  obj=s.Sentry(); self.assertEqual(obj.token,'000000')
  self.assertEqual(s.TOKEN.stat().st_mode & 0o777,0o600)
  obj.deadline=time.monotonic()+100; obj.off(); self.assertEqual(obj.deadline,0)
if __name__=='__main__': unittest.main()
