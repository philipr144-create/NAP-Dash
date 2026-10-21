import ast
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace as NS
from server import sentry_common as c
from server.sentry_bump import BumpDetector
from server.sentry_buffer import Packet,VideoRing
from server.sentry_worker import Worker
from test_sentry import Base,Handler
from server import nap_sentry as n

class BumpTests(unittest.TestCase):
    def settled(self):
        d=BumpDetector()
        for i in range(400):
            t=1_000_000_000+i*10_000_000
            self.assertFalse(d.update((0,0,9.81),t,t,.35))
        self.assertTrue(d.ready)
        return d,t
    def test_solid_jolt_and_cooldown(self):
        d,t=self.settled();t+=10_000_000
        self.assertFalse(d.update((4,0,9.81),t,t,.35));t+=10_000_000
        self.assertTrue(d.update((4,0,9.81),t,t,.35));t+=10_000_000
        self.assertFalse(d.update((10,0,9.81),t,t,.35))
    def test_single_hard_impact(self):
        d,t=self.settled();t+=10_000_000
        self.assertTrue(d.update((8,0,9.81),t,t,.35))
    def test_gentle_movement_and_static_gravity(self):
        d,t=self.settled()
        for i in range(100):
            t+=10_000_000
            self.assertFalse(d.update((.15*(-1)**i,.1,9.81),t,t,.35))
    def test_startup_and_sensor_gap_rewarm(self):
        d=BumpDetector()
        self.assertFalse(d.update((8,0,9.81),1_000_000_000,1_000_000_000,.35))
        d,t=self.settled();t+=1_000_000_000
        self.assertFalse(d.update((8,0,9.81),t,t,.35));self.assertFalse(d.ready)
    def test_stale_future_duplicate_and_invalid(self):
        d,t=self.settled()
        for ts,now in ((t,t+300_000_000),(t+10_000_000,t),(t,t)):
            self.assertFalse(d.update((10,0,9.81),ts,now,.35))
        self.assertFalse(d.update((float('nan'),0,9.81),t+10_000_000,t+10_000_000,.35))
        self.assertFalse(d.ready)
    def test_missing_sensor_reported(self):
        d,t=self.settled();self.assertEqual(d.status(t+300_000_000,True)['state'],'unavailable')
        self.assertEqual(d.status(t,False)['state'],'off')

class RingTests(unittest.TestCase):
    def packet(self,second,key=False,size=100):return Packet(int(second*1e9),key,b'x'*size,640,360)
    def test_decodable_ten_seconds_with_keyframe_margin(self):
        r=VideoRing()
        for i in range(300):r.append(self.packet(i/10,i%10==0),10)
        data=r.snapshot(30_000_000_000,10)
        self.assertTrue(data[0].key);self.assertEqual(data[0].timestamp,20_000_000_000)
        self.assertLessEqual(r.seconds(),13)
    def test_memory_and_no_keyframe_bound(self):
        r=VideoRing();r.MAX_BYTES=1000
        for i in range(100):r.append(self.packet(i/100,True,300),20)
        self.assertLessEqual(r.bytes,1000)
        r.clear()
        for i in range(100):r.append(self.packet(i/100,False,300),20)
        self.assertEqual(r.bytes,0)
    def test_disable_flushes_and_old_packets_ignored(self):
        r=VideoRing();r.append(self.packet(10,True),10);r.append(self.packet(9,True),10)
        self.assertEqual(len(r.items),1);r.append(self.packet(11,True),0);self.assertEqual(r.bytes,0)

