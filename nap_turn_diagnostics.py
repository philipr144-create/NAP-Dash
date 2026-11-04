"""Passive city-turn evidence from existing subscriptions; never publishes control messages."""
from collections import deque
import json
import math
import os
from pathlib import Path
import tempfile
import time

SNAPSHOT = Path('/dev/shm/nap_turn_diagnostics.json')
DECISION = Path('/dev/shm/nap_navigation_decision.json')
INTENT = Path('/dev/shm/nap_city_turn_intent.json')
LONGITUDINAL = Path('/dev/shm/nap_turn_longitudinal.json')

def get(obj, key, default=None):
    try:
        return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)
    except Exception:
        return default

def number(value):
    return round(float(value), 6) if type(value) in (int, float) and math.isfinite(value) else None

def model_summary(model, path=True):
    pos = get(model, 'position'); orientation = get(model, 'orientation')
    result = {'frame_id': get(model, 'frameId'), 'desired_curvature': number(get(get(model, 'action'), 'desiredCurvature'))}
    arrays = {k: [number(v) for v in list(get(pos, k, []))[:33]] for k in ('t', 'x', 'y')}
    arrays['yaw'] = [number(v) for v in list(get(orientation, 'z', []))[:33]]
    lengths = [len(v) for v in arrays.values()]
    valid = bool(lengths[0] >= 3 and len(set(lengths)) == 1
                 and all(v is not None for values in arrays.values() for v in values)
                 and all(b > a for a, b in zip(arrays['t'], arrays['t'][1:])))
    result['geometry_available'] = valid
    if valid:
        indices = [i for i, t in enumerate(arrays['t']) if .5 <= t <= 3]
        if indices:
            i = indices[-1]
            result.update(horizon_s=arrays['t'][i], forward_m=number(arrays['x'][i]-arrays['x'][0]),
                          offset_m=number(arrays['y'][i]-arrays['y'][0]),
                          heading_deg=number(math.degrees(arrays['yaw'][i]-arrays['yaw'][0])))
        if path:
            result['path'] = arrays
    return result

def snapshot_file(path, now, max_age=.35):
    try:
        if path.is_symlink():
            raise ValueError('symlink')
        with path.open('rb') as stream:
            raw = stream.read(16385)
        if len(raw) > 16384:
            raise ValueError('snapshot_size')
        data = json.loads(raw)
        stamp = data.get('producer_mono', data.get('received_mono'))
        age = now-stamp if type(stamp) in (int, float) and math.isfinite(stamp) else None
        return {'status': 'fresh' if age is not None and 0 <= age <= max_age else 'stale',
                'age_s': number(age), 'producer_mono': stamp, 'data': data}
    except (OSError, ValueError, TypeError, AttributeError):
        return {'status': 'unavailable', 'data': None, 'age_s': None}

