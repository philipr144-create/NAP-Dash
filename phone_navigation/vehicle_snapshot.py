"""Share telemetry's vehicle samples without allocating more msgq readers."""
import math
import threading
import time

_lock = threading.Lock()
_samples = {}


def publish(sm):
    """Called only by telemetry's subscriber thread; copy plain values."""
    samples = {}
    for service in ('carState', 'carControl'):
        if (not sm.seen.get(service, False) or not sm.valid.get(service, False)
                or not sm.alive.get(service, False)):
            continue
        message = sm[service]
        sample = {'mono': sm.logMonoTime[service] / 1e9}
        if service == 'carState':
            sample.update(speed=float(message.vEgo), raw_speed=float(message.vEgoRaw),
                          gear=str(message.gearShifter), can_valid=bool(message.canValid),
                          cruise_enabled=bool(message.cruiseState.enabled))
        else:
            sample.update(enabled=bool(message.enabled), lat_active=bool(message.latActive),
                          long_active=bool(message.longActive))
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
