"""HTTP and serialization checks use the real Comma runtime without CAN output."""
import json
import threading
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from http.server import ThreadingHTTPServer

from server.airride import frame
from server import server_v21 as web
from openpilot.selfdrive.pandad import can_list_to_can_capnp, can_capnp_to_list

class Integration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.http=ThreadingHTTPServer(('127.0.0.1',0),web.Handler)
        cls.worker=threading.Thread(target=cls.http.serve_forever,daemon=True)
        cls.worker.start()
        cls.base='http://127.0.0.1:'+str(cls.http.server_port)
    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown();cls.http.server_close();cls.worker.join()
    def test_page_dashboard_and_navigation_routes(self):
        for path,expected in (('/airride',b'Pancake'),('/',b'/airride'),('/api/nav/status',b'route_state')):
            with urlopen(self.base+path) as r:self.assertIn(expected,r.read())
    def test_api_and_origin_guard(self):
        d={'action':'stop'}
        with patch.object(web,'airride_request',return_value={'available':False}) as mock:
            req=Request(self.base+'/api/airride',json.dumps(d).encode(),headers={'Content-Type':'application/json'})
            with urlopen(req) as r:self.assertEqual(r.status,200)
            mock.assert_called_once_with(d)
            req.add_header('Origin','https://untrusted.example')
            with self.assertRaises(HTTPError) as err:urlopen(req)
            self.assertEqual(err.exception.code,403)
    def test_offline_status_is_safe(self):
        with patch.object(web,'airride_request',return_value={'available':False,'error':'offline'}):
            with urlopen(self.base+'/api/airride/status') as r:self.assertFalse(json.load(r)['available'])
    def test_real_sendcan_serializer_roundtrip(self):
        frames=[frame(bytes.fromhex('31 01 03 34 00 00')),frame(bytes.fromhex('31 02 03 34'))]
        serialized=can_list_to_can_capnp(frames,msgtype='sendcan',valid=True)
        decoded=can_capnp_to_list([serialized],msgtype='sendcan')
        self.assertEqual([f for _,fs in decoded for f in fs],frames)

if __name__=='__main__':unittest.main()
