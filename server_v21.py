#!/usr/bin/env python3
import csv, io, json, threading, time, os, subprocess, glob, traceback, uuid, sqlite3, queue, statistics
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote
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

MPH_PER_MPS = 2.2369362921
G_MPS2 = 9.80665
DATA_DIR = os.environ.get("NAP_DATA_DIR", os.environ.get("NAP_BMS_DATA_DIR", "/data/bms/nap_dash"))

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


class HistoryDatabase:
    """Bounded SQLite history with non-blocking telemetry writes and diagnostics."""
    LIMITS = {
        "performance": 500, "regen": 500, "battery": 30000,
        "efficiency": 500, "charging": 100, "settings": 1000,
    }
    LEGACY = {
        "performance": "performance_runs.jsonl",
        "regen": "regen_sessions.jsonl",
        "battery": "battery_snapshots.jsonl",
        "efficiency": "efficiency_trips.jsonl",
        "charging": "charging_curves.jsonl",
    }

    def __init__(self, directory):
        self.directory = directory
        self.path = os.path.join(directory, "nap_history.sqlite3")
        self.pending = queue.Queue(maxsize=2000)
        self.ready = False
        self.started_at = time.time()
        self.enqueued = 0
        self.written = 0
        self.dropped = 0
        self.write_errors = 0
        self.last_write_at = None
        self.last_error = ""
        try:
            os.makedirs(directory, exist_ok=True)
            self._initialize()
            self.ready = True
            threading.Thread(target=self._writer, name="nap-history-writer", daemon=True).start()
        except Exception as exc:
            self.last_error = str(exc)
            traceback.print_exc()

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=3)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        db.execute("PRAGMA busy_timeout=3000")
        return db

    @staticmethod
    def _created(row):
        value = row.get("completed_at", row.get("ended_at", row.get("recorded_at", row.get("changed_at", row.get("started_at", time.time())))))
        try:
            return float(value)
        except Exception:
            return time.time()

    def _initialize(self):
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS history (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, created REAL NOT NULL, payload TEXT NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS history_kind_created ON history(kind, created DESC)")
            db.execute("CREATE TABLE IF NOT EXISTS segment_telemetry (segment TEXT PRIMARY KEY, updated REAL NOT NULL, payload TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT)")
            migrated = db.execute("SELECT value FROM metadata WHERE key='jsonl_migrated_v1'").fetchone()
            if not migrated:
                for kind, filename in self.LEGACY.items():
                    path = os.path.join(self.directory, filename)
                    try:
                        with open(path, "r") as source:
                            for line in source:
                                if not line.strip():
                                    continue
                                row = json.loads(line)
                                db.execute(
                                    "INSERT INTO history(kind,created,payload) VALUES(?,?,?)",
                                    (kind, self._created(row), json.dumps(row, separators=(",", ":"))),
                                )
                    except Exception:
                        pass
                db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('jsonl_migrated_v1',?)", (str(time.time()),))

    def _enqueue(self, item):
        if not self.ready:
            return False
        try:
            self.pending.put_nowait(item)
            self.enqueued += 1
            return True
        except queue.Full:
            self.dropped += 1
            return False

    def append(self, kind, row):
        if kind not in self.LIMITS:
            raise ValueError("unknown history type")
        return self._enqueue(("history", kind, self._created(row), json.dumps(row, separators=(",", ":"))))

    def load_state(self, key, default):
        if not self.ready:
            return default
        try:
            with self._connect() as db:
                row = db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default
        except Exception:
            return default

    def save_state(self, key, value):
        return self._enqueue(("metadata", key, json.dumps(value, separators=(",", ":"))))

    def save_segment(self, segment, value):
        return self._enqueue(("segment", segment, time.time(), json.dumps(value, separators=(",", ":"))))

    def read_segment(self, segment):
        if not self.ready:
            return None
        try:
            with self._connect() as db:
                row = db.execute("SELECT payload FROM segment_telemetry WHERE segment=?", (segment,)).fetchone()
            return json.loads(row[0]) if row else None
        except Exception:
            return None

    def _writer(self):
        while not STOP.is_set() or not self.pending.empty():
            try:
                first = self.pending.get(timeout=.5)
            except queue.Empty:
                continue
            batch = [first]
            while len(batch) < 100:
                try:
                    batch.append(self.pending.get_nowait())
                except queue.Empty:
                    break
            try:
                with self._connect() as db:
                    for item in batch:
                        if item[0] == "history":
                            db.execute("INSERT INTO history(kind,created,payload) VALUES(?,?,?)", item[1:])
                        elif item[0] == "metadata":
                            db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES(?,?)", item[1:])
                        else:
                            db.execute("INSERT OR REPLACE INTO segment_telemetry(segment,updated,payload) VALUES(?,?,?)", item[1:])
                    for kind in {item[1] for item in batch if item[0] == "history"}:
                        db.execute(
                            "DELETE FROM history WHERE kind=? AND id NOT IN (SELECT id FROM history WHERE kind=? ORDER BY created DESC LIMIT ?)",
                            (kind, kind, self.LIMITS[kind]),
                        )
                    if any(item[0] == "segment" for item in batch):
                        db.execute("DELETE FROM segment_telemetry WHERE segment NOT IN (SELECT segment FROM segment_telemetry ORDER BY updated DESC LIMIT 1000)")
                self.written += len(batch)
                self.last_write_at = time.time()
                self.last_error = ""
            except Exception as exc:
                self.write_errors += 1
                self.last_error = str(exc)
                traceback.print_exc()
            finally:
                for _ in batch:
                    self.pending.task_done()

    def read(self, kind, limit=100, since=None):
        if not self.ready or kind not in self.LIMITS:
            return []
        limit = max(1, min(int(limit), self.LIMITS[kind]))
        try:
            with self._connect() as db:
                if since is None:
                    rows = db.execute("SELECT payload FROM history WHERE kind=? ORDER BY created DESC LIMIT ?", (kind, limit)).fetchall()
                else:
                    rows = db.execute("SELECT payload FROM history WHERE kind=? AND created>=? ORDER BY created DESC LIMIT ?", (kind, float(since), limit)).fetchall()
            return [json.loads(row[0]) for row in rows]
        except Exception as exc:
            self.last_error = str(exc)
            return []

    def read_since(self, kind, since, max_points=500):
        if not self.ready or kind not in self.LIMITS:
            return []
        try:
            with self._connect() as db:
                count = db.execute("SELECT COUNT(*) FROM history WHERE kind=? AND created>=?", (kind, float(since))).fetchone()[0]
                step = max(1, int(count / max_points))
                rows = db.execute(
                    "SELECT payload FROM (SELECT payload,ROW_NUMBER() OVER (ORDER BY created) AS rn FROM history WHERE kind=? AND created>=?) WHERE (rn-1) % ?=0 ORDER BY rn",
                    (kind, float(since), step),
                ).fetchall()
            return [json.loads(row[0]) for row in rows[-max_points:]]
        except Exception as exc:
            self.last_error = str(exc)
            return []

    def stats(self):
        counts = {kind: {"count": 0, "latest_at": None} for kind in self.LIMITS}
        segments = 0
        if self.ready:
            try:
                with self._connect() as db:
                    for kind, count, latest in db.execute("SELECT kind,COUNT(*),MAX(created) FROM history GROUP BY kind"):
                        if kind in counts:
                            counts[kind] = {"count": int(count), "latest_at": latest}
                    segments = int(db.execute("SELECT COUNT(*) FROM segment_telemetry").fetchone()[0])
            except Exception as exc:
                self.last_error = str(exc)
        size = 0
        for suffix in ("", "-wal", "-shm"):
            try:
                size += os.path.getsize(self.path + suffix)
            except OSError:
                pass
        return {
            "ready": self.ready,
            "path": self.path,
            "size_bytes": size,
            "queue_depth": self.pending.qsize(),
            "enqueued": self.enqueued,
            "written": self.written,
            "dropped": self.dropped,
            "write_errors": self.write_errors,
            "last_write_at": self.last_write_at,
            "last_error": self.last_error,
            "segments": segments,
            "types": counts,
        }

    def flush(self):
        if self.ready:
            self.pending.join()


class SqliteStore:
    def __init__(self, database, kind):
        self.database, self.kind = database, kind

    def append(self, row):
        return self.database.append(self.kind, row)

    def read(self, limit=100, since=None):
        return self.database.read(self.kind, limit, since)

    def read_since(self, since, max_points=500):
        return self.database.read_since(self.kind, since, max_points)


HISTORY_DB = HistoryDatabase(DATA_DIR)
RUN_STORE = SqliteStore(HISTORY_DB, "performance")
REGEN_STORE = SqliteStore(HISTORY_DB, "regen")
BATTERY_STORE = SqliteStore(HISTORY_DB, "battery")
TRIP_STORE = SqliteStore(HISTORY_DB, "efficiency")
CHARGE_STORE = SqliteStore(HISTORY_DB, "charging")
SETTINGS_STORE = SqliteStore(HISTORY_DB, "settings")


def interp_time(t0, value0, t1, value1, target):
    if t1 <= t0 or value1 == value0:
        return t1
    fraction = max(0.0, min(1.0, (target - value0) / (value1 - value0)))
    return t0 + (t1 - t0) * fraction


def pack_conditions(sample):
    bms = sample.get("bms", {})
    temps = [num(x) for x in (bms.get("temps_dict") or {}).values() if -40 < num(x) < 120]
    cells = [num(v) for v in bms.get("bricks", []) if 2000 < num(v) < 5000]
    return {
        "soc_pct": round(num(bms.get("ui_soc")), 1),
        "pack_temp_c": round(sum(temps) / len(temps), 1) if temps else None,
        "pack_voltage_v": round(num(bms.get("pack_v")), 1) or None,
        "nominal_full_kwh": round(num(bms.get("nom_full")), 1) or None,
        "rated_range_mi": round(num(bms.get("rated_range")), 1) or None,
        "cell_spread_mv": round(max(cells) - min(cells), 1) if cells else None,
    }


def enrich_run_history(runs, snapshots):
    """Backfill older run cards from the nearest battery snapshot when possible."""
    enriched = []
    for original in runs:
        run = json.loads(json.dumps(original))
        at = num(run.get("started_at"))
        nearby = min(snapshots, key=lambda row: abs(num(row.get("recorded_at")) - at)) if snapshots and at else None
        if nearby and abs(num(nearby.get("recorded_at")) - at) <= 600:
            conditions = run.setdefault("conditions", {})
            mapping = {"soc_pct": "soc_pct", "pack_temp_c": "pack_temp_c", "nominal_full_kwh": "nom_full_kwh", "cell_spread_mv": "cell_spread_mv"}
            for target, source in mapping.items():
                if conditions.get(target) is None and nearby.get(source) is not None:
                    conditions[target] = nearby[source]
        enriched.append(run)
    return enriched


class TelemetryRecorder:
    DRAG_SPEEDS = (10, 20, 30, 40, 50, 60, 70, 80, 100)
    ROLLS = ((20, 60), (30, 70), (40, 80), (50, 100), (60, 100))
    BRAKES = (40, 50, 60)

    def __init__(self):
        self.lock = threading.Lock()
        self.started_at = time.time()
        self.sample_count = 0
        self.last_sample_at = None
        self.last_error = ""
        self.previous = None
        self.drag = None
        self.rolls = {}
        self.brakes = {}
        self.regen = None
        self.trip = None
        self.charge_candidate = None
        self.charge = None
        self.live = {}
        self.energy = {"power_kw": 0.0, "regen_kw": 0.0, "soc_pct": 0.0}
        self.battery_minute = []
        self.last_graph_sample = 0.0
        self.health_window = []
        self.health_resistance = []
        self.health_prev = None
        self.last_health_sample = 0.0
        self.last_snapshot = 0.0
        self.segment_name = None
        self.segment_points = []
        self.last_segment_check = 0.0
        self.last_segment_sample = 0.0
        self.last_segment_save = 0.0
        self.last_checkpoint = 0.0
        empty = lambda name: {
            "name": name, "enabled": False, "started_at": None, "elapsed_s": 0.0,
            "distance_m": 0.0, "energy_used_kwh": 0.0, "regen_kwh": 0.0,
            "net_kwh": 0.0, "start_soc": None, "start_temp_c": None,
            "min_voltage_v": None, "peak_power_kw": 0.0, "peak_regen_kw": 0.0,
        }
        saved = HISTORY_DB.load_state("trip_meters", {})
        self.trip_meters = {name: {**empty(name), **saved.get(name, {})} for name in ("A", "B", "SHIFT")}
        runs = RUN_STORE.read(1)
        self.last_run = runs[0] if runs else None
        interrupted = HISTORY_DB.load_state("active_sessions", {})
        for key, store in (("trip", TRIP_STORE), ("charge", CHARGE_STORE)):
            row = interrupted.get(key) if isinstance(interrupted, dict) else None
            if isinstance(row, dict) and row.get("started_at"):
                row.update({"ended_at": time.time(), "interrupted": True})
                if key == "trip":
                    miles = num(row.get("distance_m")) * .000621371
                    row["distance_mi"] = round(miles, 2)
                    row["wh_per_mi"] = round(num(row.get("energy_kwh")) * 1000 / miles, 1) if miles > .05 else None
                store.append(row)
        HISTORY_DB.save_state("active_sessions", {})

    def _base(self, kind, now, sample):
        run = {
            "id": uuid.uuid4().hex[:12], "kind": kind, "started_at": time.time(),
            "start_mono": now, "start_time": now, "conditions": pack_conditions(sample),
            "peak_accel_g": 0.0, "peak_decel_g": 0.0,
            "min_pack_voltage_v": None, "max_pack_voltage_v": None,
            "peak_discharge_kw": 0.0, "peak_regen_kw": 0.0,
            "peak_abs_current_a": 0.0, "worst_cell_spread_mv": 0.0,
        }
        self._battery_metrics(run, sample)
        return run

    @staticmethod
    def _battery_metrics(run, sample):
        bms = sample.get("bms", {})
        voltage, current = num(bms.get("pack_v")), num(bms.get("pack_i"))
        power_kw = -(voltage * current) / 1000.0
        cells = [num(v) for v in bms.get("bricks", []) if 2000 < num(v) < 5000]
        if voltage > 0:
            run["min_pack_voltage_v"] = voltage if run.get("min_pack_voltage_v") is None else min(run["min_pack_voltage_v"], voltage)
            run["max_pack_voltage_v"] = voltage if run.get("max_pack_voltage_v") is None else max(run["max_pack_voltage_v"], voltage)
        run["peak_discharge_kw"] = max(num(run.get("peak_discharge_kw")), power_kw)
        run["peak_regen_kw"] = max(num(run.get("peak_regen_kw")), -power_kw)
        run["peak_abs_current_a"] = max(num(run.get("peak_abs_current_a")), abs(current))
        if cells:
            run["worst_cell_spread_mv"] = max(num(run.get("worst_cell_spread_mv")), max(cells) - min(cells))

    @staticmethod
    def _peaks(run, accel_g):
        run["peak_accel_g"] = round(max(num(run.get("peak_accel_g")), accel_g), 3)
        run["peak_decel_g"] = round(min(num(run.get("peak_decel_g")), accel_g), 3)

    def _save_run(self, run, finished_mono, extra=None, sample=None):
        out = dict(run)
        start = num(out.pop("start_time", finished_mono), finished_mono)
        out.pop("start_mono", None)
        out.pop("last_distance_m", None)
        out.update(extra or {})
        out["duration_s"] = round(max(0, finished_mono - start), 3)
        out["completed_at"] = time.time()
        if sample is not None:
            out["ending_conditions"] = pack_conditions(sample)
        self.last_run = json.loads(json.dumps(out))
        RUN_STORE.append(out)

    @staticmethod
    def _public_session(value):
        if not value:
            return None
        out = json.loads(json.dumps(value))
        for key in ("start_mono", "start_time", "last_regen_mono", "last_move_mono", "last_charge_mono", "last_curve_mono"):
            out.pop(key, None)
        return out

    def sample(self, now, sample):
        speed = max(0.0, num(sample.get("car", {}).get("vEgo")))
        mph = speed * MPH_PER_MPS
        accel_g = num(sample.get("car", {}).get("aEgo")) / G_MPS2
        with self.lock:
            self.sample_count += 1
            self.last_sample_at = time.time()
            previous = self.previous
            if previous is None:
                self.previous = (now, speed, mph)
                return
            pt, pspeed, pmph = previous
            dt = now - pt
            if dt <= 0 or dt > 1.0:
                self.previous = (now, speed, mph)
                return
            step_m = (pspeed + speed) * .5 * dt
            try:
                self._performance(now, pt, pmph, mph, speed, step_m, accel_g, sample)
                self._energy(now, dt, step_m, speed, sample)
                self._segment_sample(now, mph, accel_g, sample)
                self.live = {
                    "speed_mph": round(mph, 1), "accel_g": round(accel_g, 3),
                    "drag": self._public_session(self.drag),
                    "active_rolls": list(self.rolls), "active_braking": list(self.brakes),
                }
                self.last_error = ""
            except Exception as exc:
                self.last_error = str(exc)
            self.previous = (now, speed, mph)

    def _performance(self, now, pt, pmph, mph, speed, step_m, accel_g, sample):
        if self.drag is None and pmph <= .5 < mph and accel_g > .02:
            self.drag = self._base("drag", now, sample)
            self.drag.update({"start_time": interp_time(pt, pmph, now, mph, .5), "last_distance_m": 0.0, "milestones_s": {}})
        if self.drag:
            run = self.drag
            run["last_distance_m"] += step_m
            self._peaks(run, accel_g)
            self._battery_metrics(run, sample)
            for target in self.DRAG_SPEEDS:
                key = "0-%d" % target
                if key not in run["milestones_s"] and pmph < target <= mph:
                    crossing = interp_time(pt, pmph, now, mph, target)
                    run["milestones_s"][key] = round(crossing - run["start_time"], 3)
            if "0-60" in run["milestones_s"] and "0-100" in run["milestones_s"]:
                run["milestones_s"]["60-100"] = round(run["milestones_s"]["0-100"] - run["milestones_s"]["0-60"], 3)
            for meters, label in ((201.168, "1/8-mile"), (402.336, "1/4-mile")):
                if label not in run["milestones_s"] and run["last_distance_m"] >= meters:
                    over = run["last_distance_m"] - meters
                    cross = now - over / max(speed, .1)
                    run["milestones_s"][label] = round(cross - run["start_time"], 3)
                    run["milestones_s"][label + "-trap-mph"] = round(mph, 1)
            elapsed = now - run["start_time"]
            if "1/4-mile" in run["milestones_s"] or elapsed > 60 or (elapsed > 3 and mph < 1):
                self._save_run(run, now, {"distance_m": round(run["last_distance_m"], 1), "complete": "1/4-mile" in run["milestones_s"]}, sample)
                self.drag = None

        for low, high in self.ROLLS:
            key = "%d-%d" % (low, high)
            if key not in self.rolls and pmph < low <= mph and accel_g > .02:
                run = self._base("roll", now, sample)
                run.update({"preset": key, "start_time": interp_time(pt, pmph, now, mph, low)})
                self.rolls[key] = run
            run = self.rolls.get(key)
            if run:
                self._peaks(run, accel_g)
                self._battery_metrics(run, sample)
                if pmph < high <= mph:
                    finish = interp_time(pt, pmph, now, mph, high)
                    self._save_run(run, finish, {"result_s": round(finish - run["start_time"], 3), "complete": True}, sample)
                    del self.rolls[key]
                elif now - run["start_time"] > 45 or mph < low - 5:
                    del self.rolls[key]

        for start_mph in self.BRAKES:
            key = "%d-0" % start_mph
            if key not in self.brakes and pmph > start_mph >= mph and accel_g < -.05:
                run = self._base("braking", now, sample)
                run.update({"preset": key, "start_time": interp_time(pt, pmph, now, mph, start_mph), "distance_m": 0.0})
                self.brakes[key] = run
            run = self.brakes.get(key)
            if run:
                run["distance_m"] += step_m
                self._peaks(run, accel_g)
                self._battery_metrics(run, sample)
                if mph <= 1:
                    self._save_run(run, now, {"result_s": round(now - run["start_time"], 3), "distance_ft": round(run["distance_m"] * 3.28084, 1), "complete": True}, sample)
                    del self.brakes[key]
                elif now - run["start_time"] > 20 or accel_g > .1:
                    del self.brakes[key]

    def _energy(self, now, dt, step_m, speed, sample):
        bms = sample.get("bms", {})
        voltage, current = num(bms.get("pack_v")), num(bms.get("pack_i"))
        power_kw = -(voltage * current) / 1000.0
        if now - self.last_graph_sample >= .5:
            self.battery_minute.append({"t": now, "v": round(voltage, 1), "a": round(current, 1), "kw": round(power_kw, 1), "soc": round(num(bms.get("ui_soc")), 1)})
            self.battery_minute = [point for point in self.battery_minute if now - point["t"] <= 60]
            self.last_graph_sample = now
        self._battery_health(now, speed, power_kw, sample)
        self._update_trip_meters(now, dt, step_m, speed, power_kw, sample)
        regen_now = power_kw < -1 and speed > 1
        self.energy = {"power_kw": round(power_kw, 2), "regen_kw": round(-power_kw if regen_now else 0, 2), "soc_pct": round(num(bms.get("ui_soc")), 1)}

        if regen_now and self.regen is None:
            self.regen = self._base("regen", now, sample)
            self.regen.update({"energy_recovered_kwh": 0.0, "peak_kw": 0.0})
        if self.regen:
            if regen_now:
                self.regen["energy_recovered_kwh"] += -power_kw * dt / 3600
                self.regen["peak_kw"] = max(self.regen["peak_kw"], -power_kw)
                self.regen["last_regen_mono"] = now
            elif now - self.regen.get("last_regen_mono", now) > 3:
                out = self._public_session(self.regen)
                out.update({"ended_at": time.time(), "duration_s": round(now - self.regen["start_time"], 1), "energy_recovered_kwh": round(out["energy_recovered_kwh"], 4), "peak_kw": round(out["peak_kw"], 1)})
                REGEN_STORE.append(out)
                self.regen = None

        charging_now = power_kw < -1 and speed < .2
        if charging_now:
            if self.charge_candidate is None:
                self.charge_candidate = {"since": now, "sample": sample}
            if self.charge is None and now - self.charge_candidate["since"] >= 10:
                self.charge = self._base("charging", self.charge_candidate["since"], self.charge_candidate["sample"])
                self.charge.update({"curve": [], "energy_added_kwh": 0.0, "peak_kw": 0.0, "last_curve_mono": 0.0})
            if self.charge:
                charge_kw = -power_kw
                self.charge["energy_added_kwh"] += charge_kw * dt / 3600
                self.charge["peak_kw"] = max(self.charge["peak_kw"], charge_kw)
                self.charge["last_charge_mono"] = now
                if now - self.charge["last_curve_mono"] >= 5:
                    conditions = pack_conditions(sample)
                    self.charge["curve"].append({"elapsed_s": round(now - self.charge["start_time"], 1), "kw": round(charge_kw, 1), "soc": conditions["soc_pct"], "v": round(voltage, 1), "temp_c": conditions["pack_temp_c"]})
                    self.charge["last_curve_mono"] = now
        else:
            self.charge_candidate = None
            if self.charge and now - self.charge.get("last_charge_mono", now) > 30:
                out = self._public_session(self.charge)
                out.update({"ended_at": time.time(), "duration_s": round(now - self.charge["start_time"], 1), "energy_added_kwh": round(out["energy_added_kwh"], 3), "peak_kw": round(out["peak_kw"], 1), "ending_conditions": pack_conditions(sample)})
                CHARGE_STORE.append(out)
                self.charge = None

        if speed > .5 and self.trip is None:
            self.trip = self._base("efficiency", now, sample)
            self.trip.update({"distance_m": 0.0, "energy_kwh": 0.0, "energy_used_kwh": 0.0, "regen_kwh": 0.0})
        if self.trip:
            self.trip["distance_m"] += step_m
            self.trip["energy_kwh"] += power_kw * dt / 3600
            if power_kw > 0:
                self.trip["energy_used_kwh"] += power_kw * dt / 3600
            elif speed > 1:
                self.trip["regen_kwh"] += -power_kw * dt / 3600
            if speed > .5:
                self.trip["last_move_mono"] = now
            if now - self.trip.get("last_move_mono", now) > 180:
                self._finish_trip(now)

        if now - self.last_checkpoint >= 30:
            HISTORY_DB.save_state("trip_meters", self.trip_meters)
            HISTORY_DB.save_state("active_sessions", {"trip": self._public_session(self.trip), "charge": self._public_session(self.charge)})
            self.last_checkpoint = now

    def _finish_trip(self, now):
        miles = self.trip["distance_m"] * .000621371
        out = self._public_session(self.trip)
        duration = max(0, now - self.trip["start_time"])
        out.update({
            "ended_at": time.time(), "duration_s": round(duration, 1),
            "distance_mi": round(miles, 2), "energy_kwh": round(out["energy_kwh"], 3),
            "energy_used_kwh": round(out["energy_used_kwh"], 3), "regen_kwh": round(out["regen_kwh"], 3),
            "wh_per_mi": round(out["energy_kwh"] * 1000 / miles, 1) if miles > .05 else None,
            "avg_mph": round(miles / (duration / 3600), 1) if duration > 0 else 0,
        })
        TRIP_STORE.append(out)
        self.trip = None

    def _update_trip_meters(self, now, dt, step_m, speed, power_kw, sample):
        conditions = pack_conditions(sample)
        voltage = num(sample.get("bms", {}).get("pack_v"))
        for meter in self.trip_meters.values():
            if not meter["enabled"]:
                continue
            if meter["started_at"] is None:
                meter["started_at"] = time.time()
                meter["start_soc"] = conditions["soc_pct"]
                meter["start_temp_c"] = conditions["pack_temp_c"]
            meter["elapsed_s"] += dt
            meter["distance_m"] += step_m
            meter["net_kwh"] += power_kw * dt / 3600
            if power_kw > 0:
                meter["energy_used_kwh"] += power_kw * dt / 3600
            elif speed > 1:
                meter["regen_kwh"] += -power_kw * dt / 3600
            if voltage > 0:
                meter["min_voltage_v"] = voltage if meter["min_voltage_v"] is None else min(meter["min_voltage_v"], voltage)
            meter["peak_power_kw"] = max(meter["peak_power_kw"], power_kw)
            meter["peak_regen_kw"] = max(meter["peak_regen_kw"], -power_kw if speed > 1 else 0)

    def trip_action(self, name, action):
        name = str(name).upper()
        if name not in self.trip_meters or action not in ("toggle", "reset"):
            raise ValueError("invalid trip action")
        with self.lock:
            meter = self.trip_meters[name]
            if action == "toggle":
                meter["enabled"] = not meter["enabled"]
            else:
                if meter["started_at"] is not None and meter["distance_m"] > 10:
                    miles = meter["distance_m"] * .000621371
                    TRIP_STORE.append({**meter, "kind": "trip_meter", "meter": name, "ended_at": time.time(), "distance_mi": round(miles, 2), "wh_per_mi": round(meter["net_kwh"] * 1000 / miles, 1) if miles else None})
                enabled = meter["enabled"]
                meter.update({"started_at": None, "elapsed_s": 0.0, "distance_m": 0.0, "energy_used_kwh": 0.0, "regen_kwh": 0.0, "net_kwh": 0.0, "start_soc": None, "start_temp_c": None, "min_voltage_v": None, "peak_power_kw": 0.0, "peak_regen_kw": 0.0, "enabled": enabled})
            HISTORY_DB.save_state("trip_meters", self.trip_meters)
            return json.loads(json.dumps(self.trip_meters))

    def _battery_health(self, now, speed, power_kw, sample):
        bms = sample.get("bms", {})
        voltage, current = num(bms.get("pack_v")), num(bms.get("pack_i"))
        load_a = -current
        cells = [num(v) for v in bms.get("bricks", []) if 2000 < num(v) < 5000]
        if voltage < 100 or len(cells) < 80:
            return
        previous = self.health_prev
        if previous and now - previous[0] <= 2:
            delta_load = load_a - previous[2]
            voltage_drop = previous[1] - voltage
            if delta_load >= 50 and voltage_drop > 0:
                resistance = voltage_drop / delta_load * 1000
                if 1 <= resistance <= 500:
                    self.health_resistance.append(resistance)
        self.health_prev = (now, voltage, load_a)
        if now - self.last_health_sample < 1:
            return
        conditions = pack_conditions(sample)
        self.health_window.append({
            "voltage": voltage, "power": power_kw, "current": current,
            "soc": conditions["soc_pct"], "temp": conditions["pack_temp_c"],
            "nominal": conditions["nominal_full_kwh"], "spread": conditions["cell_spread_mv"],
            "min_cell": min(cells) if cells else None,
            "min_cell_index": bms.get("bricks", []).index(min(cells)) if cells else None,
            "max_discharge": num(bms.get("max_discharge")), "max_regen": num(bms.get("max_regen")),
            "resting": abs(power_kw) < 2 and speed < .2,
        })
        self.last_health_sample = now
        if now - self.last_snapshot < 300:
            return
        rows, resistances = self.health_window, self.health_resistance
        valid = lambda key: [num(row[key]) for row in rows if row.get(key) is not None]
        volts, powers, spreads, temps = valid("voltage"), valid("power"), valid("spread"), valid("temp")
        weakest = min((row for row in rows if row.get("min_cell") is not None), key=lambda row: row["min_cell"], default={})
        resting = [row["voltage"] for row in rows if row.get("resting") and row["voltage"] > 0]
        latest = rows[-1] if rows else {}
        record = {
            "recorded_at": time.time(), "soc_pct": latest.get("soc"), "pack_temp_c": latest.get("temp"),
            "nom_full_kwh": latest.get("nominal"), "pack_voltage_v": latest.get("voltage"),
            "resting_voltage_v": round(statistics.median(resting), 2) if resting else None,
            "voltage_min_v": round(min(volts), 2) if volts else None,
            "voltage_max_v": round(max(volts), 2) if volts else None,
            "voltage_sag_v": round(max(volts) - min(volts), 2) if volts else None,
            "peak_discharge_kw": round(max(powers), 1) if powers else 0,
            "peak_regen_kw": round(abs(min(powers)), 1) if powers else 0,
            "resistance_mohm": round(statistics.median(resistances), 2) if resistances else None,
            "cell_spread_mv": round(statistics.median(spreads), 1) if spreads else None,
            "loaded_spread_max_mv": round(max(spreads), 1) if spreads else None,
            "weakest_brick": weakest.get("min_cell_index"), "weakest_brick_mv": weakest.get("min_cell"),
            "temp_min_c": round(min(temps), 1) if temps else None, "temp_max_c": round(max(temps), 1) if temps else None,
            "max_discharge_kw": round(max(valid("max_discharge") or [0]), 1),
            "max_regen_kw": round(max(valid("max_regen") or [0]), 1),
            "sample_count": len(rows), "resistance_samples": len(resistances),
        }
        BATTERY_STORE.append(record)
        self.health_window = []
        self.health_resistance = []
        self.last_snapshot = now

    def _segment_sample(self, now, mph, accel_g, sample):
        if now - self.last_segment_check >= 1:
            try:
                paths = glob.glob("/data/media/0/realdata/*--[0-9]*")
                current = os.path.basename(max(paths, key=os.path.getmtime)) if paths else None
            except Exception:
                current = None
            if current and current != self.segment_name:
                if self.segment_name and self.segment_points:
                    HISTORY_DB.save_segment(self.segment_name, {"schema": 1, "points": self.segment_points})
                self.segment_name, self.segment_points = current, []
            self.last_segment_check = now
        if not self.segment_name or now - self.last_segment_sample < .1:
            return
        car, bms = sample.get("car", {}), sample.get("bms", {})
        conditions = pack_conditions(sample)
        point = [
            round(now, 3), round(mph, 1), round(accel_g, 3), round(num(car.get("steer")), 1),
            1 if car.get("brakePressed") else 0, 1 if car.get("gasPressed") else 0,
            round(num(bms.get("pack_v")), 1), round(num(bms.get("pack_i")), 1),
            round(-(num(bms.get("pack_v")) * num(bms.get("pack_i"))) / 1000, 1),
            conditions["soc_pct"], conditions["pack_temp_c"], conditions["cell_spread_mv"],
        ]
        self.segment_points.append(point)
        self.segment_points = self.segment_points[-900:]
        self.last_segment_sample = now
        if now - self.last_segment_save >= 5:
            HISTORY_DB.save_segment(self.segment_name, {"schema": 1, "points": self.segment_points})
            self.last_segment_save = now

    def diagnostics(self):
        age = time.time() - self.last_sample_at if self.last_sample_at else None
        return {
            "running": age is not None and age < 5,
            "started_at": self.started_at,
            "last_sample_at": self.last_sample_at,
            "last_sample_age_s": round(age, 1) if age is not None else None,
            "sample_count": self.sample_count,
            "last_error": self.last_error,
            "segment": self.segment_name,
            "active": {"trip": bool(self.trip), "regen": bool(self.regen), "charging": bool(self.charge), "performance": bool(self.drag or self.rolls or self.brakes)},
        }

    def checkpoint(self):
        with self.lock:
            HISTORY_DB.save_state("trip_meters", self.trip_meters)
            HISTORY_DB.save_state("active_sessions", {"trip": self._public_session(self.trip), "charge": self._public_session(self.charge)})
            if self.segment_name and self.segment_points:
                HISTORY_DB.save_segment(self.segment_name, {"schema": 1, "points": self.segment_points})

    def status(self, include_history=False):
        with self.lock:
            now = time.monotonic()
            minute = [{**point, "age_s": round(now - point["t"], 1)} for point in self.battery_minute]
            for point in minute:
                point.pop("t", None)
            result = {
                "live": json.loads(json.dumps(self.live)), "energy": dict(self.energy),
                "regen": self._public_session(self.regen), "trip": self._public_session(self.trip),
                "charge": self._public_session(self.charge), "battery_minute": minute,
                "trip_meters": json.loads(json.dumps(self.trip_meters)),
                "last_run": json.loads(json.dumps(self.last_run)) if self.last_run else None,
                "recorder": self.diagnostics(), "database": HISTORY_DB.stats(),
            }
        if not result["charge"]:
            saved = CHARGE_STORE.read(1)
            result["last_charge"] = saved[0] if saved else None
        if include_history:
            battery_trend = BATTERY_STORE.read(100)
            result.update({
                "recent_runs": enrich_run_history(RUN_STORE.read(60), battery_trend), "recent_regen": REGEN_STORE.read(30),
                "recent_trips": TRIP_STORE.read(30), "recent_charging": CHARGE_STORE.read(20),
                "recent_settings": SETTINGS_STORE.read(30), "battery_trend": battery_trend,
            })
        return result


RECORDER = TelemetryRecorder()


def battery_health_report(days=30):
    days = 7 if days <= 7 else 30 if days <= 30 else 90
    rows = BATTERY_STORE.read_since(time.time() - days * 86400, 600)
    values = lambda key, source=None: [num(row[key]) for row in (rows if source is None else source) if row.get(key) is not None and num(row[key]) != 0]
    qualified = [row for row in rows if row.get("nom_full_kwh") and row.get("pack_temp_c") is not None and 5 <= num(row["pack_temp_c"]) <= 50]
    early = qualified[:min(36, len(qualified))]
    recent = qualified[-min(36, len(qualified)):]
    learned = statistics.median(values("nom_full_kwh", recent)) if recent else None
    live = num(STATE.get("bms", {}).get("nom_full")) or None
    capacity = live or learned
    reference = 77.5
    resistances, spreads = values("resistance_mohm"), values("cell_spread_mv")
    loaded, sags = values("loaded_spread_max_mv"), values("voltage_sag_v")
    points = len(qualified) + min(30, len(resistances))
    weakest_counts = {}
    for row in rows:
        idx = row.get("weakest_brick")
        if idx is not None:
            weakest_counts[str(idx)] = weakest_counts.get(str(idx), 0) + 1
    weakest = max(weakest_counts, key=weakest_counts.get) if weakest_counts else None
    return {
        "days": days, "sample_count": len(rows), "qualified_sample_count": len(qualified),
        "confidence": "high" if points >= 100 else "medium" if points >= 30 else "learning",
        "capacity_kwh": round(capacity, 2) if capacity else None,
        "learned_capacity_kwh": round(learned, 2) if learned else None,
        "baseline_capacity_kwh": reference,
        "observed_baseline_kwh": round(statistics.median(values("nom_full_kwh", early)), 2) if early else None,
        "capacity_retention_pct": round(capacity / reference * 100, 1) if capacity else None,
        "last_observation_at": max((num(row.get("recorded_at")) for row in rows), default=0) or None,
        "resistance_mohm": round(statistics.median(resistances), 2) if len(resistances) >= 3 else None,
        "typical_cell_spread_mv": round(statistics.median(spreads), 1) if spreads else None,
        "worst_loaded_spread_mv": round(max(loaded), 1) if loaded else None,
        "largest_sag_v": round(max(sags), 1) if sags else None,
        "weakest_brick": safe_int(weakest, -1) if weakest is not None else None,
        "peak_discharge_kw": round(max(values("peak_discharge_kw") or [0]), 1),
        "peak_regen_kw": round(max(values("peak_regen_kw") or [0]), 1),
        "trend": [{"t": row.get("recorded_at"), "capacity": row.get("nom_full_kwh"), "resistance": row.get("resistance_mohm"), "spread": row.get("cell_spread_mv"), "sag": row.get("voltage_sag_v"), "temp": row.get("pack_temp_c")} for row in rows],
    }


HISTORY_STORES = {
    "performance": RUN_STORE, "regen": REGEN_STORE, "battery": BATTERY_STORE,
    "efficiency": TRIP_STORE, "charging": CHARGE_STORE, "settings": SETTINGS_STORE,
}


def history_payload(kind="all", limit=100, days=0):
    limit = max(1, min(safe_int(limit, 100), 1000))
    since = time.time() - safe_int(days, 0) * 86400 if safe_int(days, 0) > 0 else None
    if kind in HISTORY_STORES:
        return {kind: HISTORY_STORES[kind].read(limit, since)}
    return {name: store.read(limit, since) for name, store in HISTORY_STORES.items()}


def history_summary(days=30):
    since = time.time() - max(1, safe_int(days, 30)) * 86400
    trips = TRIP_STORE.read(500, since)
    regen = REGEN_STORE.read(500, since)
    charging = CHARGE_STORE.read(100, since)
    runs = RUN_STORE.read(500, since)
    trip_miles = sum(num(row.get("distance_mi")) for row in trips)
    net_kwh = sum(num(row.get("energy_kwh", row.get("net_kwh"))) for row in trips)
    trip_regen_kwh = sum(num(row.get("regen_kwh")) for row in trips)
    regen_event_kwh = sum(num(row.get("energy_recovered_kwh")) for row in regen)
    charge_kwh = sum(num(row.get("energy_added_kwh")) for row in charging)
    weighted_wh = (net_kwh * 1000 / trip_miles) if trip_miles > .05 else None
    return {
        "days": max(1, safe_int(days, 30)), "trip_count": len(trips),
        "distance_mi": round(trip_miles, 1), "net_kwh": round(net_kwh, 2),
        "regen_kwh": round(trip_regen_kwh, 2), "regen_event_kwh": round(regen_event_kwh, 2),
        "charging_kwh": round(charge_kwh, 2),
        "average_wh_per_mi": round(weighted_wh, 0) if weighted_wh is not None else None,
        "performance_runs": len(runs), "database": HISTORY_DB.stats(),
        "recorder": RECORDER.diagnostics(),
    }


def history_export(kind, format_name, limit=1000):
    payload = history_payload(kind, limit)
    generated = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if format_name == "json":
        body = json.dumps({"generated_at": generated, "history": payload}, indent=2, sort_keys=True).encode()
        return body, "application/json", "nap-history-%s.json" % time.strftime("%Y%m%d")
    if format_name != "csv":
        raise ValueError("format must be json or csv")
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["type", "timestamp", "field", "value"])
    for row_kind, rows in payload.items():
        for row in rows:
            created = HistoryDatabase._created(row)
            for field, value in sorted(row.items()):
                if isinstance(value, (dict, list)):
                    value = json.dumps(value, separators=(",", ":"), sort_keys=True)
                writer.writerow([row_kind, created, field, value])
    return output.getvalue().encode(), "text/csv; charset=utf-8", "nap-history-%s.csv" % time.strftime("%Y%m%d")


