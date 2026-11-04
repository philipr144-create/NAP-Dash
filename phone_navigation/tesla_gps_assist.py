"""Select fresh Tesla GPS for route tracking with a comma fallback."""
import math
import json
from pathlib import Path

TOGGLE = Path('/data/nap_tesla_gps_assist')
SNAPSHOT = Path('/dev/shm/tesla_gps_assist.json')
BOOT = Path('/proc/sys/kernel/random/boot_id')


def read_fix(now, path=SNAPSHOT):
    try:
        with path.open() as stream:
            sample = json.loads(stream.read(2048))
        if sample['boot_id'] != BOOT.read_text().strip():
            return None
        position, motion = sample['position'], sample['motion']
        lon, lat = position['point']
        if not (0 <= now-position['mono'] <= 1.5 and 0 <= now-motion['mono'] <= 1.5
                and -180 <= lon <= 180 and -90 <= lat <= 90 and (lon or lat)
                and 0 < motion['hdop'] <= 3 and not motion['nmea_missing']
                and 0 <= motion['speed_mps'] <= 100 and 0 <= motion['heading_deg'] < 360):
            return None
        return position, motion
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        return None


def enabled():
    return TOGGLE.is_file()


def set_enabled(value):
    if value:
        TOGGLE.touch(mode=0o600)
    else:
        TOGGLE.unlink(missing_ok=True)


def select_fix(now, route, progress, comma_fix, wheel_speed):
    if not enabled():
        return comma_fix
    sample = read_fix(now)
    if sample is None:
        return comma_fix
    position, motion = sample
    if ((motion['speed_mps'] >= 2 if wheel_speed is None else abs(motion['speed_mps'] - wheel_speed) > 2)
            or abs(position['mono'] - motion['mono']) > 1.1):
        return comma_fix
    bearing = motion['heading_deg'] if wheel_speed is not None and wheel_speed >= 2 else None
    # Source selection must not depend on agreement with the current route.
    # Otherwise a genuine off-route Tesla fix is replaced by a different GPS
    # source instead of letting the tracker diagnose/recalculate the route.
    accuracy = None
    accuracy_source = 'unknown'
    if comma_fix is not None:
        measured = comma_fix.get('accuracy_m')
        if (type(measured) in (int, float) and math.isfinite(measured)
                and 0 < measured <= 40
                and 0 <= now-comma_fix['mono'] <= 1.5
                and abs(comma_fix['mono']-position['mono']) <= .5):
            lon, lat = position['point']
            clon, clat = comma_fix['point']
            separation = math.hypot(111195*(clon-lon)*math.cos(math.radians(lat)),
                                    111195*(clat-lat))
            if separation <= measured:
                # Conservative corroborated bound, never an HDOP-to-metres guess.
                accuracy = measured + separation
                accuracy_source = 'corroborated_comma_gps'
    result = {'point': position['point'], 'mono': position['mono'],
            'sample_mono': position['mono'], 'source': 'tesla-can',
            'accuracy_m': accuracy, 'accuracy_source': accuracy_source, 'wheel_speed': wheel_speed,
            'speed': motion['speed_mps'], 'bearing_deg': bearing}

    # One immutable Tesla position per CAN position timestamp. Blending a
    # changing comma fix into a held Tesla sample invents new observations.
    return result
