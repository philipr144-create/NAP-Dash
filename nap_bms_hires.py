
"""NAP high-resolution passive BMS recording, no CAN transmission."""
from collections import deque
from statistics import median
import time

class BMSHiRes:
    def __init__(self):
        self.ring = deque(maxlen=4000)
        self.last_mux = [None] * 24
        self.mux_counts = [0] * 24
        self.mux_periods = [deque(maxlen=30) for _ in range(24)]
        self.group_values = [None] * 24
        self.pack_voltage = None
        self.pack_current = None
        self.pack_mono = None
        self.active = None
        self.last_finished = -1e12
        self.last_status = 0.0
        self.last_limits = 0.0
        self.frames = 0
        self.saved = 0
        self.saved_parts = 0
        self.dropped = 0
        self.errors = 0
        self.last_error = ""
        self.last_trigger = None

    def _row(self, t, row):
        self.ring.append(row)
        cutoff = t - 25.0
        while self.ring and self.ring[0][1] < cutoff:
            self.ring.popleft()
        if self.active is not None:
            if len(self.active["rows"]) < 6000:
                self.active["rows"].append(row)
            else:
                self.active["dropped"] += 1

    def _trigger(self, t, reason, detail):
        if self.active is not None or t - self.last_finished < 45:
            return
        self.active = {
            "start": t, "until": t + 25.0,
            "reason": reason, "detail": detail,
            "rows": list(self.ring),
            "dropped": 0,
        }
        self.last_trigger = reason

    def observe_frame(self, address, data, bus, mono, speed_mps=0):
        try:
            if address not in (0x132, 0x102, 0x6F2):
                return

            t = float(mono)
            d = bytes(data)
            self.frames += 1

            if address in (0x132, 0x102) and len(d) >= 4:
                voltage = (d[0] | (d[1] << 8)) * .01
                raw_i = d[2] | (d[3] << 8)
                if raw_i >= 32768:
                    raw_i -= 65536
                current = raw_i * .1

                if not (100 < voltage < 600 and abs(current) < 2000):
                    return

                self.pack_voltage = voltage
                self.pack_current = current
                self.pack_mono = t
                self._row(t, ["p", round(t, 4), bus,
                              round(voltage, 2), round(current, 1)])

                if -current >= 200:
                    self._trigger(t, "high_discharge",
                                  {"current_a": round(current, 1)})
                elif current >= 80:
                    reason = "charging" if speed_mps < .5 else "regeneration"
                    self._trigger(t, reason,
                                  {"current_a": round(current, 1)})
                return

            if address != 0x6F2 or len(d) < 8:
                return

            mux = int(d[0])
            if mux > 31 or d[1:8] == b"\xff" * 7:
                return

            bits = int.from_bytes(d[1:8], "little")
            raw_values = [(bits >> (14 * k)) & 0x3FFF for k in range(4)]

            if mux < 24:
                previous = self.last_mux[mux]
                if previous is not None and t >= previous:
                    self.mux_periods[mux].append(t - previous)
                self.last_mux[mux] = t
                self.mux_counts[mux] += 1

                values = [
                    round(raw * .305175, 3)
                    if raw not in (0, 0x3FFF) else None
                    for raw in raw_values
                ]
                self.group_values[mux] = values
                self._row(t, ["b", round(t, 4), bus, mux, values])

                # Only evaluate cell spread when all 24 mux
                # groups belong to a reasonably recent sweep.
                if all(v is not None for v in self.last_mux):
                    width = max(self.last_mux) - min(self.last_mux)
                    if width <= 8.0:
                        cells = [
                            v for group in self.group_values
                            for v in group if v is not None
                        ]
                        if len(cells) >= 90:
                            spread = max(cells) - min(cells)
                            if spread >= 150:
                                self._trigger(t, "high_cell_spread", {
                                    "spread_mv": round(spread, 1),
                                    "sweep_width_s": round(width, 2),
                                })
            else:
                values = []
                for raw in raw_values:
                    if raw == 0x3FFF:
                        values.append(None)
                    else:
                        if raw & 0x2000:
                            raw -= 0x4000
                        values.append(round(raw * .0122, 3))
                self._row(t, ["t", round(t, 4), bus, mux, values])

        except Exception as e:
            self.errors += 1
            self.last_error = str(e)[:180]

    def _finish(self, db, now):
        event = self.active
        if event is None:
            return

        self.active = None
        self.last_finished = now
        rows = event["rows"]
        part_size = 250
        parts = max(1, (len(rows) + part_size - 1) // part_size)
        event_id = str(time.time_ns())
        event_wall = time.time() - (time.monotonic() - event["start"])

        try:
            db.LIMITS.setdefault("bms_hires", 1500)
            for part in range(parts):
                payload = {
                    "format": "nap_bms_hires_v1",
                    "event_id": event_id,
                    "part": part + 1,
                    "parts": parts,
                    "trigger": event["reason"],
                    "trigger_detail": event["detail"],
                    "estimated_start_unix": event_wall,
                    "start_mono": event["start"],
                    "end_mono": now,
                    "rows_total": len(rows),
                    "rows_dropped": event["dropped"],
                    "rows": rows[part*part_size:(part+1)*part_size],
                    "timestamp_note": "CAN batch monotonic time, not hardware per-frame time",
                }
                db.append("bms_hires", payload)
                self.saved_parts += 1
            self.saved += 1
            self.dropped += event["dropped"]
        except Exception as e:
            self.errors += 1
            self.last_error = "History write: " + str(e)[:150]

    def tick(self, mono, db, state):
        try:
            now = float(mono)
            if now - self.last_limits >= 1:
                self.last_limits = now
                bms = state.get("bms", {})
                self._row(now, [
                    "l", round(now, 4),
                    bms.get("max_discharge"),
                    bms.get("max_regen"),
                ])

            if self.active is not None and (
                now >= self.active["until"] or
                len(self.active["rows"]) >= 6000
            ):
                self._finish(db, now)

            if now - self.last_status < 1:
                return
            self.last_status = now

            periods = [
                round(median(p), 2) if p else None
                for p in self.mux_periods
            ]
            ages = [
                round(max(0, now-t), 2) if t is not None else None
                for t in self.last_mux
            ]

            state["bms_capture"] = {
                "running": True,
                "passive_only": True,
                "source_can_id": "0x6F2",
                "brick_groups": 24,
                "group_update_counts": list(self.mux_counts),
                "group_period_median_s": periods,
                "group_age_s": ages,
                "groups_fresh_5s": sum(
                    x is not None and x <= 5 for x in ages
                ),
                "groups_fresh_15s": sum(
                    x is not None and x <= 15 for x in ages
                ),
                "groups_fresh_60s": sum(
                    x is not None and x <= 60 for x in ages
                ),
                "pack_current_a": self.pack_current,
                "pack_voltage_v": self.pack_voltage,
                "total_bms_frames": self.frames,
                "ring_rows": len(self.ring),
                "pending_event": self.active is not None,
                "saved_events": self.saved,
                "saved_history_parts": self.saved_parts,
                "dropped_event_rows": self.dropped,
                "errors": self.errors,
                "last_error": self.last_error,
                "last_trigger": self.last_trigger,
                "timestamp_precision": "CAN message batch",
            }
        except Exception as e:
            self.errors += 1
            self.last_error = str(e)[:180]

BMS_HIRES = BMSHiRes()
