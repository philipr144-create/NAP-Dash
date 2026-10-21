"""Low-rate VisionIPC luma motion sampling and on-demand hardware H264 evidence."""
import io
import os
import signal
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from server import sentry_common as common
from server.sentry_bump import BumpDetector
from server.sentry_buffer import Packet, VideoRing

class Detector:
    def __init__(self):
        self.previous = None
        self.consecutive = 0
        self.warmup = 5
        self.reference = None
        self.samples = 0
    def update(self, gray, cfg):
        import numpy as np
        if self.previous is None:
            self.previous=gray.copy()
            return False,0.,False
        delta=np.subtract(gray,self.previous,dtype=np.int16)
        self.previous[:]=gray
        shift=float(np.median(delta))
        if self.warmup:
            self.warmup-=1;self.consecutive=0
            return False,0.,False
        if abs(shift) >= cfg['brightness_jump']:
            self.consecutive=0;self.warmup=2;self.reference=None;self.samples=0
            return False,0.,True
        # A short background comparison keeps brief/small objects visible across
        # confirmations even when consecutive frames differ only slightly.
        if self.reference is None:self.reference=gray.copy()
        background=np.subtract(gray,self.reference,dtype=np.int16)
        background-=int(np.median(background))
        self.samples+=1
        if self.samples >= max(2,round(cfg['analysis_fps']*2)):
            self.reference=gray.copy();self.samples=0
        changed=(np.abs(delta-shift)>cfg['pixel_threshold']) | (np.abs(background)>cfg['pixel_threshold'])
        # Require spatial support: discard isolated sensor noise before scoring.
        support=changed[1:-1,1:-1].astype(np.uint8)
        for dy,dx in ((0,0),(0,1),(0,2),(1,0),(1,2),(2,0),(2,1),(2,2)):
            support += changed[dy:dy+gray.shape[0]-2,dx:dx+gray.shape[1]-2]
        score=float(np.mean(support>=5))
        self.consecutive=self.consecutive+1 if score >= cfg['changed_fraction'] else 0
        return self.consecutive >= cfg['confirmation_frames'],score,False

