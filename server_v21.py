#!/usr/bin/env python3
import json, threading, time, os, subprocess, glob, traceback
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
import cereal.messaging as messaging
from openpilot.common.params import Params

HOST = os.environ.get("NAP_SERVER_HOST", "0.0.0.0")
PORT = int(os.environ.get("NAP_SERVER_PORT", "7070"))

PARAMS = {
    "personality": "LongitudinalPersonality",
    "follow_distance": "NAPFollowDistance",
    "adaptive_accel": "NAPAdaptiveAccel",
    "experimental": "ExperimentalMode"
}
PERSONALITIES = {0: "aggressive", 1: "standard", 2: "chill"}
STATE = {
    "ts":0, "car":{}, "drive":{}, "plan":{}, "lead1":{}, "tracks":[],
    "settings":{}, "health":{}, "errors":[], "engagement":{},
    "bms": {"bricks": [0]*96, "temps_dict": {}, "pack_v": 0, "pack_i": 0, "ui_soc": 0, "rated_range": 0, "nom_full": 0.0, "nom_rem": 0.0, "buffer": 0.0, "max_discharge": 0, "max_regen": 0},
    "navigation": {
        "connected": False, "source": "", "route_id": "", "route_state": "inactive",
        "sequence": 0, "received_mono": 0.0, "maneuver": {
            "type": "", "modifier": "", "distance_m": 0.0,
            "primary_text": "", "secondary_text": ""
        },
        "distance_remaining_m": 0.0, "time_remaining_s": 0.0,
        "eta_unix_ms": 0, "speed_limit_mps": 0.0, "coordinates": []
    }
}
LOCK = threading.Lock()
STOP = threading.Event()

# Single global Params instance and lock to prevent cache desync and ZMQ thread crashes
pm = Params()
PM_LOCK = threading.Lock()

NAV_MAX_BODY = 256 * 1024
NAV_STALE_SECONDS = 5.0

def clean_text(value, limit=160):
    return str(value or "").strip()[:limit]

def clean_float(value, default=0.0, low=None, high=None):
    value = num(value, default)
    if low is not None: value = max(low, value)
    if high is not None: value = min(high, value)
    return value

def navigation_snapshot():
    """Return a JSON-safe nav snapshot and expire stale phone data."""
    with LOCK:
        nav = STATE["navigation"]
        if nav["connected"] and time.monotonic() - nav["received_mono"] > NAV_STALE_SECONDS:
            nav["connected"] = False
            nav["route_state"] = "stale"
        return json.loads(json.dumps(nav))

def update_navigation(payload):
    if not isinstance(payload, dict): raise ValueError("JSON object required")
    maneuver = payload.get("maneuver") or {}
    if not isinstance(maneuver, dict): raise ValueError("maneuver must be an object")
    coordinates = payload.get("coordinates")
    with LOCK:
        old = STATE["navigation"]
        seq = safe_int(payload.get("sequence"), old["sequence"] + 1)
        if old["connected"] and seq < old["sequence"]:
            raise ValueError("sequence moved backwards")
        route_state = clean_text(payload.get("route_state", old["route_state"]), 24).lower()
        if route_state not in ("inactive", "calculating", "active", "recalculating", "arrived", "error"):
            raise ValueError("invalid route_state")
        old.update({
            "connected": True, "source": clean_text(payload.get("source", "pixel3"), 32),
            "route_id": clean_text(payload.get("route_id", old["route_id"]), 96),
            "route_state": route_state, "sequence": seq, "received_mono": time.monotonic(),
            "maneuver": {
                "type": clean_text(maneuver.get("type"), 32),
                "modifier": clean_text(maneuver.get("modifier"), 32),
                "distance_m": clean_float(maneuver.get("distance_m"), 0, 0, 100000),
                "primary_text": clean_text(maneuver.get("primary_text"), 120),
                "secondary_text": clean_text(maneuver.get("secondary_text"), 160),
            },
            "distance_remaining_m": clean_float(payload.get("distance_remaining_m"), 0, 0, 10000000),
            "time_remaining_s": clean_float(payload.get("time_remaining_s"), 0, 0, 604800),
            "eta_unix_ms": safe_int(payload.get("eta_unix_ms"), 0),
            "speed_limit_mps": clean_float(payload.get("speed_limit_mps"), 0, 0, 100),
        })
        if coordinates is not None:
            if not isinstance(coordinates, list) or len(coordinates) > 10000:
                raise ValueError("coordinates must be a list of at most 10000 points")
            cleaned = []
            for point in coordinates:
                if not isinstance(point, (list, tuple)) or len(point) < 2: continue
                lat, lon = clean_float(point[0]), clean_float(point[1])
                if -90 <= lat <= 90 and -180 <= lon <= 180: cleaned.append([lat, lon])
            old["coordinates"] = cleaned
        return json.loads(json.dumps(old))

def clear_navigation():
    with LOCK:
        nav = STATE["navigation"]
        nav.update({"connected": False, "route_id": "", "route_state": "inactive",
                    "received_mono": 0.0, "maneuver": {"type": "", "modifier": "",
                    "distance_m": 0.0, "primary_text": "", "secondary_text": ""},
                    "distance_remaining_m": 0.0, "time_remaining_s": 0.0,
                    "eta_unix_ms": 0, "speed_limit_mps": 0.0, "coordinates": []})
        return json.loads(json.dumps(nav))

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


# NAP_SPEED_JSON_BRIDGE_V3
NAP_SETTINGS_FILE = "/data/nap_settings.json"
NAP_SETTINGS_LOCK = threading.Lock()

def read_nap_settings_file():
    try:
        with NAP_SETTINGS_LOCK:
            if os.path.isfile(NAP_SETTINGS_FILE):
                with open(NAP_SETTINGS_FILE, "r") as f:
                    data = json.load(f)

                if isinstance(data, dict):
                    return data
    except Exception:
        pass

    return {}

def update_nap_settings_file(**updates):
    """Preserve other NAP settings and update this file atomically."""
    with NAP_SETTINGS_LOCK:
        data = {}

        try:
            if os.path.isfile(NAP_SETTINGS_FILE):
                with open(NAP_SETTINGS_FILE, "r") as f:
                    current = json.load(f)

                if isinstance(current, dict):
                    data.update(current)
        except Exception:
            pass

        data.update(updates)
        temporary = NAP_SETTINGS_FILE + ".tmp"

        try:
            with open(temporary, "w") as f:
                json.dump(
                    data,
                    f,
                    separators=(",", ":"),
                    sort_keys=True
                )
                f.flush()
                os.fsync(f.fileno())

            os.replace(temporary, NAP_SETTINGS_FILE)
        finally:
            try:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            except Exception:
                pass

def nap_bool(value, default=False):
    if isinstance(value, bool):
        return value

    if isinstance(value, (int, float)):
        return value != 0

    if isinstance(value, str):
        value = value.strip().lower()

        if value in ("1", "true", "yes", "on"):
            return True

        if value in ("0", "false", "no", "off", ""):
            return False

    return default

