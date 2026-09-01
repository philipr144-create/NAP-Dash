#!/usr/bin/env python3
import json, threading, time, os, subprocess, glob, traceback
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
import cereal.messaging as messaging
from openpilot.common.params import Params

HOST, PORT = "0.0.0.0", 7070
SETTINGS_FILE = "/data/nap_settings.json"

PARAMS = {
    "personality": "LongitudinalPersonality",
    "follow_distance": "NAPFollowDistance",
    "adaptive_accel": "NAPAdaptiveAccel",
    "experimental": "ExperimentalMode"
}
PERSONALITIES = {0: "aggressive", 1: "standard", 2: "chill"}
STATE = {
    "ts":0, "car":{}, "drive":{}, "plan":{}, "lead1":{}, 
    "settings":{}, "health":{}, "errors":[]
}
LOCK = threading.Lock()
STOP = threading.Event()

def load_custom_settings():
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, "r") as f: return json.load(f)
    except: pass
    return {"speed_offset": False, "auto_resume": False}

def save_custom_settings(sd):
    try:
        with open(SETTINGS_FILE, "w") as f: json.dump(sd, f)
    except: pass

def num(v, d=0.0):
    try:
        if hasattr(v, 'raw'): v = v.raw
        x = float(v)
        return x if x == x and abs(x) != float("inf") else d
    except Exception: return d

def safe_int(v, d=0):
    try: return int(v.raw if hasattr(v, 'raw') else v)
    except Exception: return d

def safe_attr(obj, attr, default=0):
    if obj is None: return default
    try: return getattr(obj, attr, default)
    except Exception: return default

def lead_dict(lead):
    out = {}
    if lead is None: return out
    keys = ["status","dRel","yRel","vRel","vLead","aLeadK","aLeadTau","modelProb","radar","fcw"]
    for k in keys:
        try:
            v = getattr(lead, k)
            out[k] = v if isinstance(v, bool) else num(v)
        except: pass
    return out

def read_params():
    try:
        p = Params()
        r = {}
        try: r["personality_raw"] = int(p.get(PARAMS["personality"]))
        except: r["personality_raw"] = 1
        
        try: r["follow_distance"] = int(p.get(PARAMS["follow_distance"]))
        except: r["follow_distance"] = 4
        
        try: r["adaptive_accel"] = p.get_bool(PARAMS["adaptive_accel"])
        except: r["adaptive_accel"] = False
        
        try: r["experimental"] = p.get_bool(PARAMS["experimental"])
        except: r["experimental"] = False

        custom = load_custom_settings()
        r["speed_offset"] = bool(custom.get("speed_offset", False))
        r["auto_resume"] = bool(custom.get("auto_resume", False))
        
        r["personality"] = PERSONALITIES.get(r["personality_raw"], "unknown")
        return r
    except:
        return {"personality_raw": 1, "follow_distance": 4, 
                "adaptive_accel": False, "experimental": False, 
                "speed_offset": False, "auto_resume": False, "personality": "standard"}

def get_routes():
    routes_dict = {}
    base = "/data/media/0/realdata"
    if not os.path.exists(base): return []
    try:
        for f in os.listdir(base):
            path = os.path.join(base, f)
            if os.path.isdir(path) and "--" in f:
                route_name, _, seg = f.rpartition("--")
                if seg.isdigit():
                    if route_name not in routes_dict: routes_dict[route_name] = []
                    routes_dict[route_name].append(int(seg))
    except: pass
    routes = []
    for route, segs in routes_dict.items():
        routes.append({"name": route, "segs": sorted(segs)})
    routes.sort(key=lambda x: x["name"], reverse=True)
    return routes

def telemetry():
    svcs = [
        "carState", "selfdriveState", "controlsState", 
        "longitudinalPlan", "radarState", "deviceState", "modelV2"
    ]
    sm = None
    while svcs:
        try:
            sm = messaging.SubMaster(svcs)
            break
        except Exception as e:
            bad_srv = str(e.args[0]) if e.args else str(e)
            removed = False
            for s in svcs.copy():
                if s in bad_srv:
                    svcs.remove(s)
                    removed = True
            if not removed: return

    tick = 0
    while not STOP.is_set():
        try:
            sm.update(100)
            cs = sm["carState"] if "carState" in svcs else None
            sd = sm["selfdriveState"] if "selfdriveState" in svcs else None
            ctl = sm["controlsState"] if "controlsState" in svcs else None
            lp = sm["longitudinalPlan"] if "longitudinalPlan" in svcs else None
            radar = sm["radarState"] if "radarState" in svcs else None
            ds = sm["deviceState"] if "deviceState" in svcs else None
            mdl = sm["modelV2"] if "modelV2" in svcs else None
            
            if tick % 20 == 0: 
                current_settings = read_params()
            else: 
                current_settings = STATE.get("settings", {})

            enabled = safe_attr(sd, 'enabled', safe_attr(ctl, 'enabled', False))
            active = safe_attr(sd, 'active', safe_attr(ctl, 'active', False))
            exp_mode = safe_attr(sd, 'experimentalMode', False)
            pers_raw = safe_int(safe_attr(sd, 'personality', 1))
            v_cruise = num(safe_attr(cs, 'vCruise', 0))
            
            path_data = []
            if mdl is not None:
                try:
                    xs = getattr(mdl.position, 'x', [])
                    ys = getattr(mdl.position, 'y', [])
                    for i in range(0, min(len(xs), len(ys), 30), 2):
                        path_data.append([float(xs[i]), float(ys[i])])
                except: pass

            with LOCK:
                try:
                    uptime = float(open("/proc/uptime").read().split()[0])
                except Exception:
                    uptime = 0.0
                try:
                    st = os.statvfs("/data")
                    storage_total = st.f_frsize * st.f_blocks
                    storage_free = st.f_frsize * st.f_bavail
                    storage_used = max(0, storage_total - storage_free)
                    storage_pct = (storage_used / storage_total * 100.0) if storage_total else 0.0
                except Exception:
                    storage_total = storage_free = storage_used = 0
                    storage_pct = 0.0
                STATE.update({
                    "ts": time.time(),
                    "health": {"temp": max(safe_attr(ds, 'cpuTempC', [0])), "uptime": uptime,
                                "storageUsed": storage_used, "storageTotal": storage_total,
                                "storagePct": storage_pct},
                    "car": {
                        "vEgo": num(safe_attr(cs, 'vEgo', 0)),
                        "steer": num(safe_attr(cs, 'steeringAngleDeg', 0)),
                        "vCruise": v_cruise,
                        "brakePressed": bool(safe_attr(cs, 'brakePressed', False)),
                        "gasPressed": bool(safe_attr(cs, 'gasPressed', False)),
                        "leftBlinker": bool(safe_attr(cs, 'leftBlinker', False)),
                        "rightBlinker": bool(safe_attr(cs, 'rightBlinker', False))
                    },
                    "drive": {"active": active, "enabled": enabled},
                    "plan": {"path": path_data},
                    "lead1": lead_dict(safe_attr(radar, 'leadOne', None)),
                    "settings": current_settings
                })
            tick += 1
        except Exception: pass
        time.sleep(.05)

