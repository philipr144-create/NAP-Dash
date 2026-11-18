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

def diagnostic_status(now, comma_fix=None, wheel_speed=None):
    """Observe both GPS sources without changing navigation or control selection.

    These are receiver-comparison measurements, NOT calibrated localization
    confidence. In particular, GNSS agreement does not prove road or lane ID.
    """
    report = {'mode': 'shadow', 'used_for_control': False,
              'assist_enabled': enabled(), 'tesla_snapshot_valid': False,
              'selection_reason': 'tesla_snapshot_unavailable',
              'compared': False}
    sample = read_fix(now)
    if sample is None:
        if not report['assist_enabled']:
            report['selection_reason'] = 'assist_disabled'
        return report

    position, motion = sample
    report['tesla_snapshot_valid'] = True
    report['tesla_position_age_s'] = round(max(0., now-position['mono']), 3)
    report['tesla_motion_age_s'] = round(max(0., now-motion['mono']), 3)
    report['tesla_position_motion_skew_s'] = round(abs(position['mono']-motion['mono']), 3)
    report['tesla_hdop'] = motion['hdop']
    speed = motion['speed_mps']
    if not report['assist_enabled']:
        report['selection_reason'] = 'assist_disabled'
    elif wheel_speed is None and speed >= 2:
        report['selection_reason'] = 'wheel_speed_unavailable'
    elif wheel_speed is not None and abs(speed-wheel_speed) > 2:
        report['selection_reason'] = 'tesla_wheel_speed_disagreement'
    elif abs(position['mono']-motion['mono']) > 1.1:
        report['selection_reason'] = 'tesla_position_motion_skew'
    else:
        report['selection_reason'] = 'eligible_for_legacy_source_selection'

    if wheel_speed is not None and type(wheel_speed) in (int, float) and math.isfinite(wheel_speed):
        report['tesla_wheel_speed_difference_mps'] = round(speed-wheel_speed, 3)

    if not isinstance(comma_fix, dict):
        report['comparison_reason'] = 'comma_fix_unavailable'
        return report
    point = comma_fix.get('point')
    stamp = comma_fix.get('mono')
    if (not isinstance(point, (list, tuple)) or len(point) != 2
            or not all(type(v) in (int, float) and math.isfinite(v) for v in point)
            or type(stamp) not in (int, float) or not math.isfinite(stamp)
            or not 0 <= now-stamp <= 3):
        report['comparison_reason'] = 'comma_fix_invalid_or_stale'
        return report

    lon, lat = position['point']
    clon, clat = point
    separation = math.hypot(111195*(clon-lon)*math.cos(math.radians((lat+clat)/2)),
                            111195*(clat-lat))
    report['compared'] = True
    report['position_separation_m'] = round(separation, 2)
    report['timestamp_separation_s'] = round(abs(stamp-position['mono']), 3)
    comma_speed = comma_fix.get('speed')
    if type(comma_speed) in (int, float) and math.isfinite(comma_speed):
        report['receiver_speed_difference_mps'] = round(speed-comma_speed, 3)
    comma_accuracy = comma_fix.get('accuracy_m')
    if type(comma_accuracy) in (int, float) and math.isfinite(comma_accuracy) and comma_accuracy > 0:
        report['comma_reported_accuracy_m'] = round(comma_accuracy, 2)
    comma_bearing = comma_fix.get('bearing_deg')
    if (type(comma_bearing) in (int, float) and math.isfinite(comma_bearing)
            and speed >= 3 and type(comma_speed) in (int, float)
            and math.isfinite(comma_speed) and comma_speed >= 3):
        report['receiver_heading_difference_deg'] = round(
            abs((motion['heading_deg']-comma_bearing+180) % 360-180), 2)
    report['comparison_reason'] = 'receiver_offsets_only_not_road_validation'
    return report
