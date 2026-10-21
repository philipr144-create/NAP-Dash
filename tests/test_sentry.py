import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
import numpy as np
from server import sentry_common as c
from server import nap_sentry as n
from server.sentry_worker import Detector,Worker

class Handler:
    def __init__(self,op,pin='000000',body=None):
        self.path='/api/sentry/'+op;self.command='GET' if body is None else 'POST'
        raw=b'' if body is None else json.dumps(body).encode()
        self.rfile=io.BytesIO(raw);self.headers={'Content-Type':'application/json','Content-Length':str(len(raw)),'Host':'local'}
        if pin is not None:self.headers['Authorization']='Bearer '+pin
    def send_json(self,d,status=200):self.result=status,d

class Base(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();r=Path(self.tmp.name)
        self.disk=patch.object(c.shutil,'disk_usage',return_value=NS(free=10*1073741824));self.disk.start()
        self.old={k:getattr(c,k) for k in ('LEASE','RECORD_LEASE','WORKER_STATUS','ROOT','CONFIG','POLICY')};self.token=n.TOKEN
        for k in self.old:setattr(c,k,r/k)
        n.TOKEN=r/'pin'
    def tearDown(self):
        for k,v in self.old.items():setattr(c,k,v)
        n.TOKEN=self.token;self.disk.stop();self.tmp.cleanup()

class SafetyTests(Base):
    def setUp(self):
        super().setUp()
        class SM(dict):pass
        self.sm=SM(deviceState=NS(started=False,thermalStatus='green',maxTempC=40),pandaStates=[NS(pandaType='tres',ignitionLine=False,ignitionCan=False,harnessStatus='notConnected')],peripheralState=NS(pandaType='tres',voltage=5000))
        self.sm.valid={k:True for k in self.sm};self.sm.recv_time={k:100 for k in self.sm}
    def test_safety_guards(self):
        self.assertEqual(n.safety(self.sm,101),'')
        for field,value in [('started',True),('thermalStatus','yellow'),('maxTempC',c.DEFAULTS['suspend_c']),('maxTempC',float('nan'))]:
            old=getattr(self.sm['deviceState'],field);setattr(self.sm['deviceState'],field,value);self.assertTrue(n.safety(self.sm,101));setattr(self.sm['deviceState'],field,old)
        self.assertTrue(n.safety(self.sm,104))
        self.sm['pandaStates'][0].ignitionCan=True;self.assertTrue(n.safety(self.sm,101))
    def test_power_guards(self):
        for v in (4750,5250,12000,16000):self.sm['peripheralState'].voltage=v;self.assertEqual(n.safety(self.sm,101),'')
        for v in (4749,5251,11000,16001):self.sm['peripheralState'].voltage=v;self.assertTrue(n.safety(self.sm,101))
        self.sm['pandaStates'][0].harnessStatus='normal';self.sm['peripheralState'].voltage=5000;self.assertTrue(n.safety(self.sm,101))
    def test_separate_leases(self):
        now=time.monotonic();c.atomic(c.LEASE,{'until':now+3,'deadline':now+300,'armed':True,'live':False})
        self.assertTrue(c.enabled());self.assertFalse(c.enabled(resource='encoder'));self.assertFalse(c.enabled(resource='live'));self.assertFalse(c.enabled(True))
        c.atomic(c.RECORD_LEASE,{'until':now+3});self.assertTrue(c.enabled(resource='encoder'))
        c.atomic(c.RECORD_LEASE,{'until':'corrupt'});self.assertFalse(c.enabled(resource='encoder'))
        c.atomic(c.LEASE,{'until':now-1,'deadline':now+300,'live':True});self.assertFalse(c.enabled(resource='encoder'))
    def test_parked_thermal_ceiling(self):
        self.assertEqual(c.DEFAULTS['warn_c'],70)
        self.assertEqual(c.DEFAULTS['suspend_c'],75)
        self.sm['deviceState'].maxTempC=74.9
        self.assertEqual(n.safety(self.sm,101),'')
        self.sm['deviceState'].maxTempC=75
        self.assertEqual(n.safety(self.sm,101),'Device too warm')
        for status in ('yellow','red','danger'):
            self.sm['deviceState'].maxTempC=40
            self.sm['deviceState'].thermalStatus=status
            self.assertEqual(n.safety(self.sm,101),'Device too warm')
        with self.assertRaises(ValueError):c.validate_config({'suspend_c':76})
    def test_settings_bounds(self):
        for changes in ({'suspend_c':100},{'fps':60},{'storage_mb':99999},{'confirmation_frames':1},{'warn_c':60,'suspend_c':55},{'width':641},{'clip_seconds':60,'max_clip_seconds':30},{'fps':True}):
            with self.assertRaises(ValueError):c.validate_config(changes)
        self.assertEqual(c.validate_config({'analysis_fps':1})['analysis_fps'],1)

class MotionTests(unittest.TestCase):
    def warmed(self):
        d=Detector();a=np.full((90,160),80,dtype=np.uint8)
        for _ in range(8):self.assertFalse(d.update(a,c.DEFAULTS)[0])
        return d,a
    def test_local_motion_confirmation(self):
        d,a=self.warmed();hits=[]
        for x in (0,25,50,75):
            b=a.copy();b[20:55,x:x+25]=180;hits.append(d.update(b,c.DEFAULTS)[0])
        self.assertTrue(any(hits[2:]))
    def test_global_light_and_noise(self):
        d,a=self.warmed();hit,_,global_change=d.update(a+50,c.DEFAULTS)
        self.assertFalse(hit);self.assertTrue(global_change)
        rng=np.random.default_rng(4)
        for _ in range(50):
            b=a.copy();mask=rng.random(a.shape)<.01;b[mask]=200
            self.assertFalse(d.update(b,c.DEFAULTS)[0])
    def test_small_dark_moving_object(self):
        d=Detector();a=np.full((90,160),8,dtype=np.uint8)
        for _ in range(8):d.update(a,c.DEFAULTS)
        hits=[]
        for x in (20,24,28,32,36):
            b=a.copy();b[40:50,x:x+18]=35;hits.append(d.update(b,c.DEFAULTS)[0])
        self.assertTrue(any(hits))
    def test_dark_sensor_noise_does_not_trigger(self):
        d=Detector();rng=np.random.default_rng(15)
        for _ in range(80):
            a=np.clip(rng.normal(8,2,(90,160)),0,255).astype(np.uint8)
            self.assertFalse(d.update(a,c.DEFAULTS)[0])
    def test_small_global_drift(self):
        d,a=self.warmed()
        for k in range(20):self.assertFalse(d.update(a+k,c.DEFAULTS)[0])

class ApiTests(Base):
    def setUp(self):super().setUp();self.obj=n.Sentry();self.obj.safe=True;self.obj.cooldown=0
    def call(self,op,body=None,pin='000000'):
        h=Handler(op,pin,body);self.obj.handle(h);return h.result
    def test_auth_including_evidence(self):
        for op in ('status','events','media/'+'a'*32+'/video','media/'+'a'*32+'/thumbnail'):
            self.assertEqual(self.call(op,pin=None)[0],401)
        self.assertEqual(self.call('status',pin='999999')[0],401)
        self.assertEqual(self.call('status')[0],200)
        self.assertEqual(n.TOKEN.stat().st_mode&0o777,0o600)
    def test_armed_live_independence(self):
        self.assertEqual(self.call('arm',{})[0],200);armed=self.obj.armed_until
        self.assertEqual(self.call('start',{})[0],200);deadline=self.obj.deadline
        self.call('start',{});self.call('ping',{});self.assertEqual(self.obj.deadline,deadline)
        self.call('stop',{});self.assertEqual(self.obj.armed_until,armed);self.assertFalse(self.obj.deadline)
        self.call('disarm',{});self.assertFalse(self.obj.armed_until);self.assertFalse(c.LEASE.exists())
    def test_unsafe_no_start(self):
        self.obj.safe=False;self.obj.reason='Too warm'
        for op in ('arm','start','snapshot','record_start'):self.assertEqual(self.call(op,{})[0],409)
    def test_config_only_when_idle(self):
        self.call('arm',{});self.assertEqual(self.call('config',{'fps':5})[0],409)
        self.call('disarm',{});self.assertEqual(self.call('config',{'fps':5})[0],200)
    def test_offer_fixed_cameras_no_controls(self):
        def proxy(req,timeout):
            body=json.loads(req.data);self.assertEqual(body['cameras'],n.CAMERAS);self.assertEqual(body['bridge_services_in'],[])
            return io.BytesIO(b'{"type":"answer","sdp":"answer"}')
        with patch.object(c,'enabled',return_value=True),patch.object(n,'urlopen',side_effect=proxy):
            self.assertEqual(self.call('offer',{'sdp':'offer','bridge_services_in':['carControl']})[0],200)
    def test_browser_keeps_pin_private_from_page(self):
        self.assertIn('localStorage.getItem(storageKey)',n.PAGE);self.assertNotIn('000000',n.PAGE)

class StorageTests(Base):
    def test_delete_validation(self):
        store=c.Store()
        with self.assertRaises(ValueError):store.delete('../driving-logs')
        e=store.create('motion',b'jpg',c.DEFAULTS)
        with self.assertRaises(ValueError):store.delete(e['id'])
        e['state']='ready';store.metadata(e);store.delete(e['id']);self.assertFalse(store.events())
    def test_oldest_unprotected_only(self):
        store=c.Store();cfg=dict(c.DEFAULTS,storage_mb=1,reserve_mb=0)
        first=store.create('snapshot',b'a'*600000,c.DEFAULTS);time.sleep(.002)
        second=store.create('snapshot',b'b'*600000,c.DEFAULTS);second['protected']=True;store.metadata(second)
        self.assertTrue(store.prune(cfg));self.assertFalse(store.path(first['id'],'.jpg').exists());self.assertTrue(store.path(second['id'],'.jpg').exists())
    def test_failed_interrupted_clip(self):
        store=c.Store();event=store.create('motion',b'thumb',c.DEFAULTS)
        store.recover(c.DEFAULTS)
        d=store.events()[0];self.assertEqual(d['state'],'failed');self.assertTrue(d['interrupted'])
    def test_free_space_fail_closed(self):
        store=c.Store()
        with patch.object(c.shutil,'disk_usage',return_value=NS(free=100)):
            with self.assertRaises(ValueError):store.create('motion',b'jpg',c.DEFAULTS)
    def test_record_tail_extension_is_bounded(self):
        worker=Worker()
        with patch('server.sentry_worker.time.monotonic',return_value=100):worker.begin('motion')
        self.assertEqual(worker.until,130)
        with patch('server.sentry_worker.time.monotonic',return_value=120):worker.begin('motion')
        self.assertEqual(worker.until,150)
        with patch('server.sentry_worker.time.monotonic',return_value=219):worker.begin('motion')
        self.assertEqual(worker.until,220)
        [f.close() for f in worker.files.values()];worker.files={};worker.file=None
    def test_waits_for_keyframe(self):
        worker=Worker();worker.begin('manual')
        msg=NS(livestreamRoadEncodeData=NS(idx=NS(flags=0),header=b'',data=b'p',width=640,height=360))
        worker.packet(msg);self.assertEqual(worker.frames,0)
        msg.livestreamRoadEncodeData.idx.flags=8;msg.livestreamRoadEncodeData.header=b'header'
        worker.packet(msg);self.assertEqual(worker.frames,1);[f.close() for f in worker.files.values()];worker.files={};worker.file=None

class MultiCameraTests(Base):
    def test_event_opens_all_tracks_and_waits_for_each_keyframe(self):
        w=Worker();w.begin('manual')
        self.assertEqual(set(w.files),set(c.CAMERAS))
        for camera,service in (('road','livestreamRoadEncodeData'),('wideRoad','livestreamWideRoadEncodeData'),('driver','livestreamDriverEncodeData')):
            data=NS(idx=NS(flags=0,timestampEof=0),header=b'',data=b'p',width=640,height=360)
            w.packet(NS(**{service:data}),camera);self.assertEqual(w.camera_frames[camera],0)
            data.idx.flags=8;data.header=b'h';w.packet(NS(**{service:data}),camera)
            self.assertEqual(w.camera_frames[camera],1)
        self.assertEqual(w.frames,3)
        for f in w.files.values():f.close()
    def test_group_delete_and_retention_remove_every_camera(self):
        store=c.Store();e=store.create('motion',None,c.DEFAULTS);e['state']='ready';store.metadata(e)
        for camera in c.CAMERAS:store.path(e['id'],c.video_suffix(camera)).write_bytes(b'video')
        store.delete(e['id']);self.assertFalse(list(store.root.glob(e['id']+'*')))
    def test_partial_finalization_preserves_available_cameras(self):
        store=c.Store();e=store.create('manual',None,c.DEFAULTS)
        e['cameras']={camera:{} for camera in c.CAMERAS}
        for camera in ('road','driver'):store.path(e['id'],c.video_suffix(camera,True)).write_bytes(b'encoded')
        def remux(args,**kwargs):Path(args[-1]).write_bytes(b'mp4')
        with patch.object(c.subprocess,'run',side_effect=remux):store.finalize(e,10)
        self.assertEqual(e['state'],'ready');self.assertFalse(e['complete'])
        self.assertEqual(e['cameras']['wideRoad']['state'],'failed')
        self.assertEqual(e['cameras']['driver']['state'],'ready')
        self.assertEqual(e['bytes'],6)
    def test_camera_names_cannot_escape_evidence_directory(self):
        with self.assertRaises(ValueError):c.video_suffix('../../logs')
    def test_recovery_preserves_already_finalized_track(self):
        store=c.Store();e=store.create('manual',None,c.DEFAULTS);e['cameras']={'road':{},'driver':{}}
        store.path(e['id'],'.mp4').write_bytes(b'already ready')
        store.path(e['id'],'.driver.partial.h264').write_bytes(b'raw')
        def remux(args,**kwargs):Path(args[-1]).write_bytes(b'mp4')
        with patch.object(c.subprocess,'run',side_effect=remux):store.finalize(e,10,True)
        self.assertTrue(e['complete']);self.assertEqual(store.path(e['id'],'.mp4').read_bytes(),b'already ready')

class AutomaticTests(Base):
    def setUp(self):
        super().setUp();self.obj=n.Sentry();self.obj.safe=True;self.obj.cooldown=0
        self.obj.metrics={'max_c':40}
    def test_offroad_arms_without_browser_and_keeps_renewing(self):
        self.obj.update_auto(100,True);self.assertFalse(self.obj.armed_until)
        self.obj.update_auto(110,True);self.assertTrue(self.obj.auto_armed)
        self.obj.update_auto(3600,True);self.assertEqual(self.obj.armed_until,3660)
        self.assertIsNone(self.obj.status()['armed_remaining'])
    def test_explicit_resume_has_no_one_minute_lease_gap(self):
        self.obj.resume_wait=60
        with patch('server.nap_sentry.time.monotonic',return_value=100):
            self.obj.handle(Handler('arm',body={}))
        self.assertTrue(self.obj.auto_armed)
        self.obj.update_auto(100.4,True)
        self.obj.update_auto(160,True)
        self.assertGreater(self.obj.armed_until,160)
        self.obj.update_auto(3600,True)
        self.assertEqual(self.obj.armed_until,3660)
    def test_drive_stops_and_next_park_rearms(self):
        self.obj.pause_auto=True;self.obj.fault_blocked=True;self.obj.save_policy()
        self.obj.update_auto(100,False,driving=True)
        self.assertFalse(self.obj.pause_auto);self.assertFalse(self.obj.fault_blocked)
        self.obj.update_auto(200,True);self.obj.update_auto(210,True);self.assertTrue(self.obj.auto_armed)
    def test_manual_pause_survives_restart_until_next_drive(self):
        h=Handler('disarm',body={});self.obj.handle(h)
        self.obj.update_auto(100,True);self.obj.update_auto(200,True);self.assertFalse(self.obj.armed_until)
        new=n.Sentry();self.assertTrue(new.pause_auto)
        new.safe=True;new.cooldown=0;new.update_auto(300,True);self.assertFalse(new.armed_until)
        new.update_auto(400,False,driving=True);new.update_auto(500,True);new.update_auto(510,True);self.assertTrue(new.auto_armed)
    def test_no_arm_without_safe_park_data(self):
        self.obj.safe=False;self.obj.reason='Waiting for telemetry'
        for now in (100,200,300):self.obj.update_auto(now,True)
        self.assertFalse(self.obj.armed_until)
        self.obj.safe=True
        for now in (400,500):self.obj.update_auto(now,False)
        self.assertFalse(self.obj.armed_until)
    def test_thermal_resume_needs_cooling_and_stable_safety(self):
        self.obj.armed_until=500;self.obj.auto_armed=True
        self.obj.safe=False;self.obj.reason='Device too warm';self.obj.metrics['max_c']=c.DEFAULTS['suspend_c']
        self.obj.update_auto(100,True);self.obj.off();self.obj.cooldown=0
        self.obj.safe=True;self.obj.reason='';self.obj.metrics['max_c']=c.DEFAULTS['warn_c']+1
        self.obj.update_auto(200,True);self.obj.update_auto(300,True);self.assertFalse(self.obj.armed_until)
        self.obj.metrics['max_c']=c.DEFAULTS['warn_c']-5;self.obj.update_auto(400,True)
        self.obj.update_auto(459,True);self.assertFalse(self.obj.armed_until)
        self.obj.update_auto(460,True);self.assertTrue(self.obj.auto_armed)
    def test_auto_setting_persisted(self):
        h=Handler('auto',body={'enabled':False});self.obj.handle(h);self.assertEqual(h.result[0],200)
        self.obj.update_auto(100,True);self.obj.update_auto(500,True);self.assertFalse(self.obj.armed_until)
        self.assertFalse(n.Sentry().cfg['auto_arm'])
    def test_fault_blocks_automatic_retry(self):
        self.obj.fault_blocked=True
        self.obj.update_auto(100,True);self.obj.update_auto(500,True);self.assertFalse(self.obj.armed_until)

class AgeRetentionTests(Base):
    def test_expiry_without_storage_pressure(self):
        store=c.Store();old=store.create('snapshot',b'old',c.DEFAULTS)
        old['timestamp']=time.time()-86401;store.metadata(old)
        fresh=store.create('snapshot',b'fresh',c.DEFAULTS)
        self.assertFalse(store.path(old['id'],'.jpg').exists());self.assertTrue(store.path(fresh['id'],'.jpg').exists())
    def test_protected_and_active_survive_age_expiry(self):
        store=c.Store();old=store.create('snapshot',b'old',c.DEFAULTS)
        old.update(timestamp=time.time()-86401,protected=True);store.metadata(old)
        active=store.create('motion',b'active',c.DEFAULTS);active['timestamp']=time.time()-86401;store.metadata(active)
        store.prune(c.DEFAULTS)
        self.assertTrue(store.path(old['id'],'.jpg').exists());self.assertTrue(store.path(active['id'],'.json').exists())
    def test_recent_events_kept_and_expiry_reported(self):
        store=c.Store();e=store.create('snapshot',b'fresh',c.DEFAULTS);store.prune(c.DEFAULTS)
        self.assertAlmostEqual(store.events()[0]['expires_at'],e['timestamp']+86400)
    def test_pause_does_not_disable_expiry(self):
        obj=n.Sentry();h=Handler('disarm',body={});obj.handle(h)
        e=obj.store.create('snapshot',b'expire',obj.cfg);e['timestamp']=time.time()-90000;obj.store.metadata(e)
        obj.store.prune(obj.cfg);self.assertFalse(obj.store.events())

if __name__=='__main__':unittest.main()