def frame_gray(buf):
    import numpy as np
    from PIL import Image, ImageFilter
    # Read only the existing NV12 luma plane. Subsample before resizing/blur.
    plane=np.frombuffer(buf.data,dtype=np.uint8,count=buf.height*buf.stride).reshape(buf.height,buf.stride)[:,:buf.width]
    small=plane[::max(1,buf.height//180),::max(1,buf.width//320)]
    return np.array(Image.fromarray(small).resize((160,90),Image.Resampling.BILINEAR).filter(ImageFilter.BoxBlur(1)))

def jpeg(gray):
    from PIL import Image
    out=io.BytesIO();Image.fromarray(gray).resize((640,360)).save(out,format='JPEG',quality=75)
    return out.getvalue()

class Worker:
    def __init__(self):
        self.store=common.Store();self.cfg=common.config();self.detector=Detector()
        self.detectors={c:Detector() for c in common.CAMERAS}
        self.files={};self.camera_frames={};self.camera_status={};self.trigger_camera=None;self.last_trigger_wall=None
        self.event=None;self.file=None;self.until=0;self.record_start=0;self.last_motion=0
        self.cooldown=0;self.last_frame=0;self.last_packet=0;self.last_sample=0;self.last_status=0
        self.score=0.;self.brightness=False;self.thumbnail=None;self.last_snapshot='';self.last_manual='';self.last_stop=0
        self.rings={c:VideoRing() for c in common.CAMERAS};self.buffer_enabled=False
        self.bump=BumpDetector();self.bump_enabled=False;self.finalizer=None;self.finalizing=None
        self.gray=None;self.error='';self.frames=0;self.stopping=False;self.last_space=0;self.space_ok=True
    def analyze(self,camera,gray,enabled):
        # Disabling a trigger source leaves its recording/live track untouched.
        # Reset history so re-enabling it starts with a fresh warmup.
        if not enabled or (camera=='driver' and not self.cfg.get('cabin_motion',0)):
            self.detectors[camera]=Detector()
            return False,0.,False
        return self.detectors[camera].update(gray,self.cfg)
    def event_metadata(self):
        self.event['protected']=common.read(self.store.path(self.event['id'],'.json')).get('protected',self.event['protected'])
        self.store.metadata(self.event)
    def begin(self,kind):
        now=time.monotonic()
        if self.event:
            if kind in ('motion','bump'):
                self.until=min(self.record_start+self.cfg['max_clip_seconds'],now+self.cfg['clip_seconds'])
                if kind not in self.event['triggers']:self.event['triggers'].append(kind)
                if kind=='bump':self.event['bump_peak_g']=round(max((self.event.get('bump_peak_g') or 0),self.bump.current_g),3)
                self.event_metadata()
            return
        if now < self.cooldown and kind!='bump':
            return
        try:
            self.event=self.store.create(kind,jpeg(self.gray) if self.gray is not None else None,self.cfg)
            self.event.update(fps=self.cfg['fps'],width=self.cfg['width'],height=self.cfg['height'],trigger_camera=self.trigger_camera if kind=='motion' else None,triggers=[kind],trigger_mono_ns=int(now*1e9),bump_peak_g=round(self.bump.current_g,3) if kind=='bump' else None,cameras={c:dict(state='recording',frames=0) for c in common.CAMERAS})
            self.event_metadata()
            self.files={c:self.store.path(self.event['id'],common.video_suffix(c,True)).open('wb') for c in common.CAMERAS}
            self.file=self.files['road'];self.camera_frames={c:0 for c in common.CAMERAS}
            self.record_start=now;self.until=now+self.cfg['clip_seconds'];self.last_packet=now;self.frames=0
            self.error=''
            if self.buffer_enabled:
                for camera,ring in self.rings.items():
                    for packet in ring.snapshot(int(now*1e9),self.cfg['prebuffer_seconds']):
                        self.write_packet(packet,camera)
        except Exception as e:
            self.error=str(e)
            for f in self.files.values():f.close()
            self.files={};self.file=None
            if self.event:self.store.finalize(self.event,self.cfg['fps'],True)
            self.event=None;self.cooldown=now+self.cfg['cooldown_seconds']
    def finish(self,reason='complete'):
        for f in self.files.values():
            if not f.closed:f.flush();os.fsync(f.fileno());f.close()
        self.files={};self.file=None
        if self.event:
            starts=[t.get('start_mono_ns',0) for t in self.event['cameras'].values() if t.get('start_mono_ns')]
            start=min(starts)/1e9 if starts else self.record_start
            self.event.update(duration_s=round(time.monotonic()-start,2),post_trigger_s=round(time.monotonic()-self.record_start,2),frames=self.frames,ended_reason=reason)
            event=self.event;self.event=None
            event['protected']=common.read(self.store.path(event['id'],'.json')).get('protected',event['protected'])
            event['state']='finalizing';self.store.metadata(event)
            if self.finalizer is None:self.finalizer=ThreadPoolExecutor(max_workers=1)
            # At most one pending finalizer: never accumulate unbounded work.
            if self.finalizing is not None and not self.finalizing.done():self.finalizing.result()
            self.finalizing=self.finalizer.submit(self.store.finalize,event,self.cfg['fps'],reason!='complete')
            self.cooldown=time.monotonic()+self.cfg['cooldown_seconds']
        common.RECORD_LEASE.unlink(missing_ok=True)
    def packet(self,msg,camera='road'):
        if not self.buffer_enabled and not self.event:return
        service={'road':'livestreamRoadEncodeData','wideRoad':'livestreamWideRoadEncodeData','driver':'livestreamDriverEncodeData'}[camera]
        data=getattr(msg,service);now_ns=time.monotonic_ns()
        timestamp=getattr(data.idx,'timestampEof',0) or now_ns
        # Reject stale/backlogged data and future timestamps; no unbounded queue.
        if not 0<=now_ns-timestamp<1_000_000_000:return
        packet=Packet(timestamp,bool(data.idx.flags & 8 and data.header),bytes(data.header)+bytes(data.data),data.width,data.height)
        if self.buffer_enabled:self.rings[camera].append(packet,self.cfg['prebuffer_seconds'])
        if self.event:self.write_packet(packet,camera)
    def write_packet(self,packet,camera):
        if not self.event or camera not in self.files:return
        track=self.event['cameras'][camera];count=self.camera_frames[camera]
        if packet.timestamp<=track.get('last_mono_ns',0):return
        if not count and not packet.key:return
        if not self.buffer_enabled and packet.timestamp<int(self.record_start*1e9):return
        total=sum(x.tell() for x in self.files.values())
        if total+len(packet.payload)>min(128*1048576,self.cfg['storage_mb']*1048576//2):
            self.finish('clip size limit');return
        self.files[camera].write(packet.payload)
        self.camera_frames[camera]+=1;self.frames=sum(self.camera_frames.values());self.last_packet=time.monotonic()
        track.update(frames=self.camera_frames[camera],last_mono_ns=packet.timestamp)
        if not count:
            track.update(width=packet.width,height=packet.height,start_mono_ns=packet.timestamp,pre_event_s=round(max(0,(int(self.record_start*1e9)-packet.timestamp)/1e9),3))
            self.event_metadata()
    def sensor(self,msg,now_ns):
        if not self.bump_enabled:return
        try:
            event=msg.accelerometer
            if not msg.valid or event.which()!='acceleration':return
            if self.bump.update(event.acceleration.v,event.timestamp,now_ns,self.cfg['bump_threshold_g']):
                self.last_trigger_wall=time.time();self.trigger_camera=None;self.begin('bump')
        except (ValueError,TypeError,AttributeError):
            self.bump.reset()
    def status(self):
        now=time.monotonic()
        common.atomic(common.WORKER_STATUS,dict(mono=now,camera=now-self.last_frame<3,frame_age_s=round(now-self.last_frame,1),recording=bool(self.event),event_id=self.event['id'] if self.event else None,motion_score=round(self.score,4),brightness_change=self.brightness,error=self.error,frames=self.frames,last_manual=self.last_manual,last_snapshot=self.last_snapshot,cameras=self.camera_status,trigger_camera=self.trigger_camera,last_trigger_at=self.last_trigger_wall,bump=self.bump.status(time.monotonic_ns(),self.bump_enabled),buffer=dict(enabled=self.buffer_enabled,seconds={c:round(min(self.cfg['prebuffer_seconds'],r.seconds()),1) for c,r in self.rings.items()},bytes=sum(r.bytes for r in self.rings.values()))))
    def run(self):
        from cereal import messaging
        from msgq.visionipc import VisionIpcClient, VisionStreamType
        # Existing manager is the owner; a lock also prevents duplicate test workers.
        import fcntl
        lock=Path('/dev/shm/nap_sentry_worker.lock').open('w')
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        signal.signal(signal.SIGTERM,lambda *a:setattr(self,'stopping',True))
        signal.signal(signal.SIGINT,lambda *a:setattr(self,'stopping',True))
        self.store.recover(self.cfg)
        clients={c:VisionIpcClient('camerad',getattr(VisionStreamType,t),True) for c,t in (('road','VISION_STREAM_ROAD'),('wideRoad','VISION_STREAM_WIDE_ROAD'),('driver','VISION_STREAM_DRIVER'))}
        sensor_sock=messaging.sub_sock('accelerometer',conflate=False,timeout=0)
        socks={c:messaging.sub_sock(service,conflate=False,timeout=0) for c,service in (('road','livestreamRoadEncodeData'),('wideRoad','livestreamWideRoadEncodeData'),('driver','livestreamDriverEncodeData'))}
        try:
            while not self.stopping:
                now=time.monotonic();d=common.lease()
                if not d:
                    break
                self.cfg=d.get('config',self.cfg)
                self.buffer_enabled=bool(d.get('armed') and self.cfg.get('prebuffer_seconds',0)>0)
                bump_enabled=bool(d.get('armed') and self.cfg.get('bump_detection',0))
                if bump_enabled!=self.bump_enabled:self.bump.reset()
                self.bump_enabled=bump_enabled
                if not self.buffer_enabled:
                    for ring in self.rings.values():ring.clear()
                for _ in range(256):
                    sensor_msg=messaging.recv_one_or_none(sensor_sock)
                    if sensor_msg is None:break
                    self.sensor(sensor_msg,time.monotonic_ns())
                stop=d.get('stop_seq',0)
                if stop!=self.last_stop:
                    self.finish('manual stop');self.last_stop=stop
                if now-self.last_sample >= 1/self.cfg['analysis_fps']:
                    self.last_sample=now
                    for camera,client in clients.items():
                        if not client.is_connected():client.connect(False)
                        buf=client.recv() if client.is_connected() else None
                        if buf is None:
                            prior=self.camera_status.get(camera,{})
                            prior['available']=now-prior.get('mono',0)<3
                            self.camera_status[camera]=prior
                            continue
                        gray=frame_gray(buf)
                        if camera=='road':
                            self.last_frame=now;self.gray=gray
                        hit,score,brightness=self.analyze(camera,gray,d.get('armed') and d.get('motion'))
                        prior=self.camera_status.get(camera,{})
                        self.camera_status[camera]=dict(available=True,detection_enabled=bool(d.get('armed') and d.get('motion') and (camera!='driver' or self.cfg.get('cabin_motion',0))),mono=now,score=round(score,4),peak=max(score,prior.get('peak',0)),luma=round(float(gray.mean()),1),brightness_change=brightness,confirmations=self.detectors[camera].consecutive)
                        if hit:
                            self.last_motion=now;self.last_trigger_wall=time.time();self.trigger_camera=camera;self.begin('motion')
                    self.score=max((v.get('score',0) for v in self.camera_status.values()),default=0)
                    self.brightness=any(v.get('brightness_change') for v in self.camera_status.values())
                    if now-self.last_frame>5:self.error='Front camera frames unavailable'
                    snap=d.get('snapshot')
                    if snap and snap!=self.last_snapshot and self.gray is not None:
                        self.last_snapshot=snap
                        try:self.store.create('snapshot',jpeg(self.gray),self.cfg)
                        except Exception as e:self.error=str(e)
                manual=d.get('record_request')
                if manual and manual!=self.last_manual:
                    self.last_manual=manual;self.begin('manual')
                if self.event:
                    common.atomic(common.RECORD_LEASE,{'until':now+3})
                # Always drain; retain only bounded compressed RAM rings when armed.
                for camera,sock in socks.items():
                    for _ in range(32):
                        msg=messaging.recv_one_or_none(sock)
                        if msg is None:break
                        self.packet(msg,camera)
                if self.event:
                    if now-self.last_space>=1:
                        self.last_space=now;self.space_ok=self.store.prune(self.cfg)
                    if now>=self.until or now-self.record_start>=self.cfg['max_clip_seconds'] or now-self.last_packet>12 or not self.space_ok:
                        self.finish('complete' if self.space_ok and now-self.last_packet<=12 else 'encoder or storage unavailable')
                if now-self.last_status>=.5:
                    self.last_status=now;self.status()
                time.sleep(.025 if self.event else .05)
        finally:
            self.finish('security stopped')
            if self.finalizer is not None:self.finalizer.shutdown(wait=True)
            self.status();lock.close()

def main():
    Worker().run()

if __name__=='__main__':main()