class BufferedWorkerTests(Base):
    def test_trigger_flushes_three_pre_event_tracks_once(self):
        w=Worker();w.buffer_enabled=True;w.cfg['prebuffer_seconds']=10
        for camera,r in w.rings.items():
            for i in range(110):r.append(Packet(int((90+i/10)*1e9),i%10==0,b'h264',640,360),10)
        with patch('server.sentry_worker.time.monotonic',return_value=100):w.begin('bump')
        self.assertEqual(set(w.files),set(c.CAMERAS))
        self.assertEqual(w.event['triggers'],['bump'])
        for camera in c.CAMERAS:
            self.assertEqual(w.event['cameras'][camera]['pre_event_s'],10)
            frames=w.camera_frames[camera]
            w.write_packet(Packet(99_900_000_000,False,b'same',640,360),camera)
            self.assertEqual(w.camera_frames[camera],frames)
        for f in w.files.values():f.close()
    def test_bump_during_motion_is_labeled_and_extends_bounded_tail(self):
        w=Worker()
        with patch('server.sentry_worker.time.monotonic',return_value=100):w.begin('motion')
        with patch('server.sentry_worker.time.monotonic',return_value=119):w.begin('bump')
        self.assertEqual(w.event['triggers'],['motion','bump']);self.assertEqual(w.until,149)
        for f in w.files.values():f.close()
    def test_bump_bypasses_visual_event_cooldown(self):
        w=Worker();w.cooldown=time.monotonic()+10;w.begin('bump');self.assertIsNotNone(w.event)
        for f in w.files.values():f.close()
    def test_finalization_keeps_monitor_loop_available(self):
        w=Worker();w.begin('manual');gate=threading.Event()
        def finalize(*args):gate.wait(2)
        try:
            with patch.object(w.store,'finalize',side_effect=finalize):
                start=time.monotonic();w.finish();self.assertLess(time.monotonic()-start,.5)
                self.assertIsNone(w.event);self.assertFalse(w.finalizing.done())
                w.bump_enabled=True
                event=NS(timestamp=1,acceleration=NS(v=[0,0,9.81]),which=lambda:'acceleration')
                with patch.object(w.bump,'update',return_value=False) as update:
                    w.sensor(NS(valid=True,accelerometer=event),1);update.assert_called_once()
        finally:
            gate.set()
            if w.finalizer:w.finalizer.shutdown(wait=True)
    def test_protection_survives_background_finalization(self):
        store=c.Store();e=store.create('manual',None,c.DEFAULTS)
        store.path(e['id'],'.partial.h264').write_bytes(b'raw')
        def remux(args,**kwargs):
            Path(args[-1]).write_bytes(b'mp4')
            current=c.read(store.path(e['id'],'.json'));current['protected']=True;store.metadata(current)
        with patch.object(c.subprocess,'run',side_effect=remux):store.finalize(e,10)
        self.assertTrue(store.events()[0]['protected'])
    def test_protection_survives_another_trigger_in_active_event(self):
        w=Worker();w.begin('manual')
        d=c.read(w.store.path(w.event['id'],'.json'));d['protected']=True;w.store.metadata(d)
        w.begin('bump');self.assertTrue(c.read(w.store.path(w.event['id'],'.json'))['protected'])
        for f in w.files.values():f.close()
    def test_fresh_sensor_only_when_enabled(self):
        w=Worker();event=NS(timestamp=1,acceleration=NS(v=[0,0,9.81]),which=lambda:'acceleration')
        msg=NS(valid=True,accelerometer=event)
        with patch.object(w.bump,'update',return_value=True) as update,patch.object(w,'begin') as begin:
            w.sensor(msg,1);update.assert_not_called()
            w.bump_enabled=True;w.sensor(msg,1);begin.assert_called_once_with('bump')
            msg.valid=False;w.sensor(msg,1);self.assertEqual(update.call_count,1)

class BumpPolicyTests(Base):
    def test_separate_parked_buffer_and_sensor_leases(self):
        now=time.monotonic();d={'until':now+3,'deadline':now+60,'armed':True,'live':False,'config':dict(c.DEFAULTS)}
        c.atomic(c.LEASE,d);self.assertTrue(c.enabled(resource='sensors'));self.assertTrue(c.enabled(resource='encoder'))
        self.assertFalse(c.enabled(True,'sensors'))
        d['armed']=False;c.atomic(c.LEASE,d);self.assertFalse(c.enabled(resource='sensors'));self.assertFalse(c.enabled(resource='encoder'))
        d['armed']=True;d['config'].update(bump_detection=0,prebuffer_seconds=0);c.atomic(c.LEASE,d)
        self.assertFalse(c.enabled(resource='sensors'));self.assertFalse(c.enabled(resource='encoder'))
    def test_toggles_persist_without_changing_cabin_or_session(self):
        obj=n.Sentry();obj.armed_until=1000
        for op in ('bump_detection','prebuffer'):
            for enabled in (False,True):
                h=Handler(op,body={'enabled':enabled});obj.handle(h);self.assertEqual(h.result[0],200)
                cfg=n.Sentry().cfg;self.assertEqual(cfg['cabin_motion'],0)
                self.assertEqual(cfg['prebuffer_seconds' if op=='prebuffer' else op],10*enabled if op=='prebuffer' else int(enabled))
                self.assertEqual(obj.armed_until,1000)
    def test_new_settings_bounds(self):
        for changes in ({'prebuffer_seconds':21},{'bump_threshold_g':.01},{'bump_detection':2}):
            with self.assertRaises(ValueError):c.validate_config(changes)
    def test_sensor_process_preserves_driving_and_requires_valid_lease(self):
        module=ast.parse(Path('/data/openpilot/system/manager/process_config.py').read_text())
        fn=next(x for x in module.body if isinstance(x,ast.FunctionDef) and x.name=='sentry_sensors')
        # Annotations refer to imported types; only the actual predicate is exercised.
        for a in fn.args.args:a.annotation=None
        fn.returns=None;ns={'only_onroad':lambda started,*a:started,'sentry_enabled':c.enabled}
        exec(compile(ast.Module(body=[fn],type_ignores=[]),'predicate','exec'),ns)
        self.assertTrue(ns['sentry_sensors'](True,None,None));self.assertFalse(ns['sentry_sensors'](False,None,None))
        now=time.monotonic();c.atomic(c.LEASE,{'until':now+3,'deadline':now+30,'armed':True,'config':dict(c.DEFAULTS)})
        self.assertTrue(ns['sentry_sensors'](False,None,None))
