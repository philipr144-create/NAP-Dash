
"""Bounded, read-only qlog/rlog diagnostic replay."""
from pathlib import Path
import re
import time
import threading

ROOT = Path("/data/media/0/realdata")
PATTERN = re.compile(r"[0-9]{8}--[0-9a-fA-F]{10,40}--[0-9]{1,4}")
REPLAY_LOCK = threading.Lock()

def get(obj, key, default=None):
    try:
        return getattr(obj, key, default)
    except Exception:
        return default

def label(value):
    return str(value).split(".")[-1][:90]

def number(value):
    try:
        value = float(value)
        if value == value and abs(value) < 1e8:
            return round(value, 3)
    except Exception:
        pass
    return None

def replay(segment, source="qlog"):
    if not PATTERN.fullmatch(segment):
        raise ValueError("Invalid segment name")
    if source not in ("qlog", "rlog"):
        raise ValueError("Invalid recording type")

    folder = ROOT / segment
    if not folder.is_dir():
        raise FileNotFoundError("Segment not found")

    path = next(
        (folder / f"{source}.{ext}" for ext in ("zst", "bz2")
         if (folder / f"{source}.{ext}").is_file()), None
    )
    if path is None:
        raise FileNotFoundError("Recording not found")
    if path.stat().st_size > 16000000:
        raise ValueError("Recording exceeds replay size limit")

    from tools.lib.logreader import LogReader

    start = time.monotonic()
    first = None
    events = []
    samples = []
    previous = {}
    services = {}
    scanned = 0
    next_sample = 0
    truncated = False

    def event(t, field, old, new, kind="change"):
        nonlocal truncated
        if len(events) >= 140:
            truncated = True
            return
        events.append({
            "segment_s": round(t, 3),
            "type": kind, "field": field,
            "before": old, "after": new
        })

    watched = {
        "steering_pressed", "steer_fault_temporary",
        "steer_fault_permanent", "cruise_enabled",
        "cruise_speed", "set_speed", "enabled", "active",
        "experimental", "alert", "controls_active",
        "lateral_active", "longitudinal_active",
        "panda_allowed", "panda_faults", "onroad_events"
    }

    for msg in LogReader(str(path)):
        scanned += 1
        if scanned > 160000:
            truncated = True
            break
        if scanned % 256 == 0 and time.monotonic()-start > 10:
            truncated = True
            break

        try:
            service = msg.which()
            if service not in (
                "carState", "selfdriveState", "controlsState",
                "carControl", "pandaStates", "onroadEvents", "can"
            ):
                continue
            mono = msg.logMonoTime / 1e9
            if first is None:
                first = mono
            t = max(0, mono-first)
            if t > 65:
                break
            data = getattr(msg, service)
        except Exception:
            continue

        services[service] = services.get(service, 0) + 1
        values = {}

        try:
            if service == "carState":
                cruise = get(data, "cruiseState")
                values = {
                    "speed_mps": number(get(data, "vEgo")),
                    "steering_angle": number(get(data, "steeringAngleDeg")),
                    "steering_torque": number(get(data, "steeringTorque")),
                    "steering_pressed": bool(get(data, "steeringPressed", False)),
                    "steer_fault_temporary": bool(get(data, "steerFaultTemporary", False)),
                    "steer_fault_permanent": bool(get(data, "steerFaultPermanent", False)),
                    "cruise_enabled": get(cruise, "enabled"),
                    "cruise_speed": number(get(cruise, "speed")),
                    "set_speed": number(get(data, "vCruise")),
                }
                for button in list(get(data, "buttonEvents", []))[:8]:
                    event(t, "button", None, {
                        "name": label(get(button, "type")),
                        "pressed": get(button, "pressed")
                    }, "button")

            elif service == "selfdriveState":
                values = {
                    "enabled": get(data, "enabled"),
                    "active": get(data, "active"),
                    "experimental": get(data, "experimentalMode"),
                    "alert": str(get(data, "alertType", "") or "")[:120],
                }

            elif service == "controlsState":
                values = {
                    "controls_active": get(data, "active"),
                    "controls_alert": str(get(data, "alertText1", "") or "")[:120],
                }

            elif service == "carControl":
                values = {
                    "lateral_active": get(data, "latActive"),
                    "longitudinal_active": get(data, "longActive"),
                }

            elif service == "pandaStates":
                states = list(get(data, "pandaStates", []))[:3]
                values = {
                    "panda_allowed": [
                        get(p, "controlsAllowed") for p in states
                    ],
                    "panda_faults": [
                        [label(f) for f in list(get(p, "faults", []))[:8]]
                        for p in states
                    ],
                }

            elif service == "onroadEvents":
                evs = get(data, "events",
                          get(data, "onroadEvents", []))
                values = {
                    "onroad_events": [
                        label(get(e, "name", e))
                        for e in list(evs)[:20]
                    ]
                }

            elif service == "can":
                for frame in data:
                    if int(get(frame, "address", -1)) != 0x45:
                        continue
                    raw = bytes(get(frame, "dat", b""))
                    if not raw:
                        continue
                    bus = int(get(frame, "src", -1))
                    lever = raw[0] & 0x3f
                    key = f"stalk_bus_{bus}"
                    old = previous.get(key)
                    if old is not None and old != lever:
                        event(t, key, old, lever, "raw_can_stalk")
                    previous[key] = lever
                continue

        except Exception:
            continue

        for key, value in values.items():
            if value is None:
                continue
            old = previous.get(key)
            previous[key] = value

            if old is None or old == value:
                continue
            if key not in watched:
                continue
            if key in ("cruise_speed", "set_speed"):
                if not isinstance(old, (int, float)):
                    continue
                if abs(value-old) < 0.5:
                    continue
            event(t, key, old, value)

        if t >= next_sample and len(samples) < 110:
            next_sample = t + 0.5
            samples.append({
                "segment_s": round(t, 3),
                "state": {
                    k: v for k, v in previous.items()
                    if k not in ("onroad_events", "panda_faults")
                }
            })

    return {
        "segment": segment,
        "source": source,
        "timebase": "seconds since first selected log message",
        "log_start_mono": first,
        "services": services,
        "scanned": scanned,
        "events": events,
        "samples": samples,
        "truncated": truncated,
        "decode_seconds": round(time.monotonic()-start, 2),
        "read_only": True,
    }
