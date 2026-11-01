
from collections import deque
import time

class ControlDiagnostics:
    """In-process, read-only control event recorder."""

    def __init__(self, history):
        self.history = history
        self.ring = deque(maxlen=48)
        self.previous = {}
        self.pending = None
        self.next_sample = 0.0
        self.saved = 0
        self.dropped = 0
        self.last_error = ""

    def observe(self, sm, now):
        values = {}
        updated = []
        changed = []

        def current(service):
            try:
                if (not sm.updated.get(service, False) or
                    not sm.valid.get(service, False) or
                    not sm.seen.get(service, False)):
                    return None
                updated.append(service)
                return sm[service]
            except Exception:
                return None

        def get(obj, field, default=None):
            try:
                return getattr(obj, field, default)
            except Exception:
                return default

        def number(v):
            try:
                n = float(v)
                return round(n, 3) if n == n and abs(n) < 1e8 else None
            except Exception:
                return None

        cs = current("carState")
        if cs is not None:
            cruise = get(cs, "cruiseState")
            values.update({
                "speed_mps": number(get(cs, "vEgo")),
                "steering_angle": number(get(cs, "steeringAngleDeg")),
                "steering_pressed": bool(get(cs, "steeringPressed", False)),
                "steer_fault_temporary": bool(get(cs, "steerFaultTemporary", False)),
                "steer_fault_permanent": bool(get(cs, "steerFaultPermanent", False)),
                "cruise_enabled": bool(get(cruise, "enabled", False)),
                "cruise_speed": number(get(cruise, "speed")),
                "v_cruise": number(get(cs, "vCruise")),
                "brake_pressed": bool(get(cs, "brakePressed", False)),
                "gas_pressed": bool(get(cs, "gasPressed", False)),
            })
            try:
                buttons = [
                    {"type": str(get(b, "type", ""))[:60],
                     "pressed": bool(get(b, "pressed", False))}
                    for b in list(get(cs, "buttonEvents", []))[:8]
                ]
                if buttons:
                    changed.append({"button_events": buttons})
            except Exception:
                pass

        sd = current("selfdriveState")
        if sd is not None:
            values.update({
                "enabled": bool(get(sd, "enabled", False)),
                "active": bool(get(sd, "active", False)),
                "experimental": bool(get(sd, "experimentalMode", False)),
            })
            alert = str(get(sd, "alertType", "") or "")[:100]
            if alert:
                values["alert"] = alert

        cc = current("carControl")
        if cc is not None:
            values.update({
                "lateral_active": bool(get(cc, "latActive", False)),
                "longitudinal_active": bool(get(cc, "longActive", False)),
            })

        ps = current("pandaStates")
        if ps is not None:
            try:
                states = list(get(ps, "pandaStates", []))[:3]
                values["panda_controls_allowed"] = [
                    bool(get(s, "controlsAllowed"))
                    for s in states
                    if get(s, "controlsAllowed") is not None
                ]
                values["panda_safety_modes"] = [
                    str(get(s, "safetyModel", ""))[:50]
                    for s in states
                ]
            except Exception:
                pass

        oe = current("onroadEvents")
        if oe is not None:
            try:
                events = get(oe, "events", get(oe, "onroadEvents", []))
                values["onroad_events"] = [
                    str(get(e, "name", e))[:80]
                    for e in list(events)[:24]
                ]
            except Exception:
                pass

        can = current("can")
        if can is not None:
            try:
                for i, msg in enumerate(can):
                    if i >= 700:
                        break
                    if int(msg.address) == 0x45:
                        raw = bytes(msg.dat)
                        if raw:
                            values["raw_stalk"] = {
                                "bus": int(get(msg, "src", -1)),
                                "lever": raw[0] & 0x3f,
                                "data": raw[:8].hex()
                            }
            except Exception:
                pass

        for key, value in values.items():
            if key not in self.previous:
                continue
            old = self.previous[key]
            if old == value:
                continue

            # Small speed changes and ordinary acceleration should
            # not generate thousands of diagnostic incidents.
            if key in ("speed_mps", "steering_angle",
                       "brake_pressed", "gas_pressed"):
                continue

            if key in ("cruise_speed", "v_cruise"):
                if value is None or old is None or abs(value-old) < 0.5:
                    continue

            # Ignore rolling CAN bytes while stalk position is unchanged.
            # Preserve raw payload when the physical lever changes.
            if key == "raw_stalk":
                if (isinstance(old, dict) and isinstance(value, dict)
                    and old.get("bus") == value.get("bus")
                    and old.get("lever") == value.get("lever")):
                    continue

            changed.append({
                "field": key,
                "before": old,
                "after": value
            })

        self.previous.update(values)

        wall = time.time()
        if now >= self.next_sample or changed:
            self.next_sample = now + 0.25
            point = {
                "mono": round(now, 3),
                "unix": round(wall, 3),
                "state": dict(self.previous)
            }
            self.ring.append(point)
        else:
            point = None

        if changed:
            if self.pending is None:
                self.pending = {
                    "recorded_at": wall,
                    "trigger_mono": round(now, 3),
                    "last_event_mono": now,
                    "source": "nap_live",
                    "events": [],
                    "samples": list(self.ring)
                }

            self.pending["last_event_mono"] = now
            for change in changed[:12]:
                if len(self.pending["events"]) < 80:
                    self.pending["events"].append({
                        "mono": round(now, 3),
                        "change": change
                    })

        elif point is not None and self.pending is not None:
            if len(self.pending["samples"]) < 110:
                self.pending["samples"].append(point)

        if self.pending is not None:
            duration = now - self.pending["trigger_mono"]
            quiet = now - self.pending["last_event_mono"]
            if duration >= 16 or quiet >= 6:
                self.flush()

    def flush(self):
        row = self.pending
        self.pending = None
        if row is None:
            return

        row.pop("last_event_mono", None)
        try:
            if self.history.append("control_diagnostic", row):
                self.saved += 1
            else:
                self.dropped += 1
                self.last_error = "Database queue full"
        except Exception as e:
            self.dropped += 1
            self.last_error = str(e)[:160]

    def status(self):
        return {
            "mode": "existing_telemetry_thread",
            "running": True,
            "saved": self.saved,
            "dropped": self.dropped,
            "ring_samples": len(self.ring),
            "pending": self.pending is not None,
            "last_error": self.last_error
        }
