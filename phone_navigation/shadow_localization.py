"""Read-only dual-GPS / route-geometry shadow trial.

This never publishes guidance or actuators and does NOT produce an absolute lane
fix or authorize any map controls. Tesla HDOP is not a horizontal error in metres.
"""
import math
from bisect import bisect_left

from .segment_match import nearest_segment

EARTH_M = 6371000.0
MAX_AGE_S = 1.5
MAX_PROJECTION_S = 1.0


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def xy(point, origin):
    lon, lat = point
    lon0, lat0 = origin
    return (EARTH_M * math.radians(lon - lon0) * math.cos(math.radians(lat0)),
            EARTH_M * math.radians(lat - lat0))


def meters_between(a, b):
    return math.hypot(*xy(a, b))


def project(now, fix, wheel_speed):
    """Bring one receiver sample to 'now' using fresh vehicle speed/course.

    No synthetic fix is ever inserted into a production tracker. Missing course,
    missing wheel speed, stale timestamps, or contradictory speeds fail closed.
    """
    if not isinstance(fix, dict):
        return {'valid': False, 'reason': 'unavailable'}
    point = fix.get('point')
    stamp = fix.get('mono')
    speed = fix.get('speed')
    if (not isinstance(point, (tuple, list)) or len(point) != 2
            or not all(finite(v) for v in point)
            or not -180 <= point[0] <= 180 or not -85 <= point[1] <= 85
            or not finite(stamp) or not finite(speed) or not 0 <= speed <= 100):
        return {'valid': False, 'reason': 'invalid_sample'}
    age = now - stamp
    if not finite(now) or not 0 <= age <= MAX_AGE_S:
        return {'valid': False, 'reason': 'stale_or_future'}
    if wheel_speed is None or not finite(wheel_speed) or not 0 <= wheel_speed <= 45:
        return {'valid': False, 'reason': 'wheel_unavailable'}
    if abs(speed - wheel_speed) > 2.5:
        return {'valid': False, 'reason': 'wheel_speed_conflict'}
    bearing = fix.get('bearing_deg')
    if wheel_speed >= 2 and (not finite(bearing) or not 0 <= bearing < 360):
        return {'valid': False, 'reason': 'course_unavailable'}
    if age > MAX_PROJECTION_S and wheel_speed >= 2:
        return {'valid': False, 'reason': 'projection_too_old'}
    lon, lat = point
    travel = max(0., wheel_speed) * min(age, MAX_PROJECTION_S) if wheel_speed >= 2 else 0.
    if travel:
        direction = math.radians(bearing)
        north = travel * math.cos(direction)
        east = travel * math.sin(direction)
        lat += math.degrees(north / EARTH_M)
        lon += math.degrees(east / (EARTH_M * math.cos(math.radians(lat))))
    return {'valid': True, 'reason': 'motion_projected' if travel else 'raw_stationary',
            'point': [lon, lat], 'sample_age_s': round(age, 3),
            'travel_projected_m': round(travel, 2),
            'bearing_deg': bearing if wheel_speed >= 2 else None,
            'sample_mono': stamp}


def route_candidate(route, progress, candidate):
    """Independent segment projection; never calls mutable Route.match()."""
    if route is None:
        return {'state': 'no_route'}
    points = getattr(route, 'points', None)
    if not isinstance(points, (list, tuple)) or len(points) < 2 or not finite(progress):
        return {'state': 'invalid_route'}
    start = max(0, bisect_left(points, progress - 80, key=lambda p: p[1]) - 1)
    end = min(len(points) - 1, bisect_left(points, progress + 350, key=lambda p: p[1]) + 1)
    if end <= start:
        return {'state': 'not_in_window'}
    origin = candidate['point']
    fn = lambda pt: xy(pt, origin)
    best, ambiguous = nearest_segment(points, 0., 0., candidate.get('bearing_deg'), start, end, fn)
    if best is None:
        return {'state': 'no_segment'}
    distance, along, heading = best
    state = 'ambiguous' if ambiguous else 'outside_route' if distance > 25 else 'candidate'
    return {'state': state, 'cross_track_m': round(distance, 2),
            'progress_m': round(along, 2),
            'heading_error_deg': round(heading, 2) if heading is not None else None}


def evaluate(now, comma_fix, tesla_sample, wheel_speed, route=None, progress=0.):
    """Compute a disposable shadow-only comparison, no authority changes."""
    out = {'mode': 'shadow_localization_v1', 'used_for_control': False,
           'used_for_guidance': False, 'map_control_authorized': False,
           'result': 'insufficient_evidence'}
    tesla_fix = None
    if tesla_sample is not None:
        position, motion = tesla_sample
        if abs(position['mono'] - motion['mono']) <= .5:
            tesla_fix = {'point': position['point'], 'mono': position['mono'],
                         'speed': motion['speed_mps'], 'bearing_deg': motion['heading_deg']}
        else:
            out['tesla_reason'] = 'position_motion_timestamp_skew'
    comma = project(now, comma_fix, wheel_speed)
    tesla = project(now, tesla_fix, wheel_speed)
    if out.get('tesla_reason'):
        tesla['reason'] = out['tesla_reason']
    out['comma'] = {k: v for k, v in comma.items() if k != 'point'}
    out['tesla'] = {k: v for k, v in tesla.items() if k != 'point'}
    if not comma['valid'] or not tesla['valid']:
        return out
    gap = abs(comma['sample_mono'] - tesla['sample_mono'])
    out['sample_time_gap_s'] = round(gap, 3)
    if gap > 1.2:
        out['result'] = 'sample_clock_disagreement'
        return out
    separation = meters_between(comma['point'], tesla['point'])
    out['projected_separation_m'] = round(separation, 2)
    accuracy = comma_fix.get('accuracy_m') if isinstance(comma_fix, dict) else None
    # Unknown Tesla accuracy; comparing to Comma accuracy is a diagnostic,
    # not proof of absolute position or independent receiver error.
    if finite(accuracy) and 0 < accuracy <= 40:
        out['comma_reported_accuracy_m'] = round(accuracy, 2)
        out['receiver_agreement'] = ('within_comma_accuracy' if separation <= accuracy
                                     else 'outside_comma_accuracy')
    else:
        out['receiver_agreement'] = 'accuracy_unknown'
    cm = route_candidate(route, progress, comma)
    tm = route_candidate(route, progress, tesla)
    out['route_candidates'] = {'comma': cm, 'tesla': tm}
    if out['receiver_agreement'] == 'outside_comma_accuracy':
        out['result'] = 'receiver_conflict'
    elif (cm.get('state') == 'candidate' and tm.get('state') == 'candidate'
          and abs(cm['progress_m'] - tm['progress_m']) <= 25
          and cm['cross_track_m'] <= 15 and tm['cross_track_m'] <= 15):
        out['result'] = 'route_candidates_consistent'
    elif route is not None and (cm.get('state') != 'candidate' or tm.get('state') != 'candidate'
                                or abs(cm['progress_m'] - tm['progress_m']) > 25):
        out['result'] = 'route_candidate_conflict'
    else:
        out['result'] = 'receiver_consistent_route_unverified'
    return out


class ShadowSession:
    """Compact run statistics; only one count per new sample pair."""
    def __init__(self):
        self.last_key = None
        self.counts = {}

    def update(self, report):
        comma, tesla = report.get('comma', {}), report.get('tesla', {})
        key = (comma.get('sample_mono'), tesla.get('sample_mono'))
        if None in key or key == self.last_key:
            return dict(self.counts)
        self.last_key = key
        result = report['result']
        self.counts[result] = self.counts.get(result, 0) + 1
        return dict(self.counts)