def read_params():
    try:
        r = {}
        with PM_LOCK:
            try: r["personality_raw"] = int(pm.get(PARAMS["personality"]))
            except: r["personality_raw"] = 1
            
            try: r["follow_distance"] = int(pm.get(PARAMS["follow_distance"]))
            except: r["follow_distance"] = 4
            
            try: r["adaptive_accel"] = pm.get_bool(PARAMS["adaptive_accel"])
            except: r["adaptive_accel"] = False
            
            try: r["experimental"] = pm.get_bool(PARAMS["experimental"])
            except: r["experimental"] = False

            nap_file = read_nap_settings_file()

            try:
                if "speed_offset" in nap_file:
                    r["speed_offset"] = nap_bool(
                        nap_file["speed_offset"]
                    )
                else:
                    r["speed_offset"] = pm.get_bool(
                        "NAPSpeedOffset"
                    )
            except Exception:
                r["speed_offset"] = False

            try:
                if "speed_trim" in nap_file:
                    r["speed_trim"] = float(
                        nap_file["speed_trim"]
                    )
                else:
                    r["speed_trim"] = float(
                        pm.get("NAPSpeedTrim")
                    )
            except Exception:
                r["speed_trim"] = 0.0
            
        r["personality"] = PERSONALITIES.get(r["personality_raw"], "unknown")
        return r
    except:
        return {"personality_raw": 1, "follow_distance": 4, 
                "adaptive_accel": False, "experimental": False, 
                "speed_offset": False, "speed_trim": 0.0, "personality": "standard"}

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
        "longitudinalPlan", "radarState", "deviceState", "modelV2", "can"
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
    stats_file = "/data/nap_miles.json"
    engage_counts = {"manual": 0.0, "lat": 0.0, "long": 0.0, "both": 0.0}
    try:
        with open(stats_file, "r") as f:
            saved_data = json.load(f)
            engage_counts.update(saved_data)
    except: pass

    last_time = time.monotonic()

    while not STOP.is_set():
        try:
            sm.update(100)
            
            now = time.monotonic()
            dt = now - last_time
            last_time = now
            if dt > 1.0 or dt < 0: dt = 0

            cs = sm["carState"] if "carState" in svcs else None
            sd = sm["selfdriveState"] if "selfdriveState" in svcs else None
            ctl = sm["controlsState"] if "controlsState" in svcs else None
            radar = sm["radarState"] if "radarState" in svcs else None
            ds = sm["deviceState"] if "deviceState" in svcs else None
            mdl = sm["modelV2"] if "modelV2" in svcs else None
            
            if tick % 20 == 0: 
                current_settings = read_params()
                with LOCK:
                    STATE["settings"] = current_settings
            if tick % 100 == 0:
                try:
                    with open(stats_file, "w") as f: json.dump(engage_counts, f)
                except: pass

            enabled = safe_attr(sd, 'enabled', safe_attr(ctl, 'enabled', False))
            active = safe_attr(sd, 'active', safe_attr(ctl, 'active', False))
            v_cruise = num(safe_attr(cs, 'vCruise', 0))
            v_ego = num(safe_attr(cs, 'vEgo', 0))
            
            dist_mi = (v_ego * dt) * 0.000621371
            
            path_data = {"ego": [], "lanes": [], "edges": [], "leads": []}
            if mdl is not None:
                try:
                    xs = getattr(mdl.position, 'x', [])
                    ys = getattr(mdl.position, 'y', [])
                    for i in range(0, min(len(xs), len(ys), 30), 2):
                        path_data["ego"].append([float(xs[i]), float(ys[i])])
                except: pass
                
                try:
                    if hasattr(mdl, 'laneLines') and hasattr(mdl, 'laneLineProbs'):
                        for idx, line in enumerate(mdl.laneLines):
                            prob = float(mdl.laneLineProbs[idx])
                            if prob > 0.3:
                                pts = []
                                l_xs = getattr(line, 'x', [])
                                l_ys = getattr(line, 'y', [])
                                for i in range(0, min(len(l_xs), len(l_ys), 30), 3):
                                    pts.append([float(l_xs[i]), float(l_ys[i])])
                                path_data["lanes"].append({"pts": pts, "prob": prob, "idx": idx})
                except: pass
                
                try:
                    if hasattr(mdl, 'roadEdges') and hasattr(mdl, 'roadEdgeStds'):
                        for idx, edge in enumerate(mdl.roadEdges):
                            std = float(mdl.roadEdgeStds[idx])
                            pts = []
                            r_xs = getattr(edge, 'x', [])
                            r_ys = getattr(edge, 'y', [])
                            for i in range(0, min(len(r_xs), len(r_ys), 30), 3):
                                pts.append([float(r_xs[i]), float(r_ys[i])])
                            path_data["edges"].append({"pts": pts, "std": std})
                except: pass

                try:
                    if hasattr(mdl, 'leadsV3'):
                        for l in mdl.leadsV3:
                            prob = float(getattr(l, 'prob', 0))
                            if prob > 0.1:
                                l_xs = getattr(l, 'x', [0])
                                l_ys = getattr(l, 'y', [0])
                                l_vs = getattr(l, 'v', [0])
                                path_data["leads"].append({
                                    "x": float(l_xs[0]), "y": float(l_ys[0]), 
                                    "v": float(l_vs[0]), "prob": prob
                                })
                except: pass

            if not path_data["leads"] and radar is not None:
                for l_name in ['leadOne', 'leadTwo']:
                    ld = getattr(radar, l_name, None)
                    if ld and getattr(ld, 'status', False):
                        v_lead = float(getattr(ld, 'vLead', getattr(ld, 'vRel', 0) + v_ego))
                        path_data["leads"].append({
                            "x": float(getattr(ld, 'dRel', 0)),
                            "y": float(getattr(ld, 'yRel', 0)),
                            "v": v_lead,
                            "prob": 1.0
                        })

            gear_str = str(safe_attr(cs, 'gearShifter', '')).lower()
            in_drive = ('drive' in gear_str) or (v_ego > 0.5)

            if active:
                engage_counts["both"] += dist_mi
            elif in_drive:
                engage_counts["manual"] += dist_mi

            with LOCK:
                try:
                    uptime = float(open("/proc/uptime").read().split()[0])
                except:
                    uptime = 0.0
                try:
                    st = os.statvfs("/data")
                    storage_total = st.f_frsize * st.f_blocks
                    storage_free = st.f_frsize * st.f_bavail
                    storage_used = max(0, storage_total - storage_free)
                    storage_pct = (storage_used / storage_total * 100.0) if storage_total else 0.0
                except:
                    storage_total = storage_free = storage_used = 0
                    storage_pct = 0.0
                    
                STATE.update({
                    "ts": time.time(),
                    "health": {"temp": max(safe_attr(ds, 'cpuTempC', [0])), "battery": num(safe_attr(ds, "batteryPercent", 0)), "uptime": uptime,
                                "storageUsed": storage_used, "storageTotal": storage_total,
                                "storagePct": storage_pct},
                    "car": {
                        "vEgo": v_ego,
                        "aEgo": num(safe_attr(cs, 'aEgo', 0)),
                        "steer": num(safe_attr(cs, 'steeringAngleDeg', 0)),
                        "vCruise": v_cruise,
                        "brakePressed": bool(safe_attr(cs, 'brakePressed', False)),
                        "gasPressed": bool(safe_attr(cs, 'gasPressed', False)),
                        "leftBlinker": bool(safe_attr(cs, 'leftBlinker', False)),
                        "rightBlinker": bool(safe_attr(cs, 'rightBlinker', False))
                    },
                    "drive": {"active": active, "enabled": enabled},
                    "plan": path_data,
                    "lead1": lead_dict(safe_attr(radar, 'leadOne', None)),
                    "tracks": [],
                    "engagement": {
                        "manual": engage_counts.get("manual", 0.0),
                        "both": engage_counts.get("both", 0.0)
                    }
                })
                
                if "can" in svcs and sm.updated.get("can", False):
                    try:
                        STATE["bms"].setdefault("temps_dict", {})
                        for msg in sm["can"]:
                            addr = msg.address
                            d = msg.dat
                            
                            if (addr == 0x132 or addr == 0x102) and len(d) >= 4:
                                STATE["bms"]["pack_v"] = (d[0] | (d[1] << 8)) * 0.01
                                raw_i = d[2] | (d[3] << 8)
                                if raw_i >= 32768: raw_i -= 65536
                                STATE["bms"]["pack_i"] = raw_i * 0.1
                                
                            elif addr == 0x302 and len(d) >= 3:
                                STATE["bms"]["ui_soc"] = ((d[1] >> 2) | ((d[2] & 0x0F) << 6)) * 0.1
                                
                            elif addr == 0x338 and len(d) >= 2:
                                STATE["bms"]["rated_range"] = d[0] | (d[1] << 8)
                                
                            elif addr == 0x6F2 and len(d) >= 8:
                                mux = d[0]
                                if mux <= 31 and d[1:8] != b"\xff" * 7:
                                    bits = int.from_bytes(bytes(d[1:8]), "little")
                                    vals = [(bits >> (14 * k)) & 0x3FFF for k in range(4)]
                                    if mux < 24:
                                        idx = mux * 4
                                        for k, raw in enumerate(vals):
                                            if raw in (0, 0x3FFF): continue
                                            STATE["bms"]["bricks"][idx + k] = raw * 0.305175
                                    else:
                                        base = (mux - 24) * 4
                                        for k, raw in enumerate(vals):
                                            if raw == 0x3FFF: continue
                                            if raw & 0x2000: raw -= 0x4000
                                            t = raw * 0.0122
                                            if -50 < t < 120:
                                                STATE["bms"]["temps_dict"][f"{base+k}"] = t
                                                
                            elif addr == 0x382 and len(d) >= 8:
                                # Current legacy Model S firmware layout:
                                # five consecutive 11-bit energy fields,
                                # followed by a 9-bit energy buffer.
                                bits = int.from_bytes(
                                    bytes(d[:8]),
                                    "little"
                                )

                                nom_full = (
                                    (bits >> 0) & 0x7FF
                                ) * 0.1

                                nom_rem = (
                                    (bits >> 11) & 0x7FF
                                ) * 0.1

                                expected_rem = (
                                    (bits >> 22) & 0x7FF
                                ) * 0.1

                                ideal_rem = (
                                    (bits >> 33) & 0x7FF
                                ) * 0.1

                                charge_complete = (
                                    (bits >> 44) & 0x7FF
                                ) * 0.1

                                buffer = (
                                    (bits >> 55) & 0x1FF
                                ) * 0.1

                                plausible = (
                                    20.0 <= nom_full <= 120.0 and
                                    0.0 <= nom_rem <= nom_full + 2.0 and
                                    0.0 <= expected_rem <= nom_full + 10.0 and
                                    0.0 <= ideal_rem <= nom_full + 10.0 and
                                    0.0 <= buffer < nom_full and
                                    buffer <= 20.0
                                )

                                if plausible:
                                    usable_full = max(
                                        0.0,
                                        nom_full - buffer
                                    )

                                    usable_rem = max(
                                        0.0,
                                        min(
                                            usable_full,
                                            nom_rem - buffer
                                        )
                                    )

                                    display_soc = (
                                        usable_rem /
                                        usable_full *
                                        100.0
                                        if usable_full > 0.0
                                        else 0.0
                                    )

                                    STATE["bms"]["nom_full"] = nom_full
                                    STATE["bms"]["nom_rem"] = nom_rem
                                    STATE["bms"]["expected_rem"] = expected_rem
                                    STATE["bms"]["ideal_rem"] = ideal_rem
                                    STATE["bms"]["charge_complete"] = charge_complete
                                    STATE["bms"]["buffer"] = buffer
                                    STATE["bms"]["usable_full"] = usable_full
                                    STATE["bms"]["usable_rem"] = usable_rem
                                    STATE["bms"]["display_soc"] = max(
                                        0.0,
                                        min(100.0, display_soc)
                                    )
                                
                            elif addr == 0x252 and len(d) >= 4:
                                STATE["bms"]["max_regen"] = (d[0] | (d[1] << 8)) * 0.01
                                STATE["bms"]["max_discharge"] = (d[2] | (d[3] << 8)) * 0.01
                                
                    except Exception: pass

                valid_bricks = [v for v in STATE["bms"]["bricks"] if v > 2000]
                if valid_bricks:
                    STATE["bms"]["min_v"] = min(valid_bricks)
                    STATE["bms"]["max_v"] = max(valid_bricks)
                
                valid_temps = [t for t in STATE["bms"]["temps_dict"].values() if -40 < t < 120]
                if valid_temps:
                    STATE["bms"]["min_t"] = min(valid_temps)
                    STATE["bms"]["max_t"] = max(valid_temps)

            tick += 1
        except Exception: pass
        time.sleep(.05)

