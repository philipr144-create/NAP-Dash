"""Lightweight, read-only NAP control diagnostics: runs in the existing telemetry thread.

No CAN writes, commands, subprocesses or additional worker threads. This module
collects bounded pre/post context around engagement, cruise and safety events.
"""
from collections import deque
import time

INTERESTING = ("mismatch", "steer", "can", "panda", "safety", "cruise", "control", "fault")


def _name(value):
    try:
        return str(value).split(".")[-1][:96]
    except Exception:
        return ""


def _flag(obj, name):
    try:
        return bool(getattr(obj, name)) if obj is not None else None
    except Exception:
        return None


def _number(obj, name):
    try:
        val = float(getattr(obj, name))
        return round(val, 3) if val == val and abs(val) < 1e8 else None
    except Exception:
        return None


def _state(car=None, selfdrive=None, control=None, panda=None, onroad=None):
    cruise = getattr(car, "cruiseState", None)
    p_states = getattr(panda, "pandaStates", []) if panda is not None else []
    try:
        ps = list(p_states)[:3]
    except Exception:
        ps = []
    if panda is not None and not ps and hasattr(panda, "controlsAllowed"):
        ps = [panda]
    panda_allowed = [bool(getattr(p, "controlsAllowed")) for p in ps if hasattr(p, "controlsAllowed")]
    buttons = []
    try:
        for b in list(getattr(car, "buttonEvents", []))[:8]:
            buttons.append({"type": _name(getattr(b, "type", "")), "pressed": _flag(b, "pressed")})
    except Exception:
        pass
    event_names = []
    try:
        evs = getattr(onroad, "onroadEvents", onroad)
        for ev in list(evs)[:24]:
            event_names.append(_name(getattr(ev, "name", ev)))
    except Exception:
        pass
    return {
        "v_mps": _number(car, "vEgo"),
        "angle_deg": _number(car, "steeringAngleDeg"),
        "steer_pressed": _flag(car, "steeringPressed"),
        "steer_fault_tmp": _flag(car, "steerFaultTemporary"),
        "steer_fault_perm": _flag(car, "steerFaultPermanent"),
        "brake": _flag(car, "brakePressed"),
        "gas": _flag(car, "gasPressed"),
        "cruise_enabled": _flag(cruise, "enabled"),
        "cruise_speed_mps": _number(cruise, "speed"),
        "v_cruise_kph": _number(car, "vCruise"),
        "buttons": buttons,
        "sd_enabled": _flag(selfdrive, "enabled"),
        "sd_active": _flag(selfdrive, "active"),
        "exp": _flag(selfdrive, "experimentalMode"),
        "alert_type": _name(getattr(selfdrive, "alertType", "")) if selfdrive is not None else "",
        "alert_text": str(getattr(selfdrive, "alertText1", ""))[:120] if selfdrive is not None else "",
        "lat_active": _flag(control, "latActive"),
        "long_active": _flag(control, "longActive"),
        "panda_allowed": panda_allowed if panda is not None else None,
        "onroad_events": event_names if onroad is not None else None,
    }


