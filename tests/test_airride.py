import math
import unittest
from server.airride import Controller, GROUPS, LIMIT, RESTORE, SETUP, frame

TOKEN='0123456789abcdef'

class AirRideTest(unittest.TestCase):
    def setUp(self):
        self.c=Controller()
        self.now=1.
        self.tick()
    def tick(self, speed=0., valid=True, reply=None, fresh=True, dt=.01):
        self.now+=dt
        can=[(0x155,b'\x00'*8,0)] if fresh else []
        if reply is not None:
            can.append((0x65B,bytes([len(reply)])+reply,0))
        return self.c.tick(self.now,speed,valid,can)
    def start(self,group='front'):
        return self.c.request({'action':'start','group':group,'token':TOKEN},self.now)
    def ack(self):
        p=self.c.pending
        return self.tick(reply=bytes([p[0]+0x40])+p[1:4])
    def setup(self,group='front'):
        self.start(group)
        self.assertEqual(self.tick(),[frame(SETUP[0])])
        self.assertEqual(self.ack(),[frame(SETUP[1])])
        return self.ack()
    def test_exact_frames_all_groups(self):
        for group,corners in GROUPS.items():
            self.setUp()
            frames=self.setup(group)
            for corner in corners:
                self.assertEqual(frames,[frame(bytes([0x31,1,3,0x34,corner,0]))])
                self.ack()
                frames=[]
                while not frames:
                    self.c.request({'action':'heartbeat','group':group,'token':TOKEN},self.now)
                    frames=self.tick(dt=.1)
    def test_speed_cutoff_has_cleanup_priority(self):
        self.setup()
        frames=self.tick(speed=LIMIT)
        self.assertEqual(frames,[frame(RESTORE[0])])
        self.assertIsNone(self.c.hold)
        self.assertFalse(self.c.status(self.now)['available'])
        with self.assertRaises(ValueError):self.start()
    def test_invalid_stale_speed_and_heartbeat(self):
        for options in ({'speed':math.nan},{'valid':False},{'fresh':False,'dt':.25},{'dt':.81}):
            self.setUp();self.setup()
            self.assertEqual(self.tick(**options),[frame(RESTORE[0])])
            self.assertIsNone(self.c.hold)
    def test_cleanup_sequence_ack_required(self):
        self.setup()
        self.c.request({'action':'restore','token':TOKEN},self.now)
        self.assertEqual(self.tick(),[frame(RESTORE[0])])
        for p in RESTORE[1:]:self.assertEqual(self.ack(),[frame(p)])
        self.ack()
        self.assertTrue(self.c.status(self.now)['factory_restored'])
    def test_timeout_never_reports_restored(self):
        self.setup();self.tick(dt=.61)
        self.assertTrue(self.c.fault)
        for _ in range(6):self.tick(dt=.61)
        self.assertFalse(self.c.status(self.now)['factory_restored'])
        self.assertFalse(self.c.status(self.now)['available'])
    def test_negative_response_stops(self):
        self.setup()
        self.assertEqual(self.tick(reply=bytes.fromhex('7f 31 22')),[frame(RESTORE[0])])
        self.assertTrue(self.c.fault)
    def test_delayed_start_and_heartbeat_cannot_restart(self):
        self.c.request({'action':'stop','token':TOKEN},self.now)
        with self.assertRaises(ValueError):self.start()
        with self.assertRaises(ValueError):self.c.request({'action':'heartbeat','group':'front','token':TOKEN},self.now)
    def test_rejection_reports_exact_request_and_never_claims_restored(self):
        self.setup()
        rejected=self.c.pending.hex(' ')
        self.tick(reply=bytes.fromhex('7f 31 aa'))
        self.assertIn(rejected,self.c.fault)
        self.assertIn('7f 31 aa',self.c.fault)
        self.assertFalse(self.c.status(self.now)['factory_restored'])
    def test_idle_startup_does_not_send_cleanup(self):
        self.assertEqual(self.tick(),[])
        self.assertIsNone(self.c.status(self.now)['factory_restored'])
        self.assertEqual(self.c.request({'action':'status'},self.now)['last_request'],None)
    def test_trace_retains_stop_timeout_and_late_reply(self):
        self.setup()
        self.c.request({'action':'stop','token':TOKEN},self.now)
        self.tick()
        self.tick(dt=.61)
        self.tick(reply=bytes.fromhex('71 02 03 34'))
        trace=self.c.status(self.now)['trace']
        self.assertTrue(any(e['event']=='timeout' and e['payload']=='31 02 03 34' for e in trace))
        self.assertTrue(any(e['event']=='rx' and e['payload']=='71 02 03 34' for e in trace))
        self.assertTrue(self.c.fault)
        self.assertFalse(self.c.status(self.now)['factory_restored'])
    def test_explicit_reset_allows_fresh_hold_without_actuation(self):
        self.c.fault='EAS response timeout'
        self.c.restored=False
        self.assertTrue(self.c.status(self.now)['reset_available'])
        self.c.request({'action':'reset'},self.now)
        self.assertFalse(self.c.fault)
        self.assertFalse(self.c.status(self.now)['factory_restored'])
        self.assertEqual(self.tick(),[])
        self.assertTrue(self.start()['available'])
    def test_reset_preserves_speed_and_command_gates(self):
        for mode in ('speed','stale','hold','pending','queue'):
            self.setUp();self.c.fault='failure'
            if mode=='speed':self.c.speed=LIMIT
            elif mode=='stale':self.now+=1
            elif mode=='hold':self.c.hold='front'
            elif mode=='pending':self.c.pending=bytes.fromhex('31 02 03 34')
            elif mode=='queue':self.c.queue.append(bytes.fromhex('10 01'))
            with self.assertRaises(ValueError):self.c.request({'action':'reset'},self.now)
            self.assertEqual(self.c.fault,'failure')
    def test_finger_release_stops_vent_without_factory_restore(self):
        self.setup()
        self.c.request({'action':'stop','token':TOKEN},self.now)
        self.assertEqual(self.tick(),[frame(RESTORE[0])])
        self.assertEqual(self.ack(),[])
        self.assertTrue(self.c.manual)
        self.assertTrue(self.c.service_ready)
        self.assertIsNone(self.c.hold)
        self.assertFalse(self.c.status(self.now)['factory_restored'])
        self.assertEqual(self.tick(dt=.1),[])
    def test_speed_cutoff_still_restores_after_finger_release(self):
        self.setup()
        self.c.request({'action':'stop','token':TOKEN},self.now)
        self.tick();self.ack()
        self.assertEqual(self.tick(speed=LIMIT),[frame(RESTORE[0])])
        for p in RESTORE[1:]:self.assertEqual(self.ack(),[frame(p)])
        self.ack()
        self.assertTrue(self.c.status(self.now)['factory_restored'])
    def test_next_hold_reuses_acknowledged_service_state(self):
        self.setup();self.c.request({'action':'stop','token':TOKEN},self.now)
        self.tick();self.ack()
        self.c.request({'action':'start','group':'rear','token':'new-hold-0123456789'},self.now)
        self.assertEqual(self.tick(),[frame(SETUP[0])])
        self.assertEqual(self.ack(),[frame(bytes.fromhex('31 01 03 34 02 00'))])
    def test_reset_does_not_erase_unconfirmed_manual_safety_gate(self):
        self.c.fault='failed cleanup';self.c.restored=False
        self.c.request({'action':'reset'},self.now)
        self.assertEqual(self.tick(speed=LIMIT),[frame(RESTORE[0])])
    def test_other_clients_do_not_steal_hold(self):
        self.start()
        self.c.request({'action':'stop','token':'another-client-123'},self.now)
        self.assertEqual(self.c.hold,'front')
        with self.assertRaises(ValueError):self.c.request({'action':'start','group':'rear','token':'another-client-123'},self.now)
    def test_missing_speed_blocks(self):
        self.c=Controller()
        with self.assertRaises(ValueError):self.start()

if __name__=='__main__':unittest.main()
