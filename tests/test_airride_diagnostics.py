import time
import unittest
from unittest.mock import patch
from server.airride_diagnostics import DiagnosticReader

class Diagnostics(unittest.TestCase):
    def test_only_read_packets_and_flow_control_allowed(self):
        d=DiagnosticReader()
        for packet in ('03 19 02 ff 00 00 00 00','30 00 00 00 00 00 00 00'):
            d.can_send(0x64B,bytes.fromhex(packet),0)
        for packet in ('04 14 ff ff ff 00 00 00','06 31 01 03 34 00 00 00'):
            with self.assertRaises(ValueError):d.can_send(0x64B,bytes.fromhex(packet),0)
        with self.assertRaises(ValueError):d.can_send(0x64B,bytes.fromhex('03 19 02 ff 00 00 00 00'),1)
    def test_read_and_session_exit_without_clear(self):
        class Client:
            def __init__(self,*a,**kw):self.sessions=[]
            def diagnostic_session_control(self,s):self.sessions.append(int(s))
            def _uds_request(self,service,subfunction,data):
                assert int(service)==0x19 and subfunction==2 and data==b'\xff'
                return bytes.fromhex('ff 12 34 56 89')
        d=DiagnosticReader()
        with patch('server.airride_diagnostics.UdsClient',Client):d._read()
        self.assertEqual(d.result['records'],[{'code':'123456','status':'89'}])
        self.assertFalse(d.result['cleared'])
    def test_real_transport_reassembles_multiframe_dtc_reply(self):
        d=DiagnosticReader();d.start();rx=[];seen=[];end=time.monotonic()+3
        while time.monotonic()<end and d.busy:
            out=d.update(rx,True);rx=[]
            for address,data,bus in out:
                seen.append(data.hex(' '))
                if data[:2]==bytes.fromhex('02 10'):
                    rx.append((0x65B,bytes([2,0x50,data[2],0,0,0,0,0]),0))
                elif data[:4]==bytes.fromhex('03 19 02 ff'):
                    rx.append((0x65B,bytes.fromhex('10 0b 59 02 ff 12 34 56'),0))
                elif data[0]==0x30:
                    rx.append((0x65B,bytes.fromhex('21 89 ab cd ef 01 00 00'),0))
            time.sleep(.005)
        self.assertFalse(d.busy)
        self.assertNotIn('error',d.result)
        self.assertEqual(d.result['records'],[{'code':'123456','status':'89'},{'code':'ABCDEF','status':'01'}])
        self.assertIn('30 00 00 00 00 00 00 00',seen)
    def test_cancel_drops_transmission_and_aborts_reader(self):
        d=DiagnosticReader();d.busy=True
        d.can_send(0x64B,bytes.fromhex('03 19 02 ff 00 00 00 00'),0)
        self.assertEqual(d.update([],False),[])
        with self.assertRaises(RuntimeError):d.can_recv()

if __name__=='__main__':unittest.main()
