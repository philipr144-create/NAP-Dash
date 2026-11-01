"""Motion-assisted position for live human guidance. Never authorizes controls.

Coordinates are local east/north metres. Device NED yaw supplies *changes* in
heading; moving GNSS course estimates the fixed device-to-vehicle yaw offset.
This is bounded dead reckoning with a conservative error budget, not surveyed
position, lane localization, a stop-line detector, or a calibrated covariance.
"""
import math
from bisect import bisect_left
from collections import deque

EARTH_M = 6371000.0
MOTION_MAX_AGE_S = .25
MOTION_MAX_GAP_S = .45
GPS_MAX_AGE_S = 6.0


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def angle(value):
    return (value + math.pi) % (2 * math.pi) - math.pi


def rotate(x, y, offset):
    # Positive NED yaw turns north toward east.
    return x * math.cos(offset) + y * math.sin(offset), y * math.cos(offset) - x * math.sin(offset)


def observation_snapshot(observation, now):
    """Status reads cannot make an old worker result appear live."""
    result = dict(observation)
    stamp = result.get('received_mono')
    age = now - stamp if finite(now) and finite(stamp) else None
    result['snapshot_age_s'] = round(age, 3) if age is not None else None
    result['fresh'] = age is not None and 0 <= age <= .5
    if not result['fresh']:
        result.update(valid=False, used_for_guidance=False, reason='observer_snapshot_expired')
    elif finite(result.get('gps_sensor_age_s')):
        result['gps_sensor_age_s'] = round(result['gps_sensor_age_s'] + age, 3)
        if result['gps_sensor_age_s'] > GPS_MAX_AGE_S:
            result.update(valid=False, reason='gps_prediction_expired')
    return result