def write_setting(name, value):
    p = Params()
    if name == "personality":
        v = int(value)
        if v not in PERSONALITIES: raise ValueError("Invalid")
        p.put(PARAMS[name], v)
    elif name == "follow_distance":
        v = int(value)
        if not 1 <= v <= 7: raise ValueError("Invalid")
        p.put(PARAMS[name], v)
    elif name in ("adaptive_accel", "experimental"):
        try: p.put_bool(PARAMS[name], bool(value))
        except: p.put(PARAMS[name], 1 if bool(value) else 0)
    elif name in ("speed_offset", "auto_resume"):
        custom = load_custom_settings()
        custom[name] = bool(value)
        save_custom_settings(custom)
        
    time.sleep(0.05)
    return read_params()

def find_log_path(seg_dir):
    for f in ["rlog.zst", "rlog.bz2", "qlog.zst", "qlog.bz2"]:
        p = os.path.join(seg_dir, f)
        if os.path.exists(p): return p
    return None

def parse_telemetry_timeline(log_path):
    import sys
    if "/data/openpilot" not in sys.path: sys.path.insert(0, "/data/openpilot")
    from tools.lib.logreader import LogReader

    lr = LogReader(log_path)
    timeline, engagement = [], []
    vEgo, steer, gas, brake, lead_d = 0.0, 0.0, False, False, 0.0
    left_b, right_b, engaged = False, False, False
    t0 = None

    for msg in lr:
        try:
            w = msg.which()
            if w not in ("carState","radarState","selfdriveState","controlsState"):
                continue
            t = msg.logMonoTime / 1e9
            if t0 is None: t0 = t
            rel_t = t - t0
            if rel_t < 0 or rel_t > 61.0: continue

            if w == "carState":
                cs = msg.carState
                vEgo = getattr(cs, "vEgo", 0)
                steer = getattr(cs, "steeringAngleDeg", 0)
                gas = getattr(cs, "gasPressed", False)
                brake = getattr(cs, "brakePressed", False)
                left_b = getattr(cs, "leftBlinker", False)
                right_b = getattr(cs, "rightBlinker", False)
            elif w == "radarState":
                lead = getattr(msg.radarState, "leadOne", None)
                lead_d = getattr(lead, "dRel", 0) if (lead and getattr(lead, "status", False)) else 0
            elif w == "selfdriveState":
                sd = msg.selfdriveState
                active = getattr(sd, "active", None)
                if active is not None: engaged = bool(active)
            elif w == "controlsState":
                ctl = msg.controlsState
                active = getattr(ctl, "active", None)
                if active is not None and bool(active): engaged = True
                elif active is False: engaged = False

            expected_idx = int(rel_t * 10)
            while len(timeline) <= expected_idx and len(timeline) < 600:
                timeline.append([
                    round(vEgo * 2.23694, 1), round(steer, 1),
                    1 if gas else 0, 1 if brake else 0, round(lead_d, 1),
                    1 if left_b else 0, 1 if right_b else 0, 1 if engaged else 0
                ])
                engagement.append(1 if engaged else 0)
        except: continue

    if engagement:
        last = engagement[0]
        for i in range(len(engagement)):
            if engagement[i]: last = 1
            elif last: engagement[i] = 1
        for i, value in enumerate(engagement):
            if i < len(timeline): timeline[i][7] = value

    return timeline, engagement

