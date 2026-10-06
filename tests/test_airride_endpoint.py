import json
import socket
import tempfile
import time
import unittest
from unittest.mock import patch
from server import airride

class EndpointSafety(unittest.TestCase):
    def test_startup_and_status_never_send_commands(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(airride,'SOCKET',directory+'/controller'), patch.object(airride,'RECOVERY',directory+'/recovery'):
            ep=airride.Endpoint()
            try:
                self.assertEqual(ep.update(0,True,[(0x155,b'\x00'*8,0)],active=False),[])
                self.assertFalse(ep.controller.fault)
                self.assertFalse(ep.controller.status(time.monotonic())['available'])
                self.assertEqual(list(ep.controller.queue),[])
                self.assertIsNone(ep.controller.status(time.monotonic())['factory_restored'])
                self.assertEqual(ep.update(0,True,[(0x155,b'\x00'*8,0)],active=True),[])
                self.assertEqual(ep.controller.request({'action':'status'},time.monotonic())['last_request'],None)
            finally:ep.sock.close()
    def test_reload_preserves_uncertainty_without_startup_actuation(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(airride,'SOCKET',directory+'/controller'), patch.object(airride,'RECOVERY',directory+'/recovery'):
            with open(airride.RECOVERY,'w') as f:json.dump({'vent_stopped':True},f)
            ep=airride.Endpoint()
            try:
                self.assertEqual(ep.update(0,True,[(0x155,b'\x00'*8,0)],active=True),[])
                self.assertFalse(ep.controller.restored)
                self.assertTrue(ep.controller.vent_stopped)
                self.assertFalse(ep.controller.status(time.monotonic())['available'])
                self.assertTrue(ep.controller.fault)
            finally:ep.sock.close()
    def test_stale_queued_start_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(airride,'SOCKET',directory+'/controller'), patch.object(airride,'RECOVERY',directory+'/recovery'):
            ep=airride.Endpoint()
            client=socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM)
            try:
                client.bind(directory+'/client');client.settimeout(.2)
                client.sendto(json.dumps({'action':'start','group':'front','token':'0123456789abcdef','_received_at':time.monotonic()-1}).encode(),airride.SOCKET)
                ep.update(0,True,[],active=False)
                self.assertIn('expired',json.loads(client.recv(4096))['error'])
                self.assertIsNone(ep.controller.hold)
            finally:client.close();ep.sock.close()

if __name__=='__main__':unittest.main()