class ControlDiagnostics:
    """Single-thread producer; append callback uses the existing SQLite queue."""

    def __init__(self, append, pre_seconds=10, post_seconds=6, sample_seconds=0.25):
        self.append = append
        self.pre_seconds = max(1, min(float(pre_seconds), 15))
        self.post_seconds = max(1, min(float(post_seconds), 10))
        self.sample_seconds = max(0.2, float(sample_seconds))
        self.ring = deque(maxlen=int(self.pre_seconds / self.sample_seconds) + 3)
        self.pending = None
        self.prev = None
        self.next_sample = 0.0
        self.saved = 0
        self.errors = 0
        self.last_error = ""

    def observe(self, now, unix, car=None, selfdrive=None, control=None,
                panda=None, onroad=None, updated=()):
        updated = set(updated)
        sample = _state(car, selfdrive, control, panda, onroad)
        # Build state from fresh producers only; stale/unavailable fields never
        # count as an observed change.
        changes = []
        old = self.prev or {}
        check = {
            "steer_pressed": "carState",
            "cruise_enabled": "carState",
            "sd_active": "selfdriveState",
            "sd_enabled": "selfdriveState",
            "alert_type": "selfdriveState",
            "alert_text": "selfdriveState",
            "lat_active": "carControl",
            "long_active": "carControl",
            "panda_allowed": "pandaStates",
            "onroad_events": "onroadEvents",
        }
        for key, service in check.items():
            if service not in updated or sample.get(key) is None:
                if key in old:
                    sample[key] = old[key]
                continue
            before, after = old.get(key), sample[key]
            if before is None or before == after:
                continue
            if key in ("steer_pressed", "cruise_enabled", "sd_active", "sd_enabled",
                       "lat_active", "long_active", "panda_allowed"):
                changes.append({"field": key, "from": before, "to": after})
            elif key.startswith("alert_") and after:
                changes.append({"field": key, "from": before, "to": after})
            elif key == "onroad_events":
                novel = [s for s in after if s not in before and any(t in s.lower() for t in INTERESTING)]
                if novel:
                    changes.append({"field": key, "new": novel[:8]})
        if "carState" in updated and sample["buttons"]:
            changes.append({"field": "buttons", "events": sample["buttons"]})

        # Do not fabricate state changes when feeds disappear/reconnect.
        for key, service in check.items():
            if service not in updated and key in old:
                sample[key] = old[key]
        self.prev = sample

        sampled = now >= self.next_sample or bool(changes)
        if sampled:
            self.next_sample = now + self.sample_seconds
            point = {"mono": round(now, 3), "unix": round(unix, 3)}
            # Exclude verbose onroad event arrays from every point; they are
            # already recorded in the trigger list.
            point.update({k: v for k, v in sample.items() if k not in ("onroad_events", "buttons")})
            self.ring.append(point)
        if changes:
            if self.pending is None:
                self.pending = {
                    "recorded_at": unix, "trigger_mono": round(now, 3),
                    "trigger_unix": round(unix, 3), "events": [],
                    "samples": list(self.ring), "source": "nap_live",
                }
            self.pending["events"].extend([{"mono": round(now, 3), "change": c} for c in changes][:12])
            self.pending["events"] = self.pending["events"][:48]
        elif sampled and self.pending is not None:
            self.pending["samples"].append(self.ring[-1])
        if self.pending and now - self.pending["trigger_mono"] >= self.post_seconds:
            self.flush()

    def flush(self):
        if self.pending is None:
            return False
        row = self.pending
        self.pending = None
        # A fixed cap makes malformed streams unable to grow record size.
        row["samples"] = row["samples"][-100:]
        try:
            ok = self.append("control_diagnostic", row)
            if ok is False:
                self.errors += 1
                self.last_error = "history queue full"
                return False
            self.saved += 1
            return True
        except Exception as exc:
            self.errors += 1
            self.last_error = str(exc)[:180]
            return False

    def status(self):
        return {
            "running_in_existing_telemetry_loop": True,
            "pre_seconds": self.pre_seconds,
            "post_seconds": self.post_seconds,
            "ring_points": len(self.ring),
            "incident_pending": self.pending is not None,
            "incidents_saved": self.saved,
            "write_errors": self.errors,
            "last_error": self.last_error,
        }


def replay_segment(log_path, segment, max_messages=160000, max_seconds=8.0):
    """Offline, bounded one-segment qlog/rlog replay; caller must gate offroad."""
    from tools.lib.logreader import LogReader
    states = {}
    incidents = []
    capture = ControlDiagnostics(lambda kind, row: incidents.append(row))
    started = time.monotonic()
    count = 0
    t0 = None
    last_mono = None
    truncated = False
    supported = {"carState": "car", "selfdriveState": "selfdrive",
                 "carControl": "control", "onroadEvents": "onroad",
                 "pandaStates": "panda"}
    for msg in LogReader(log_path):
        count += 1
        if count > max_messages or time.monotonic() - started > max_seconds:
            truncated = True
            break
        which = msg.which()
        if which not in supported:
            continue
        mono = float(msg.logMonoTime) / 1e9
        if t0 is None:
            t0 = mono
        if mono < t0 or mono - t0 > 65:
            continue
        states[supported[which]] = getattr(msg, which)
        capture.observe(mono, time.time(), updated=(which,), **states)
        last_mono = mono
        if len(incidents) >= 20:
            truncated = True
            break
    capture.flush()
    for row in incidents[:20]:
        row.update({"source": "offline_log_replay", "segment": segment,
                    "timebase": "log_monotonic_seconds_not_unix",
                    "log_start_mono": round(t0, 3) if t0 is not None else None})
        # UNIX times collected during replay are not event wall-clock time.
        row.pop("trigger_unix", None)
        for point in row["samples"]:
            point.pop("unix", None)
    return {"segment": segment, "incidents": incidents[:20],
            "message_count": count, "truncated": truncated,
            "log_start_mono": t0, "log_end_mono": last_mono,
            "event_wall_clock_available": False}