def state_snapshot(view="summary"):
    navigation_snapshot()
    with LOCK:
        if view == "battery":
            return json.loads(json.dumps(STATE))
        keys = ("ts", "car", "drive", "lead1", "settings", "health", "engagement", "navigation")
        result = {key: json.loads(json.dumps(STATE[key])) for key in keys}
        keep = ("ui_soc", "rated_range", "pack_v", "pack_i", "nom_full", "nom_rem", "buffer", "display_soc", "usable_full", "usable_rem", "max_discharge", "max_regen")
        result["bms"] = {key: STATE["bms"].get(key, 0) for key in keep}
        return result


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
    route_times = {}
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
                    route_times[(route_name, int(seg))] = os.path.getmtime(path)
    except: pass
    routes = []
    for route, segs in routes_dict.items():
        routes.append({"name": route, "segs": sorted(segs),
                       "times": {str(seg): route_times.get((route, seg), 0) for seg in segs}})
    routes.sort(key=lambda x: x["name"], reverse=True)
    return routes

def telemetry():
    svcs = [
        "carState", "selfdriveState", "controlsState",
        "radarState", "deviceState", "can"
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

            recorder_sample = None
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

                recorder_sample = {"car": dict(STATE["car"]), "bms": json.loads(json.dumps(STATE["bms"]))}

            if recorder_sample is not None:
                RECORDER.sample(now, recorder_sample)
            tick += 1
        except Exception: pass
        time.sleep(.05)

def write_setting(name, value):
    before = read_params()
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
    updated = read_params()
    if name in updated and before.get(name) != updated.get(name):
        SETTINGS_STORE.append({"changed_at": time.time(), "name": name,
                               "before": before.get(name), "after": updated.get(name)})
    return updated

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

    return timeline, engagement, t0

def get_mp4_path(route_seg, cam_type="qcamera"):
    if ".." in route_seg or "/" in route_seg or "\\" in route_seg:
        return None
    base_path = f"/data/media/0/realdata/{route_seg}/{cam_type}"
    cam_file = base_path + ".hevc"
    if not os.path.exists(cam_file): cam_file = base_path + ".ts"
    if not os.path.exists(cam_file): return None
      
    tmp_path = f"/dev/shm/vid_{route_seg}_{cam_type}.mp4"
    if not os.path.exists(tmp_path) or os.path.getsize(tmp_path) < 1024 or os.path.getmtime(tmp_path) < os.path.getmtime(cam_file):
        temporary = tmp_path + ".part"
        try:
            proc = subprocess.run(
                ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", cam_file,
                 "-c", "copy", "-movflags", "+faststart", "-f", "mp4", temporary],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120,
            )
            if proc.returncode == 0 and os.path.exists(temporary) and os.path.getsize(temporary) >= 1024:
                os.replace(temporary, tmp_path)
        except Exception:
            pass
        finally:
            try:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            except Exception:
                pass
    return tmp_path if os.path.exists(tmp_path) and os.path.getsize(tmp_path) >= 1024 else None

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