def get_mp4_path(route_seg, cam_type="qcamera"):
    base_path = f"/data/media/0/realdata/{route_seg}/{cam_type}"
    cam_file = base_path + ".hevc"
    if not os.path.exists(cam_file): cam_file = base_path + ".ts"
    if not os.path.exists(cam_file): return None
      
    tmp_path = f"/dev/shm/vid_{route_seg}_{cam_type}.mp4"
    if not os.path.exists(tmp_path):
        subprocess.run(
            ["ffmpeg", "-y", "-i", cam_file, "-c", "copy", "-movflags", "faststart", tmp_path], 
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    return tmp_path if os.path.exists(tmp_path) else None

def serve_file_with_range(handler, path, content_type="video/mp4", attachment=None):
    try: file_size=os.path.getsize(path)
    except OSError: handler.send_error(404); return
    if file_size<=0: handler.send_error(404); return
    rh=handler.headers.get("Range"); start,end=0,file_size-1; status=200
    if rh:
        try:
            unit,_,rng=rh.partition("=")
            if unit.strip().lower()!="bytes": raise ValueError()
            first,_,last=rng.partition("-")
            if first.strip(): start=int(first); end=int(last) if last.strip() else file_size-1
            else:
                suffix=int(last)
                if suffix<=0: raise ValueError()
                start=max(0,file_size-suffix); end=file_size-1
            if start<0 or start>=file_size or end<start: raise ValueError()
            end=min(end,file_size-1);status=206
        except Exception:
            handler.send_response(416);handler.send_header("Content-Range",f"bytes */{file_size}");handler.send_header("Accept-Ranges","bytes");handler.end_headers();return
    length=end-start+1
    handler.send_response(status);handler.send_header("Content-Type",content_type);handler.send_header("Accept-Ranges","bytes");handler.send_header("Content-Length",str(length));handler.send_header("Cache-Control","no-cache")
    if status==206: handler.send_header("Content-Range",f"bytes {start}-{end}/{file_size}")
    if attachment:
        safe=os.path.basename(attachment).replace('"','');handler.send_header("Content-Disposition",f'attachment; filename="{safe}"')
    handler.end_headers()
    if getattr(handler,"command","GET")=="HEAD": return
    try:
        with open(path,"rb") as f:
            f.seek(start);remaining=length
            while remaining>0:
                chunk=f.read(min(262144,remaining))
                if not chunk: break
                handler.wfile.write(chunk);remaining-=len(chunk)
    except (BrokenPipeError,ConnectionResetError): pass


HTML = r"""<!doctype html><html><head><meta name=viewport content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>NAP Dash</title><style>
:root{--p:#111720;--line:#293241;--t:#f5f7fa;--m:#9aa7b7;--a:#56b6ff;--g:#34c759;}
*{box-sizing:border-box}
body{margin:0;background:#05070a;color:var(--t);font-family:-apple-system,BlinkMacSystemFont,"SF Pro Display",sans-serif;}
.wrap{max-width:980px;margin:auto;padding:10px}

.top{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}
.title{font-size:18px;font-weight:800}
.health-bar{font-size:11px;color:var(--m);display:flex;gap:10px;}
.nav-tabs{display:flex;gap:8px;margin-bottom:12px;background:#111720;padding:4px;border-radius:12px;border:1px solid var(--line)}
.nav-tabs button{flex:1;background:transparent;border:none;color:var(--m);padding:8px;font-weight:700;border-radius:8px;font-size:13px}
.nav-tabs button.active{background:#1a222e;color:var(--t)}

.cluster{position:relative;background:radial-gradient(circle at center, #111720 0%, #05070a 100%);border-radius:20px;border:1px solid #293241;overflow:hidden;display:flex;flex-direction:column;align-items:center;padding-top:15px;margin-bottom:15px;aspect-ratio:3/4;box-shadow:inset 0 0 40px #000;}
@media(min-width:600px){.cluster{aspect-ratio:16/9;}}
.cluster-top{display:flex;justify-content:space-between;width:100%;padding:0 20px;z-index:10;align-items:flex-start;}

.speed-block{text-align:center;display:flex;flex-direction:column;align-items:center;}
.speed-val{font-size:64px;font-weight:800;line-height:1;letter-spacing:-2px;text-shadow:0 0 20px rgba(255,255,255,0.2);}
.speed-unit{font-size:14px;color:var(--m);font-weight:700;}

.max-speed{display:flex;flex-direction:column;align-items:center;background:rgba(26,34,46,0.6);padding:6px 12px;border-radius:10px;border:1px solid #344153;backdrop-filter:blur(4px);}
.max-lbl{font-size:10px;color:var(--m);font-weight:800;}
.max-val{font-size:22px;font-weight:800;color:var(--a);text-shadow:0 0 10px rgba(86,182,255,0.4);}

.steer-block{display:flex;flex-direction:column;align-items:center;background:rgba(26,34,46,0.6);padding:6px 12px;border-radius:10px;border:1px solid #344153;backdrop-filter:blur(4px);}
.steer-val{font-size:22px;font-weight:800;color:#fff;}

.radar-canvas{position:absolute;inset:0;width:100%;height:100%;z-index:1;}
.hud-pedals{position:absolute;bottom:15px;left:15px;display:flex;gap:10px;z-index:10;}
.pedal{width:30px;height:8px;border-radius:4px;background:#344153;transition:0.1s;}
.pedal.brk.active{background:#ff3b30;box-shadow:0 0 10px #ff3b30;}
.pedal.gas.active{background:var(--g);box-shadow:0 0 10px var(--g);}

.turn-arrow{font-size:24px;color:#293241;transition:0.15s;}
.turn-arrow.active{color:var(--g);text-shadow:0 0 10px var(--g);}

.grid-layout{display:grid;grid-template-columns:1fr;gap:12px;}
@media(min-width:600px){.grid-layout{grid-template-columns:1fr 1fr;}}
.card{background:#111720;border:1px solid var(--line);border-radius:16px;padding:14px;}
.label{font-size:12px;color:var(--m);text-transform:uppercase;letter-spacing:.08em;margin-bottom:9px}
.buttons{display:flex;gap:8px}.btn{flex:1;border:1px solid #344153;background:#1a222e;color:var(--t);padding:10px 6px;border-radius:10px;font-weight:700;font-size:13px;}
.btn.active{background:#284d68;border-color:var(--a)}
.row{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:8px 0}.small{font-size:12px;color:var(--m)}
.switch{width:50px;height:28px;border-radius:16px;background:#303a47;border:0;position:relative;cursor:pointer;}.switch i{position:absolute;width:22px;height:22px;top:3px;left:3px;border-radius:50%;background:#fff;transition:.15s}.switch.on{background:var(--g)}.switch.on i{left:25px}
.follow{display:grid;grid-template-columns:repeat(7,1fr);gap:5px}.follow button{padding:10px 2px;border-radius:8px;border:1px solid #344153;background:#1a222e;color:#fff;font-weight:700}.follow button.active{background:#315b75;border-color:var(--a)}

.kv{display:grid;grid-template-columns:1fr 1fr;gap:5px;font-size:11px}.kv span{color:var(--m)}

/* Video */
.vid-container{width:100%;position:relative;aspect-ratio:16/9;background:#000;border-radius:16px;overflow:hidden;margin-bottom:12px;border:1px solid var(--line)}
video{width:100%;height:100%}
.hud-overlay{position:absolute;inset:0;pointer-events:none;padding:15px;display:none;flex-direction:column;justify-content:space-between;z-index:20;background:linear-gradient(180deg, rgba(0,0,0,0.5) 0%, transparent 20%, transparent 80%, rgba(0,0,0,0.6) 100%);}
.hud-top{display:flex;justify-content:space-between;align-items:center;}
.hud-bot{display:flex;justify-content:space-between;align-items:flex-end;}
.hud-box{background:#00000088;backdrop-filter:blur(4px);border:1px solid #ffffff33;padding:6px 12px;border-radius:10px;color:#fff;font-weight:800;font-family:"SF Pro Display",-apple-system,sans-serif;text-transform:uppercase;text-shadow:0 2px 4px #000;font-size:14px;display:flex;flex-direction:column;align-items:center;}
.hud-pedal-col{display:flex;flex-direction:column;align-items:center;gap:4px;}
.hud-pedal-label{font-size:9px;font-weight:800;letter-spacing:.06em;color:#8f9baa;text-shadow:0 1px 3px #000;}
.pedal.brk.active + .hud-pedal-label{color:#ff5d67;}
.pedal.gas.active + .hud-pedal-label{color:var(--g);}
.steer-wrap{width:100%; margin-top:4px; display:flex; justify-content:center;}
.steer-gauge{width:100px; height:6px; background:#293241; border-radius:3px; position:relative;}
.steer-center{position:absolute; width:2px; height:10px; background:#8f9baa; left:50%; top:-2px; transform:translateX(-50%);}
.steer-ind{position:absolute; width:12px; height:10px; background:#fff; border-radius:2px; top:-2px; left:50%; transform:translateX(-50%); transition:transform 0.1s ease-out;}
select.cam-drop{background:#1a222e;color:var(--t);border:1px solid #344153;padding:8px 12px;border-radius:10px;font-weight:700;font-size:13px;outline:none;width:100%;margin-bottom:10px;}
.route-item{background:#111720;border-radius:12px;padding:12px;margin-bottom:10px;border:1px solid var(--line)}
.segs-grid{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px;}
.seg-btn{background:#1a222e;border:1px solid #344153;color:var(--t);padding:8px 12px;border-radius:8px;font-size:12px;font-weight:700}
.seg-btn:active{background:var(--a)}.seg-btn.playing{background:var(--a);color:#000}
.btn-ui{text-decoration:none;padding:8px 14px;background:#1a222e;color:var(--t);border-radius:10px;font-weight:700;font-size:13px;border:1px solid #344153;cursor:pointer;}
.timeline{background:#111720;border:1px solid var(--line);border-radius:16px;padding:12px;margin-bottom:12px;}
.timeline-head{display:flex;justify-content:space-between;align-items:center;gap:10px;font-size:12px;color:var(--m);margin-bottom:8px;}
.timeline input[type=range]{width:100%;accent-color:var(--a);margin:0;}
.timeline-track{height:7px;border-radius:4px;background:#293241;position:relative;overflow:hidden;margin-top:7px;}
.engage-segment{position:absolute;top:0;bottom:0;background:var(--g);opacity:.8;}
.event-legend{display:flex;gap:14px;flex-wrap:wrap;margin-top:8px;font-size:10px;color:var(--m);}
.health-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:7px;margin-top:8px;}
.health-pill{background:#0b0f15;border:1px solid #293241;border-radius:9px;padding:7px;text-align:center;font-size:10px;color:var(--m);}
.health-pill b{display:block;color:var(--t);font-size:13px;margin-top:2px;}
@media(max-width:600px){.health-grid{grid-template-columns:repeat(2,1fr)}}
</style></head><body><div class=wrap>

<div class=top>
  <div class=title>NAP Dash</div>
  <div class=health-bar>
    <span>CPU: <b id="cpu-temp">—</b>°C</span>
    <span><b id="status">Wait</b></span>
  </div>
</div>

<div class="nav-tabs">
  <button id="tabbtn-drive" class="active" onclick="switchTab('drive')">Drive Dash</button>
  <button id="tabbtn-video" onclick="switchTab('video')">Dashcam Viewer</button>
</div>

<div id="tab-drive">
  
  <div class="cluster">
    <div class="cluster-top">
      <div id="arr-l" class="turn-arrow">◀</div>
      <div class="steer-block"><span class="max-lbl">STEER</span><span id="steer-val" class="steer-val">0°</span></div>
      <div class="speed-block">
        <div id="speed-val" class="speed-val">0</div>
        <div class="speed-unit">MPH</div>
      </div>
      <div class="max-speed"><span class="max-lbl">MAX</span><span id="max-val" class="max-val">--</span></div>
      <div id="arr-r" class="turn-arrow">▶</div>
    </div>
    
    <svg class="radar-canvas" viewBox="0 0 200 300" preserveAspectRatio="xMidYMax slice">
      <defs>
        <filter id="glow" x="-20%" y="-20%" width="140%" height="140%">
          <feGaussianBlur stdDeviation="3" result="blur" />
          <feMerge><feMergeNode in="blur" /><feMergeNode in="SourceGraphic" /></feMerge>
        </filter>
      </defs>
      <path d="M62 280 L78 85 L122 85 L138 280 Z" fill="#0b1017" stroke="#293241" stroke-width="1" opacity="0.95"/>
      <path d="M87 280 L96 85 M113 280 L104 85" stroke="#293241" stroke-width="1" stroke-dasharray="6 8" opacity="0.75"/>
      <path d="M74 220 L126 220 M79 165 L121 165 M84 115 L116 115" stroke="#293241" stroke-width="1" opacity="0.65"/>
      <circle cx="100" cy="280" r="50" fill="none" stroke="#293241" stroke-width="1"/>
      <circle cx="100" cy="280" r="100" fill="none" stroke="#293241" stroke-width="1"/>
      <circle cx="100" cy="280" r="150" fill="none" stroke="#293241" stroke-width="1"/>
      <circle cx="100" cy="280" r="200" fill="none" stroke="#293241" stroke-width="1"/>
      
      <!-- Actual Steering Angle Projection (Dashed Yellow) -->
      <path id="steer-path" fill="none" stroke="#ffcc00" stroke-width="4" stroke-dasharray="8 6" opacity="0.8" filter="url(#glow)" stroke-linecap="round"/>
      
      <!-- AI Predicted Path (Solid Blue/Green) -->
      <path id="ai-path" fill="none" stroke="#56b6ff" stroke-width="6" opacity="0.8" stroke-linecap="round" filter="url(#glow)"/>
      
      <g id="lead-grp" style="display:none; transition:transform 0.1s linear;">
        <path d="M-13,-8 L-9,-13 L9,-13 L13,-8 L11,10 L-11,10 Z" fill="#ff5d67" filter="url(#glow)"/>
        <path d="M-7,-10 L7,-10 L9,-3 L-9,-3 Z" fill="#24313d"/>
        <rect x="-12" y="5" width="4" height="4" fill="#fff"/><rect x="8" y="5" width="4" height="4" fill="#fff"/>
        <text id="lead-dist" x="0" y="-19" fill="#fff" font-size="11" font-weight="800" text-anchor="middle">--m</text>
      </g>
      <g transform="translate(100, 280)">
        <path d="M-10,-15 L10,-15 L12,10 L-12,10 Z" fill="#f5f7fa" filter="url(#glow)"/>
        <rect id="ego-brake" x="-10" y="10" width="6" height="4" fill="#ff3b30" opacity="0" filter="url(#glow)"/>
        <rect id="ego-brake2" x="4" y="10" width="6" height="4" fill="#ff3b30" opacity="0" filter="url(#glow)"/>
      </g>
    </svg>

    <div class="hud-pedals">
      <div id="pedal-brk" class="pedal brk"></div>
      <div id="pedal-gas" class="pedal gas"></div>
    </div>
  </div>

  <div class="grid-layout">
    <div class=card>
      <div class=label>Personality</div>
      <div class=buttons>
        <button class="btn p-btn" data-val="2" onclick="setv('personality',2)">Chill</button>
        <button class="btn p-btn" data-val="1" onclick="setv('personality',1)">Std</button>
        <button class="btn p-btn" data-val="0" onclick="setv('personality',0)">Aggr</button>
      </div>
      <br>
      <div class=row><div><b>Follow distance</b></div></div>
      <div class=follow id=follow></div>
    </div>
    
    <div class=card>
      <div class=label>Toggles</div>
      <div class=row><div><b>Experimental Mode</b></div><button id=exp class=switch onclick="toggle('experimental')"><i></i></button></div>
      <div class=row><div><b>Adaptive Accel</b></div><button id=ada class=switch onclick="toggle('adaptive_accel')"><i></i></button></div>
      <div class=row><div><b>+5 MPH Speed Offset</b></div><button id=spd class=switch onclick="toggle('speed_offset')"><i></i></button></div>
      <div class=row><div><b>Lateral Auto-Resume</b></div><button id=aut class=switch onclick="toggle('auto_resume')"><i></i></button></div>
    </div>
    
    <!-- Restored Lead Telemetry Box -->
    <div class=card>
      <div class=label>Lead Telemetry</div>
      <div id=l1 class=kv></div>
    </div>
  </div>
</div>

<div id="tab-video" style="display:none;">
  <div class="vid-container">
    <video id="player" playsinline autoplay></video>
    <div id="hud-overlay" class="hud-overlay">
      <div class="hud-top">
        <div id="hud-left-arrow" class="turn-arrow">◀</div>
        <div class="hud-box" style="color:var(--a);">LEAD: <span id="hud-lead">--</span> m</div>
        <div id="hud-right-arrow" class="turn-arrow">▶</div>
      </div>
      <div class="hud-bot">
        <div class="hud-box" style="font-size:22px"><span id="hud-speed">0</span> <span style="font-size:12px;color:var(--m)">MPH</span></div>
        <div style="display:flex;gap:14px;">
          <div class="hud-pedal-col">
            <div id="hud-brake" class="pedal brk"></div>
            <span class="hud-pedal-label">BRAKE</span>
          </div>
          <div class="hud-pedal-col">
            <div id="hud-gas" class="pedal gas"></div>
            <span class="hud-pedal-label">GAS</span>
          </div>
        </div>
        <div class="hud-box">
          STR: <span id="hud-steer">0</span>°
          <div class="steer-wrap">
            <div class="steer-gauge">
              <div class="steer-center"></div>
              <div id="steer-ind" class="steer-ind"></div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
  
  <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:15px; flex-wrap:wrap; gap:10px;">
    <div style="display:flex; gap:10px;">
      <select id="cam-select" class="cam-drop" onchange="if(currentRoute) playVid(currentRoute, currentSeg, $('player').currentTime)" style="margin:0;">
        <option value="qcamera">Road (Fast)</option>
        <option value="fcamera">Road (High-Res)</option>
        <option value="dcamera">Driver Cam</option>
      </select>
      <button class="btn-ui" onclick="toggleHud()">Toggle HUD</button>
    </div>
    <div style="display:flex; gap:10px; align-items:center;">
      <button class="btn-ui" onclick="togglePlay()" id="vid-play-btn" style="display:none;">Pause</button>
      <button class="btn-ui" onclick="exportWithHud()" id="export-btn" style="display:none;background:var(--g);color:#000;">Export w/ HUD ↓</button>
    </div>
  </div>
  
  <div id="routes-list"><div class="label" style="text-align:center;">Loading logs...</div></div>
</div>

<script>
let S={settings:{}};const $=x=>document.getElementById(x);
function switchTab(t){
  $('tab-drive').style.display = t==='drive'?'block':'none';
  $('tab-video').style.display = t==='video'?'block':'none';
  $('tabbtn-drive').classList.toggle('active', t==='drive');
  $('tabbtn-video').classList.toggle('active', t==='video');
  if(t==='video') loadRoutes();
}

let routesLoaded = false;
let currentRoute = null; let currentSeg = null;
let hudActive = false; let logData = []; let videoDuration = 0; 

function toggleHud(){
  hudActive = !hudActive;
  $('hud-overlay').style.display = hudActive ? 'flex' : 'none';
}
function togglePlay(){
  let v = $('player');
  if(v.paused) { v.play(); $('vid-play-btn').textContent = "Pause"; }
  else { v.pause(); $('vid-play-btn').textContent = "Play"; }
}

async function loadRoutes(){
  if(routesLoaded) return;
  try {
    let r = await fetch("/api/routes").then(x=>x.json());
    let h = r.length===0 ? "<div class='label'>No drives found.</div>" : "";
    r.forEach(rt => {
      let d = rt.name.includes("|") ? rt.name.split("|")[1].split("--") : rt.name.split("--");
      let readable = (d[0]||"Route") + " at " + (d[1]?d[1].replace(/-/g,":"):"");
      let btns = rt.segs.map(s => `<button class="seg-btn" onclick="playVid('${rt.name}',${s})">Seg ${s}</button>`).join("");
      h += `<div class="route-item"><b>${readable}</b><div class="segs-grid">${btns}</div></div>`;
    });
    $('routes-list').innerHTML = h; routesLoaded = true;
  } catch(e) { $('routes-list').innerHTML = "<div class='label'>Failed to load logs.</div>"; }
}

let logRequestId = 0;
async function fetchLogData(route, seg){
  const reqId = ++logRequestId; logData = [];
  $('hud-speed').textContent = "..."; $('hud-steer').textContent = "...";
  $('hud-lead').textContent = "..."; $('steer-ind').style.transform = `translateX(-50%)`;
  try {
    let r = await fetch(`/api/log/${route}--${seg}`);
    let j = await r.json();
    if(reqId !== logRequestId) return; 
    if(j.data && j.data.length) { logData = j.data; buildEngagementTrack(); }
    else { $('hud-speed').textContent = "--"; $('hud-lead').textContent = "--"; }
  } catch(e) {}
}

function playVid(route, seg, preserveTime=null){
  document.querySelectorAll('.seg-btn').forEach(b => b.classList.remove('playing'));
  currentRoute=route; currentSeg=seg;
  let cam=$('cam-select').value, vid=$('player'), oldTime=preserveTime===null?0:preserveTime;
  vid.src=`/stream/${route}--${seg}?cam=${cam}`; vid.load();
  vid.addEventListener('loadedmetadata',function restore(){
    vid.removeEventListener('loadedmetadata',restore); videoDuration=isFinite(vid.duration)?vid.duration:60;
    $('timeline-range').max=videoDuration.toFixed(1);
    if(oldTime>0) vid.currentTime=Math.min(oldTime,Math.max(0,videoDuration-0.1));
    vid.play().catch(()=>{});
  });
  $('vid-play-btn').style.display='inline-block'; $('vid-play-btn').textContent='Pause'; $('export-btn').style.display='inline-block';
  $('timeline-title').textContent=`${route} — Seg ${seg}`; fetchLogData(route,seg); window.scrollTo({top:0,behavior:'smooth'});
}

async function exportWithHud(){
  if(!currentRoute) return;
  let btn = $('export-btn'); let cam = $('cam-select').value;
  btn.disabled = true; btn.textContent = 'Exporting...';
  try {
    let r = await fetch(`/export/${currentRoute}--${currentSeg}?cam=${cam}`);
    if(!r.ok) throw new Error("Export failed");
    let blob = await r.blob();
    let url = URL.createObjectURL(blob);
    let a = document.createElement('a'); a.href = url;
    a.download = `Comma_Clip_${currentRoute}_${currentSeg}.mp4`.replace(/[|]/g, '_');
    document.body.appendChild(a); a.click(); a.remove();
  } catch(e) { alert('Export failed'); } 
  finally { btn.disabled = false; btn.textContent = 'Export w/ HUD ↓'; }
}

function fmtTime(t){t=Math.max(0,t||0);let m=Math.floor(t/60),s=Math.floor(t%60);return `${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;}
function seekTimeline(v){if($('player').readyState>=1)$('player').currentTime=+v;}
function buildEngagementTrack(){let track=$('engagement-track');track.innerHTML='';if(!logData.length)return;let run=null;logData.forEach((f,i)=>{let e=f[7]===1;if(e&&run===null)run=i;if(!e&&run!==null){let el=document.createElement('div');el.className='engage-segment';el.style.left=(run/logData.length*100)+'%';el.style.width=((i-run)/logData.length*100)+'%';track.appendChild(el);run=null;}});if(run!==null){let el=document.createElement('div');el.className='engage-segment';el.style.left=(run/logData.length*100)+'%';el.style.width=((logData.length-run)/logData.length*100)+'%';track.appendChild(el);}}

$('player').addEventListener('timeupdate', () => {
  let t=$('player').currentTime||0; $('timeline-range').value=t; $('timeline-time').textContent=`${fmtTime(t)} / ${fmtTime($('player').duration)}`;
  if(!hudActive || logData.length === 0) return;
  let idx=Math.floor(t*10);
  if(idx >= 0 && idx < logData.length){
    let f = logData[idx];
    $('hud-speed').textContent = f[0].toFixed(0);
    $('hud-steer').textContent = f[1].toFixed(1);
    let steerPx = Math.max(-50, Math.min(50, (-f[1] / 45.0) * 50));
    $('steer-ind').style.transform = `translateX(calc(-50% + ${steerPx}px))`;
    $('hud-gas').classList.toggle('active', f[2] === 1);
    $('hud-brake').classList.toggle('active', f[3] === 1);
    $('hud-lead').textContent = f[4] > 0 ? f[4].toFixed(1) : '--';
    $('hud-left-arrow').classList.toggle('active', f[5] === 1);
    $('hud-right-arrow').classList.toggle('active', f[6] === 1);
  }
});

function follow(v){let h="";for(let i=1;i<8;i++)h+=`<button class="${+v===i?'active':''}" onclick="setv('follow_distance',${i})">${i}</button>`;$("follow").innerHTML=h}
function sw(id,v){$(id).classList.toggle("on",!!v)}
function lb(id,l){if(!l||!l.status){$(id).innerHTML="<span>status</span><b>none</b>";return}let a=[["dRel","m"],["yRel","m"],["vRel","m/s"],["vLead","m/s"],["aLeadK","m/s²"],["fcw","fcw"]];$(id).innerHTML=a.map(x=>`<span>${x[0]}</span><b>${typeof l[x[0]]==="number"?l[x[0]].toFixed(2):l[x[0]]??"—"} ${x[1]}</b>`).join("")}

function render(s){
  try {
    S=s;
    $("status").textContent=s.drive?.active?"ENGAGED":s.drive?.enabled?"READY":"STANDBY";
    $("status").style.color=s.drive?.active?"var(--g)":"var(--m)";
    $("cpu-temp").textContent=Math.round(s.health?.temp||0);
    $("health-temp").textContent=Math.round(s.health?.temp||0)+"°C";
    let sp=Number(s.health?.storagePct||0); $("health-storage").textContent=sp?sp.toFixed(0)+"%":"—";
    let up=Number(s.health?.uptime||0); $("health-uptime").textContent=up?`${Math.floor(up/3600)}h ${Math.floor((up%3600)/60)}m`:"—";

    let q=s.settings||{},p=+q.personality_raw;follow(q.follow_distance);
    document.querySelectorAll(".p-btn").forEach(b=>b.classList.toggle("active",+b.dataset.val===p));
    sw("exp",q.experimental);sw("ada",q.adaptive_accel); sw("spd",q.speed_offset); sw("aut",q.auto_resume);
    
    let c = s.car || {};
    let pl = s.plan || {};
    let l = s.lead1 || {};
    
    let speedMph = (+c.vEgo) * 2.23694;
    $("speed-val").textContent = Math.round(speedMph);
    
    let maxMph = (+c.vCruise) * 0.621371;
    $("max-val").textContent = maxMph > 5 ? Math.round(maxMph) : "--";
    $("steer-val").textContent = Math.round(c.steer||0) + "°";

    $("arr-l").classList.toggle("active", c.leftBlinker);
    $("arr-r").classList.toggle("active", c.rightBlinker);
    $("pedal-brk").classList.toggle("active", c.brakePressed);
    $("pedal-gas").classList.toggle("active", c.gasPressed);
    
    $("ego-brake").style.opacity = c.brakePressed ? "1" : "0";
    $("ego-brake2").style.opacity = c.brakePressed ? "1" : "0";
    
    lb("l1", l);

    // AI Path Math (X:100 is center. Y:280 is ego base. Scale modified for 120m view).
    let pSvg = "M 100,280 ";
    if(pl.path && pl.path.length > 0) {
      pl.path.forEach(pt => {
        let py = 280 - (pt[0] * 2.3);
        let px = 100 + (pt[1] * 6.6); // + because Openpilot lateral is positive to the left
        pSvg += `L ${px.toFixed(1)},${py.toFixed(1)} `;
      });
      $("ai-path").setAttribute("d", pSvg);
      $("ai-path").setAttribute("stroke", s.drive?.active ? "var(--g)" : "#56b6ff");
    } else {
      $("ai-path").setAttribute("d", "");
    }
    
    // Steering Angle Projection Math
    let steerCurve = (c.steer || 0) * 2.5; 
    let sSvg = `M 100,280 Q ${100 + steerCurve/2},220 ${100 + steerCurve},140`;
    $("steer-path").setAttribute("d", sSvg);

    // Lead Car Math
    let lGrp = $("lead-grp");
    let dist = +l.dRel || 0;
    if(l.status && dist > 2.0) {
      let lat = +l.yRel || 0;
      let ly = 280 - (dist * 2.3);
      let lx = 100 + (lat * 6.6);
      ly = Math.max(20, Math.min(265, ly));
      lx = Math.max(20, Math.min(180, lx));
      let scale = Math.max(0.4, 1.0 - (dist / 120));
      lGrp.style.display = "block";
      lGrp.style.transform = `translate(${lx}px, ${ly}px) scale(${scale})`;
      $("lead-dist").textContent = dist.toFixed(1) + "m";
    } else {
      lGrp.style.display = "none";
    }
  } catch(e) {}
}

async function get(){
  try{
    let r=await fetch("/api/state",{cache:"no-store"});
    if(!r.ok)throw Error();
    render(await r.json());
  }catch(e){
    $("status").textContent="Disconnected";
    $("status").style.color="#ff5d67";
  }
}
async function setv(name,value){
  try{
    let r=await fetch("/api/set",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name,value})});
    render(await r.json());
  }catch(e){}
}
function toggle(n){setv(n,!S.settings[n])}
follow(4); get(); setInterval(get, 150);
</script></body></html>"""

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*a): pass
    def send_json(self, o, status=200):
        b = json.dumps(o, separators=(",", ":")).encode()
        self.send_response(status); self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-cache, no-store"); self.send_header("Content-Length", str(len(b)))
        self.end_headers(); self.wfile.write(b)
    def do_HEAD(self):
        p=urlparse(self.path).path
        if p.startswith("/stream/"):
            route_seg=p.split("/stream/",1)[1];cam=parse_qs(urlparse(self.path).query).get("cam",["qcamera"])[0]
            if cam not in ("qcamera","fcamera","dcamera"): return self.send_error(400,"Invalid camera")
            vid=get_mp4_path(route_seg,cam)
            if vid: return serve_file_with_range(self,vid,"video/mp4")
            return self.send_error(404,"Video not found")
        self.send_error(404)
    def do_GET(self):
        p = urlparse(self.path).path
        if p in ("/", "/index.html"):
            b = HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-store")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        elif p == "/api/state":
            with LOCK: o = json.loads(json.dumps(STATE))
            self.send_json(o)
        elif p == "/api/routes":
            self.send_json(get_routes())
        elif p.startswith("/api/log/"):
            route_seg = p.split("/api/log/")[1]
            cache = f"/dev/shm/rl_{route_seg}.json"
            if os.path.exists(cache):
                try:
                    with open(cache, "r") as f:
                        self.send_json(json.load(f))
                        return
                except: pass
            s_dir = f"/data/media/0/realdata/{route_seg}"
            log_p = None
            for fname in ["rlog.zst", "qlog.zst"]:
                cand = os.path.join(s_dir, fname)
                if os.path.exists(cand):
                    log_p = cand; break
            if not log_p: return self.send_json({"error": "not found"})
            try:
                tl, en = parse_telemetry_timeline(log_p)
                res = {"data": tl}
                if tl:
                    with open(cache, "w") as f: json.dump(res, f)
                self.send_json(res)
            except Exception as e:
                self.send_json({"error": str(e)})
        elif p.startswith("/stream/"):
            route_seg=p.split("/stream/",1)[1]
            cam=parse_qs(urlparse(self.path).query).get("cam",["qcamera"])[0]
            if cam not in ("qcamera","fcamera","dcamera"): return self.send_error(400,"Invalid camera")
            vid=get_mp4_path(route_seg,cam)
            if vid: serve_file_with_range(self,vid,"video/mp4")
            else: self.send_error(404,"Video not found")
        elif p.startswith("/export/"):
            route_seg = p.split("/export/")[1]
            cam = parse_qs(urlparse(self.path).query).get("cam", ["qcamera"])[0]
            self.handle_export(route_seg, cam)
        else:
            self.send_json({"error": "not found"}, 404)
            
    def handle_export(self, route_seg, cam_type):
        # Export with FFmpeg/ASS instead of pushing raw frames through Python.
        src_path = get_mp4_path(route_seg, cam_type)
        if not src_path: return self.send_json({"error": "no video"}, 404)
        seg_dir = f"/data/media/0/realdata/{route_seg}"
        log_path = next((os.path.join(seg_dir, f) for f in ("rlog.zst", "qlog.zst") if os.path.exists(os.path.join(seg_dir, f))), None)
        if not log_path: return self.send_json({"error": "no telemetry log"}, 404)
        out_path = f"/dev/shm/exp_{route_seg}_{cam_type}.mp4"
        ass_path = f"/dev/shm/hud_{route_seg}_{cam_type}.ass"
        try:
            if not os.path.exists(out_path) or os.path.getsize(out_path) < 1024:
                tl, en = parse_telemetry_timeline(log_path)
                if not tl: return self.send_json({"error": "no telemetry"}, 422)
                def ass_time(sec):
                    sec=max(0.0,float(sec)); whole=int(sec); cs=int(round((sec-whole)*100))
                    if cs>=100: whole+=1; cs=0
                    return f"{whole//3600}:{(whole%3600)//60:02d}:{whole%60:02d}.{cs:02d}"
                def ass_escape(text): return str(text).replace("\\","\\\\").replace("{","\\{").replace("}","\\}")
                styles = """[Script Info]
ScriptType: v4.00+
PlayResX: 1928
PlayResY: 1208
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: State,Arial,34,&H00FFFFFF,&H00000000,&H99000000,&H99000000,1,0,0,0,100,100,0,0,1,2,2,2,30,30,30,1
Style: Speed,Arial,72,&H00FFFFFF,&H00000000,&H99000000,&H99000000,1,0,0,0,100,100,0,0,1,3,3,2,30,30,70,1
Style: Lead,Arial,36,&H00FFB656,&H00000000,&H99000000,&H99000000,1,0,0,0,100,100,0,0,1,2,2,8,30,30,50,1
Style: Steer,Arial,30,&H00FFFFFF,&H00000000,&H99000000,&H99000000,1,0,0,0,100,100,0,0,1,2,2,1,30,30,55,1
Style: Brake,Arial,34,&H003B3BFF,&H00000000,&H99000000,&H99000000,1,0,0,0,100,100,0,0,1,2,2,3,30,30,60,1
Style: Gas,Arial,34,&H0034C759,&H00000000,&H99000000,&H99000000,1,0,0,0,100,100,0,0,1,2,2,3,30,30,120,1
Style: Arrow,Arial,44,&H0034C759,&H00000000,&H99000000,&H99000000,1,0,0,0,100,100,0,0,1,2,2,5,30,30,55,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
                lines=[]
                for i,fr in enumerate(tl[:600]):
                    a,b=ass_time(i/10),ass_time((i+1)/10)
                    state="ENGAGED" if (i<len(en) and en[i]) else "STANDBY"
                    lines.append(f"Dialogue: 0,{a},{b},State,,0,0,0,,{ass_escape(state)}")
                    lines.append(f"Dialogue: 1,{a},{b},Speed,,0,0,0,,{ass_escape(f'{fr[0]:.0f} MPH')}")
                    lines.append(f"Dialogue: 1,{a},{b},Lead,,0,0,0,,{ass_escape(f'LEAD {fr[4]:.1f} M' if fr[4]>0 else 'LEAD --')}")
                    lines.append(f"Dialogue: 1,{a},{b},Steer,,0,0,0,,{ass_escape(f'STR {fr[1]:+.1f}°')}")
                    if fr[3]: lines.append(f"Dialogue: 2,{a},{b},Brake,,0,0,0,,BRAKE")
                    if fr[2]: lines.append(f"Dialogue: 2,{a},{b},Gas,,0,0,0,,GAS")
                    if fr[5]: lines.append(f"Dialogue: 2,{a},{b},Arrow,,0,0,0,,◀")
                    if fr[6]: lines.append(f"Dialogue: 2,{a},{b},Arrow,,0,0,0,,▶")
                Path(ass_path).write_text(styles+"\n".join(lines)+"\n", encoding="utf-8")
                cmd=["ffmpeg","-y","-hide_banner","-loglevel","error","-i",src_path,"-vf",f"ass={ass_path}","-map","0:v:0","-map","0:a?","-c:v","libx264","-preset","veryfast","-crf","23","-pix_fmt","yuv420p","-c:a","copy","-movflags","+faststart",out_path]
                proc=subprocess.run(cmd,capture_output=True,text=True,timeout=180)
                if proc.returncode!=0 or not os.path.exists(out_path):
                    return self.send_json({"error":"ffmpeg export failed","detail":proc.stderr[-1200:]},500)
            serve_file_with_range(self,out_path,"video/mp4",f"Comma_Clip_{route_seg}_{cam_type}.mp4")
        except subprocess.TimeoutExpired:
            self.send_json({"error":"export timed out"},504)
        except Exception as e:
            self.send_json({"error":f"export failed: {e}"},500)
        finally:
            try:
                if os.path.exists(ass_path): os.unlink(ass_path)
            except Exception: pass

    def do_POST(self):
        if urlparse(self.path).path != "/api/set":
            return self.send_json({"error": "not found"}, 404)
        try:
            n = int(self.headers.get("Content-Length", "0"))
            d = json.loads(self.rfile.read(n).decode())
            settings = write_setting(str(d.get("name")), d.get("value"))
            with LOCK:
                STATE["settings"] = settings 
                o = json.loads(json.dumps(STATE))
            self.send_json(o)
        except Exception as e:
            self.send_json({"error": str(e)}, 400)

def main():
    print(f"NAP Drive Panel listening on port {PORT}")
    threading.Thread(target=telemetry, daemon=True).start()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    try: srv.serve_forever()
    except KeyboardInterrupt: pass
    finally:
        STOP.set()
        srv.server_close()

if __name__ == "__main__":
    main()
