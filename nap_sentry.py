"""NAP Sentry: short-lived parked camera lease. No persistent Params or driving overrides."""
import json
import math
import os
import secrets
import threading
import time
from pathlib import Path
from urllib.request import Request, urlopen

LEASE = Path('/dev/shm/nap_sentry.json')
TOKEN = Path('/data/nap_sentry_pin')
CAMERAS = ['road', 'wideRoad', 'driver']
MAX_SESSION = 300
IDLE = 20


def enabled(started=False):
    if started:
        return False
    try:
        d = json.loads(LEASE.read_text())
        now = time.monotonic()
        return now < d['until'] <= now + 4 and now < d['deadline'] <= now + MAX_SESSION
    except (OSError, ValueError, KeyError, TypeError):
        return False


def safety(sm, now):
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
    if str(ds.thermalStatus) != 'green' or not math.isfinite(ds.maxTempC) or ds.maxTempC >= 70:
        return 'Device too warm'
    return ''


class Sentry:
    def __init__(self):
        self.lock = threading.RLock()
        self.deadline = self.heartbeat = 0
        self.reason = 'Waiting for safety telemetry'
        self.safe = False
        self.negotiating = False
        self.off()
        try:
            fd = os.open(TOKEN, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as f:
                f.write('000000')
        except FileExistsError:
            pass
        self.token = TOKEN.read_text().strip()
        if len(self.token) != 6 or not self.token.isascii() or not self.token.isdigit():
            raise ValueError('NAP Sentry PIN must contain six digits')

    def off(self):
        self.deadline = self.heartbeat = 0
        self.cooldown = time.monotonic() + 5
        LEASE.unlink(missing_ok=True)

    def watch(self):
        from cereal import messaging
        sm = messaging.SubMaster(['deviceState', 'pandaStates', 'peripheralState'])
        while True:
            try:
                sm.update(200)
                with self.lock:
                    now = time.monotonic()
                    self.reason = safety(sm, now)
                    self.safe = not self.reason
                    if self.deadline:
                        if self.reason or now >= self.deadline or now - self.heartbeat >= IDLE:
                            self.reason = self.reason or 'Session expired'
                            self.off()
                        else:
                            tmp = LEASE.with_suffix('.tmp')
                            tmp.write_text(json.dumps({'until': now + 3, 'deadline': self.deadline}))
                            tmp.replace(LEASE)
            except Exception:
                with self.lock:
                    self.safe = False
                    self.reason = 'Safety telemetry unavailable'
                    self.off()
            time.sleep(.2)

    def handle(self, h):
        path = h.path.split('?')[0]
        if path not in ('/sentry', '/sentry/', '/api/sentry/status', '/api/sentry/start', '/api/sentry/stop', '/api/sentry/ping', '/api/sentry/offer'):
            return False
        if path in ('/sentry', '/sentry/') and h.command == 'GET':
            h.send_bytes(PAGE.encode(), 'text/html; charset=utf-8')
            return True
        if not secrets.compare_digest(h.headers.get('Authorization', ''), 'Bearer ' + self.token):
            h.send_json({'error': 'Sentry PIN required'}, 401)
            return True
        origin = h.headers.get('Origin')
        from urllib.parse import urlparse
        if origin and urlparse(origin).netloc != h.headers.get('Host'):
            h.send_json({'error': 'Same origin required'}, 403)
            return True
        try:
            if h.command == 'GET' and path == '/api/sentry/status':
                with self.lock:
                    h.send_json({'active': enabled(), 'safe': self.safe, 'reason': self.reason,
                                 'remaining': max(0, int(self.deadline - time.monotonic()))})
                return True
            if h.command != 'POST':
                h.send_json({'error': 'Method not allowed'}, 405)
                return True
            n = int(h.headers.get('Content-Length', '0'))
            if not 0 < n <= 100000 or h.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                raise ValueError('Bounded JSON body required')
            body = json.loads(h.rfile.read(n))
            with self.lock:
                now = time.monotonic()
                if path == '/api/sentry/stop':
                    self.off()
                elif path == '/api/sentry/start':
                    if now < self.cooldown:
                        raise ValueError('Please wait five seconds before starting again')
                    if not self.safe:
                        raise ValueError(self.reason)
                    if not self.deadline:
                        self.deadline = now + MAX_SESSION
                    self.heartbeat = now
                elif path == '/api/sentry/ping':
                    if not self.deadline or now >= self.deadline:
                        raise ValueError('Session expired')
                    self.heartbeat = now
                elif path == '/api/sentry/offer':
                    if not enabled() or not self.safe or self.negotiating:
                        raise ValueError('Camera unavailable or already negotiating')
                    sdp = body.get('sdp')
                    if not isinstance(sdp, str) or len(sdp) > 90000:
                        raise ValueError('Invalid SDP')
                    self.negotiating = True
                else:
                    raise ValueError('Unknown operation')
            if path == '/api/sentry/offer':
                try:
                    payload = json.dumps({'sdp': sdp, 'cameras': CAMERAS, 'bridge_services_in': [], 'bridge_services_out': []}).encode()
                    with urlopen(Request('http://127.0.0.1:5002/stream', payload, {'Content-Type': 'application/json'}), timeout=12) as r:
                        answer = json.load(r)
                    if not enabled():
                        raise ValueError('Session ended during negotiation')
                    h.send_json(answer)
                finally:
                    with self.lock:
                        self.negotiating = False
            else:
                h.send_json({'ok': True})
        except Exception as e:
            h.send_json({'error': str(e)}, 409)
        return True


def main():
    """Separate loopback-only WebRTC process; manager kills it when the lease ends."""
    import asyncio
    from aiohttp import web
    from openpilot.system.webrtc.webrtcd import StreamSession
    sessions = []
    async def stream(request):
        if not enabled() or sessions:
            raise web.HTTPConflict()
        body = await request.json()
        if body.get('cameras') != CAMERAS or body.get('bridge_services_in') or body.get('bridge_services_out'):
            raise web.HTTPForbidden()
        session = StreamSession(body['sdp'], CAMERAS, [], [], False)
        sessions.append(session)
        try:
            answer = await asyncio.wait_for(session.get_answer(), 10)
            if not enabled():
                raise web.HTTPConflict()
            session.start()
            return web.json_response({'sdp': answer.sdp, 'type': answer.type})
        except BaseException:
            await session.post_run_cleanup()
            sessions.remove(session)
            raise
    async def cleanup(app):
        for session in sessions:
            if session.run_task:
                session.run_task.cancel()
            await session.post_run_cleanup()
    app = web.Application(client_max_size=100000)
    app.router.add_post('/stream', stream)
    app.on_shutdown.append(cleanup)
    web.run_app(app, host='127.0.0.1', port=5002)


PAGE = r'''<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>NAP Sentry</title><style>
:root{color-scheme:dark}*{box-sizing:border-box}body{background:#09141a;color:#eaf4f5;font:16px system-ui;max-width:1440px;margin:24px auto;padding:16px}h1{font-size:1.6rem}button,input,select{font:inherit;padding:12px;margin:4px;border:1px solid #34505a;border-radius:10px;background:#192a31;color:inherit}button{cursor:pointer;font-weight:700}button.primary{background:#55d6d1;color:#062027}input{width:145px}a{color:#55d6d1}label{display:inline-flex;align-items:center;gap:4px}label input[type=checkbox]{width:auto}.controls{display:flex;align-items:center;flex-wrap:wrap;gap:4px}.cameras{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,340px),1fr));gap:14px}.camera{background:#101b20;border:1px solid #29414a;border-radius:14px;padding:12px}.camera h2{font-size:.8rem;letter-spacing:.07em;text-transform:uppercase;margin:2px 0 12px}.camera[hidden]{display:none}video{width:100%;aspect-ratio:16/9;object-fit:contain;background:#000;border-radius:8px}.sub{color:#a5bdc5}.controls label{font-size:.85rem}</style></head><body>
<a href="/">← NAP Dash</a><h1>NAP Sentry</h1><p class="sub">Front · wide front · cabin · parked only · five minute maximum</p>
<div class="controls"><input id="key" type="password" inputmode="numeric" pattern="[0-9]{6}" maxlength="6" placeholder="6-digit PIN" autocomplete="off" aria-label="Sentry PIN"><label><input id="remember" type="checkbox" checked>Remember on this browser</label><button id="forget">Forget PIN</button><button id="start" class="primary">Start / view</button><button id="stop">Stop</button><select id="view" aria-label="Camera view"><option value="all">All cameras</option><option value="road">Front</option><option value="wideRoad">Wide front</option><option value="driver">Cabin</option></select></div>
<p id="status" role="status">Enter your PIN once. Remote video requires a direct Tailscale connection to the comma.</p><div class="cameras"><section class="camera" data-camera="road"><h2>Front</h2><video id="video-road" autoplay playsinline muted controls></video></section><section class="camera" data-camera="wideRoad"><h2>Wide front</h2><video id="video-wideRoad" autoplay playsinline muted controls></video></section><section class="camera" data-camera="driver"><h2>Cabin</h2><video id="video-driver" autoplay playsinline muted controls></video></section></div><script>
const el=id=>document.getElementById(id),cameras=['road','wideRoad','driver'],storageKey='nap-sentry-pin';let pc=null,running=false,busy=false,generation=0;
try{el('key').value=localStorage.getItem(storageKey)||''}catch(e){}
function savePin(){try{if(el('remember').checked)localStorage.setItem(storageKey,el('key').value);else localStorage.removeItem(storageKey)}catch(e){}}
el('remember').onchange=()=>{if(!el('remember').checked){try{localStorage.removeItem(storageKey)}catch(e){}}};
el('forget').onclick=()=>{try{localStorage.removeItem(storageKey)}catch(e){}el('key').value='';el('remember').checked=false;el('status').textContent='Saved PIN forgotten'};
el('view').onchange=()=>document.querySelectorAll('[data-camera]').forEach(tile=>tile.hidden=el('view').value!=='all'&&tile.dataset.camera!==el('view').value);
async function api(op,body){let r=await fetch('/api/sentry/'+op,{method:body?'POST':'GET',headers:{'Authorization':'Bearer '+el('key').value,'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});let d=await r.json();if(!r.ok)throw Error(d.error||r.status);return d}
function close(){generation++;running=false;if(pc){pc.onconnectionstatechange=null;pc.close()}pc=null;cameras.forEach(cam=>el('video-'+cam).srcObject=null)}
async function stop(){close();try{await api('stop',{});el('status').textContent='Stopped'}catch(e){el('status').textContent=e.message}}
el('stop').onclick=stop;
el('start').onclick=async()=>{if(busy||running)return;busy=true;try{if(!/^[0-9]{6}$/.test(el('key').value))throw Error('Enter a six-digit PIN');await api('start',{});savePin();running=true;const current=++generation;el('status').textContent='Starting cameras…';await new Promise(r=>setTimeout(r,4000));if(current!==generation)return;const peer=new RTCPeerConnection({iceServers:[]});pc=peer;const transceivers=cameras.map(()=>peer.addTransceiver('video',{direction:'recvonly'}));peer.ontrack=e=>{if(current!==generation)return;let cam=cameras.find(name=>e.track.id.startsWith(name+':'))||cameras[transceivers.indexOf(e.transceiver)];if(!cam)return;el('video-'+cam).srcObject=new MediaStream([e.track]);el('video-'+cam).play().catch(()=>{})};peer.onconnectionstatechange=()=>{if(current===generation&&['failed','closed'].includes(peer.connectionState))stop()};await peer.setLocalDescription(await peer.createOffer());await new Promise((resolve,reject)=>{if(peer.iceGatheringState==='complete')return resolve();let t=setTimeout(()=>reject(Error('ICE gathering timed out')),8000);peer.addEventListener('icegatheringstatechange',()=>{if(peer.iceGatheringState==='complete'){clearTimeout(t);resolve()}})});if(current!==generation)return;let answer=await api('offer',{sdp:peer.localDescription.sdp});if(current!==generation)return;await peer.setRemoteDescription(answer);el('status').textContent='Connecting cameras over your private network…';setTimeout(()=>{if(current===generation&&peer.connectionState!=='connected'){stop();el('status').textContent='Camera connection timed out. Check your private network connection.'}},20000)}catch(e){await stop();el('status').textContent=e.message}finally{busy=false}};
setInterval(async()=>{if(!running)return;try{await api('ping',{});let d=await api('status');if(!d.active){await stop();el('status').textContent=d.reason||'Stopped'}else el('status').textContent=(pc&&pc.connectionState==='connected'?'Live':'Connecting')+' · '+d.remaining+' seconds remaining'}catch(e){close();el('status').textContent=e.message}},5000);
window.addEventListener('pagehide',()=>{close();fetch('/api/sentry/stop',{method:'POST',headers:{'Authorization':'Bearer '+el('key').value,'Content-Type':'application/json'},body:'{}',keepalive:true}).catch(()=>{})});
</script></body></html>'''

if __name__ == '__main__':
    main()
