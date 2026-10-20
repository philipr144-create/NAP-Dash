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
            self.consecutive=0;self.warmup=2
            return False,0.,True
        changed=np.abs(delta-shift)>cfg['pixel_threshold']
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
            self.event.update(fps=self.cfg['fps'],width=self.cfg['width'],height=self.cfg['height'])
            self.store.metadata(self.event)
            self.file=self.store.path(self.event['id'],'.partial.h264').open('wb')
            self.record_start=now;self.until=now+self.cfg['clip_seconds'];self.last_packet=now;self.frames=0
            self.error=''
        except Exception as e:
            self.error=str(e);self.event=None;self.cooldown=now+self.cfg['cooldown_seconds']
    def finish(self,reason='complete'):
        if self.file:
            self.file.flush();os.fsync(self.file.fileno());self.file.close();self.file=None
        if self.event:
            self.event.update(duration_s=round(time.monotonic()-self.record_start,2),frames=self.frames,ended_reason=reason)
            self.store.finalize(self.event,self.cfg['fps'],reason!='complete')
            self.event=None
            self.cooldown=time.monotonic()+self.cfg['cooldown_seconds']
        common.RECORD_LEASE.unlink(missing_ok=True)
    def packet(self,msg):
        if not self.event or not self.file:
            return
        data=msg.livestreamRoadEncodeData
        if not self.frames and not (data.idx.flags & 8 and data.header):
            return
        # Bound every write, including an encoder malfunction or unexpected bitrate.
        if self.file.tell()+len(data.header)+len(data.data)>min(128*1048576,self.cfg['storage_mb']*1048576//2):
            self.finish('clip size limit');return
        self.file.write(data.header);self.file.write(data.data)
        self.frames+=1;self.last_packet=time.monotonic()
        if self.frames==1:
            self.event.update(width=data.width,height=data.height)
            self.store.metadata(self.event)
    def status(self):
        now=time.monotonic()
        common.atomic(common.WORKER_STATUS,dict(mono=now,camera=now-self.last_frame<3,frame_age_s=round(now-self.last_frame,1),recording=bool(self.event),event_id=self.event['id'] if self.event else None,motion_score=round(self.score,4),brightness_change=self.brightness,error=self.error,frames=self.frames,last_manual=self.last_manual,last_snapshot=self.last_snapshot))
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
        client=VisionIpcClient('camerad',VisionStreamType.VISION_STREAM_ROAD,True)
        sock=messaging.sub_sock('livestreamRoadEncodeData',conflate=False,timeout=0)
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
                    if not client.is_connected():client.connect(False)
                    if client.is_connected():
                        buf=client.recv()
                        if buf is not None:
                            self.last_frame=now;gray=frame_gray(buf);self.gray=gray
                            if self.event and not self.store.path(self.event['id'],'.jpg').exists():
                                self.store.path(self.event['id'],'.jpg').write_bytes(jpeg(gray))
                            if d.get('armed') and d.get('motion'):
                                hit,self.score,self.brightness=self.detector.update(gray,self.cfg)
                                if hit:self.last_motion=now;self.begin('motion')
                            else:
                                self.detector=Detector();self.score=0.
                            snap=d.get('snapshot')
                            if snap and snap!=self.last_snapshot:
                                self.last_snapshot=snap
                                try:self.store.create('snapshot',jpeg(gray),self.cfg)
                                except Exception as e:self.error=str(e)
                        elif now-self.last_frame>5:
                            self.error='Camera frames unavailable'
                manual=d.get('record_request')
                if manual and manual!=self.last_manual:
                    self.last_manual=manual;self.begin('manual')
                if self.event:
                    common.atomic(common.RECORD_LEASE,{'until':now+3})
                # Drain even when not recording: never retain encoded pre-event footage
                # or let a live-view subscription accumulate old packets in memory.
                for _ in range(64):
                    msg=messaging.recv_one_or_none(sock)
                    if msg is None:break
                    self.packet(msg)
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