def write_setting(name, value):
    with PM_LOCK:
        if name == "personality":
            v = int(value)
            if v not in PERSONALITIES: raise ValueError("Invalid")
            pm.put(PARAMS[name], v)
            
        elif name == "follow_distance":
            v = int(value)
            if not 1 <= v <= 7: raise ValueError("Invalid")
            pm.put(PARAMS[name], v)
            
        elif name in ("adaptive_accel", "experimental"):
            pm.put_bool(PARAMS[name], bool(value))
            
        elif name == "speed_trim":
            v = max(-15.0, min(15.0, float(value)))

            # This JSON file is consumed by the Pre-AP car code.
            update_nap_settings_file(speed_trim=v)

            # These keys are not registered on every openpilot fork.
            try:
                pm.put("NAPSpeedTrim", str(v))
            except Exception:
                pass

        elif name == "speed_offset":
            v = nap_bool(value)

            # This JSON file is consumed by the Pre-AP car code.
            update_nap_settings_file(speed_offset=v)

            # These keys are not registered on every openpilot fork.
            try:
                pm.put_bool("NAPSpeedOffset", v)
            except Exception:
                pass
            
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
:root{--p:#10151d;--line:#293241;--t:#f5f7fa;--m:#9aa7b7;--a:#56b6ff;--g:#34c759;--nav:#78d6ff;--warn:#ffb340;}
*{box-sizing:border-box}
body{margin:0;background:#05070a;color:var(--t);font-family:-apple-system,BlinkMacSystemFont,"SF Pro Display",sans-serif;}
.wrap{max-width:980px;margin:auto;padding:10px}

.top{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}
.title{font-size:18px;font-weight:800}
.health-bar{font-size:11px;color:var(--m);display:flex;gap:10px;}
.nav-tabs{display:flex;gap:8px;margin-bottom:12px;background:#111720;padding:4px;border-radius:12px;border:1px solid var(--line)}
.nav-tabs button{flex:1;background:transparent;border:none;color:var(--m);padding:8px;font-weight:700;border-radius:8px;font-size:13px}
.nav-tabs button.active{background:#1a222e;color:var(--t)}

.cluster{position:relative;background:radial-gradient(ellipse at 50% 25%,#152230 0%,#080c12 48%,#030507 100%);border-radius:22px;border:1px solid #293241;overflow:hidden;display:flex;flex-direction:column;align-items:center;padding-top:15px;margin-bottom:15px;width:100%;height:72vh;min-height:500px;box-shadow:inset 0 0 60px #000,0 16px 60px #0008;}
.cluster-top{display:flex;justify-content:space-between;width:100%;padding:0 20px;z-index:10;align-items:flex-start;}

.nav-guidance{position:absolute;left:18px;top:86px;z-index:12;width:min(390px,calc(100% - 36px));display:grid;grid-template-columns:72px 1fr;gap:13px;padding:13px 15px;background:linear-gradient(135deg,#132232ee,#0b1119e8);border:1px solid #6fcfff55;border-radius:18px;box-shadow:0 12px 38px #0009,0 0 24px #56b6ff14;opacity:0;transform:translateY(-8px);transition:opacity .28s ease,transform .28s ease;pointer-events:none;}
.nav-guidance.on{opacity:1;transform:translateY(0)}
.nav-guidance.stale{opacity:.48;filter:saturate(.4)}
.nav-arrow{height:66px;border-radius:15px;background:#56b6ff18;color:var(--nav);display:flex;align-items:center;justify-content:center;font-size:48px;font-weight:800;text-shadow:0 0 18px #56b6ffaa;transition:transform .25s ease}
.nav-guidance.changed .nav-arrow{animation:nav-pop .45s ease}
@keyframes nav-pop{0%{transform:scale(.72)}60%{transform:scale(1.12)}100%{transform:scale(1)}}
.nav-copy{min-width:0;display:flex;flex-direction:column;justify-content:center}
.nav-distance{font-size:13px;color:var(--nav);font-weight:850;letter-spacing:.05em;text-transform:uppercase}
.nav-primary{font-size:23px;font-weight:850;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;line-height:1.2;margin-top:2px}
.nav-secondary{font-size:12px;color:var(--m);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:4px}
.nav-trip{position:absolute;right:18px;bottom:17px;z-index:12;display:flex;gap:8px;align-items:center;background:#070b11cc;border:1px solid #ffffff18;border-radius:13px;padding:8px 11px;font-size:12px;color:var(--m);backdrop-filter:blur(5px);opacity:0;transition:opacity .25s}
.nav-trip.on{opacity:1}.nav-trip b{color:#fff;font-size:13px}.nav-dot{width:7px;height:7px;border-radius:50%;background:var(--g);box-shadow:0 0 9px var(--g)}.nav-dot.stale{background:var(--warn);box-shadow:0 0 9px var(--warn)}

.speed-block{text-align:center;display:flex;flex-direction:column;align-items:center;}
.speed-val{font-size:64px;font-weight:800;line-height:1;letter-spacing:-2px;text-shadow:0 0 20px rgba(255,255,255,0.2);}
.speed-unit{font-size:14px;color:var(--m);font-weight:700;}

.max-speed{display:flex;flex-direction:column;align-items:center;background:rgba(26,34,46,0.6);padding:6px 12px;border-radius:10px;border:1px solid #344153;backdrop-filter:blur(4px);}
.max-lbl{font-size:10px;color:var(--m);font-weight:800;}
.max-val{font-size:22px;font-weight:800;color:var(--a);text-shadow:0 0 10px rgba(86,182,255,0.4);}

.steer-block{display:flex;flex-direction:column;align-items:center;background:rgba(26,34,46,0.6);padding:6px 12px;border-radius:10px;border:1px solid #344153;backdrop-filter:blur(4px);}
.steer-val{font-size:22px;font-weight:800;color:#fff;}

.radar-canvas{position:absolute;inset:0;width:100%;height:100%;z-index:1;}
.radar-canvas path { transition:d .1s linear,opacity .18s ease,fill .25s ease; }

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

/* Modern Power HUD */
.power-hud { background:#0b0f15; padding:15px; border-radius:12px; border:1px solid #293241; margin-top:10px; }
.power-labels { display:flex; justify-content:space-between; font-size:10px; font-weight:800; color:var(--m); margin-bottom:8px;}
.power-bar-container { display:flex; align-items:center; height:16px; background:#111720; border-radius:8px; overflow:hidden; position:relative; box-shadow:inset 0 0 5px #000; border:1px solid #1f2733;}
.power-side { flex:1; height:100%; position:relative; }
.power-side.left { border-right:1px solid #344153; }
.power-center-mark { position:absolute; left:50%; width:2px; height:100%; background:#fff; z-index:10; transform:translateX(-50%); }
.lim-bar { position:absolute; height:100%; background:#293241; transition:width 0.2s;}
.left .lim-bar { right:0; }
.right .lim-bar { left:0; }
.act-bar { position:absolute; height:100%; transition:width 0.1s ease-out; z-index:5;}
.left .act-bar { right:0; background:var(--g); box-shadow:0 0 10px var(--g); }
.right .act-bar { left:0; background:#ff5d67; box-shadow:0 0 10px #ff5d67; }

/* BMS Bricks */
.brick { aspect-ratio:1; border-radius:3px; background:#1a222e; display:flex; align-items:center; justify-content:center; font-size:10px; font-weight:800; color:#fff; font-family:monospace; }
@media(max-width:600px){ .brick { font-size:8px; } }

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
@media(max-width:650px){.cluster{height:68vh;min-height:460px}.nav-guidance{top:82px;grid-template-columns:58px 1fr}.nav-arrow{height:56px;font-size:39px}.nav-primary{font-size:19px}.nav-trip{left:18px;right:auto}.steer-block{display:none}}


/* NAP CINEMATIC WORKING-BASE V1 */
.wrap{max-width:1320px}
.cluster{height:min(72vh,740px);min-height:560px;padding-top:18px;border-color:#203850;background:#03070c;box-shadow:inset 0 0 90px #000b,0 20px 70px #0009}
.cluster-top{padding:0 24px}
.radar-canvas{background:#03070c}
.scene-lane{fill:none;stroke:#edf7ff;stroke-linecap:round;vector-effect:non-scaling-stroke}
.scene-edge{fill:none;stroke:#657b91;stroke-dasharray:9 9;vector-effect:non-scaling-stroke}
.scene-plan-center{fill:none;stroke-linecap:round;stroke-dasharray:18 14;vector-effect:non-scaling-stroke;animation:nap-road-flow .65s linear infinite}
.scene-lead{transition:transform .1s linear;will-change:transform}
.scene-lead-label{fill:#fff;font-size:15px;font-weight:850;text-anchor:middle;paint-order:stroke;stroke:#06101a;stroke-width:5px}
.scene-lead-sub{fill:#b9c9d9;font-size:10px;font-weight:800;text-anchor:middle;paint-order:stroke;stroke:#06101a;stroke-width:4px}
.scene-ego-body{fill:#dce9f4;stroke:#fff;stroke-width:2}.scene-ego-glass{fill:#24384c}.scene-ego-light{fill:#6a141b}
@keyframes nap-road-flow{from{stroke-dashoffset:32}to{stroke-dashoffset:0}}
@media(max-width:650px){.cluster{height:68vh;min-height:470px}.cluster-top{padding:0 12px}}

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
  <button id="tabbtn-bms" onclick="switchTab('bms')">BMS Data</button>
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

    <div id="nav-guidance" class="nav-guidance">
      <div id="nav-arrow" class="nav-arrow">↑</div>
      <div class="nav-copy">
        <div id="nav-distance" class="nav-distance">Navigation ready</div>
        <div id="nav-primary" class="nav-primary">Waiting for phone</div>
        <div id="nav-secondary" class="nav-secondary">Open the Pixel test page to begin</div>
      </div>
    </div>
    <div id="nav-trip" class="nav-trip"><span id="nav-dot" class="nav-dot"></span><b id="nav-remaining">--</b><span id="nav-eta">PHONE NAV</span></div>
    
    <svg class="radar-canvas" viewBox="0 0 960 540" preserveAspectRatio="xMidYMid slice">
      <defs>
        <linearGradient id="nap-sky" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stop-color="#020509"/><stop offset=".56" stop-color="#112033"/><stop offset="1" stop-color="#07101a"/>
        </linearGradient>
        <linearGradient id="nap-road" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stop-color="#17273a"/><stop offset="1" stop-color="#070c13"/>
        </linearGradient>
        <linearGradient id="nap-plan-blue" x1="0" y1="1" x2="0" y2="0">
          <stop offset="0" stop-color="#56b6ff" stop-opacity=".60"/><stop offset="1" stop-color="#56b6ff" stop-opacity="0"/>
        </linearGradient>
        <linearGradient id="nap-plan-green" x1="0" y1="1" x2="0" y2="0">
          <stop offset="0" stop-color="#34c759" stop-opacity=".62"/><stop offset="1" stop-color="#34c759" stop-opacity="0"/>
        </linearGradient>
      </defs>

      <rect width="960" height="540" fill="url(#nap-sky)"/>
      <!-- Ground and shoulders: simple shapes keep MCU2 rendering inexpensive. -->
      <path d="M0 166 L430 166 L30 540 L0 540 Z" fill="#08121b"/>
      <path d="M530 166 L960 166 L960 540 L930 540 Z" fill="#08121b"/>
      <path d="M430 166 L530 166 L930 540 L30 540 Z" fill="url(#nap-road)"/>

      <!-- Pale shoulder strips make the roadway readable without a wireframe grid. -->
      <path d="M426 166 L434 166 L48 540 L20 540 Z" fill="#9aa9b5" opacity=".58"/>
      <path d="M526 166 L534 166 L940 540 L912 540 Z" fill="#9aa9b5" opacity=".58"/>
      <path d="M434 166 L438 166 L64 540 L48 540 Z" fill="#f0f4f7" opacity=".72"/>
      <path d="M522 166 L526 166 L912 540 L896 540 Z" fill="#f0f4f7" opacity=".72"/>

      <!-- Subtle pavement wear adds depth but does not compete with detected lanes. -->
      <path d="M466 166 L356 540" stroke="#02060a" stroke-width="30" opacity=".18"/>
      <path d="M494 166 L604 540" stroke="#02060a" stroke-width="30" opacity=".18"/>
      <path d="M0 166 L960 166" stroke="#416078" stroke-width="2" opacity=".42"/>

      <g id="road-grp"></g>
      <g id="track-grp"></g>

      <g id="scene-ego" transform="translate(480 478)">
        <path class="scene-ego-body" d="M-70 18 L-58-34 Q-50-66 0-72 Q50-66 58-34 L70 18 Q71 39 50 44 L-50 44 Q-71 39-70 18Z"/>
        <path class="scene-ego-glass" d="M-39-35 Q-30-57 0-60 Q30-57 39-35 L28-18 L-28-18Z"/>
        <rect id="scene-brake-l" class="scene-ego-light" x="-51" y="13" width="22" height="8" rx="3"/>
        <rect id="scene-brake-r" class="scene-ego-light" x="29" y="13" width="22" height="8" rx="3"/>
        <rect id="scene-turn-l" x="-60" y="4" width="7" height="11" rx="3" fill="#493b19"/>
        <rect id="scene-turn-r" x="53" y="4" width="7" height="11" rx="3" fill="#493b19"/>
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
      <div class=row><div><b>Speed Trim</b><div class=small id="trim-val">0 MPH</div></div>
      <div style="display:flex;gap:5px">
        <button class="btn-ui" onclick="trim(-1)">−1</button>
        <button class="btn-ui" onclick="trim(1)">+1</button>
      </div></div>
    </div>
    
    <div class=card>
      <div class=label>Remote Cockpit</div>
      <div class=health-grid>
        <div class=health-pill>BATTERY<b id="battery">--%</b></div>
        <div class=health-pill>BRAKE<b id="brake-state">OFF</b></div>
        <div class=health-pill>REGEN<b id="regen-state">OFF</b></div>
        <div class=health-pill>ENGAGED<b id="engage-state">--</b></div>
      </div>
      <div style="margin-top:12px;font-size:11px;color:var(--m);font-weight:800;display:flex;justify-content:space-between;letter-spacing:0.05em;">
        <span>MANUAL <b id="eng-man-txt" style="color:#fff;font-size:14px;margin-left:4px;">--%</b></span>
        <span>AUTO <b id="eng-aut-txt" style="color:var(--g);font-size:14px;margin-left:4px;">--%</b></span>
      </div>
      <div style="height:10px;background:#293241;border-radius:5px;margin-top:6px;overflow:hidden;display:flex">
        <div id="eng-man-bar" style="height:100%;background:#56b6ff;width:0%;transition:width 0.3s"></div>
        <div id="eng-aut-bar" style="height:100%;background:var(--g);width:0%;transition:width 0.3s"></div>
      </div>
    </div>

    <div class=card>
      <div class=label>Lead Telemetry</div>
      <div id=l1 class=kv></div>
    </div>
  </div>
</div>

<div id="tab-bms" style="display:none; padding:5px;">
  <div class="card" style="margin-bottom:12px;">
    <div class="label">Powertrain Output</div>
    <div style="display:flex; justify-content:space-between; align-items:flex-end;">
      <div style="font-size:42px; font-weight:800; color:var(--a); line-height:1;"><span id="bms-kw">0.0</span> <span style="font-size:16px;color:var(--m)">kW</span></div>
      <div style="text-align:right;">
        <div style="font-size:16px; font-weight:700;"><span id="bms-pack-v">0.0</span> V</div>
        <div style="font-size:16px; font-weight:700; color:var(--m)"><span id="bms-pack-i">0.0</span> A</div>
      </div>
    </div>
    
    <div class="power-hud">
       <div class="power-labels">
         <span id="txt-max-regen" style="color:var(--g)">-0 kW</span>
         <span>0 kW</span>
         <span id="txt-max-dis" style="color:#ff5d67">+0 kW</span>
       </div>
       <div class="power-bar-container">
         <div class="power-side left">
           <div id="bar-regen-lim" class="lim-bar"></div>
           <div id="bar-regen-act" class="act-bar"></div>
         </div>
         <div class="power-center-mark"></div>
         <div class="power-side right">
           <div id="bar-dis-lim" class="lim-bar"></div>
           <div id="bar-dis-act" class="act-bar"></div>
         </div>
       </div>
    </div>
  </div>

  <div class="grid-layout">
    <div class="card">
      <div class="label">Battery Health</div>
      <div class="kv">
        <span>Raw BMS SOC</span><b id="bms-soc">0.0 %</b>
        <span>Rated Range</span><b id="bms-range">0 mi</b>
        <span>Nominal Full</span><b id="bms-nom-full">0.0 kWh</b>
        <span>Nom. Remain</span><b id="bms-nom-rem">0.0 kWh</b>
        <span>Energy Buffer</span><b id="bms-buffer">0.0 kWh</b>
        <!-- NAP_EXTENDED_BMS_UI_V1 -->
        <span>Usable / Dash SOC</span><b id="bms-display-soc">0.0 %</b>
        <span>Usable Full</span><b id="bms-usable-full">0.0 kWh</b>
        <span>Usable Remaining</span><b id="bms-usable-rem">0.0 kWh</b>
        <span>Expected Remaining</span><b id="bms-expected-rem">0.0 kWh</b>
        <span>Ideal Remaining</span><b id="bms-ideal-rem">0.0 kWh</b>
        <span>Energy to Full</span><b id="bms-to-full">0.0 kWh</b>
        <span>Capacity Est. (77.5 baseline)</span><b id="bms-capacity-health">0.0 %</b>
        <span>Signed Pack Power</span><b id="bms-pack-power">0.0 kW</b>
        <span>Average Cell</span><b id="bms-cell-avg">0.000 V</b>
        <span>Cell Spread</span><b id="bms-cell-spread">0 mV</b>
        <span>Average Temperature</span><b id="bms-temp-avg">0.0 °F</b>
        <span>Temperature Spread</span><b id="bms-temp-spread">0.0 °F</b>
      </div>
    </div>
    <div class="card">
      <div class="label">Thermal & Balance</div>
      <div class="kv">
        <span>Min Temp</span><b id="bms-min-t">0.0 °F</b>
        <span>Max Temp</span><b id="bms-max-t">0.0 °F</b>
        <span>Imbalance</span><b id="bms-delta-v">0 mV</b>
        <span>Cell Range</span><b id="bms-cell-range">0.00 - 0.00 V</b>
      </div>
    </div>
  </div>

  <div class="card" style="margin-top:12px;">
    <div class="label" style="display:flex;justify-content:space-between">
      <span>96-Brick Cell Voltages</span>
      <span id="bms-brick-avg" style="color:var(--t)">AVG: 0.00 V</span>
    </div>
    <div id="brick-grid" style="display:grid; grid-template-columns:repeat(16, 1fr); gap:4px; margin-top:12px;"></div>
    <div style="display:flex; justify-content:space-between; margin-top:12px; font-size:10px; color:var(--m); font-weight:700;">
      <span style="display:flex; align-items:center;"><div style="width:10px;height:10px;background:#ff5d67;margin-right:6px;border-radius:2px;"></div> Low (-10mV)</span>
      <span style="display:flex; align-items:center;"><div style="width:10px;height:10px;background:var(--g);margin-right:6px;border-radius:2px;"></div> Balanced</span>
      <span style="display:flex; align-items:center;"><div style="width:10px;height:10px;background:#56b6ff;margin-right:6px;border-radius:2px;"></div> High (+10mV)</span>
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
  
  <div class="timeline">
    <div class="timeline-head">
      <span id="timeline-title">Select a drive</span>
      <span id="timeline-time">00:00 / 00:00</span>
    </div>
    <input id="timeline-range" type="range" min="0" max="0" step="0.1" value="0" oninput="seekTimeline(this.value)">
    <div id="engagement-track" class="timeline-track"></div>
    <div class="event-legend"><span>● OpenPilot engaged</span><span>Drag to scrub</span></div>
  </div>
  <div id="routes-list"><div class="label" style="text-align:center;">Loading logs...</div></div>
</div>

<script>
let S={settings:{}};const $=x=>document.getElementById(x);
let VIS={speed:0,speedTarget:0,steer:0,steerTarget:0};
function animateTelemetry(){
  VIS.speed += (VIS.speedTarget - VIS.speed) * .16;
  VIS.steer += (VIS.steerTarget - VIS.steer) * .18;
  let sp=$("speed-val"),st=$("steer-val");
  if(sp)sp.textContent=Math.round(VIS.speed);
  if(st)st.textContent=Math.round(VIS.steer)+"°";
  requestAnimationFrame(animateTelemetry);
}
function switchTab(t){
  $('tab-drive').style.display = t==='drive'?'block':'none';
  $('tab-video').style.display = t==='video'?'block':'none';
  $('tab-bms').style.display = t==='bms'?'block':'none';
  $('tabbtn-drive').classList.toggle('active', t==='drive');
  $('tabbtn-video').classList.toggle('active', t==='video');
  $('tabbtn-bms').classList.toggle('active', t==='bms');
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
      let routeStr = rt.name.includes("|") ? rt.name.split("|")[1] : rt.name;
      let dateMatch = routeStr.match(/(\d{4}-\d{2}-\d{2})--(\d{2}-\d{2}-\d{2})/);
      let readable = dateMatch ? `${dateMatch[1]} at ${dateMatch[2].replace(/-/g, ':')}` : routeStr;
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
  vid.src=`/stream/${encodeURIComponent(route)}--${seg}?cam=${cam}`; vid.load();
  vid.addEventListener('loadedmetadata',function restore(){
    vid.removeEventListener('loadedmetadata',restore); videoDuration=isFinite(vid.duration)?vid.duration:60;
    $('timeline-range').max=videoDuration.toFixed(1);
    if(oldTime>0) vid.currentTime=Math.min(oldTime,Math.max(0,videoDuration-0.1));
    vid.play().catch(()=>{});
  });
  $('vid-play-btn').style.display='inline-block'; $('vid-play-btn').textContent='Pause'; $('export-btn').style.display='inline-block';
  
  let routeStr = route.includes("|") ? route.split("|")[1] : route;
  let dateMatch = routeStr.match(/(\d{4}-\d{2}-\d{2})--(\d{2}-\d{2}-\d{2})/);
  let titleStr = dateMatch ? `${dateMatch[1]} at ${dateMatch[2].replace(/-/g, ':')}` : routeStr;
  $('timeline-title').textContent=`${titleStr} — Seg ${seg}`; 
  fetchLogData(route,seg); window.scrollTo({top:0,behavior:'smooth'});
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
    
    let dateMatch = currentRoute.match(/(\d{4}-\d{2}-\d{2})--(\d{2}-\d{2}-\d{2})/);
    let cleanName = dateMatch ? `${dateMatch[1]}_${dateMatch[2]}` : currentRoute.replace(/[|]/g, '_');
    a.download = `Comma_Clip_${cleanName}_Seg${currentSeg}.mp4`;
    
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
    let steerPx = Math.max(-50, Math.min(50, ((f[1] || 0) / 45.0) * 50));
    $('steer-ind').style.transform = `translateX(calc(-50% + ${steerPx}px))`;
    $('hud-gas').classList.toggle('active', f[2] === 1);
    $('hud-brake').classList.toggle('active', f[3] === 1);
    $('hud-lead').textContent = f[4] > 0 ? f[4].toFixed(1) : '--';
    $('hud-left-arrow').classList.toggle('active', f[5] === 1);
    $('hud-right-arrow').classList.toggle('active', f[6] === 1);
  }
});

function initFollow() {
  let h = "";
  for (let i = 1; i < 8; i++) {
    h += `<button class="f-btn" data-val="${i}" onclick="setv('follow_distance',${i})">${i}</button>`;
  }
  $("follow").innerHTML = h;
}
function sw(id,v){$(id).classList.toggle("on",!!v)}
function lb(id,l){if(!l||!l.status){$(id).innerHTML="<span>status</span><b>none</b>";return}let a=[["dRel","m"],["yRel","m"],["vRel","m/s"],["vLead","m/s"],["aLeadK","m/s²"],["fcw","fcw"]];$(id).innerHTML=a.map(x=>`<span>${x[0]}</span><b>${typeof l[x[0]]==="number"?l[x[0]].toFixed(2):l[x[0]]??"—"} ${x[1]}</b>`).join("")}

function renderBMS(b) {
  if(!b) return;
  let pv = b.pack_v || 0; let pi = b.pack_i || 0; 
  let pkw = -(pv * pi) / 1000.0; 
  
  let elPackV = $("bms-pack-v"); if(elPackV) elPackV.textContent = pv.toFixed(1);
  let elPackI = $("bms-pack-i"); if(elPackI) elPackI.textContent = pi.toFixed(1);
  
  let elKw = $("bms-kw"); 
  if(elKw) { 
      elKw.textContent = Math.abs(pkw).toFixed(1); 
      elKw.style.color = pkw < 0 ? "var(--g)" : "#ff5d67"; 
  }
  
  let maxD = b.max_discharge || 0; let maxR = b.max_regen || 0;
  let elTxtD = $("txt-max-dis"); if(elTxtD) elTxtD.textContent = "+" + maxD.toFixed(0) + " kW";
  let elTxtR = $("txt-max-regen"); if(elTxtR) elTxtR.textContent = "-" + maxR.toFixed(0) + " kW";
  
  let boundR = Math.max(60, maxR); 
  let boundD = Math.max(160, maxD); 
  
  let limPctR = Math.min(100, (maxR / boundR) * 100);
  let limPctD = Math.min(100, (maxD / boundD) * 100);
  let barLimR = $("bar-regen-lim"); if(barLimR) barLimR.style.width = limPctR + "%";
  let barLimD = $("bar-dis-lim"); if(barLimD) barLimD.style.width = limPctD + "%";
  
  let actR = $("bar-regen-act"); let actD = $("bar-dis-act");
  if(actR && actD) {
      if (pkw < 0) {
          let pct = Math.min(100, (Math.abs(pkw) / boundR) * 100);
          actR.style.width = pct + "%"; actD.style.width = "0%";
      } else {
          let pct = Math.min(100, (pkw / boundD) * 100);
          actD.style.width = pct + "%"; actR.style.width = "0%";
      }
  }

  let elSoc = $("bms-soc"); if(elSoc) elSoc.textContent = (b.ui_soc || 0).toFixed(1) + " %";
  let elRan = $("bms-range"); if(elRan) elRan.textContent = (b.rated_range || 0) + " mi";
  
  let elNom = $("bms-nom-full"); if(elNom) elNom.textContent = (b.nom_full || 0).toFixed(1) + " kWh";
  let elRem = $("bms-nom-rem"); if(elRem) elRem.textContent = (b.nom_rem || 0).toFixed(1) + " kWh";
  let elBuff = $("bms-buffer"); if(elBuff) elBuff.textContent = (b.buffer || 0).toFixed(1) + " kWh";

  // NAP_EXTENDED_BMS_RENDERER_V1
  const finiteBms = (value, fallback=0) => {
    const number = Number(value);
    return Number.isFinite(number) ? number : fallback;
  };

  const bmsNomFull = finiteBms(b.nom_full);
  const bmsNomRem = finiteBms(b.nom_rem);
  const bmsBuffer = finiteBms(b.buffer);
  const bmsUsableFull = finiteBms(
    b.usable_full,
    Math.max(0, bmsNomFull - bmsBuffer)
  );
  const bmsUsableRem = finiteBms(
    b.usable_rem,
    Math.max(0, bmsNomRem - bmsBuffer)
  );

  const bmsDisplaySoc = finiteBms(
    b.display_soc,
    bmsUsableFull > 0
      ? bmsUsableRem / bmsUsableFull * 100
      : 0
  );

  const bmsExpectedRem = finiteBms(b.expected_rem);
  const bmsIdealRem = finiteBms(b.ideal_rem);
  const bmsToFull = finiteBms(b.charge_complete);

  // Early P85 reference baseline. This is an estimate because
  // temperature and BMS calibration affect nominal-full energy.
  const p85BaselineKwh = 77.5;
  const bmsCapacityHealth = bmsNomFull > 0
    ? Math.max(0, Math.min(120, bmsNomFull / p85BaselineKwh * 100))
    : 0;

  const bmsPackV = finiteBms(b.pack_v);
  const bmsPackI = finiteBms(b.pack_i);
  const bmsPackPower = bmsPackV * bmsPackI / 1000;

  const bmsCells = Array.isArray(b.bricks)
    ? b.bricks
        .map(Number)
        .filter(v => Number.isFinite(v) && v > 2000 && v < 5000)
    : [];

  const bmsCellMin = bmsCells.length
    ? Math.min(...bmsCells)
    : 0;
  const bmsCellMax = bmsCells.length
    ? Math.max(...bmsCells)
    : 0;
  const bmsCellAvg = bmsCells.length
    ? bmsCells.reduce((sum, value) => sum + value, 0) / bmsCells.length
    : 0;
  const bmsCellSpread = bmsCells.length
    ? bmsCellMax - bmsCellMin
    : 0;

  const bmsTempsC = b.temps_dict && typeof b.temps_dict === "object"
    ? Object.values(b.temps_dict)
        .map(Number)
        .filter(v => Number.isFinite(v) && v > -40 && v < 120)
    : [];

  const bmsTempMinC = bmsTempsC.length
    ? Math.min(...bmsTempsC)
    : 0;
  const bmsTempMaxC = bmsTempsC.length
    ? Math.max(...bmsTempsC)
    : 0;
  const bmsTempAvgC = bmsTempsC.length
    ? bmsTempsC.reduce((sum, value) => sum + value, 0) / bmsTempsC.length
    : 0;

  const cToF = value => value * 9 / 5 + 32;

  let elDisplaySoc = $("bms-display-soc");
  if(elDisplaySoc) {
    elDisplaySoc.textContent = bmsDisplaySoc.toFixed(1) + " %";
  }

  let elUsableFull = $("bms-usable-full");
  if(elUsableFull) {
    elUsableFull.textContent = bmsUsableFull.toFixed(1) + " kWh";
  }

  let elUsableRem = $("bms-usable-rem");
  if(elUsableRem) {
    elUsableRem.textContent = bmsUsableRem.toFixed(1) + " kWh";
  }

  let elExpectedRem = $("bms-expected-rem");
  if(elExpectedRem) {
    elExpectedRem.textContent = bmsExpectedRem.toFixed(1) + " kWh";
  }

  let elIdealRem = $("bms-ideal-rem");
  if(elIdealRem) {
    elIdealRem.textContent = bmsIdealRem.toFixed(1) + " kWh";
  }

  let elToFull = $("bms-to-full");
  if(elToFull) {
    elToFull.textContent = bmsToFull.toFixed(1) + " kWh";
  }

  let elCapacityHealth = $("bms-capacity-health");
  if(elCapacityHealth) {
    elCapacityHealth.textContent = bmsCapacityHealth.toFixed(1) + " %";
  }

  let elPackPower = $("bms-pack-power");
  if(elPackPower) {
    elPackPower.textContent =
      (bmsPackPower > 0 ? "+" : "") +
      bmsPackPower.toFixed(1) +
      " kW";
  }

  let elCellAvg = $("bms-cell-avg");
  if(elCellAvg) {
    elCellAvg.textContent = (bmsCellAvg / 1000).toFixed(3) + " V";
  }

  let elCellSpread = $("bms-cell-spread");
  if(elCellSpread) {
    elCellSpread.textContent = bmsCellSpread.toFixed(0) + " mV";
  }

  let elTempAvg = $("bms-temp-avg");
  if(elTempAvg) {
    elTempAvg.textContent = bmsTempsC.length
      ? cToF(bmsTempAvgC).toFixed(1) + " °F"
      : "--";
  }

  let elTempSpread = $("bms-temp-spread");
  if(elTempSpread) {
    elTempSpread.textContent = bmsTempsC.length
      ? ((bmsTempMaxC - bmsTempMinC) * 9 / 5).toFixed(1) + " °F"
      : "--";
  }

  let minV = (b.min_v || 0) / 1000.0; 
  let maxV = (b.max_v || 0) / 1000.0; 
  let delta = (b.max_v || 0) - (b.min_v || 0);
  let minF = (b.min_t || 0) * (9/5) + 32;
  let maxF = (b.max_t || 0) * (9/5) + 32;

  let elMinT = $("bms-min-t"); if(elMinT) elMinT.textContent = minF.toFixed(1) + " °F";
  let elMaxT = $("bms-max-t"); if(elMaxT) elMaxT.textContent = maxF.toFixed(1) + " °F";
  let elDel = $("bms-delta-v"); if(elDel) { elDel.textContent = delta.toFixed(0) + " mV"; elDel.style.color = delta > 50 ? "#ffcc00" : "var(--g)"; }
  let elRanStr = $("bms-cell-range"); if(elRanStr) elRanStr.textContent = `${minV.toFixed(2)} - ${maxV.toFixed(2)} V`;

  let bricks = b.bricks || [];
  if(bricks.length === 96 && $("tab-bms").style.display === "block") {
    let validBricks = bricks.filter(v => v > 2000);
    let sum = validBricks.reduce((a,c)=>a+c, 0);
    let avg = validBricks.length ? (sum / validBricks.length) : 0;
    let elAvg = $("bms-brick-avg"); if(elAvg) elAvg.textContent = `AVG: ${(avg / 1000.0).toFixed(2)} V`;
    
    let html = "";
    bricks.forEach(v => {
      let diff = v - avg;
      let bg = "#1a222e";
      if(v < 2000) bg = "#0b0f15";
      else if(diff > 10) bg = "#56b6ff";
      else if(diff < -10) bg = "#ff5d67";
      else bg = "var(--g)";
      
      let vStr = v > 2000 ? (v / 1000.0).toFixed(2) : "--";
      html += `<div class="brick" style="background:${bg}" title="${Math.round(v)} mV">${vStr}</div>`;
    });
    let elGrid = $("brick-grid"); if(elGrid) elGrid.innerHTML = html;
  }
}

function proj(lat, dist, z=0) {
  let d = Math.max(0, Number(dist || 0));
  let t = 16.0 / (d + 16.0);
  return {
    x: 480 + (Number(lat || 0) * 128.0 * t),
    y: 540 - (374.0 * (1.0 - t)) - (Number(z || 0) * 78.0 * t),
    s: Math.max(.42, Math.min(1.55, 30.0 / (d + 10.0)))
  };
}

function projStr(lat, dist, z=0) {
  let p = proj(lat, dist, z);
  return `${p.x.toFixed(1)},${p.y.toFixed(1)}`;
}

let lastNavKey="";
function navArrow(type,modifier){
  let key=((type||"")+" "+(modifier||"")).toLowerCase();
  if(key.includes("roundabout")||key.includes("rotary"))return "⟳";
  if(key.includes("uturn"))return key.includes("right")?"⤵":"⤴";
  if(key.includes("left"))return key.includes("slight")?"↖":"↰";
  if(key.includes("right"))return key.includes("slight")?"↗":"↱";
  if(key.includes("arrive"))return "◆";
  if(key.includes("merge"))return modifier==="left"?"↖":"↗";
  return "↑";
}
function navDistance(m){
  m=Number(m||0);if(m<=0)return "NOW";
  if(m<305)return Math.max(25,Math.round(m/25)*25)+" FT";
  let mi=m/1609.344;return mi<10?mi.toFixed(mi<1?1:1)+" MI":Math.round(mi)+" MI";
}
function tripDistance(m){let mi=Number(m||0)/1609.344;return mi>0?mi.toFixed(mi<10?1:0)+" mi":"--"}
function navTime(sec){sec=Math.max(0,Number(sec||0));let h=Math.floor(sec/3600),m=Math.round((sec%3600)/60);return h?`${h}h ${m}m`:`${m} min`}
function renderNavigation(n){
  n=n||{};let box=$("nav-guidance"),trip=$("nav-trip"),dot=$("nav-dot");if(!box||!trip)return;
  let active=!!n.connected&&n.route_state!=="inactive",stale=n.route_state==="stale";
  box.classList.toggle("on",active);box.classList.toggle("stale",stale);trip.classList.toggle("on",active);dot.classList.toggle("stale",stale);
  if(!active)return;
  let m=n.maneuver||{},key=[n.route_id,m.type,m.modifier,m.primary_text].join("|");
  if(key!==lastNavKey){box.classList.remove("changed");void box.offsetWidth;box.classList.add("changed");lastNavKey=key;}
  $("nav-arrow").textContent=navArrow(m.type,m.modifier);
  $("nav-distance").textContent=stale?"PHONE SIGNAL LOST":navDistance(m.distance_m);
  $("nav-primary").textContent=m.primary_text||"Continue on route";
  $("nav-secondary").textContent=m.secondary_text||n.route_state.replace(/_/g," ");
  $("nav-remaining").textContent=`${navTime(n.time_remaining_s)} · ${tripDistance(n.distance_remaining_m)}`;
  let eta=Number(n.eta_unix_ms||0);$("nav-eta").textContent=eta?new Date(eta).toLocaleTimeString([],{hour:"numeric",minute:"2-digit"}):n.source||"PHONE NAV";
}

function render(s){
  try {
    S=s;
    let driveActive = s.drive && s.drive.active;
    let driveEnabled = s.drive && s.drive.enabled;
    let elStatus = $("status");
    if(elStatus) {
        elStatus.textContent = driveActive ? "ENGAGED" : driveEnabled ? "READY" : "STANDBY";
        elStatus.style.color = driveActive ? "var(--g)" : "var(--m)";
    }
    
    let elCpu = $("cpu-temp");
    if(elCpu) elCpu.textContent = Math.round((s.health && s.health.temp) || 0);

    let q = s.settings || {};
    let p = +(q.personality_raw || 1);
    let f_dist = q.follow_distance || 4;
    
    document.querySelectorAll("#follow button").forEach(b => b.classList.toggle("active", +b.dataset.val === f_dist));
    document.querySelectorAll(".p-btn").forEach(b=>b.classList.toggle("active",+b.dataset.val===p));
    sw("exp", q.experimental); sw("ada", q.adaptive_accel); sw("spd", q.speed_offset);
    
    let elTrim = $("trim-val");
    if(elTrim) elTrim.textContent = (Number(q.speed_trim || 0) > 0 ? "+" : "") + Number(q.speed_trim || 0).toFixed(0) + " MPH";
    
    let c = s.car || {};
    let pl = s.plan || {};
    let l = s.lead1 || {};
    
    let speedMph = (+c.vEgo || 0) * 2.23694;
    VIS.speedTarget=speedMph;VIS.steerTarget=Number(c.steer||0);
    
    let maxMph = (+c.vCruise || 0) * 0.621371;
    let elMax = $("max-val"); if(elMax) elMax.textContent = maxMph > 5 ? Math.round(maxMph) : "--";
    renderNavigation(s.navigation);

    let arrL = $("arr-l"); if(arrL) arrL.classList.toggle("active", !!c.leftBlinker);
    let arrR = $("arr-r"); if(arrR) arrR.classList.toggle("active", !!c.rightBlinker);
    let pedB = $("pedal-brk"); if(pedB) pedB.classList.toggle("active", !!c.brakePressed);
    let pedG = $("pedal-gas"); if(pedG) pedG.classList.toggle("active", !!c.gasPressed);
    
    lb("l1", l);
    renderBMS(s.bms);
    
    let eng = s.engagement || {};
    let manMi = eng.manual || 0;
    let autMi = eng.both || 0;
    let totalMi = Math.max(0.001, manMi + autMi);
    
    let manPct = (manMi / totalMi) * 100;
    let autPct = (autMi / totalMi) * 100;
    
    let setT = (id, pct, mi) => { 
        let el = $(id); 
        if(el) el.innerHTML = `${pct.toFixed(0)}% <span style="font-size:11px; color:var(--m); font-weight:normal; margin-left:4px;">(${mi.toFixed(1)} mi)</span>`; 
    };
    
    setT("eng-man-txt", manPct, manMi);
    setT("eng-aut-txt", autPct, autMi);
    
    if($("eng-man-bar")) $("eng-man-bar").style.width = manPct + "%";
    if($("eng-aut-bar")) $("eng-aut-bar").style.width = autPct + "%";

    let rd = "";
  (pl.edges || []).forEach(edge => {
    if(edge.pts && edge.pts.length > 1) {
      let d = "M " + edge.pts.map(pt => projStr(pt[1], pt[0])).join(" L ");
      let opacity = Math.max(.18, Math.min(.7, 1 / (1 + Number(edge.std || 1))));
      rd += `<path class="scene-edge" d="${d}" stroke-width="2" opacity="${opacity.toFixed(2)}"/>`;
    }
  });

  (pl.lanes || []).forEach(lane => {
    if(lane.pts && lane.pts.length > 1) {
      let isEgo = lane.idx === 1 || lane.idx === 2;
      let opacity = isEgo ? Math.max(.38, Number(lane.prob || 0)) : Math.max(.12, Number(lane.prob || 0) * .5);
      let width = isEgo ? 4 : 2;
      let d = "M " + lane.pts.map(pt => projStr(pt[1], pt[0])).join(" L ");
      rd += `<path class="scene-lane" d="${d}" stroke-width="${width}" opacity="${Math.min(1, opacity).toFixed(2)}"/>`;
    }
  });

  if(pl.ego && pl.ego.length > 1) {
    let left = [], right = [], center = [];
    pl.ego.forEach(pt => {
      let distance = Number(pt[0] || 0), lateral = Number(pt[1] || 0);
      left.push(projStr(lateral + 1.75, distance));
      right.unshift(projStr(lateral - 1.75, distance));
      center.push(projStr(lateral, distance));
    });
    let fill = driveActive ? "url(#nap-plan-green)" : "url(#nap-plan-blue)";
    let color = driveActive ? "#34c759" : "#56b6ff";
    rd += `<path d="M ${left.join(" L ")} L ${right.join(" L ")} Z" fill="${fill}"/>`;
    rd += `<path class="scene-plan-center" d="M ${center.join(" L ")}" stroke="${color}" stroke-width="5" opacity=".9"/>`;
  }

  let rGrp = $("road-grp");
  if(rGrp) rGrp.innerHTML = rd;

  let targetHtml = "";
  let targets = (pl.leads || []).slice().sort((a,b) => Number(b.x || 0) - Number(a.x || 0));
  targets.forEach((target, index) => {
    let distance = Number(target.x || 0);
    let probability = Number(target.prob || 0);
    if(distance <= 1 || probability <= .1 || index > 3) return;
    let p = proj(Number(target.y || 0), distance, 0);
    let scale = p.s;
    let targetSpeed = Math.max(0, Number(target.v || 0) * 2.23694);
    let side = Math.abs(Number(target.y || 0)) < 1.8 ? "AHEAD" : Number(target.y || 0) > 0 ? "LEFT" : "RIGHT";
    let color = distance < 15 ? "#ff4d5e" : distance < 30 ? "#ffb84d" : "#56b6ff";
    targetHtml += `<g class="scene-lead" transform="translate(${p.x.toFixed(1)} ${p.y.toFixed(1)}) scale(${scale.toFixed(2)})">
      <path d="M-32-13 L-24-35 Q-18-47 0-48 Q18-47 24-35 L32-13 L32 5 Q32 13 23 13 L-23 13 Q-32 13-32 5Z" fill="#26394c" stroke="${color}" stroke-width="2"/>
      <path d="M-18-33 Q0-42 18-33 L14-20 L-14-20Z" fill="#09121c"/>
      <rect x="-24" y="-9" width="10" height="5" rx="2" fill="#ff4050"/><rect x="14" y="-9" width="10" height="5" rx="2" fill="#ff4050"/>
      <text class="scene-lead-label" x="0" y="-62">${distance.toFixed(0)} m</text>
      <text class="scene-lead-sub" x="0" y="-49">${side} · ${targetSpeed.toFixed(0)} mph</text>
    </g>`;
  });
  let tGrp = $("track-grp");
  if(tGrp) tGrp.innerHTML = targetHtml;

  let brakeColor = c.brakePressed ? "#ff3348" : "#6a141b";
  if($("scene-brake-l")) $("scene-brake-l").setAttribute("fill", brakeColor);
  if($("scene-brake-r")) $("scene-brake-r").setAttribute("fill", brakeColor);
  if($("scene-turn-l")) $("scene-turn-l").setAttribute("fill", c.leftBlinker ? "#ffb020" : "#493b19");
  if($("scene-turn-r")) $("scene-turn-r").setAttribute("fill", c.rightBlinker ? "#ffb020" : "#493b19");

  } catch(e) {
    console.error("NAP Dash render error:", e);
    const el=$("status");
    if(el) { el.textContent="UI ERROR"; el.style.color="#ff5d67"; }
  }
}

async function get(){
  try{
    let r=await fetch("/api/state",{cache:"no-store"});
    if(!r.ok)throw Error();
    render(await r.json());
  }catch(e){
    let el=$("status");
    if(el) { el.textContent="Disconnected"; el.style.color="#ff5d67"; }
  }
}
async function setv(name,value){
  try{
    let r=await fetch("/api/set",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name,value})});
    if(!r.ok) throw Error("HTTP "+r.status);
    render(await r.json());
  }catch(e){ console.error("NAP Dash set error:", e); }
}
function trim(delta){
  let cur=Number(S.settings && S.settings.speed_trim ? S.settings.speed_trim : 0);
  setv("speed_trim", Math.max(-15,Math.min(15,cur+delta)));
}
function toggle(n){setv(n,!(S.settings && S.settings[n]));}
initFollow(); animateTelemetry(); get(); setInterval(get, 100);
</script></body></html>
"""

PHONE_HTML = r"""<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no"><title>NAP Nav Remote</title><style>
:root{color-scheme:dark;--bg:#05070a;--card:#111720;--line:#293241;--text:#f5f7fa;--muted:#9aa7b7;--blue:#56b6ff;--green:#34c759}*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 50% -10%,#17304a,#05070a 45%);color:var(--text);font-family:system-ui,-apple-system,sans-serif}.wrap{max-width:560px;margin:auto;padding:18px}.head{display:flex;justify-content:space-between;align-items:center;margin-bottom:18px}.title{font-size:22px;font-weight:850}.status{font-size:12px;color:var(--muted)}.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--green);margin-right:6px;box-shadow:0 0 9px var(--green)}.card{background:#111720ee;border:1px solid var(--line);border-radius:18px;padding:16px;margin-bottom:13px;box-shadow:0 12px 34px #0006}.label{font-size:11px;color:var(--muted);font-weight:800;letter-spacing:.1em;text-transform:uppercase;margin-bottom:10px}.grid{display:grid;grid-template-columns:repeat(2,1fr);gap:9px}.maneuvers{grid-template-columns:repeat(3,1fr)}button,input,select{font:inherit}button{border:1px solid #344153;background:#1a222e;color:#fff;border-radius:12px;padding:13px 8px;font-weight:750}button:active{transform:scale(.97);background:#284d68}.primary{background:var(--blue);color:#04101a;border-color:var(--blue)}.danger{color:#ff7777}input,select{width:100%;background:#090d13;border:1px solid #344153;color:#fff;border-radius:11px;padding:12px;margin-bottom:9px;outline:none}input:focus{border-color:var(--blue)}.preview{display:grid;grid-template-columns:62px 1fr;gap:12px;align-items:center}.arrow{height:62px;border-radius:14px;background:#56b6ff18;color:#78d6ff;display:flex;align-items:center;justify-content:center;font-size:42px}.street{font-size:20px;font-weight:850}.sub{font-size:12px;color:var(--muted);margin-top:4px}.toast{position:fixed;left:50%;bottom:20px;transform:translate(-50%,20px);background:#182330;border:1px solid #56b6ff66;padding:10px 16px;border-radius:99px;opacity:0;transition:.2s}.toast.on{opacity:1;transform:translate(-50%,0)}</style></head><body><div class="wrap">
<div class="head"><div class="title">NAP Nav Remote</div><div class="status"><span class="dot"></span>Pixel → comma</div></div>
<div class="card preview"><div id="arrow" class="arrow">↰</div><div><div id="previewDistance" class="label">500 ft</div><div id="previewStreet" class="street">Main Street</div><div class="sub">Synthetic guidance tester for MCU2 v22</div></div></div>
<div class="card"><div class="label">Maneuver</div><div class="grid maneuvers"><button onclick="pick('turn','left','↰')">↰ Left</button><button onclick="pick('turn','right','↱')">↱ Right</button><button onclick="pick('continue','straight','↑')">↑ Straight</button><button onclick="pick('off ramp','left','↖')">↖ Exit L</button><button onclick="pick('off ramp','right','↗')">↗ Exit R</button><button onclick="pick('roundabout','right','⟳')">⟳ Rotary</button></div></div>
<div class="card"><div class="label">Instruction</div><input id="street" value="Main Street" placeholder="Street or instruction"><input id="secondary" value="Then right on Oak Avenue" placeholder="Secondary instruction"><div class="grid"><input id="distance" type="number" value="152" min="0" placeholder="Meters"><select id="state"><option value="active">Active</option><option value="recalculating">Recalculating</option><option value="arrived">Arrived</option></select></div><div class="grid"><input id="remaining" type="number" value="14800" min="0" placeholder="Remaining meters"><input id="minutes" type="number" value="18" min="0" placeholder="Minutes"></div><button class="primary" style="width:100%" onclick="sendNav()">Send navigation update</button></div>
<div class="grid"><button onclick="countdown()">Run countdown demo</button><button class="danger" onclick="clearNav()">Clear route</button></div></div><div id="toast" class="toast">Update sent</div>
<script>let maneuver={type:'turn',modifier:'left'},sequence=0,timer=null;const $=id=>document.getElementById(id);function pick(type,modifier,arrow){maneuver={type,modifier};$('arrow').textContent=arrow}function metersLabel(m){return m<305?Math.max(25,Math.round(m*3.28084/25)*25)+' ft':(m/1609.344).toFixed(1)+' mi'}function payload(){let m=+$('distance').value||0;return{source:'pixel3-test',route_id:'pixel-demo',route_state:$('state').value,sequence:++sequence,maneuver:{...maneuver,distance_m:m,primary_text:$('street').value,secondary_text:$('secondary').value},distance_remaining_m:+$('remaining').value||0,time_remaining_s:(+$('minutes').value||0)*60,eta_unix_ms:Date.now()+(+$('minutes').value||0)*60000}}async function sendNav(){let p=payload(),r=await fetch('/api/nav/update',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(p)});if(!r.ok)throw Error(await r.text());$('previewDistance').textContent=metersLabel(p.maneuver.distance_m);$('previewStreet').textContent=p.maneuver.primary_text;toast('Update sent')}async function clearNav(){if(timer)clearInterval(timer);await fetch('/api/nav/clear',{method:'POST',body:'{}'});toast('Route cleared')}function countdown(){if(timer)clearInterval(timer);sendNav();timer=setInterval(()=>{let d=Math.max(0,(+$('distance').value||0)-18);$('distance').value=d;sendNav();if(!d){clearInterval(timer);timer=null}},1000)}function toast(t){$('toast').textContent=t;$('toast').classList.add('on');setTimeout(()=>$('toast').classList.remove('on'),1000)}</script></body></html>"""

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
        elif p in ("/phone", "/phone/"):
            b = PHONE_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-store")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers(); self.wfile.write(b)
        elif p == "/api/state":
            navigation_snapshot()
            with LOCK: o = json.loads(json.dumps(STATE))
            self.send_json(o)
        elif p == "/api/nav/status":
            self.send_json(navigation_snapshot())
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
        p = urlparse(self.path).path
        try:
            n = int(self.headers.get("Content-Length", "0"))
            if n < 0 or n > NAV_MAX_BODY: return self.send_json({"error": "payload too large"}, 413)
            d = json.loads(self.rfile.read(n).decode())
            if p == "/api/nav/update":
                nav = update_navigation(d)
                return self.send_json({"ok": True, "navigation": nav})
            if p == "/api/nav/clear":
                return self.send_json({"ok": True, "navigation": clear_navigation()})
            if p != "/api/set": return self.send_json({"error": "not found"}, 404)
            settings = write_setting(str(d.get("name")), d.get("value"))
            with LOCK:
                STATE["settings"] = settings 
                o = json.loads(json.dumps(STATE))
            self.send_json(o)
        except Exception as e:
            self.send_json({"error": str(e)}, 400)

def main():
    print(f"NAP Drive Panel v22 listening on port {PORT} (phone tester: /phone)")
    threading.Thread(target=telemetry, daemon=True).start()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    try: srv.serve_forever()
    except KeyboardInterrupt: pass
    finally:
        STOP.set()
        srv.server_close()

if __name__ == "__main__":
    main()