class TurnDiagnostics:
    def __init__(self, history, snapshot=SNAPSHOT, decision=DECISION, intent=INTENT):
        self.history = history
        self.snapshot, self.decision, self.intent = Path(snapshot), Path(decision), Path(intent)
        self.ring = deque(maxlen=40)
        self.ring_sizes = deque(maxlen=40)
        self.pending_bytes = 0
        self.previous = None
        self.pending = None
        self.saved = self.dropped = self.sequence = 0
        self.next_write = self.last_model_mono = 0.
        self.last_error = ''
        try:
            self.boot_id = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        except OSError:
            self.boot_id = None

    def observe(self, sm, now):
        # Must be called by the existing telemetry thread, never the controller.
        sources = {}
        def read(service):
            seen = get(sm.seen, service, False)
            stamp = get(sm.logMonoTime, service, 0)/1e9
            age = now-stamp if seen else None
            valid = bool(get(sm.valid, service, False))
            fresh = bool(seen and valid and age is not None and 0 <= age <= .35)
            sources[service] = {'status': 'fresh' if fresh else 'stale' if seen else 'unavailable',
                                'valid': valid, 'producer_mono': stamp if seen else None, 'age_s': number(age)}
            return sm[service] if seen else None
        cs, cc, mdl, ctl, output, calibration = [read(k) for k in
            ('carState', 'carControl', 'modelV2', 'controlsState', 'carOutput', 'liveCalibration')]
        decision = snapshot_file(self.decision, now)
        intent = snapshot_file(self.intent, now)
        longitudinal = snapshot_file(LONGITUDINAL, now)
        d = decision.get('data') or {}
        authorization = (intent.get('data') or {}).get('authorization_trace') or {}
        state = {'left_blinker': get(cs, 'leftBlinker'), 'right_blinker': get(cs, 'rightBlinker'),
                 'speed_mps': number(get(cs, 'vEgo')), 'steering_angle_deg': number(get(cs, 'steeringAngleDeg')),
                 'steering_pressed': get(cs, 'steeringPressed'), 'steering_disengage': get(cs, 'steeringDisengage'),
                 'can_valid': get(cs, 'canValid'), 'left_blindspot': get(cs, 'leftBlindspot'),
                 'right_blindspot': get(cs, 'rightBlindspot'), 'gear': str(get(cs, 'gearShifter', 'unavailable')),
                 'lateral_active': get(cc, 'latActive'), 'longitudinal_active': get(cc, 'longActive'),
                 'actual_curvature': number(get(ctl, 'curvature')), 'requested_curvature': number(get(ctl, 'desiredCurvature')),
                 'output_angle_deg': number(get(get(output, 'actuatorsOutput'), 'steeringAngleDeg')),
                 'calibration': str(get(calibration, 'calStatus', 'unavailable'))}
        signature = (state['left_blinker'], state['right_blinker'], state['steering_pressed'], state['steering_disengage'],
                     state['can_valid'], state['left_blindspot'], state['right_blindspot'], state['lateral_active'],
                     d.get('source'), d.get('signal_owner'), d.get('final_desire'), d.get('manual_turn_phase'),
                     d.get('manual_turn_reason'), d.get('manual_turn_blocked'), decision['status'], intent['status'],
                     authorization.get('direction_retained'), authorization.get('raw_classified_direction'),
                     tuple((k, v['status']) for k, v in sources.items()))
        changed = signature != self.previous
        active = (state['left_blinker'] is True or state['right_blinker'] is True
                  or d.get('final_desire') in ('turnLeft', 'turnRight'))
        model_mono = sources['modelV2']['producer_mono']
        new_model = model_mono is not None and model_mono != self.last_model_mono
        if not (changed or new_model or now >= self.next_write):
            return
        self.sequence += 1
        point = {'version': 1, 'producer_mono': now, 'recorded_at': time.time(), 'boot_id': self.boot_id,
                 'sequence': self.sequence, 'sources': sources, 'state': state,
                 'model': model_summary(mdl), 'decision': decision, 'intent': intent, 'longitudinal': longitudinal,
                 'used_for_control': False}
        frame, decision_frame = point['model'].get('frame_id'), d.get('frame_id')
        point['decision_model_frame_skew'] = frame-decision_frame if type(frame) is int and type(decision_frame) is int else None
        point_size = len(json.dumps(point, separators=(',', ':'), allow_nan=False).encode())
        self.ring.append(point)
        self.ring_sizes.append(point_size)
        if changed and (active or self.pending is not None):
            if self.pending is None:
                samples, sizes = list(self.ring), list(self.ring_sizes)
                while sum(sizes) > 100000 and len(samples) > 1:
                    samples.pop(0); sizes.pop(0)
                self.pending_bytes = sum(sizes)+1000
                self.pending = {'version': 1, 'recorded_at': time.time(), 'trigger_mono': now,
                                'boot_id': self.boot_id, 'events': [], 'samples': samples}
            if self.pending['samples'][-1]['producer_mono'] != now and len(self.pending['samples']) < 200:
                self.pending['samples'].append(point)
                self.pending_bytes += point_size
            if len(self.pending['events']) < 80:
                self.pending['events'].append({'mono': now, 'state': state,
                    'phase': d.get('manual_turn_phase'), 'reason': d.get('manual_turn_reason'),
                    'source': d.get('source'), 'desire': d.get('final_desire'),
                    'authorization': dict(authorization)})
                self.pending_bytes += 2000
        elif self.pending is not None and len(self.pending['samples']) < 200:
            self.pending['samples'].append(point)
            self.pending_bytes += point_size
        if self.pending is not None and (now-self.pending['trigger_mono'] >= 6 or len(self.pending['samples']) >= 200
                or self.pending_bytes >= 200000):
            self.flush()
        self.previous, self.last_model_mono = signature, model_mono
        if changed or now >= self.next_write:
            self.next_write = now+.2
            point['recorder'] = self.status()
            self.write_snapshot(point)

    def write_snapshot(self, point):
        name = None
        try:
            raw = json.dumps(point, allow_nan=False, separators=(',', ':'))
            if len(raw.encode()) > 20000:
                raise ValueError('turn_snapshot_too_large')
            with tempfile.NamedTemporaryFile(mode='w', dir=self.snapshot.parent, prefix='.nap-turn-', delete=False) as stream:
                name = stream.name
                stream.write(raw)
            os.replace(name, self.snapshot)
        except (OSError, ValueError, TypeError) as exc:
            self.last_error = str(exc)[:160]
        finally:
            if name:
                try: os.unlink(name)
                except OSError: pass

    def flush(self):
        row, self.pending = self.pending, None
        if row is None: return
        try:
            removed = 0
            while len(json.dumps(row, separators=(',', ':')).encode()) > 240000 and row['samples']:
                row['samples'].pop(0)
                removed += 1
            row['omitted_oldest_samples_for_size'] = removed
            if self.history.append('turn_diagnostic', row): self.saved += 1
            else: self.dropped += 1
        except Exception as exc:
            self.dropped += 1
            self.last_error = str(exc)[:160]

    def status(self):
        return {'version': 1, 'implementation': 'unified-city-turn-v3', 'master_toggle': 'NAPCityTurns', 'mode': 'existing_telemetry_thread', 'saved': self.saved, 'dropped': self.dropped,
                'ring_samples': len(self.ring), 'pending': self.pending is not None, 'last_error': self.last_error,
                'max_samples_per_capture': 200, 'max_capture_bytes': 240000, 'retained_captures': 120, 'used_for_control': False}