# Release the retired animated dashboard string after module load. Keeping the
# source above makes this revamp easy to audit/revert without serving or polling it.
HTML = None
DASHBOARD_HTML = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<meta name="theme-color" content="#090909">
<title>NAP Telemetry</title>
<style>
:root{color-scheme:dark;--bg:#070706;--surface:#10100f;--surface2:#171715;--line:#2b2a27;--text:#f4f1e8;--muted:#a5a095;--amber:#f4a641;--amber2:#ffcc80;--cyan:#55d6d1;--green:#70d68b;--red:#ff6f69;--blue:#79b8ff;--purple:#b997ff;--radius:18px}
*{box-sizing:border-box}html{background:var(--bg)}body{margin:0;background:linear-gradient(120deg,#070706,#0d0c09 55%,#070706);color:var(--text);font-family:Inter,ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;font-size:16px;line-height:1.35}.shell{max-width:1280px;margin:auto;padding:18px}.top{display:flex;justify-content:space-between;align-items:flex-end;gap:18px;padding:6px 2px 18px}.brand{font-size:1.6rem;font-weight:900;letter-spacing:-.04em}.brand span{color:var(--amber)}.eyebrow{color:var(--muted);font-size:.75rem;font-weight:800;letter-spacing:.14em;text-transform:uppercase;margin-bottom:4px}.connection{text-align:right;color:var(--muted);font-size:.82rem}.connection b{color:var(--green)}
.tabs{position:sticky;top:0;z-index:30;display:flex;gap:4px;overflow-x:auto;padding:5px;background:#11110fee;border:1px solid var(--line);border-radius:15px;backdrop-filter:blur(12px);scrollbar-width:none}.tabs::-webkit-scrollbar{display:none}.tabs button{flex:1 0 auto;min-width:102px;border:0;border-radius:10px;padding:11px 13px;background:transparent;color:var(--muted);font:inherit;font-size:.78rem;font-weight:850;letter-spacing:.07em;text-transform:uppercase;cursor:pointer}.tabs button.active{background:var(--amber);color:#171007}.page{display:none;padding-top:14px}.page.active{display:block}.grid{display:grid;grid-template-columns:repeat(12,1fr);gap:12px}.span3{grid-column:span 3}.span4{grid-column:span 4}.span5{grid-column:span 5}.span6{grid-column:span 6}.span7{grid-column:span 7}.span8{grid-column:span 8}.span12{grid-column:span 12}
.card{background:linear-gradient(145deg,#151513,#0d0d0c);border:1px solid var(--line);border-radius:var(--radius);padding:17px;min-width:0}.card.amber{background:linear-gradient(145deg,#2a1c0d,#15110c);border-color:#59401f}.card.cyan{background:linear-gradient(145deg,#0c2221,#0d1211);border-color:#214b49}.label{color:var(--muted);font-size:.76rem;font-weight:850;letter-spacing:.12em;text-transform:uppercase}.value{font-size:2rem;line-height:1;font-weight:900;letter-spacing:-.045em;margin-top:10px}.value.huge{font-size:4.8rem}.sub{color:var(--muted);font-size:.82rem;margin-top:8px}.accent{color:var(--amber)}.cyan-text{color:var(--cyan)}.good{color:var(--green)}.bad{color:var(--red)}.rule{height:1px;background:var(--line);margin:15px 0}.statusline{display:flex;align-items:center;gap:9px;font-weight:850}.dot{width:9px;height:9px;border-radius:50%;background:var(--muted)}.dot.good{background:var(--green);box-shadow:0 0 12px #70d68b88}.dot.bad{background:var(--red);box-shadow:0 0 12px #ff6f6966}.metric-row{display:flex;justify-content:space-between;align-items:baseline;gap:14px;padding:9px 0;border-bottom:1px solid #24231f}.metric-row:last-child{border:0}.metric-row span{color:var(--muted);font-size:.88rem}.metric-row b{text-align:right}.pill{display:inline-flex;align-items:center;gap:6px;padding:6px 10px;border:1px solid var(--line);border-radius:999px;color:var(--muted);font-size:.75rem;font-weight:800}.pill.live{color:var(--green);border-color:#2e6d3d;background:#0d2514}.count-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:12px}.count{background:#0b0b0a;border:1px solid #24231f;border-radius:12px;padding:12px}.count b{font-size:1.35rem;display:block}.count span{font-size:.72rem;color:var(--muted);text-transform:uppercase;letter-spacing:.08em}
.power-track{display:grid;grid-template-columns:1fr 2px 1fr;height:17px;border-radius:9px;overflow:hidden;background:#22211e;margin-top:16px}.power-zero{background:#f4f1e8}.power-side{position:relative}.power-fill{position:absolute;top:0;bottom:0;width:0;transition:width .25s}.power-side.regen .power-fill{right:0;background:var(--cyan)}.power-side.drive .power-fill{left:0;background:var(--amber)}.bricks{display:grid;grid-template-columns:repeat(16,1fr);gap:5px;margin-top:14px}.brick{aspect-ratio:1;display:flex;align-items:center;justify-content:center;border-radius:5px;background:#22211f;color:#fff;font:700 .68rem ui-monospace,monospace}.brick.low{background:#a83f3b}.brick.high{background:#286a75}.brick.ok{background:#287340}.brick.none{color:#69665e;background:#161513}
.controls{display:flex;gap:8px;flex-wrap:wrap}.button,.choice,select{border:1px solid #393732;background:#1b1a18;color:var(--text);border-radius:11px;padding:11px 13px;font:inherit;font-size:.85rem;font-weight:800;cursor:pointer}.button:hover,.choice:hover{border-color:#6b6459}.button.primary,.choice.active{background:var(--amber);border-color:var(--amber);color:#181006}.button.danger{color:var(--red)}.choice{flex:1}.switch-row{display:flex;align-items:center;justify-content:space-between;gap:15px;padding:13px 0;border-bottom:1px solid var(--line)}.switch-row:last-child{border:0}.switch{width:52px;height:29px;border:0;border-radius:16px;background:#3a3833;position:relative;cursor:pointer;flex:none}.switch i{position:absolute;top:3px;left:3px;width:23px;height:23px;background:#f8f6ef;border-radius:50%;transition:left .15s}.switch.on{background:var(--green)}.switch.on i{left:26px}.follow{display:grid;grid-template-columns:repeat(7,1fr);gap:6px;margin-top:10px}.follow .choice{padding:10px 2px}
.chart{width:100%;height:190px;background:#090908;border:1px solid #24231f;border-radius:12px;margin-top:12px}.filters{display:flex;gap:8px;flex-wrap:wrap}.filters .button.active{background:var(--amber);border-color:var(--amber);color:#181006}.history-list{margin-top:12px}.history-item{display:grid;grid-template-columns:9rem 1fr auto;gap:14px;align-items:center;padding:14px 2px;border-bottom:1px solid var(--line)}.history-item:last-child{border:0}.history-kind{color:var(--amber);font-size:.74rem;font-weight:900;letter-spacing:.1em;text-transform:uppercase}.history-title{font-weight:850}.history-meta{color:var(--muted);font-size:.78rem;margin-top:4px}.history-result{font-size:1.05rem;font-weight:900;text-align:right}.empty{padding:32px 12px;text-align:center;color:var(--muted)}.diag{font:500 .78rem ui-monospace,monospace;color:var(--muted);word-break:break-word}
.video-wrap{position:relative;aspect-ratio:16/9;background:#000;border:1px solid var(--line);border-radius:16px;overflow:hidden}video{width:100%;height:100%}.hud{display:none;position:absolute;inset:0;pointer-events:none;padding:16px;justify-content:space-between;flex-direction:column;background:linear-gradient(#0008,transparent 28%,transparent 72%,#0009)}.hud.on{display:flex}.hud-row{display:flex;justify-content:space-between;align-items:center;gap:10px}.hud-box{padding:8px 12px;border-radius:9px;background:#050505b8;border:1px solid #ffffff30;font-weight:900}.timeline{width:100%;accent-color:var(--amber)}.routes{margin-top:12px}.route{padding:14px 0;border-bottom:1px solid var(--line)}.segments{display:flex;gap:7px;flex-wrap:wrap;margin-top:9px}.segment{padding:8px 10px}.segment.playing{background:var(--amber);color:#181006}.toast{position:fixed;right:20px;bottom:20px;z-index:50;padding:11px 15px;border-radius:12px;background:#211b11;border:1px solid #765526;color:var(--amber2);opacity:0;transform:translateY(12px);transition:.2s}.toast.on{opacity:1;transform:translateY(0)}
@media(max-width:900px){.span3,.span4{grid-column:span 6}.span5,.span6,.span7,.span8{grid-column:span 12}}
@media(max-width:600px){.shell{padding:10px}.top{padding:7px 3px 13px}.brand{font-size:1.35rem}.connection span{display:none}.span3,.span4,.span5,.span6,.span7,.span8,.span12{grid-column:span 12}.value{font-size:1.8rem}.value.huge{font-size:3.9rem}.bricks{grid-template-columns:repeat(12,1fr);gap:3px}.brick{font-size:.55rem}.history-item{grid-template-columns:1fr auto}.history-kind{grid-column:1/-1}.count-grid{grid-template-columns:repeat(2,1fr)}.tabs button{min-width:94px}}
</style></head><body><div class="shell">
<header class="top"><div><div class="eyebrow">Not Auto Pilot</div><div class="brand">NAP <span>Telemetry</span></div></div><div class="connection"><span>LOW-BANDWIDTH CONSOLE · </span><b id="top-status">CONNECTING</b><div id="top-age">Waiting for comma</div></div></header>
<nav class="tabs" aria-label="Dashboard sections">
 <button class="active" data-tab="overview">Overview</button><button data-tab="battery">Battery</button><button data-tab="energy">Energy</button><button data-tab="performance">Performance</button><button data-tab="history">History</button><button data-tab="cameras">Cameras</button><button data-tab="settings">Settings</button>
</nav>

<main>
<section id="page-overview" class="page active"><div class="grid">
 <article class="card amber span6"><div class="label">Vehicle state</div><div class="value huge" id="overview-speed">0</div><div class="sub">MPH · <b id="overview-drive">STANDBY</b></div><div class="rule"></div><div class="statusline"><i id="recorder-dot" class="dot"></i><span id="recorder-line">Recorder checking…</span></div></article>
 <article class="card cyan span6"><div class="label">Battery now</div><div class="value cyan-text"><span id="overview-soc">--</span>%</div><div class="sub"><span id="overview-power">--</span> kW · <span id="overview-range">--</span> mi rated</div><div class="power-track"><div class="power-side regen"><i id="overview-regen-bar" class="power-fill"></i></div><i class="power-zero"></i><div class="power-side drive"><i id="overview-drive-bar" class="power-fill"></i></div></div></article>
 <article class="card span4"><div class="label">This drive</div><div class="metric-row"><span>OpenPilot</span><b id="overview-engaged">--</b></div><div class="metric-row"><span>Lead</span><b id="overview-lead">--</b></div><div class="metric-row"><span>Cruise target</span><b id="overview-cruise">--</b></div></article>
 <article class="card span4"><div class="label">System</div><div class="metric-row"><span>CPU</span><b id="overview-cpu">--</b></div><div class="metric-row"><span>Storage</span><b id="overview-storage">--</b></div><div class="metric-row"><span>Uptime</span><b id="overview-uptime">--</b></div></article>
 <article class="card span4"><div class="label">Navigation</div><div id="nav-state" class="value" style="font-size:1.35rem">No route</div><div id="nav-detail" class="sub">Phone guidance remains available without the animated road scene.</div></article>
 <article class="card span12"><div style="display:flex;justify-content:space-between;gap:10px;align-items:center"><div><div class="label">Recording ledger</div><div class="sub">A visible receipt for everything actually written to disk.</div></div><button class="button" onclick="loadDiagnostics()">Check now</button></div><div id="overview-counts" class="count-grid"></div><div class="rule"></div><div id="overview-db" class="diag">Checking database…</div></article>
</div></section>

<section id="page-battery" class="page"><div class="grid">
 <article class="card cyan span5"><div class="label">Pack power</div><div class="value"><span id="bms-kw">0.0</span> kW</div><div class="sub"><span id="bms-pack-v">0.0</span> V · <span id="bms-pack-i">0.0</span> A</div><div class="power-track"><div class="power-side regen"><i id="bms-regen-bar" class="power-fill"></i></div><i class="power-zero"></i><div class="power-side drive"><i id="bms-drive-bar" class="power-fill"></i></div></div><div class="sub"><span id="bms-max-regen">0</span> kW regen available · <span id="bms-max-drive">0</span> kW discharge available</div></article>
 <article class="card span7"><div class="label">Energy state</div><div class="grid" style="margin-top:8px"><div class="span4"><div class="value" id="bms-display-soc">--%</div><div class="sub">Usable SOC</div></div><div class="span4"><div class="value" id="bms-nom-full">--</div><div class="sub">Nominal full kWh</div></div><div class="span4"><div class="value" id="bms-range">--</div><div class="sub">Rated miles</div></div></div></article>
 <article class="card span6"><div class="label">BMS energy registers</div><div id="bms-energy-kv"></div></article>
 <article class="card span6"><div class="label">Thermal & balance</div><div id="bms-health-kv"></div></article>
 <article class="card span12"><div style="display:flex;justify-content:space-between;gap:10px"><div class="label">96 brick voltages</div><div id="brick-average" class="pill">AVG --</div></div><div id="brick-grid" class="bricks"></div><div class="sub">Red is more than 10 mV below pack average · blue is more than 10 mV above · green is within band.</div></article>
 <article class="card span12"><div class="controls" style="justify-content:space-between"><div><div class="label">Battery health trend</div><div id="battery-confidence" class="sub">Learning from five-minute samples</div></div><div class="controls"><button class="button health-days" data-days="7">7D</button><button class="button health-days active" data-days="30">30D</button><button class="button health-days" data-days="90">90D</button></div></div><div class="count-grid" id="battery-health-counts"></div><canvas id="battery-health-chart" class="chart" width="1000" height="220"></canvas></article>
</div></section>

<section id="page-energy" class="page"><div class="grid">
 <article class="card span4"><div class="label">Pack power</div><div class="value" id="energy-power">--</div><div class="sub" id="energy-direction">Idle</div></article><article class="card span4"><div class="label">Active trip</div><div class="value" id="energy-distance">--</div><div class="sub" id="energy-efficiency">Waiting for movement</div></article><article class="card span4"><div class="label">Regen now</div><div class="value cyan-text" id="energy-regen">--</div><div class="sub" id="energy-regen-state">No regen event</div></article>
 <article class="card span12"><div class="label">Resettable trip meters</div><div class="grid" id="trip-meters" style="margin-top:12px"></div></article>
 <article class="card span7"><div class="label">Recent 60 seconds</div><div class="sub">Pack voltage and signed power · sampled locally, delivered only while this page is open.</div><canvas id="energy-chart" class="chart" width="900" height="210"></canvas></article>
 <article class="card span5"><div class="label">Charging session</div><div id="charge-status" class="value" style="font-size:1.5rem">Waiting</div><div id="charge-detail" class="sub">Sustained stationary charging is logged after ten seconds.</div><canvas id="charge-chart" class="chart" width="600" height="210"></canvas></article>
</div></section>

<section id="page-performance" class="page"><div class="grid">
 <article class="card amber span5" style="text-align:center"><div id="perf-chip" class="pill">READY</div><div class="value huge" id="perf-speed">0</div><div class="label">MPH</div><div class="value" style="font-size:1.5rem"><span id="perf-g">0.000</span> G</div><div id="perf-last" class="sub">Automatic recording remains active on the comma.</div></article>
 <article class="card span7"><div class="label">Automatic milestones</div><div id="perf-milestones" class="count-grid"></div><div class="rule"></div><div class="sub">Standing: 0–10 through 0–100, ⅛ and ¼ mile · Rolls: 20–60, 30–70, 40–80, 50–100, 60–100 · Braking: 40/50/60–0.</div></article>
 <article class="card span12"><div class="label">Saved runs</div><div id="performance-history" class="history-list"><div class="empty">Loading runs…</div></div></article>
</div></section>

<section id="page-history" class="page"><div class="grid">
 <article class="card span12"><div class="controls" style="justify-content:space-between"><div><div class="label">History explorer</div><div class="sub">Read every recorder directly from SQLite. Empty now means empty on disk—not hidden by the interface.</div></div><div class="controls"><button class="button" onclick="exportHistory('json')">JSON</button><button class="button" onclick="exportHistory('csv')">CSV</button><button class="button primary" onclick="loadHistory()">Refresh</button></div></div><div id="history-summary" class="count-grid"></div></article>
 <article class="card span12"><div class="filters" id="history-filters"><button class="button active" data-kind="all">All</button><button class="button" data-kind="efficiency">Trips</button><button class="button" data-kind="battery">Battery</button><button class="button" data-kind="regen">Regen</button><button class="button" data-kind="charging">Charging</button><button class="button" data-kind="performance">Performance</button><button class="button" data-kind="settings">Settings</button></div><div id="history-list" class="history-list"><div class="empty">Choose Refresh to inspect disk history.</div></div></article>
 <article class="card span12"><div class="label">Recorder diagnostics</div><div id="history-diag" class="diag">Checking…</div></article>
</div></section>

<section id="page-cameras" class="page"><div class="grid"><article class="card span12">
 <div class="video-wrap"><video id="player" playsinline></video><div id="hud" class="hud"><div class="hud-row"><div id="hud-left" class="hud-box">◀</div><div class="hud-box">LEAD <span id="hud-lead">--</span> m</div><div id="hud-right" class="hud-box">▶</div></div><div class="hud-row"><div class="hud-box"><span id="hud-speed">0</span> MPH</div><div id="hud-pack" class="hud-box">PACK DATA --</div><div class="hud-box">STR <span id="hud-steer">0</span>° · <span id="hud-pedals">COAST</span></div></div></div></div>
 <div class="controls" style="margin-top:12px"><select id="cam-select"><option value="qcamera">Road · Fast</option><option value="fcamera">Road · High resolution</option><option value="dcamera">Driver camera</option></select><button class="button" onclick="togglePlay()">Play / Pause</button><button class="button" onclick="toggleHud()">Telemetry HUD</button><button id="export-video" class="button primary" onclick="exportVideo()">Export clip + HUD</button></div>
 <div class="rule"></div><div style="display:flex;justify-content:space-between;gap:10px"><b id="timeline-title">Select a segment</b><span id="timeline-time" class="sub">00:00 / 00:00</span></div><input id="timeline" class="timeline" type="range" min="0" max="60" step=".1" value="0"><div id="engagement-track" style="height:6px;background:#282722;border-radius:4px;margin-top:5px"></div>
 </article><article class="card span12"><div class="label">Dashcam archive</div><div id="routes" class="routes"><div class="empty">Open Cameras to load routes.</div></div></article></div></section>

<section id="page-settings" class="page"><div class="grid">
 <article class="card amber span6"><div class="label">Driving personality</div><div class="controls" style="margin-top:12px"><button class="choice personality" data-value="2">Chill</button><button class="choice personality" data-value="1">Standard</button><button class="choice personality" data-value="0">Aggressive</button></div><div class="rule"></div><div class="label">Follow distance</div><div id="follow" class="follow"></div></article>
 <article class="card span6"><div class="label">Feature controls</div><div class="switch-row"><div><b>Experimental mode</b><div class="sub">OpenPilot experimental behavior</div></div><button id="setting-experimental" class="switch" onclick="toggleSetting('experimental')"><i></i></button></div><div class="switch-row"><div><b>Adaptive acceleration</b><div class="sub">NAP acceleration tuning</div></div><button id="setting-adaptive_accel" class="switch" onclick="toggleSetting('adaptive_accel')"><i></i></button></div><div class="switch-row"><div><b>+5 MPH speed offset</b><div class="sub">Pre-AP JSON bridge</div></div><button id="setting-speed_offset" class="switch" onclick="toggleSetting('speed_offset')"><i></i></button></div></article>
 <article class="card span6"><div class="label">Cruise speed trim</div><div class="value"><span id="speed-trim">0</span> MPH</div><div class="controls" style="margin-top:14px"><button class="button" onclick="trim(-5)">−5</button><button class="button" onclick="trim(-1)">−1</button><button class="button primary" onclick="setSetting('speed_trim',0)">Reset</button><button class="button" onclick="trim(1)">+1</button><button class="button" onclick="trim(5)">+5</button></div><div class="sub">Changes remain written through the existing atomic NAP settings bridge.</div></article>
 <article class="card span6"><div class="label">Recent setting changes</div><div id="settings-audit" class="history-list"><div class="empty">No recorded changes yet.</div></div></article>
</div></section>
</main></div><div id="toast" class="toast"></div>
<script>
const $=id=>document.getElementById(id);let activeTab='overview',state={settings:{}},pollTimer=null,historyKind='all',historyData={},routesLoaded=false,currentRoute=null,currentSeg=null,logData=[],batteryLogData=[],hudOn=false,healthDays=30,perfHistoryTick=0;
const esc=v=>String(v==null?'':v).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const n=(v,d=0)=>Number.isFinite(Number(v))?Number(v):d;const stamp=v=>v?new Date(n(v)*1000).toLocaleString():'never';
function toast(text,bad=false){let el=$('toast');el.textContent=text;el.style.borderColor=bad?'#843d39':'#765526';el.classList.add('on');setTimeout(()=>el.classList.remove('on'),1800)}
function humanBytes(v){v=n(v);if(v<1024)return v+' B';if(v<1048576)return (v/1024).toFixed(1)+' KB';return (v/1048576).toFixed(1)+' MB'}
function humanTime(s){s=Math.max(0,n(s));let d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);return d?`${d}d ${h}h`:h?`${h}h ${m}m`:`${m}m`}
function fmtClock(s){s=Math.max(0,n(s));return `${String(Math.floor(s/60)).padStart(2,'0')}:${String(Math.floor(s%60)).padStart(2,'0')}`}
document.querySelectorAll('.tabs button').forEach(button=>button.onclick=()=>switchTab(button.dataset.tab));
function switchTab(tab){activeTab=tab;document.querySelectorAll('.tabs button').forEach(b=>b.classList.toggle('active',b.dataset.tab===tab));document.querySelectorAll('.page').forEach(p=>p.classList.toggle('active',p.id==='page-'+tab));if(tab==='history')loadHistory();if(tab==='cameras')loadRoutes();if(tab==='battery')loadBatteryHealth();if(tab==='settings')loadSettingsAudit();schedulePoll(true)}
function schedulePoll(immediate=false){clearTimeout(pollTimer);let delay=activeTab==='performance'?500:activeTab==='battery'?2500:activeTab==='energy'?2000:activeTab==='overview'||activeTab==='settings'?3000:0;if(!delay)return;pollTimer=setTimeout(async()=>{if(['energy','performance'].includes(activeTab))await loadLab(activeTab==='performance'&&perfHistoryTick++%40===0);else await loadState(activeTab==='battery'?'battery':'summary');schedulePoll()},immediate?0:delay)}
async function fetchJson(url,options){let response=await fetch(url,{cache:'no-store',...options});if(!response.ok)throw Error('HTTP '+response.status);return response.json()}
async function loadState(view='summary'){try{let data=await fetchJson('/api/state?view='+view);renderState(data);if(view==='battery')renderBMS(data.bms||{});connected(true,data.ts)}catch(e){connected(false)}}
function connected(ok,ts){$('top-status').textContent=ok?'CONNECTED':'OFFLINE';$('top-status').className=ok?'good':'bad';$('top-age').textContent=ok&&ts?'Updated '+new Date(ts*1000).toLocaleTimeString():'No response from comma'}
function renderState(s){state=s;let c=s.car||{},b=s.bms||{},drive=s.drive||{},lead=s.lead1||{},q=s.settings||{},health=s.health||{},power=-(n(b.pack_v)*n(b.pack_i))/1000,mph=n(c.vEgo)*2.236936;
 setText('overview-speed',Math.round(mph));setText('overview-drive',drive.active?'ENGAGED':drive.enabled?'READY':'STANDBY');setText('overview-soc',n(b.ui_soc).toFixed(1));setText('overview-power',(power>0?'+':'')+power.toFixed(1));setText('overview-range',Math.round(n(b.rated_range)));setText('overview-engaged',drive.active?'ACTIVE':'OFF');setText('overview-lead',lead.status?n(lead.dRel).toFixed(1)+' m':'None');setText('overview-cruise',n(c.vCruise)>5?Math.round(n(c.vCruise)*.621371)+' mph':'--');setText('overview-cpu',Math.round(n(health.temp))+'°C');setText('overview-storage',n(health.storagePct).toFixed(0)+'% used');setText('overview-uptime',humanTime(health.uptime));
 $('overview-regen-bar').style.width=Math.min(100,Math.max(0,-power)/80*100)+'%';$('overview-drive-bar').style.width=Math.min(100,Math.max(0,power)/250*100)+'%';renderSettings(q);let nav=s.navigation||{},m=nav.maneuver||{};setText('nav-state',nav.connected?(m.primary_text||nav.route_state):'No route');setText('nav-detail',nav.connected?`${Math.round(n(m.distance_m)*3.28084)} ft · ${m.secondary_text||nav.route_state}`:'Phone guidance remains available without the animated road scene.')}
function setText(id,value){let el=$(id);if(el)el.textContent=value}
async function loadDiagnostics(){try{let d=await fetchJson('/api/history/status');renderDiagnostics(d)}catch(e){toast('Recorder check failed',true)}}
function renderDiagnostics(d){let r=d.recorder||{},db=d.database||{},types=db.types||{};$('recorder-dot').className='dot '+(r.running&&db.ready&&db.dropped===0?'good':'bad');setText('recorder-line',r.running&&db.ready?'Recorder active · disk online':'Recorder needs attention');let order=['efficiency','battery','regen','charging','performance','settings'];$('overview-counts').innerHTML=order.map(k=>`<div class="count"><b>${n(types[k]&&types[k].count)}</b><span>${k}</span></div>`).join('');$('overview-db').textContent=`DB ${db.ready?'READY':'DOWN'} · ${humanBytes(db.size_bytes)} · queue ${n(db.queue_depth)} · written this boot ${n(db.written)} · dropped ${n(db.dropped)} · errors ${n(db.write_errors)} · last write ${stamp(db.last_write_at)}${db.last_error?' · '+db.last_error:''}`}
function kv(target,rows){$(target).innerHTML=rows.map(x=>`<div class="metric-row"><span>${esc(x[0])}</span><b>${esc(x[1])}</b></div>`).join('')}
function renderBMS(b){let pv=n(b.pack_v),pi=n(b.pack_i),power=-(pv*pi)/1000,cells=(b.bricks||[]).map(Number).filter(v=>v>2000&&v<5000),temps=Object.values(b.temps_dict||{}).map(Number).filter(v=>v>-40&&v<120),min=cells.length?Math.min(...cells):0,max=cells.length?Math.max(...cells):0,avg=cells.length?cells.reduce((a,v)=>a+v,0)/cells.length:0,usable=n(b.usable_full,Math.max(0,n(b.nom_full)-n(b.buffer))),remain=n(b.usable_rem,Math.max(0,n(b.nom_rem)-n(b.buffer))),display=n(b.display_soc,usable?remain/usable*100:0);
 setText('bms-kw',(power>0?'+':'')+power.toFixed(1));setText('bms-pack-v',pv.toFixed(1));setText('bms-pack-i',pi.toFixed(1));setText('bms-display-soc',display.toFixed(1)+'%');setText('bms-nom-full',n(b.nom_full).toFixed(1));setText('bms-range',Math.round(n(b.rated_range)));setText('bms-max-regen',n(b.max_regen).toFixed(0));setText('bms-max-drive',n(b.max_discharge).toFixed(0));$('bms-regen-bar').style.width=Math.min(100,Math.max(0,-power)/Math.max(60,n(b.max_regen))*100)+'%';$('bms-drive-bar').style.width=Math.min(100,Math.max(0,power)/Math.max(160,n(b.max_discharge))*100)+'%';
 kv('bms-energy-kv',[['Raw BMS SOC',n(b.ui_soc).toFixed(1)+'%'],['Nominal remaining',n(b.nom_rem).toFixed(1)+' kWh'],['Usable full',usable.toFixed(1)+' kWh'],['Usable remaining',remain.toFixed(1)+' kWh'],['Expected remaining',n(b.expected_rem).toFixed(1)+' kWh'],['Ideal remaining',n(b.ideal_rem).toFixed(1)+' kWh'],['Energy buffer',n(b.buffer).toFixed(1)+' kWh'],['Energy to full',n(b.charge_complete).toFixed(1)+' kWh']]);let ctof=v=>v*9/5+32;kv('bms-health-kv',[['Cell range',cells.length?(min/1000).toFixed(3)+'–'+(max/1000).toFixed(3)+' V':'--'],['Cell spread',cells.length?(max-min).toFixed(1)+' mV':'--'],['Average cell',cells.length?(avg/1000).toFixed(3)+' V':'--'],['Temperature range',temps.length?ctof(Math.min(...temps)).toFixed(1)+'–'+ctof(Math.max(...temps)).toFixed(1)+' °F':'--'],['Average temperature',temps.length?ctof(temps.reduce((a,v)=>a+v,0)/temps.length).toFixed(1)+' °F':'--'],['Capacity vs 77.5 kWh',n(b.nom_full)?(n(b.nom_full)/77.5*100).toFixed(1)+'%':'--']]);setText('brick-average',cells.length?'AVG '+(avg/1000).toFixed(3)+' V':'NO CELL DATA');$('brick-grid').innerHTML=(b.bricks||Array(96).fill(0)).map(v=>{let cls=v<2000?'none':v<avg-10?'low':v>avg+10?'high':'ok';return `<div class="brick ${cls}" title="${Math.round(n(v))} mV">${v>2000?(v/1000).toFixed(2):'--'}</div>`}).join('')}
document.querySelectorAll('.health-days').forEach(b=>b.onclick=()=>loadBatteryHealth(+b.dataset.days));async function loadBatteryHealth(days=healthDays){healthDays=days;document.querySelectorAll('.health-days').forEach(b=>b.classList.toggle('active',+b.dataset.days===days));try{let r=await fetchJson('/api/battery/health?days='+days);setText('battery-confidence',`${String(r.confidence||'learning').toUpperCase()} · ${n(r.sample_count)} saved observations · latest ${stamp(r.last_observation_at)}`);$('battery-health-counts').innerHTML=[['Capacity',r.capacity_retention_pct==null?'Learning':n(r.capacity_retention_pct).toFixed(1)+'%'],['Resistance',r.resistance_mohm==null?'--':n(r.resistance_mohm).toFixed(1)+' mΩ'],['Typical spread',r.typical_cell_spread_mv==null?'--':n(r.typical_cell_spread_mv).toFixed(1)+' mV'],['Largest sag',r.largest_sag_v==null?'--':n(r.largest_sag_v).toFixed(1)+' V'],['Weak brick',r.weakest_brick==null?'--':'#'+(n(r.weakest_brick)+1)],['Peak drive / regen',`${n(r.peak_discharge_kw).toFixed(0)} / ${n(r.peak_regen_kw).toFixed(0)} kW`]].map(x=>`<div class="count"><b>${x[1]}</b><span>${x[0]}</span></div>`).join('');drawMultiChart('battery-health-chart',r.trend||[],[{key:'capacity',color:'#79b8ff'},{key:'resistance',color:'#b997ff'},{key:'spread',color:'#f4a641'}])}catch(e){toast('Battery health unavailable',true)}}
async function loadLab(history=false){try{let d=await fetchJson('/api/lab/state'+(history?'?history=1':''));renderLab(d);renderDiagnostics({recorder:d.recorder,database:d.database})}catch(e){toast('Analytics unavailable',true)}}
function renderLab(d){let live=d.live||{},trip=d.trip||{},energy=d.energy||{},regen=d.regen,charge=d.charge||d.last_charge,power=n(energy.power_kw),miles=n(trip.distance_m)*.000621371,wh=miles>.05?n(trip.energy_kwh)*1000/miles:null;setText('energy-power',(power>0?'+':'')+power.toFixed(1)+' kW');setText('energy-direction',power>1?'Driving':power<-1?'Charging / regen':'Idle');setText('energy-distance',miles.toFixed(1)+' mi');setText('energy-efficiency',wh==null?'Waiting for distance':Math.round(wh)+' Wh/mi');setText('energy-regen',n(energy.regen_kw).toFixed(1)+' kW');setText('energy-regen-state',regen?n(regen.energy_recovered_kwh).toFixed(3)+' kWh this event':'No regen event');renderMeters(d.trip_meters||{});drawEnergy(d.battery_minute||[]);setText('charge-status',d.charge?'Charging now':charge?'Last session':'Waiting');setText('charge-detail',charge?`${n(charge.energy_added_kwh).toFixed(2)} kWh · ${n(charge.peak_kw).toFixed(0)} kW peak`:'Sustained stationary charging is logged after ten seconds.');drawCharge(charge&&charge.curve||[]);setText('perf-speed',Math.round(n(live.speed_mph)));setText('perf-g',n(live.accel_g).toFixed(3));let run=live.drag||d.last_run,m=run&&run.milestones_s||{};setText('perf-chip',live.drag?'RECORDING':run?'LAST RUN':'READY');setText('perf-last',run?`${String(run.kind||'run').toUpperCase()} · ${runResult(run)} · peak ${n(run.peak_accel_g).toFixed(2)} G`:'Automatic recording remains active on the comma.');let keys=['0-10','0-30','0-60','0-100','1/8-mile','1/4-mile'];$('perf-milestones').innerHTML=keys.map(k=>`<div class="count"><b>${m[k]==null?'--':n(m[k]).toFixed(2)+' s'}</b><span>${k}</span></div>`).join('');if(Array.isArray(d.recent_runs))renderPerformance(d.recent_runs)}
function renderMeters(meters){$('trip-meters').innerHTML=['A','B','SHIFT'].map(name=>{let m=meters[name]||{},mi=n(m.distance_m)*.000621371,eff=mi>.05?n(m.net_kwh)*1000/mi:0;return `<div class="card span4"><div style="display:flex;justify-content:space-between"><b>${name==='SHIFT'?'WORK SHIFT':'TRIP '+name}</b><span class="pill ${m.enabled?'live':''}">${m.enabled?'RUNNING':'PAUSED'}</span></div><div class="value">${mi.toFixed(1)} mi</div><div class="sub">${Math.round(eff)} Wh/mi · ${n(m.regen_kwh).toFixed(2)} kWh regen · ${humanTime(m.elapsed_s)}</div><div class="controls" style="margin-top:12px"><button class="button" onclick="tripAction('${name}','toggle')">Start / pause</button><button class="button" onclick="tripAction('${name}','reset')">Reset + log</button></div></div>`}).join('')}
async function tripAction(name,action){try{let d=await fetchJson('/api/trip',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,action})});renderMeters(d.trip_meters);toast('Trip meter updated')}catch(e){toast('Trip meter failed',true)}}
function drawAxes(ctx,w,h){ctx.clearRect(0,0,w,h);ctx.strokeStyle='#292822';ctx.lineWidth=1;for(let i=1;i<4;i++){ctx.beginPath();ctx.moveTo(35,i*h/4);ctx.lineTo(w-8,i*h/4);ctx.stroke()}}
function line(ctx,vals,color,w,h){vals=vals.filter(v=>Number.isFinite(v));if(vals.length<2)return;let lo=Math.min(...vals),hi=Math.max(...vals),span=Math.max(.1,hi-lo);ctx.strokeStyle=color;ctx.lineWidth=3;ctx.beginPath();vals.forEach((v,i)=>{let x=36+i*(w-46)/(vals.length-1),y=9+(hi-v)/span*(h-18);i?ctx.lineTo(x,y):ctx.moveTo(x,y)});ctx.stroke()}
function drawEnergy(points){let c=$('energy-chart'),ctx=c.getContext('2d');drawAxes(ctx,c.width,c.height);line(ctx,points.map(p=>n(p.v)).filter(v=>v>0),'#79b8ff',c.width,c.height);line(ctx,points.map(p=>n(p.kw)),'#f4a641',c.width,c.height)}
function drawCharge(points){let c=$('charge-chart'),ctx=c.getContext('2d');drawAxes(ctx,c.width,c.height);line(ctx,points.map(p=>n(p.kw)).filter(v=>v>0),'#b997ff',c.width,c.height)}
function drawMultiChart(id,rows,series){let c=$(id),ctx=c.getContext('2d');drawAxes(ctx,c.width,c.height);series.forEach(s=>line(ctx,rows.map(r=>n(r[s.key],NaN)),s.color,c.width,c.height))}
function runResult(r){if(r.result_s!=null)return n(r.result_s).toFixed(2)+' s';let m=r.milestones_s||{};if(m['1/4-mile']!=null)return n(m['1/4-mile']).toFixed(2)+' s';if(m['0-60']!=null)return '0–60 '+n(m['0-60']).toFixed(2)+' s';return r.complete?'Complete':'Partial'}
function renderPerformance(rows){$('performance-history').innerHTML=rows.length?rows.map(r=>`<div class="history-item"><div class="history-kind">${esc(r.kind||'run')}</div><div><div class="history-title">${esc(r.preset||Object.keys(r.milestones_s||{}).join(' · ')||'Automatic capture')}</div><div class="history-meta">${stamp(r.completed_at||r.started_at)} · ${n(r.peak_discharge_kw).toFixed(0)} kW peak · ${n(r.worst_cell_spread_mv).toFixed(1)} mV spread</div></div><div class="history-result">${runResult(r)}</div></div>`).join(''):'<div class="empty">No saved runs.</div>'}
document.querySelectorAll('#history-filters .button').forEach(b=>b.onclick=()=>{historyKind=b.dataset.kind;document.querySelectorAll('#history-filters .button').forEach(x=>x.classList.toggle('active',x===b));loadHistory()});
async function loadHistory(){try{let [payload,summary,status]=await Promise.all([fetchJson(`/api/history?type=${historyKind}&limit=250`),fetchJson('/api/history/summary?days=30'),fetchJson('/api/history/status')]);historyData=payload;renderHistory(payload);renderHistorySummary(summary);renderHistoryDiag(status)}catch(e){$('history-list').innerHTML='<div class="empty bad">History could not be read.</div>';toast('History read failed',true)}}
function renderHistorySummary(s){$('history-summary').innerHTML=[['Miles',n(s.distance_mi).toFixed(1)],['Trips',n(s.trip_count)],['Average',s.average_wh_per_mi==null?'--':Math.round(n(s.average_wh_per_mi))+' Wh/mi'],['Regen',n(s.regen_kwh).toFixed(2)+' kWh'],['Charged',n(s.charging_kwh).toFixed(2)+' kWh'],['Runs',n(s.performance_runs)]].map(x=>`<div class="count"><b>${x[1]}</b><span>${x[0]} · 30D</span></div>`).join('')}
function primaryFor(kind,r){if(kind==='efficiency')return [`${n(r.distance_mi).toFixed(1)} mi`,r.wh_per_mi==null?'--':Math.round(n(r.wh_per_mi))+' Wh/mi'];if(kind==='battery')return [`${n(r.nom_full_kwh).toFixed(1)} kWh nominal`,r.cell_spread_mv==null?'--':n(r.cell_spread_mv).toFixed(1)+' mV'];if(kind==='regen')return [`${n(r.energy_recovered_kwh).toFixed(3)} kWh recovered`,n(r.peak_kw).toFixed(0)+' kW'];if(kind==='charging')return [`${n(r.energy_added_kwh).toFixed(2)} kWh added`,n(r.peak_kw).toFixed(0)+' kW'];if(kind==='performance')return [String(r.kind||'run')+' '+String(r.preset||''),runResult(r)];if(kind==='settings')return [`${r.name}: ${String(r.before)} → ${String(r.after)}`,'changed'];return ['Saved record','']}
function renderHistory(payload){let rows=[];Object.entries(payload).forEach(([kind,list])=>(list||[]).forEach(r=>rows.push({kind,r,t:n(r.completed_at||r.ended_at||r.recorded_at||r.changed_at||r.started_at)})));rows.sort((a,b)=>b.t-a.t);$('history-list').innerHTML=rows.length?rows.map(x=>{let p=primaryFor(x.kind,x.r);return `<div class="history-item"><div class="history-kind">${esc(x.kind)}</div><div><div class="history-title">${esc(p[0])}</div><div class="history-meta">${stamp(x.t)} · ${esc(JSON.stringify(x.r).slice(0,180))}</div></div><div class="history-result">${esc(p[1])}</div></div>`}).join(''):'<div class="empty">No records of this type are stored yet.</div>'}
function renderHistoryDiag(d){let r=d.recorder||{},db=d.database||{};$('history-diag').textContent=`recorder.running=${!!r.running} · recorder.samples=${n(r.sample_count)} · sample.age=${r.last_sample_age_s==null?'never':r.last_sample_age_s+'s'} · segment=${r.segment||'none'} · db.ready=${!!db.ready} · db.bytes=${n(db.size_bytes)} · queue=${n(db.queue_depth)} · dropped=${n(db.dropped)} · errors=${n(db.write_errors)} · last.write=${stamp(db.last_write_at)}${r.last_error?' · recorder.error='+r.last_error:''}${db.last_error?' · db.error='+db.last_error:''}`;renderDiagnostics(d)}
function exportHistory(format){window.location.href=`/api/history/export?type=${encodeURIComponent(historyKind)}&format=${format}&limit=1000`}
function renderSettings(q){['experimental','adaptive_accel','speed_offset'].forEach(k=>{let el=$('setting-'+k);if(el)el.classList.toggle('on',!!q[k])});document.querySelectorAll('.personality').forEach(b=>b.classList.toggle('active',+b.dataset.value===n(q.personality_raw,1)));document.querySelectorAll('#follow .choice').forEach(b=>b.classList.toggle('active',+b.dataset.value===n(q.follow_distance,4)));setText('speed-trim',(n(q.speed_trim)>0?'+':'')+n(q.speed_trim).toFixed(0))}
for(let i=1;i<=7;i++)$('follow').insertAdjacentHTML('beforeend',`<button class="choice" data-value="${i}">${i}</button>`);document.querySelectorAll('#follow .choice').forEach(b=>b.onclick=()=>setSetting('follow_distance',+b.dataset.value));document.querySelectorAll('.personality').forEach(b=>b.onclick=()=>setSetting('personality',+b.dataset.value));
async function setSetting(name,value){try{let d=await fetchJson('/api/set',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,value})});state=d;renderState(d);if(activeTab==='settings')loadSettingsAudit();toast('Setting saved')}catch(e){toast('Setting was not saved',true)}}function toggleSetting(name){setSetting(name,!(state.settings&&state.settings[name]))}function trim(delta){setSetting('speed_trim',Math.max(-15,Math.min(15,n(state.settings&&state.settings.speed_trim)+delta)))}
async function loadSettingsAudit(){try{let d=await fetchJson('/api/history?type=settings&limit=20'),rows=d.settings||[];$('settings-audit').innerHTML=rows.length?rows.map(r=>`<div class="metric-row"><span>${stamp(r.changed_at)} · ${esc(r.name)}</span><b>${esc(String(r.before))} → ${esc(String(r.after))}</b></div>`).join(''):'<div class="empty">No recorded changes yet.</div>'}catch(e){}}
async function loadRoutes(){if(routesLoaded)return;try{let rows=await fetchJson('/api/routes');$('routes').innerHTML=rows.length?rows.map(rt=>{let newest=Math.max(0,...Object.values(rt.times||{}).map(Number)),title=newest?new Date(newest*1000).toLocaleDateString([], {weekday:'short',month:'short',day:'numeric',year:'numeric'}):rt.name;return `<div class="route"><b>${esc(title)}</b><div class="segments">${rt.segs.map(seg=>{let at=n(rt.times&&rt.times[String(seg)]),label=at?new Date(at*1000).toLocaleTimeString([],{hour:'numeric',minute:'2-digit'}):'Segment '+seg;return `<button class="button segment" data-route="${esc(rt.name)}" data-seg="${seg}">${esc(label)}</button>`}).join('')}</div></div>`}).join(''):'<div class="empty">No dashcam routes found.</div>';document.querySelectorAll('.segment').forEach(b=>b.onclick=()=>playVideo(b.dataset.route,+b.dataset.seg,b)) ;routesLoaded=true}catch(e){$('routes').innerHTML='<div class="empty bad">Dashcam archive unavailable.</div>'}}
async function playVideo(route,seg,button){currentRoute=route;currentSeg=seg;document.querySelectorAll('.segment').forEach(b=>b.classList.toggle('playing',b===button));let v=$('player'),cam=$('cam-select').value;v.src=`/stream/${encodeURIComponent(route)}--${seg}?cam=${cam}`;v.load();v.play().catch(()=>{});setText('timeline-title',route+' · segment '+seg);try{let j=await fetchJson(`/api/log/${encodeURIComponent(route)}--${seg}`);logData=j.data||[];batteryLogData=j.battery||[];buildEngagement()}catch(e){logData=[];batteryLogData=[]}}
$('cam-select').onchange=()=>{if(currentRoute)playVideo(currentRoute,currentSeg,document.querySelector('.segment.playing'))};$('timeline').oninput=e=>{$('player').currentTime=n(e.target.value)};$('player').ontimeupdate=()=>{let v=$('player'),t=n(v.currentTime);$('timeline').max=n(v.duration,60);$('timeline').value=t;setText('timeline-time',`${fmtClock(t)} / ${fmtClock(v.duration)}`);if(hudOn&&logData.length){let index=Math.min(logData.length-1,Math.floor(t*10)),f=logData[index]||[],b=batteryLogData[index];setText('hud-speed',Math.round(n(f[0])));setText('hud-steer',n(f[1]).toFixed(1));setText('hud-lead',n(f[4])>0?n(f[4]).toFixed(1):'--');setText('hud-pedals',f[3]?'BRAKE':f[2]?'POWER':'COAST');setText('hud-pack',b?`${n(b[2]).toFixed(0)} kW · ${n(b[0]).toFixed(0)} V · ${n(b[3]).toFixed(0)}%`:'PACK DATA --');$('hud-left').style.color=f[5]?'#f4a641':'#555';$('hud-right').style.color=f[6]?'#f4a641':'#555'}};
function buildEngagement(){let data=logData.map(x=>n(x[7])),start=null,parts=[];data.forEach((v,i)=>{if(v&&start==null)start=i;if(!v&&start!=null){parts.push([start,i]);start=null}});if(start!=null)parts.push([start,data.length]);$('engagement-track').style.background='#282722';$('engagement-track').innerHTML=parts.map(p=>`<i style="display:block;position:absolute;left:${p[0]/data.length*100}%;width:${(p[1]-p[0])/data.length*100}%;height:6px;background:#70d68b"></i>`).join('');$('engagement-track').style.position='relative'}
function togglePlay(){let v=$('player');v.paused?v.play():v.pause()}function toggleHud(){hudOn=!hudOn;$('hud').classList.toggle('on',hudOn)}function exportVideo(){if(currentRoute)window.location.href=`/export/${encodeURIComponent(currentRoute)}--${currentSeg}?cam=${encodeURIComponent($('cam-select').value)}`}
loadState('summary');loadDiagnostics();schedulePoll();
</script></body></html>"""

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
    def send_bytes(self, body, content_type, filename=None, status=200):
        self.send_response(status); self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-cache, no-store"); self.send_header("Content-Length", str(len(body)))
        if filename:
            self.send_header("Content-Disposition", 'attachment; filename="%s"' % os.path.basename(filename).replace('"', ''))
        self.end_headers(); self.wfile.write(body)
    def do_HEAD(self):
        p=urlparse(self.path).path
        if p.startswith("/stream/"):
            route_seg=unquote(p.split("/stream/",1)[1]);cam=parse_qs(urlparse(self.path).query).get("cam",["qcamera"])[0]
            if cam not in ("qcamera","fcamera","dcamera"): return self.send_error(400,"Invalid camera")
            vid=get_mp4_path(route_seg,cam)
            if vid: return serve_file_with_range(self,vid,"video/mp4")
            return self.send_error(404,"Video not found")
        self.send_error(404)
    def do_GET(self):
        p = urlparse(self.path).path
        if p in ("/", "/index.html"):
            b = DASHBOARD_HTML.encode()
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
            view = parse_qs(urlparse(self.path).query).get("view", ["summary"])[0]
            self.send_json(state_snapshot(view))
        elif p == "/api/lab/state":
            include = parse_qs(urlparse(self.path).query).get("history", ["0"])[0] == "1"
            self.send_json(RECORDER.status(include))
        elif p == "/api/battery/health":
            days = safe_int(parse_qs(urlparse(self.path).query).get("days", [30])[0], 30)
            self.send_json(battery_health_report(days))
        elif p == "/api/history/status":
            self.send_json({"database": HISTORY_DB.stats(), "recorder": RECORDER.diagnostics()})
        elif p == "/api/history/summary":
            days = safe_int(parse_qs(urlparse(self.path).query).get("days", [30])[0], 30)
            self.send_json(history_summary(days))
        elif p == "/api/history/export":
            q = parse_qs(urlparse(self.path).query)
            try:
                body, content_type, filename = history_export(q.get("type", ["all"])[0], q.get("format", ["json"])[0], safe_int(q.get("limit", [1000])[0], 1000))
                self.send_bytes(body, content_type, filename)
            except ValueError as exc:
                self.send_json({"error": str(exc)}, 400)
        elif p == "/api/history":
            q = parse_qs(urlparse(self.path).query)
            self.send_json(history_payload(q.get("type", ["all"])[0], q.get("limit", [100])[0], q.get("days", [0])[0]))
        elif p == "/api/nav/status":
            self.send_json(navigation_snapshot())
        elif p == "/api/routes":
            self.send_json(get_routes())
        elif p.startswith("/api/log/"):
            route_seg = unquote(p.split("/api/log/")[1])
            if ".." in route_seg or "/" in route_seg or "\\" in route_seg:
                return self.send_json({"error": "invalid segment"}, 400)
            cache = f"/dev/shm/rl_{route_seg}.json"
            if os.path.exists(cache):
                try:
                    with open(cache, "r") as f:
                        cached = json.load(f)
                    if "battery" in cached:
                        self.send_json(cached)
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
                tl, en, log_start_mono = parse_telemetry_timeline(log_p)
                battery = [None] * len(tl)
                segment_log = HISTORY_DB.read_segment(route_seg)
                if segment_log and log_start_mono is not None:
                    for point in segment_log.get("points", []):
                        idx = int((num(point[0]) - log_start_mono) * 10)
                        if 0 <= idx < len(battery):
                            battery[idx] = point[6:]
                res = {"data": tl, "battery": battery,
                       "battery_fields": ["pack_v", "pack_i", "pack_kw", "soc", "pack_temp_c", "cell_spread_mv"]}
                if tl:
                    with open(cache, "w") as f: json.dump(res, f)
                self.send_json(res)
            except Exception as e:
                self.send_json({"error": str(e)})
        elif p.startswith("/stream/"):
            route_seg=unquote(p.split("/stream/",1)[1])
            cam=parse_qs(urlparse(self.path).query).get("cam",["qcamera"])[0]
            if cam not in ("qcamera","fcamera","dcamera"): return self.send_error(400,"Invalid camera")
            vid=get_mp4_path(route_seg,cam)
            if vid: serve_file_with_range(self,vid,"video/mp4")
            else: self.send_error(404,"Video not found")
        elif p.startswith("/export/"):
            route_seg = unquote(p.split("/export/")[1])
            cam = parse_qs(urlparse(self.path).query).get("cam", ["qcamera"])[0]
            self.handle_export(route_seg, cam)
        else:
            self.send_json({"error": "not found"}, 404)
            
    def handle_export(self, route_seg, cam_type):
        if cam_type not in ("qcamera", "fcamera", "dcamera"):
            return self.send_json({"error": "invalid camera"}, 400)
        src_path = get_mp4_path(route_seg, cam_type)
        if not src_path: return self.send_json({"error": "no video"}, 404)
        seg_dir = f"/data/media/0/realdata/{route_seg}"
        log_path = next((os.path.join(seg_dir, f) for f in ("rlog.zst", "qlog.zst") if os.path.exists(os.path.join(seg_dir, f))), None)
        if not log_path: return self.send_json({"error": "no telemetry log"}, 404)
        out_path = f"/dev/shm/exp_v4_{route_seg}_{cam_type}.mp4"
        work_path = out_path + ".part"
        ass_path = f"/dev/shm/hud_{route_seg}_{cam_type}.ass"
        try:
            newest_input = max(os.path.getmtime(src_path), os.path.getmtime(log_path))
            if not os.path.exists(out_path) or os.path.getsize(out_path) < 1024 or os.path.getmtime(out_path) < newest_input:
                tl, en, log_start_mono = parse_telemetry_timeline(log_path)
                if not tl: return self.send_json({"error": "no telemetry"}, 422)
                battery_frames = {}
                segment_log = HISTORY_DB.read_segment(route_seg)
                if segment_log and log_start_mono is not None:
                    for point in segment_log.get("points", []):
                        idx = int((num(point[0]) - log_start_mono) * 10)
                        if 0 <= idx < len(tl):
                            battery_frames[idx] = point[6:]
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
Style: Pack,Arial,28,&H00FFB656,&H00000000,&H99000000,&H99000000,1,0,0,0,100,100,0,0,1,2,2,9,30,30,55,1

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
                    pack = battery_frames.get(i)
                    if pack:
                        pack_text = f"{num(pack[2]):+.0f} kW  {num(pack[0]):.0f} V  {num(pack[3]):.0f}% SOC"
                        lines.append(f"Dialogue: 1,{a},{b},Pack,,0,0,0,,{ass_escape(pack_text)}")
                Path(ass_path).write_text(styles+"\n".join(lines)+"\n", encoding="utf-8")
                ass_name = os.path.basename(ass_path)
                cmd=["ffmpeg","-y","-hide_banner","-loglevel","error","-i",src_path,"-vf",f"ass=filename={ass_name}","-map","0:v:0","-map","0:a?","-c:v","libx264","-preset","ultrafast","-crf","24","-pix_fmt","yuv420p","-c:a","aac","-b:a","128k","-threads","2","-movflags","+faststart","-f","mp4",work_path]
                proc=subprocess.run(cmd,capture_output=True,text=True,timeout=600,cwd="/dev/shm")
                if proc.returncode!=0 or not os.path.exists(work_path) or os.path.getsize(work_path)<1024:
                    return self.send_json({"error":"ffmpeg export failed","detail":proc.stderr[-1200:]},500)
                os.replace(work_path, out_path)
            serve_file_with_range(self,out_path,"video/mp4",f"Comma_Clip_{route_seg}_{cam_type}.mp4")
        except subprocess.TimeoutExpired:
            self.send_json({"error":"export timed out"},504)
        except Exception as e:
            self.send_json({"error":f"export failed: {e}"},500)
        finally:
            try:
                if os.path.exists(ass_path): os.unlink(ass_path)
            except Exception: pass
            try:
                if os.path.exists(work_path): os.unlink(work_path)
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
            if p == "/api/trip":
                meters = RECORDER.trip_action(d.get("name"), str(d.get("action", "")))
                return self.send_json({"ok": True, "trip_meters": meters})
            if p != "/api/set": return self.send_json({"error": "not found"}, 404)
            settings = write_setting(str(d.get("name")), d.get("value"))
            with LOCK:
                STATE["settings"] = settings
            self.send_json(state_snapshot("summary"))
        except Exception as e:
            self.send_json({"error": str(e)}, 400)

def main():
    print(f"NAP Telemetry v23 listening on port {PORT} (phone tester: /phone)")
    threading.Thread(target=telemetry, daemon=True).start()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    try: srv.serve_forever()
    except KeyboardInterrupt: pass
    finally:
        STOP.set()
        RECORDER.checkpoint()
        HISTORY_DB.flush()
        srv.server_close()

if __name__ == "__main__":
    main()
