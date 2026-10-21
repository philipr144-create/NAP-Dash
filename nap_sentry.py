"""NAP Sentry V2 coordinator and private WebRTC service. Never changes driving state."""
import json
import math
import os
import secrets
import threading
import time
import uuid
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import urlparse
from server import sentry_common as common
from server.sentry_page import PAGE

TOKEN = Path('/data/nap_sentry_pin')
CAMERAS = ['road','wideRoad','driver']
MAX_SESSION = 300
IDLE = 20

def enabled(started=False, resource='camera'):
    return common.enabled(started,resource)

def safety(sm, now, cfg=None):
    cfg = cfg or common.DEFAULTS
    for name in ('deviceState', 'pandaStates', 'peripheralState'):
        if not sm.valid[name] or not sm.recv_time[name] or now - sm.recv_time[name] > 3:
            return 'Waiting for fresh ' + name
    ds, pandas, power = sm['deviceState'], sm['pandaStates'], sm['peripheralState']
    if ds.started or not pandas or any(p.ignitionLine or p.ignitionCan or str(p.pandaType) == 'unknown' for p in pandas):
        return 'Park and switch ignition off'
    # A disconnected vehicle harness identifies USB bench power, not a low car battery.
    bench = all(str(p.harnessStatus) == 'notConnected' for p in pandas)
    voltage_ok = 12000 <= power.voltage <= 16000 or (bench and 4750 <= power.voltage <= 5250)
    if str(power.pandaType) == 'unknown' or not voltage_ok:
        return 'Supply outside 12–16 V, or 4.75–5.25 V with vehicle harness disconnected'
    if str(ds.thermalStatus) != 'green' or not math.isfinite(ds.maxTempC) or ds.maxTempC >= cfg['suspend_c']:
        return 'Device too warm'
    return ''


