"""Low-rate VisionIPC luma motion sampling and on-demand hardware H264 evidence."""
import io
import os
import signal
import time
from pathlib import Path
from server import sentry_common as common

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
        self.gray=None;self.error='';self.frames=0;self.stopping=False;self.last_space=0;self.space_ok=True
    def begin(self,kind):
        now=time.monotonic()
        if self.event:
            if kind=='motion':self.until=min(self.record_start+self.cfg['max_clip_seconds'],now+self.cfg['clip_seconds'])
            return
        if now < self.cooldown:
            return
        try:
            self.event=self.store.create(kind,jpeg(self.gray) if self.gray is not None else None,self.cfg)
            self.event.update(fps=self.cfg['fps'],width=self.cfg['width'],height=self.cfg['height'],trigger_camera=self.trigger_camera if kind=='motion' else None,cameras={c:dict(state='recording',frames=0) for c in common.CAMERAS})
            self.store.metadata(self.event)
            self.files={c:self.store.path(self.event['id'],common.video_suffix(c,True)).open('wb') for c in common.CAMERAS}
            self.file=self.files['road'];self.camera_frames={c:0 for c in common.CAMERAS}
            self.record_start=now;self.until=now+self.cfg['clip_seconds'];self.last_packet=now;self.frames=0
            self.error=''
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
            self.event.update(duration_s=round(time.monotonic()-self.record_start,2),frames=self.frames,ended_reason=reason)
            self.store.finalize(self.event,self.cfg['fps'],reason!='complete')
            self.event=None
            self.cooldown=time.monotonic()+self.cfg['cooldown_seconds']
        common.RECORD_LEASE.unlink(missing_ok=True)
    def packet(self,msg,camera='road'):
        if not self.event or camera not in self.files:
            return
        service={'road':'livestreamRoadEncodeData','wideRoad':'livestreamWideRoadEncodeData','driver':'livestreamDriverEncodeData'}[camera]
        data=getattr(msg,service);track=self.event['cameras'][camera]
        f=self.files[camera];count=self.camera_frames[camera]
        timestamp=getattr(data.idx,'timestampEof',0)
        if timestamp and timestamp < self.record_start*1e9:
            return  # Drain old live packets without treating them as event evidence.
        if not count and not (data.idx.flags & 8 and data.header):
            return
        total=sum(x.tell() for x in self.files.values())
        if total+len(data.header)+len(data.data)>min(128*1048576,self.cfg['storage_mb']*1048576//2):
            self.finish('clip size limit');return
        f.write(data.header);f.write(data.data)
        self.camera_frames[camera]+=1;self.frames=sum(self.camera_frames.values());self.last_packet=time.monotonic()
        track.update(frames=self.camera_frames[camera],last_mono_ns=timestamp)
        if not count:
            track.update(width=data.width,height=data.height,start_mono_ns=timestamp)
            self.store.metadata(self.event)
    def status(self):
        now=time.monotonic()
        common.atomic(common.WORKER_STATUS,dict(mono=now,camera=now-self.last_frame<3,frame_age_s=round(now-self.last_frame,1),recording=bool(self.event),event_id=self.event['id'] if self.event else None,motion_score=round(self.score,4),brightness_change=self.brightness,error=self.error,frames=self.frames,last_manual=self.last_manual,last_snapshot=self.last_snapshot,cameras=self.camera_status,trigger_camera=self.trigger_camera,last_trigger_at=self.last_trigger_wall))
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
        socks={c:messaging.sub_sock(service,conflate=False,timeout=0) for c,service in (('road','livestreamRoadEncodeData'),('wideRoad','livestreamWideRoadEncodeData'),('driver','livestreamDriverEncodeData'))}
        try:
            while not self.stopping:
                now=time.monotonic();d=common.lease()
                if not d:
                    break
                self.cfg=d.get('config',self.cfg)
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
                        hit=False;score=0.;brightness=False
                        if d.get('armed') and d.get('motion'):
                            hit,score,brightness=self.detectors[camera].update(gray,self.cfg)
                        else:self.detectors[camera]=Detector()
                        prior=self.camera_status.get(camera,{})
                        self.camera_status[camera]=dict(available=True,mono=now,score=round(score,4),peak=max(score,prior.get('peak',0)),luma=round(float(gray.mean()),1),brightness_change=brightness,confirmations=self.detectors[camera].consecutive)
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
                # Drain even when not recording: never retain encoded pre-event footage
                # or let a live-view subscription accumulate old packets in memory.
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
            self.finish('security stopped');self.status()
            lock.close()

def main():
    Worker().run()

if __name__=='__main__':main()