class PositionObserver:
    def __init__(self):
        self.origin = None
        self.history = deque(maxlen=64)
        self.anchor = None
        self.offset = None
        self.gps_key = None
        self.last_accepted = None
        self.last_accuracy = None
        self.reacquire = None
        self.rejected = 0
        self.reason = 'waiting_for_inputs'
        self.route_key = None
        self.route_points = []
        self.route_steps = []
        self.route_progress = None
        self.last_route_travel = None
        self.acquisition = None

    def reset_match(self):
        self.route_progress = None
        self.last_route_travel = None
        self.acquisition = None

    def xy(self, point):
        lon, lat = point
        return (EARTH_M * math.radians(lon - self.origin[0]) * math.cos(math.radians(self.origin[1])),
                EARTH_M * math.radians(lat - self.origin[1]))

    def point(self, x, y):
        return [self.origin[0] + math.degrees(x / (EARTH_M * math.cos(math.radians(self.origin[1])))),
                self.origin[1] + math.degrees(y / EARTH_M)]

    def route(self, key, points=(), steps=()):
        """Copy plain geometry; do not call/mutate the production Route matcher."""
        if key == self.route_key:
            return
        self.route_key = key
        self.route_points = list(points)[:20000]
        self.route_steps = [(float(s['at']), str(i)) for i, s in enumerate(steps)
                            if i > 0 and finite(s.get('at'))]
        self.reset_match()

    def motion(self, now, car, pose):
        if not (isinstance(car, dict) and isinstance(pose, dict)
                and car.get('can_valid') is True and pose.get('valid') is True
                and all(finite(v) for v in (car.get('mono'), car.get('speed'), pose.get('mono'),
                                            pose.get('yaw_rad'), pose.get('yaw_std_rad')))
                and 0 <= now - car['mono'] <= MOTION_MAX_AGE_S
                and 0 <= now - pose['mono'] <= MOTION_MAX_AGE_S
                and 0 <= car['speed'] <= 45 and 0 <= pose['yaw_std_rad'] <= .35
                and (car.get('gear') == 'drive' or car['speed'] <= .1)):
            self.history.clear()
            self.anchor = None
            self.offset = None
            self.reset_match()
            self.gps_key = None
            self.reason = 'motion_unavailable'
            return None
        speed = 0.0 if car['speed'] < .1 else car['speed']
        yaw = angle(pose['yaw_rad'])
        row = [now, 0., 0., 0., yaw, speed, pose['yaw_std_rad']]
        if self.history:
            previous = self.history[-1]
            dt = now - previous[0]
            dyaw = angle(yaw - previous[4])
            if not 0 < dt <= MOTION_MAX_GAP_S or abs(dyaw) > 1.5 * dt + .02:
                self.history.clear()
                self.anchor = None
                self.offset = None
                self.reset_match()
                self.gps_key = None
                self.reason = 'motion_discontinuity'
                return None
            travel = .5 * (previous[5] + speed) * dt
            heading = previous[4] + .5 * dyaw
            row[1] = previous[1] + math.sin(heading) * travel
            row[2] = previous[2] + math.cos(heading) * travel
            row[3] = previous[3] + travel
        self.history.append(row)
        while len(self.history) > 2 and now - self.history[1][0] > 8:
            self.history.popleft()
        return row

    def at(self, stamp):
        if len(self.history) < 2 or not self.history[0][0] <= stamp <= self.history[-1][0]:
            return None
        rows = list(self.history)
        i = min(max(1, bisect_left(rows, stamp, key=lambda r: r[0])), len(rows) - 1)
        a, b = rows[i - 1], rows[i]
        if b[0] - a[0] > MOTION_MAX_GAP_S:
            return None
        f = (stamp - a[0]) / (b[0] - a[0])
        out = [a[j] + f * (b[j] - a[j]) for j in range(len(a))]
        out[4] = a[4] + f * angle(b[4] - a[4])
        return out

    def prediction(self, row):
        a = self.anchor
        dx, dy = rotate(row[1] - a['motion'][1], row[2] - a['motion'][2], self.offset)
        travel = max(0., row[3] - a['motion'][3])
        age = max(0., row[0] - a['motion'][0])
        # Report an error budget, never call a projected sample a fresh GPS fix.
        budget = a['budget'] + .03 * travel + travel * math.sin(min(.5, row[6] + .1)) + .5 * age
        return a['x'] + dx, a['y'] + dy, budget

    def gps(self, fix, now):
        if not (isinstance(fix, dict) and isinstance(fix.get('point'), (list, tuple))
                and len(fix['point']) == 2 and all(finite(v) for v in fix['point'])
                and abs(fix['point'][0]) <= 180 and abs(fix['point'][1]) < 85
                and all(finite(fix.get(k)) for k in ('mono', 'sample_mono', 'accuracy_m', 'speed'))
                and 0 <= now - fix['mono'] <= 3 and fix['mono'] <= fix['sample_mono'] <= now
                and 0 < fix['accuracy_m'] <= 40 and 0 <= fix['speed'] <= 100):
            return
        key = (fix.get('source'), fix['sample_mono'])
        if key == self.gps_key:
            return
        sample = self.at(fix['mono'])
        if sample is None:
            self.reason = 'waiting_for_motion_history'
            return
        if self.origin is None:
            self.origin = list(fix['point'])
        course = fix.get('bearing_deg')
        moving_course = finite(course) and fix['speed'] >= 3 and sample[5] >= 3
        if self.offset is None:
            if not moving_course:
                self.reason = 'waiting_for_moving_course'
                return
            self.offset = angle(math.radians(course) - sample[4])
        x, y = self.xy(fix['point'])
        self.gps_key = key
        self.reason = 'gps_corrected'
        if self.anchor is not None and fix['mono'] <= self.anchor['motion'][0]:
            self.reason = 'nonforward_gps_time'
            return
        if self.last_accepted is not None and fix['mono'] - self.last_accepted > GPS_MAX_AGE_S:
            # A single returning fix must not silently relocate the old estimate.
            previous = self.reacquire
            self.reacquire = (fix['mono'], x, y)
            if previous is None or not 0 < fix['mono'] - previous[0] <= 2.5 or math.hypot(x - previous[1], y - previous[2]) > 45 * (fix['mono'] - previous[0]) + 10:
                self.reason = 'reacquiring_two_fixes'
                return
            self.anchor = None
            self.reset_match()
            self.reason = 'gps_reacquired'
        if self.anchor is None:
            self.anchor = {'x': x, 'y': y, 'motion': sample, 'budget': fix['accuracy_m']}
        else:
            px, py, budget = self.prediction(sample)
            ix, iy = x - px, y - py
            innovation = math.hypot(ix, iy)
            if innovation > max(25., 1.5 * max(self.last_accuracy or 0., fix['accuracy_m'])):
                self.rejected += 1
                self.reason = 'gps_jump_rejected'
                return
            # Bounded, bidirectional correction: no permanent maximum latch.
            # Fresh stationary wheels prevent GPS wander from moving the car.
            correction = min(innovation * .25, 3.) if sample[5] > 0 and self.history[-1][5] > 0 else 0.
            gain = correction / innovation if innovation else 0.
            self.anchor = {'x': px + ix * gain, 'y': py + iy * gain, 'motion': sample,
                           'budget': fix['accuracy_m'] + innovation - correction}
            if moving_course:
                offset = angle(math.radians(course) - sample[4])
                residual = angle(offset - self.offset)
                if abs(residual) <= .35:
                    self.offset = angle(self.offset + .05 * residual)
        self.last_accepted = fix['mono']
        self.last_accuracy = fix['accuracy_m']

    def match(self, x, y, heading, travel):
        if not self.route_points:
            return {'state': 'no_route'}
        current = x, y, heading, travel
        candidates = []
        start, end = 0, len(self.route_points) - 1
        acquiring = self.route_progress is None
        if acquiring:
            if self.acquisition is None:
                self.acquisition = {'position': current, 'cursor': 0, 'candidates': [], 'overflow': False}
            # All chunks use one frozen observation; never combine positions
            # from different times into a seemingly precise route match.
            x, y, heading, travel = self.acquisition['position']
            start = self.acquisition['cursor']
            end = min(end, start + 256)
        else:
            advanced = max(0., travel - (self.last_route_travel if self.last_route_travel is not None else travel))
            start = max(0, bisect_left(self.route_points, self.route_progress - 25, key=lambda p: p[1]) - 1)
            end = min(end, bisect_left(self.route_points, self.route_progress + max(60., advanced + 25), key=lambda p: p[1]) + 1)
            if end - start > 512:
                return {'state': 'route_window_too_dense'}
        for index in range(start, end):
            a, aa = self.route_points[index]
            b, ba = self.route_points[index + 1]
            if self.route_progress is not None:
                if ba < self.route_progress - 25 or aa > self.route_progress + max(60., advanced + 25):
                    continue
            ax, ay = self.xy(a)
            bx, by = self.xy(b)
            vx, vy = bx - ax, by - ay
            length = vx * vx + vy * vy
            if length < .01:
                continue
            f = max(0., min(1., ((x - ax) * vx + (y - ay) * vy) / length))
            error = math.hypot(x - ax - f * vx, y - ay - f * vy)
            heading_error = abs(math.degrees(angle(math.atan2(vx, vy) - heading)))
            if heading_error <= 35:
                candidates.append((error, aa + f * (ba - aa), heading_error))
        if acquiring:
            candidates += self.acquisition['candidates']
            if candidates:
                best_error = min(c[0] for c in candidates)
                candidates = [c for c in candidates if c[0] <= best_error + 8]
            if len(candidates) > 128:
                self.acquisition['overflow'] = True
                candidates = sorted(candidates)[:128]
            self.acquisition['candidates'] = candidates
            self.acquisition['cursor'] = end
            if end < len(self.route_points) - 1:
                return {'state': 'matching_route', 'segments_checked': end,
                        'segments_total': len(self.route_points) - 1}
            overflow = self.acquisition['overflow']
            self.acquisition = None
            if overflow:
                return {'state': 'ambiguous_candidate_overflow'}
        if not candidates:
            return {'state': 'heading_or_route_mismatch'}
        best = min(candidates)
        ambiguous = any(error <= best[0] + 8 and abs(along - best[1]) > 60 for error, along, _ in candidates)
        if ambiguous or best[0] > 25:
            return {'state': 'ambiguous' if ambiguous else 'off_route', 'cross_track_m': round(best[0], 2)}
        self.route_progress, self.last_route_travel = best[1], travel
        if acquiring:
            # Re-match *now* within the acquired motion-bounded route window.
            return self.match(*current)
        maneuver = next(((at, ident) for at, ident in self.route_steps if at >= best[1] - 8), None)
        return {'state': 'matched', 'progress_m': round(best[1], 2),
                'cross_track_m': round(best[0], 2), 'heading_error_deg': round(best[2], 2),
                'maneuver_id': maneuver[1] if maneuver else None,
                'maneuver_distance_m': round(maneuver[0] - best[1], 2) if maneuver else None}

    def update(self, fix, car, pose, now):
        result = {'mode': 'live_guidance', 'used_for_control': False, 'used_for_guidance': False, 'valid': False,
                  'received_mono': now, 'reason': self.reason}
        if not finite(now):
            return dict(result, received_mono=None, reason='invalid_time')
        row = self.motion(now, car, pose)
        if row is None:
            return dict(result, reason=self.reason)
        self.gps(fix, now)
        age = now - self.last_accepted if self.last_accepted is not None else None
        result.update(reason=self.reason, gps_sensor_age_s=round(age, 3) if age is not None else None,
                      gps_rejections=self.rejected, motion_state='stopped' if row[5] == 0 else 'moving',
                      wheel_sample_age_s=round(now - car['mono'], 3), pose_sample_age_s=round(now - pose['mono'], 3))
        if self.anchor is None or age is None:
            return result
        if not 0 <= age <= GPS_MAX_AGE_S:
            return dict(result, reason='gps_prediction_expired')
        x, y, budget = self.prediction(row)
        heading = angle(row[4] + self.offset)
        result.update(valid=True, point=[round(v, 8) for v in self.point(x, y)],
                      heading_deg=round(math.degrees(heading) % 360, 2),
                      error_budget_m=round(budget, 2), error_budget_kind='conservative_heuristic_not_covariance',
                      route=self.match(x, y, heading, row[3]))
        return result