class Sentry:
    def __init__(self):
        self.lock=threading.RLock();self.cfg=common.config();self.store=common.Store()
        self.deadline=self.heartbeat=self.armed_until=self.transient_until=0
        self.reason='Waiting for safety telemetry';self.safe=False;self.negotiating=False
        self.motion=True;self.snapshot_id='';self.record_id='';self.stop_seq=0
        self.metrics={};self.suspended=False;self.activity_start=0;self.last_sensor=0
        policy=common.read(common.POLICY)
        self.pause_auto=bool(policy.get('paused'));self.fault_blocked=bool(policy.get('fault'))
        self.auto_armed=False;self.safe_since=None;self.resume_wait=10;self.cool_resume=False
        self.last_prune=-60
        self.off()
        try:
            fd=os.open(TOKEN,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'w') as f:f.write('000000')
        except FileExistsError:pass
        self.token=TOKEN.read_text().strip()
        if len(self.token)!=6 or not self.token.isascii() or not self.token.isdigit():
            raise ValueError('NAP Sentry PIN must contain six digits')
    def off(self):
        self.deadline=self.heartbeat=self.armed_until=self.transient_until=0
        self.record_id=self.snapshot_id='';self.stop_seq+=1;self.activity_start=0
        self.auto_armed=False
        self.cooldown=time.monotonic()+5
        common.LEASE.unlink(missing_ok=True);common.RECORD_LEASE.unlink(missing_ok=True)
    def save_policy(self):
        common.atomic(common.POLICY,{'paused':self.pause_auto,'fault':self.fault_blocked})
    def update_auto(self,now,parked,driving=False):
        # Manual pause/fault is scoped to this parking period, including Dash restarts.
        if driving:
            self.pause_auto=False;self.fault_blocked=False;self.safe_since=None
            self.auto_armed=False;self.resume_wait=10;self.cool_resume=False
            common.POLICY.unlink(missing_ok=True)
            return
        if not parked or not self.safe:
            if self.armed_until or self.deadline or self.transient_until:
                self.resume_wait=60
            if self.reason=='Device too warm':
                self.cool_resume=True;self.resume_wait=60
            self.safe_since=None
            return
        if not self.cfg['auto_arm'] or self.pause_auto or self.fault_blocked:
            self.safe_since=None
            return
        if self.cool_resume and self.metrics.get('max_c',100)>=self.cfg['warn_c']:
            self.safe_since=None
            return
        if self.auto_armed:
            # An already-permitted running session must renew without another
            # startup delay, including immediately after an explicit Resume.
            self.armed_until=now+60
            return
        if self.safe_since is None:self.safe_since=now
        if now-self.safe_since>=self.resume_wait and now>=self.cooldown:
            # The short renewable deadline is a watchdog, not a parking session timer.
            self.armed_until=now+60;self.auto_armed=True;self.suspended=False;self.cool_resume=False
    def worker(self):
        d=common.read(common.WORKER_STATUS)
        return d if 0<=time.monotonic()-d.get('mono',0)<3 else {}
    def status(self):
        now=time.monotonic();w=self.worker();storage=self.store.usage()
        recording=bool(w.get('recording') and common.lease())
        state='suspended' if self.suspended else 'recording' if recording else 'viewing' if self.deadline else 'monitoring' if self.armed_until else 'disarmed'
        return dict(state=state,active=common.enabled(resource='live'),armed=self.armed_until>now,motion=self.motion,
                    safe=self.safe,reason=self.reason or ('Camera/monitor stopped; Resume monitoring or next drive required' if self.fault_blocked else ''),warning=self.metrics.get('max_c',0)>=self.cfg['warn_c'],
                    remaining=max(0,int(self.deadline-now)),armed_remaining=None if self.auto_armed else max(0,int(self.armed_until-now)),
                    auto_enabled=bool(self.cfg['auto_arm']),auto_armed=self.auto_armed,paused=self.pause_auto,
                    fault_blocked=self.fault_blocked,retention_hours=24,
                    auto_ready_in=max(0,int(self.resume_wait-(now-self.safe_since))) if self.safe_since is not None else None,
                    camera=bool(w.get('camera')),recording=recording,worker=w,metrics=self.metrics,storage=storage,config=self.cfg)
    def sensors(self,ds):
        sensors={}
        for name in ('cpuTempC','gpuTempC','memoryTempC','pmicTempC','dspTempC','modemTempC'):
            try:
                val=getattr(ds,name)
                if isinstance(val,(float,int)):sensors[name]=round(float(val),1)
                else:
                    for i,v in enumerate(val):sensors[name+str(i)]=round(float(v),1)
            except Exception:pass
        for zone in Path('/sys/class/thermal').glob('thermal_zone*'):
            try:
                c=float((zone/'temp').read_text())/1000
                if -20<c<120:sensors[(zone/'type').read_text().strip()]=round(c,1)
            except (OSError,ValueError):pass
        return sensors
    def watch(self):
        from cereal import messaging
        from openpilot.common.params import Params
        params=Params();sm=messaging.SubMaster(['deviceState','pandaStates','peripheralState'])
        while True:
            try:
                sm.update(200)
                with self.lock:
                    now=time.monotonic();ds=sm['deviceState']
                    if now-self.last_prune>=60:
                        self.store.prune(self.cfg);self.last_prune=now
                    self.reason=safety(sm,now,self.cfg)
                    shutting_down=params.get_bool('DoShutdown') or params.get_bool('DoReboot')
                    onroad=params.get_bool('IsOnroad')
                    fresh=all(sm.valid[k] and sm.recv_time[k] and now-sm.recv_time[k]<=3 for k in ('deviceState','pandaStates'))
                    driving=bool(fresh and (ds.started or any(p.ignitionLine or p.ignitionCan for p in sm['pandaStates'])))
                    if shutting_down or onroad:
                        self.reason='Vehicle starting or device shutting down'
                    self.safe=not self.reason
                    self.metrics.update(max_c=round(float(ds.maxTempC),1),thermal=str(ds.thermalStatus),supply_mv=int(sm['peripheralState'].voltage))
                    if now-self.last_sensor>5:
                        self.last_sensor=now;self.metrics['sensors']=self.sensors(ds)
                        try:
                            from openpilot.system.hardware import HARDWARE
                            mv,ma=HARDWARE.get_voltage(),HARDWARE.get_current()
                            self.metrics.update(input_mv=mv,input_ma=ma,input_w=round(mv*ma/1e6,2))
                        except Exception:self.metrics['input_w']=None
                    self.update_auto(now,parked=bool(fresh and not driving and not onroad and not shutting_down),driving=driving)
                    if not self.safe:
                        if self.deadline or self.armed_until or self.transient_until:self.suspended=True
                        self.off()
                        continue
                    if self.deadline and (now>=self.deadline or now-self.heartbeat>=IDLE):self.deadline=0
                    if now>=self.armed_until:self.armed_until=0
                    if now>=self.transient_until:self.transient_until=0
                    w=self.worker()
                    if self.record_id and w.get('last_manual')==self.record_id and not w.get('recording'):
                        self.record_id=''
                        if not self.snapshot_id:self.transient_until=0
                    if self.snapshot_id and w.get('last_snapshot')==self.snapshot_id:
                        self.snapshot_id=''
                        if not self.record_id:self.transient_until=0
                    until=max(self.deadline,self.armed_until,self.transient_until)
                    if until:
                        if not self.activity_start:self.activity_start=now
                        if now-self.activity_start>15 and not self.worker().get('camera'):
                            self.reason='Camera or monitor unavailable; rearm after checking device'
                            self.suspended=True;self.fault_blocked=True;self.save_policy();self.off();continue
                        effective=dict(self.cfg)
                        if self.metrics['max_c']>=self.cfg['warn_c']:effective['analysis_fps']=1
                        common.atomic(common.LEASE,dict(until=now+3,deadline=until,live=bool(self.deadline),armed=bool(self.armed_until),motion=self.motion,snapshot=self.snapshot_id,record_request=self.record_id,stop_seq=self.stop_seq,config=effective))
                    else:
                        self.activity_start=0;common.LEASE.unlink(missing_ok=True)
            except Exception:
                with self.lock:
                    self.safe=False;self.reason='Safety telemetry unavailable';self.suspended=True;self.safe_since=None;self.resume_wait=60;self.off()
            time.sleep(.2)
    def media(self,h,ident,kind):
        suffix='.jpg' if kind=='thumbnail' else common.video_suffix(kind[6:] if kind.startswith('video-') else 'road')
        path=self.store.path(ident,suffix)
        if not path.is_file():return h.send_json({'error':'Evidence unavailable'},404)
        size=path.stat().st_size;start=0;end=size-1;partial=False
        requested=h.headers.get('Range','')
        if requested:
            import re
            m=re.fullmatch(r'bytes=(\d+)-(\d*)',requested)
            if not m:return h.send_json({'error':'Unsupported range'},416)
            start=int(m[1]);end=min(end,int(m[2])) if m[2] else end;partial=True
            if start>end or start>=size:return h.send_json({'error':'Invalid range'},416)
        h.send_response(206 if partial else 200)
        h.send_header('Content-Type','image/jpeg' if kind=='thumbnail' else 'video/mp4')
        h.send_header('Content-Length',str(end-start+1));h.send_header('Cache-Control','no-store');h.send_header('Accept-Ranges','bytes')
        if partial:h.send_header('Content-Range',f'bytes {start}-{end}/{size}')
        h.end_headers()
        with path.open('rb') as f:
            f.seek(start);remaining=end-start+1
            while remaining:
                data=f.read(min(65536,remaining))
                if not data:break
                h.wfile.write(data);remaining-=len(data)
    def handle(self,h):
        path=urlparse(h.path).path
        if path in ('/sentry','/sentry/') and h.command=='GET':
            h.send_bytes(PAGE.encode(),'text/html; charset=utf-8');return True
        if not path.startswith('/api/sentry/'):
            return False
        if not secrets.compare_digest(h.headers.get('Authorization',''),'Bearer '+self.token):
            h.send_json({'error':'Sentry PIN required'},401);return True
        origin=h.headers.get('Origin')
        if origin and urlparse(origin).netloc!=h.headers.get('Host'):
            h.send_json({'error':'Same origin required'},403);return True
        op=path[len('/api/sentry/'):]
        try:
            if h.command=='GET':
                if op=='status':
                    with self.lock:h.send_json(self.status())
                elif op=='events':h.send_json({'events':self.store.events()[:1000],'storage':self.store.usage()})
                elif op.startswith('media/'):
                    parts=op.split('/')
                    if len(parts)!=3 or parts[2] not in ('thumbnail','video','video-road','video-wideRoad','video-driver'):raise ValueError('Invalid evidence path')
                    self.media(h,parts[1],parts[2])
                else:h.send_json({'error':'Not found'},404)
                return True
            if h.command!='POST':h.send_json({'error':'Method not allowed'},405);return True
            n=int(h.headers.get('Content-Length','0'))
            if not 0<n<=100000 or h.headers.get('Content-Type','').split(';')[0]!='application/json':raise ValueError('Bounded JSON body required')
            body=json.loads(h.rfile.read(n))
            if not isinstance(body,dict):raise ValueError('JSON object required')
            with self.lock:
                now=time.monotonic()
                if op=='stop':self.deadline=0
                elif op=='disarm':
                    self.pause_auto=True;self.save_policy();self.off();self.suspended=False
                elif op=='auto':
                    if type(body.get('enabled')) is not bool:raise ValueError('Boolean required')
                    self.cfg=common.validate_config({'auto_arm':int(body['enabled'])},self.cfg)
                    common.atomic(common.CONFIG,self.cfg)
                    self.pause_auto=False;self.safe_since=None;self.save_policy()
                    if not body['enabled'] and self.auto_armed:
                        self.armed_until=0;self.auto_armed=False;self.stop_seq+=1
                elif op=='record_stop':self.stop_seq+=1;self.record_id='';self.transient_until=min(self.transient_until,now+3)
                elif op=='delete':self.store.delete(body.get('id'))
                elif op=='protect':
                    d=common.read(self.store.path(body.get('id'),'.json'))
                    if not d:raise ValueError('Event not found')
                    d['protected']=bool(body.get('protected'));self.store.metadata(d)
                elif op=='config':
                    if self.deadline or self.armed_until or self.transient_until:raise ValueError('Disarm and stop viewing before changing settings')
                    self.cfg=common.validate_config(body,self.cfg);common.atomic(common.CONFIG,self.cfg);self.store.prune(self.cfg)
                elif op=='motion':self.motion=bool(body.get('enabled'))
                elif op=='ping':
                    if not self.deadline or now>=self.deadline:raise ValueError('Live session expired')
                    self.heartbeat=now
                elif op in ('start','arm','snapshot','record_start','offer'):
                    if not self.safe:raise ValueError(self.reason)
                    if now<self.cooldown:raise ValueError('Please wait five seconds before starting again')
                    self.suspended=False
                    if op=='start':
                        if not self.deadline:self.deadline=now+MAX_SESSION
                        self.heartbeat=now
                    elif op=='arm':
                        self.pause_auto=False;self.fault_blocked=False;self.save_policy()
                        if not self.armed_until:
                            self.auto_armed=bool(self.cfg['auto_arm'])
                            self.armed_until=now+(60 if self.auto_armed else self.cfg['armed_minutes']*60)
                    elif op=='snapshot':self.snapshot_id=uuid.uuid4().hex;self.transient_until=max(self.transient_until,now+15)
                    elif op=='record_start':
                        if self.record_id or self.worker().get('recording'):raise ValueError('Recording already requested or active')
                        self.record_id=uuid.uuid4().hex;self.transient_until=max(self.transient_until,now+self.cfg['max_clip_seconds']+15)
                    elif op=='offer':
                        if not common.enabled(resource='live') or self.negotiating:raise ValueError('Camera unavailable or already negotiating')
                        sdp=body.get('sdp')
                        if not isinstance(sdp,str) or len(sdp)>90000:raise ValueError('Invalid SDP')
                        self.negotiating=True
                else:raise ValueError('Unknown operation')
            if op=='offer':
                try:
                    payload=json.dumps({'sdp':sdp,'cameras':CAMERAS,'bridge_services_in':[],'bridge_services_out':[]}).encode()
                    with urlopen(Request('http://127.0.0.1:5002/stream',payload,{'Content-Type':'application/json'}),timeout=12) as r:answer=json.load(r)
                    if not common.enabled(resource='live'):raise ValueError('Live session ended during negotiation')
                    h.send_json(answer)
                finally:
                    with self.lock:self.negotiating=False
            else:h.send_json({'ok':True})
        except (BrokenPipeError,ConnectionResetError):pass
        except Exception as e:h.send_json({'error':str(e)},409)
        return True


