"""Share telemetry's vehicle samples without allocating more msgq readers."""
import math
import threading
import time

_lock = threading.Lock()
_samples = {}




def _y_at(line, metres=10.):
    """Read the same modelV2 ego-forward/right frame used by the 3D display."""
    xs, ys = getattr(line, 'x', []), getattr(line, 'y', [])
    if len(xs) < 2 or len(xs) != len(ys):
        return None
    for i in range(1, min(len(xs), 50)):
        a,b = float(xs[i-1]),float(xs[i])
        ya,yb = float(ys[i-1]),float(ys[i])
        if all(math.isfinite(v) for v in (a,b,ya,yb)) and a <= metres <= b and b > a:
            return ya+(yb-ya)*(metres-a)/(b-a)
    return None


def _model_snapshot(message, mono):
    result = {'mono': mono, 'valid': True, 'lane_lines': [], 'road_edges': [],
              'frame': 'ego_forward_right_m', 'lookahead_m': 10.}
    try:
        lines = message.laneLines
        probs = message.laneLineProbs
        for idx in range(min(len(lines), len(probs), 4)):
            y, prob = _y_at(lines[idx]), float(probs[idx])
            if y is not None and math.isfinite(prob):
                result['lane_lines'].append({'idx': idx, 'y_m': round(y, 4), 'prob': prob})
        edges = message.roadEdges
        stds = message.roadEdgeStds
        for idx in range(min(len(edges), len(stds), 2)):
            y, std = _y_at(edges[idx]), float(stds[idx])
            if y is not None and math.isfinite(std):
                result['road_edges'].append({'idx': idx, 'y_m': round(y, 4), 'std_m': std})
    except (TypeError, ValueError, IndexError, AttributeError, OverflowError):
        result['valid'] = False
        result['lane_lines'] = []
        result['road_edges'] = []
    return result

def publish(sm):
    """Called only by telemetry's subscriber thread; copy plain values."""
    samples = {}
    for service in ('carState', 'carControl', 'modelV2'):
        if (not sm.seen.get(service, False) or not sm.valid.get(service, False)
                or not sm.alive.get(service, False)):
            continue
        message = sm[service]
        sample = {'mono': sm.logMonoTime[service] / 1e9}
        if service == 'carState':
            sample.update(speed=float(message.vEgo), raw_speed=float(message.vEgoRaw),
                          gear=str(message.gearShifter), can_valid=bool(message.canValid),
                          cruise_enabled=bool(message.cruiseState.enabled))
        elif service == 'carControl':
            sample.update(enabled=bool(message.enabled), lat_active=bool(message.latActive),
                          long_active=bool(message.longActive))
        else:
            # Copy a few scalars from the existing telemetry subscriber.
            # No additional msgq reader, camera processing, or control input.
            try:
                sample = _model_snapshot(message, sample['mono'])
            except (TypeError, ValueError, AttributeError, OverflowError):
                sample = {'mono': sample['mono'], 'valid': False,
                          'lane_lines': [], 'road_edges': []}
        samples[service] = sample
    with _lock:
        _samples.clear()
        _samples.update(samples)


def _fresh(now):
    with _lock:
        return {name: sample.copy() for name, sample in _samples.items()
                if 0 <= now - sample['mono'] < .25}


def wheel_speed(now=None):
    samples = _fresh(time.monotonic() if now is None else now)
    speed = samples.get('carState', {}).get('speed')
    return speed if speed is not None and math.isfinite(speed) and 0 <= speed <= 45 else None


def require_park(now=None):
    samples = _fresh(time.monotonic() if now is None else now)
    if not all(name in samples for name in ('carState', 'carControl')):
        raise ValueError('Wake the car and remain in Park to enable this setting.')
    cs, cc = samples['carState'], samples['carControl']
    if (cs['gear'] != 'park' or not cs['can_valid']
            or not abs(cs['speed']) <= .05 or not abs(cs['raw_speed']) <= .05
            or cc['enabled'] or cc['lat_active'] or cc['long_active'] or cs['cruise_enabled']):
        raise ValueError('Park and disengage all controls to enable this setting.')


def motion_sample(now=None):
    """Plain source-stamped wheel data for the observation-only estimator."""
    samples = _fresh(time.monotonic() if now is None else now)
    return samples.get('carState')


def model_sample(now=None):
    """Recent modelV2 lane/edge scalars for diagnostic localization only."""
    sample = _fresh(time.monotonic() if now is None else now).get('modelV2')
    if sample is None:
        return None
    return {'mono': sample['mono'], 'valid': sample.get('valid') is True,
            'lane_lines': [dict(v) for v in sample.get('lane_lines', [])],
            'road_edges': [dict(v) for v in sample.get('road_edges', [])],
            'frame': sample.get('frame')}