def main():
    import asyncio
    from aiohttp import web
    from openpilot.system.webrtc.webrtcd import StreamSession
    sessions=[]
    async def dispose(session):
        if session.run_task:
            session.run_task.cancel()
            await asyncio.gather(session.run_task,return_exceptions=True)
        await session.post_run_cleanup()
    async def stream(request):
        if not common.enabled(resource='live') or sessions:raise web.HTTPConflict()
        body=await request.json()
        if body.get('cameras')!=CAMERAS or body.get('bridge_services_in') or body.get('bridge_services_out'):raise web.HTTPForbidden()
        session=StreamSession(body['sdp'],CAMERAS,[],[],False);sessions.append(session)
        try:
            answer=await asyncio.wait_for(session.get_answer(),10)
            if not common.enabled(resource='live'):raise web.HTTPConflict()
            session.start()
            return web.json_response({'sdp':answer.sdp,'type':answer.type})
        except BaseException:
            await dispose(session);sessions.remove(session);raise
    async def watch():
        while True:
            for session in list(sessions):
                if not common.enabled(resource='live') or session.run_task and session.run_task.done():
                    await dispose(session);sessions.remove(session)
            await asyncio.sleep(.5)
    async def startup(app):app['watch']=asyncio.create_task(watch())
    async def cleanup(app):
        app['watch'].cancel();await asyncio.gather(app['watch'],return_exceptions=True)
        for session in list(sessions):await dispose(session)
    app=web.Application(client_max_size=100000);app.router.add_post('/stream',stream)
    app.on_startup.append(startup);app.on_shutdown.append(cleanup)
    web.run_app(app,host='127.0.0.1',port=5002)

if __name__=='__main__':main()
