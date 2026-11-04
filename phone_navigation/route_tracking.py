"""Route tracking on the comma. No actuator or control interfaces."""
import math
from bisect import bisect_left, bisect_right
import json
import os
from pathlib import Path
import threading
import time
import uuid
import tempfile
from .mapped_stop_signs import fetch_stops
from .road_stop_lookup import RoadStopLookup
from .vehicle_snapshot import wheel_speed, motion_sample
from .vehicle_snapshot import wheel_speed as wheel_speed_sample
from .navigation_timing import ProgressTiming, ManeuverCompletion
from .position_observer import PositionObserver, observation_snapshot
from .segment_match import nearest_segment
from .tesla_gps_assist import select_fix, enabled as tesla_gps_enabled
from .navigation_trace import NavigationTrace, snapshot as trace_snapshot

STOP_SETTINGS_PATH = Path('/data/nap_turn_settings.json')
ROUTE_CACHE_PATH = Path('/data/nap_active_route.json')
ROUTE_CACHE_MAX_BYTES = 4_000_000
GUIDANCE_INTERVAL_S = 0.2
ROAD_LOOKUP_INTERVAL_S = 1.0
MAP_STATUS_PATH = Path('/data/nap_map_lookup_status.json')


def navigation_maneuvers_enabled():
    try:
        from openpilot.common.params import Params
        return Params().get_bool("NAPNavigationManeuvers")
    except (ImportError, OSError):
        return None


def mapped_stops_enabled(path=STOP_SETTINGS_PATH):
    try:
        from openpilot.common.params import Params
        if Params().get_bool("NAPMapDrivingAssist"):
            return True
    except (ImportError, OSError):
        pass
    try:
        with open(path) as stream:
            raw = stream.read(2049)
        return len(raw) <= 2048 and json.loads(raw).get('mapped_stop_slowdown') is True
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def coordinate(value):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError('Invalid destination coordinates')
    lon, lat = map(float, value)
    if not math.isfinite(lon + lat) or abs(lon) > 180 or abs(lat) > 90:
        raise ValueError('Invalid destination coordinates')
    return [lon, lat]


def distance(a, b):
    lat = math.radians(b[1] - a[1]); lon = math.radians(b[0] - a[0])
    h = math.sin(lat / 2)**2 + math.cos(math.radians(a[1])) * math.cos(math.radians(b[1])) * math.sin(lon / 2)**2
    return 12742000 * math.asin(math.sqrt(min(1, max(0, h))))


class Route:
    def __init__(self, raw):
        self.raw = raw
        self.points, self.steps, self.length = [], [], 0.0
        self.duration = float(raw['duration'])
        if not math.isfinite(self.duration) or self.duration < 0:
            raise ValueError('Invalid route duration')
        for leg in raw['legs']:
            for step in leg['steps']:
                self.steps.append(dict(step, at=self.length))
                for value in step['geometry']['coordinates']:
                    point = coordinate(value)
                    if self.points:
                        self.length += distance(self.points[-1][0], point)
                    self.points.append((point, self.length))
                    if len(self.points) > 20000:
                        raise ValueError('Route too large; choose a closer destination')
        if len(self.points) < 2 or self.length < 1 or not self.steps:
            raise ValueError('No usable driving route returned')
        for step in self.steps:
            if not isinstance(step.get('maneuver'), dict):
                raise ValueError('Invalid route instructions')

    def match(self, location, progress, ahead, bearing=None):
        key = lambda point: point[1]
        start = max(0, bisect_left(self.points, progress-25, key=key)-1)
        end = min(len(self.points)-1, bisect_right(self.points, progress+ahead, key=key))
        lon, lat = location
        scale = math.cos(math.radians(lat))
        def xy(point):
            return (111195 * (point[0]-lon) * scale, 111195 * (point[1]-lat))
        best, self.match_ambiguous = nearest_segment(self.points, 0, 0, bearing, start, end, xy)
        self.heading_error_deg = best[2] if best else None
        self.match_error_m = best[0] if best else None
        return None if best is None or self.match_ambiguous else best[:2]

    def guidance(self, progress, label, route_id, selected_index=None):
        # Retain the intersection until actually passed; do not skip it 6 m early.
        index, step = (next(((i, s) for i, s in enumerate(self.steps) if i > 0 and s['at'] >= progress-8),
                           (len(self.steps)-1, self.steps[-1])) if selected_index is None
                       else (selected_index, self.steps[selected_index]))
        maneuver = step['maneuver']
        kind, modifier = str(maneuver.get('type', '')), str(maneuver.get('modifier', ''))
        road = (' onto ' + str(step['name'])) if step.get('name') else ''
        text = ('Keep' if kind == 'fork' else 'Turn' if kind in ('turn', 'end of road') else 'Continue') + ' ' + (modifier or 'straight') + road
        if kind in ('roundabout', 'rotary'):
            text = 'Take exit ' + str(maneuver.get('exit', '')) + ' at the roundabout' + road
        if kind == 'arrive':
            text = 'Arrive at your destination'
        remaining = max(0, self.length-progress)
        seconds = self.duration*remaining/self.length
        upcoming = []
        for i in range(index, min(index+3, len(self.steps))):
            entry = self.steps[i]
            m = entry['maneuver']
            before, after = m.get('bearing_before'), m.get('bearing_after')
            angle = None
            if (type(before) in (int, float) and type(after) in (int, float)
                    and math.isfinite(before) and math.isfinite(after)):
                angle = (after-before+180) % 360-180
            upcoming.append({'maneuver_id': str(i), 'type': str(m.get('type', ''))[:40],
                             'modifier': str(m.get('modifier', ''))[:40],
                             'road': str(entry.get('name', ''))[:80],
                             'distance_m': round(entry['at']-progress, 1),
                             'turn_angle_deg': angle})
        return {'route_context': {'progress_m': round(progress, 1),
                                 'route_length_m': round(self.length, 1),
                                 'completed_through_id': str(index-1),
                                 'upcoming': upcoming}, 'source': 'comma-gps', 'route_id': route_id, 'route_state': 'active', 'maneuver_id': str(index),
                'maneuver': {'type': kind, 'modifier': modifier, 'distance_m': step['at']-progress,
                             'primary_text': text[:160], 'secondary_text': label[:160]},
                'distance_remaining_m': remaining, 'time_remaining_s': seconds,
                'eta_unix_ms': int((time.time()+seconds)*1000)}


def valid_fix(message, mono, now, wall, valid=True, source='gpsLocation'):
    """Use producer timestamps, never renew an old fix based on receipt time."""
    try:
        age = now-mono
        point = coordinate([message.longitude, message.latitude])
        gps_age = wall-message.unixTimestampMillis/1000
        accuracy = float(message.horizontalAccuracy)
        speed = float(message.speed)
        bearing = float(message.bearingDeg)
        if (not valid or not message.hasFix or not 0 <= age <= 3 or not -1 <= gps_age <= 5
                or not math.isfinite(accuracy+speed) or accuracy < 0 or accuracy > 40 or not 0 <= speed <= 100):
            return None
        # This branch's qcomgpsd leaves horizontalAccuracy unset (zero).
        # Report unknown, rather than presenting it as perfect accuracy.
        if source == 'gpsLocationExternal' and accuracy == 0:
            return None
        return {'point': point, 'mono': now-max(age, max(0, gps_age)), 'sample_mono': mono,
                'accuracy_m': accuracy or None,
                'speed': speed, 'bearing_deg': bearing % 360 if math.isfinite(bearing) else None,
                'source': source}
    except (ValueError, TypeError, AttributeError, OverflowError):
        return None


class CommaLocation:
    def __init__(self):
        self.sm = None

    def sample(self):
        # Construct and use the messaging subscriber only on its worker thread.
        if self.sm is None:
            from cereal import messaging
            self.sm = messaging.SubMaster(['gpsLocation', 'gpsLocationExternal', 'livePose'])
        self.sm.update(0)
        now, wall = time.monotonic(), time.time()
        fixes = []
        for service in ('gpsLocationExternal', 'gpsLocation'):
            if self.sm.seen[service]:
                fix = valid_fix(self.sm[service], self.sm.logMonoTime[service]/1e9, now, wall,
                                self.sm.valid[service], service)
                if fix:
                    fixes.append(fix)
        if not fixes:
            return None
        freshest = max(fixes, key=lambda f: f['mono'])
        # Prefer a nearly simultaneous fix with measured accuracy; an unset
        # qcom accuracy is unknown and otherwise blocks signal/turn gates.
        measured = [f for f in fixes if f['accuracy_m'] is not None and freshest['mono']-f['mono'] <= .5]
        result = max(measured, key=lambda f: f['mono']) if measured else freshest
        result['wheel_speed'] = wheel_speed(now)
        return result


    def observation_pose(self, now):
        """Motion-only input; it does not replace a GNSS fix or control gate."""
        try:
            service = 'livePose'
            if self.sm is None or not self.sm.seen[service]:
                return None
            message = self.sm[service]
            orientation = message.orientationNED
            return {'mono': self.sm.logMonoTime[service] / 1e9,
                    'yaw_rad': float(orientation.z), 'yaw_std_rad': float(orientation.zStd),
                    'valid': bool(self.sm.valid[service] and self.sm.alive[service]
                                  and message.inputsOK and message.sensorsOK and orientation.valid)}
        except (AttributeError, KeyError, TypeError, ValueError):
            return None


def model_decision(path='/dev/shm/nap_navigation_decision.json'):
    try:
        with open(path) as stream:
            raw = stream.read(2049)
        if len(raw) > 2048:
            raise ValueError('oversized decision')
        state = json.loads(raw)
        if not 0 <= time.monotonic()-state['received_mono'] <= 2:
            raise ValueError('stale decision')
        return state
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return {'desire': 'none', 'final_desire': None, 'source': 'unavailable', 'model_evaluated': False, 'reason': 'Model diagnostics unavailable or stale; model may be offroad or need restart'}


class NavigationTracker:
    def __init__(self, route_request, bridge, update=None, clear=None, location=None):
        self.request, self.bridge, self.update, self.clear = route_request, bridge, update, clear
        self.location = location if location is not None else CommaLocation()
        self.road_stops = RoadStopLookup()
        self.lock = threading.RLock()
        self.timing = ProgressTiming()
        self.completion = ManeuverCompletion()
        self.trace = NavigationTrace()
        self.position_observer = PositionObserver()
        self.position_observation = {'mode': 'live_guidance', 'used_for_control': False, 'used_for_guidance': False,
                                     'valid': False, 'reason': 'waiting_for_inputs'}
        self.quit = threading.Event()
        self.worker = None
        self.generation = 0
        self.destination = None
        self.route = None
        self.route_id = ''
        self.progress = 0
        self.fix = None
        self.last_match = 0
        self.last_request = -1e9
        self.fetching = False
        self.stop_points = []
        self.stop_coverage_end = 0.0
        self.stop_fetching = False
        self.stop_last_request = -1e9
        self.stop_state = 'off'
        self.stop_error = ''
        self.last_stop_lookup = {}
        try:
            self.last_stop_lookup = json.loads(MAP_STATUS_PATH.read_text()[:1024])
        except (OSError, ValueError, TypeError):
            pass
        self.cache_path = ROUTE_CACHE_PATH
        self.last_cache_write = 0.0

        # Route-request recovery starts fast after a connectivity interruption,
        # then backs off if the network remains unavailable.
        self.retry_delay = 3.0

        # When GPS disappears while driving, allow a much wider forward search
        # on the existing route after the fix returns. This lets long cellular
        # dead zones recover without requiring a fresh online route immediately.
        self.reacquire_until = 0.0

        self.off_since = None
        self.nav = {'route_state': 'inactive'}
        self.message = 'Choose a destination. The comma supplies GPS.'
        self.cache_retry_pending = False
        self.restore_cache()

    def save_cache(self):
        if self.route is None or self.destination is None:
            return
        payload = {'version': 1, 'saved_at': time.time(), 'destination': self.destination,
                   'route_id': self.route_id, 'route': self.route.raw, 'progress': self.progress,
                   'stop_points': self.stop_points, 'stop_coverage_end': self.stop_coverage_end}
        raw = json.dumps(payload, separators=(',', ':'), allow_nan=False).encode()
        if len(raw) > ROUTE_CACHE_MAX_BYTES:
            return
        name = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.cache_path.parent, prefix='.nap-route-', delete=False) as stream:
                name = stream.name
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.cache_path)
            self.last_cache_write = time.monotonic()
        except OSError:
            pass
        finally:
            if name and os.path.exists(name):
                try:
                    os.unlink(name)
                except OSError:
                    pass

    def clear_cache(self):
        try:
            self.cache_path.unlink(missing_ok=True)
        except OSError:
            pass

    def restore_cache(self):
        try:
            with self.cache_path.open('rb') as stream:
                raw = stream.read(ROUTE_CACHE_MAX_BYTES+1)
            if len(raw) > ROUTE_CACHE_MAX_BYTES:
                return
            data = json.loads(raw)
            age = time.time()-data['saved_at']
            if data.get('version') != 1:
                return
            if age < 0:
                # AGNOS can start with an old clock before time synchronization.
                # Retry this saved route after the clock catches up.
                self.cache_retry_pending = True
                return
            self.cache_retry_pending = False
            if age > 24*3600:
                return
            destination = data['destination']
            coordinate(destination['location'])
            route = Route(data['route'])
            progress = float(data['progress'])
            if not 0 <= progress <= route.length or not isinstance(data['route_id'], str):
                return
            stops = data.get('stop_points', [])
            if not isinstance(stops, list) or len(stops) > 500:
                return
            stop_points = [s for s in stops if isinstance(s, dict) and isinstance(s.get('id'), str)
                                and type(s.get('at_m')) in (int, float) and 0 <= s['at_m'] <= route.length]
            coverage = float(data.get('stop_coverage_end', 0))
            if not math.isfinite(coverage):
                return
            coverage = min(route.length, max(0, coverage))
            # Commit only after the complete cache has passed validation.
            self.destination, self.route, self.route_id, self.progress = destination, route, data['route_id'], progress
            self.stop_points, self.stop_coverage_end = stop_points, coverage
            self.stop_state = 'ready' if self.stop_coverage_end > self.progress else 'waiting'
            self.reacquire_until = time.monotonic()+120
            self.bridge.route_active = True
            self.message = 'Cached route restored. Waiting for fresh comma GPS.'
            self.nav = {'route_state': 'recalculating'}
        except (OSError, ValueError, TypeError, KeyError, OverflowError, AttributeError, RecursionError):
            return

    def start_worker(self):
        if self.worker is None:
            self.worker = threading.Thread(target=self.run, name='nap-navigation', daemon=True)
            self.worker.start()

    def run(self):
        next_road_lookup = -1e9
        next_cache_retry = -1e9
        while not self.quit.is_set():
            cycle_start = time.monotonic()
            try:
                with self.lock:
                    if (self.cache_retry_pending and self.destination is None
                            and cycle_start >= next_cache_retry):
                        next_cache_retry = cycle_start + 5
                        self.restore_cache()
                fix = self.location.sample()
                self.tick(fix)
                # Optional map work stays at 1 Hz; only guidance runs faster.
                if cycle_start >= next_road_lookup:
                    next_road_lookup = cycle_start + ROAD_LOOKUP_INTERVAL_S
                    # The road lookup fills gaps while route stop coverage is
                    # unavailable. Avoid duplicate map requests once the route
                    # has usable cached stop coverage at the current progress.
                    with self.lock:
                        route_stop_covered = (self.nav.get('route_state') == 'active'
                                              and mapped_stops_enabled()
                                              and self.stop_state in ('ready', 'partial')
                                              and self.progress <= self.stop_coverage_end)
                    self.road_stops.tick(self.fix, route_stop_covered)
            except Exception:
                with self.lock:
                    self.fix = None
                    try:
                        self.bridge.clear('Comma location service unavailable')
                        self.message = 'Navigation paused: comma location service unavailable.'
                    except OSError:
                        self.message = 'Navigation bridge write failed; guidance will expire.'
            # Include work time in the period. Never burst through missed
            # iterations after slow I/O; stale guidance still expires normally.
            self.quit.wait(max(0.0, GUIDANCE_INTERVAL_S - (time.monotonic()-cycle_start)))

    def status(self):
        with self.lock:
            now = time.monotonic()
            fresh = self.fix is not None and 0 <= now-self.fix['mono'] <= 3
            bridge_status = self.bridge.status()
            stop_enabled = mapped_stops_enabled()
            next_stop = self.next_stop() if stop_enabled else None
            return {'ok': True, 'enabled': bridge_status['enabled'], 'bridge': bridge_status, 'gps_ready': fresh,
                    'accuracy_m': self.fix['accuracy_m'] if fresh else None,
                    'accuracy_source': self.fix.get('accuracy_source', self.fix['source']) if fresh else None,
                    'route_match': self.route_match_status(),
                    'trace': {'records_this_process':self.trace.count,'last_error':self.trace.error},
                    'tesla_gps_assist': {'enabled': tesla_gps_enabled(),
                                         'position_source': self.fix['source'] if fresh else None},
                    'position_observer': observation_snapshot(self.position_observation, now),
                    'destination': self.destination, 'navigation': self.current_guidance(now),
                    'control_navigation': self.nav,
                    'message': self.message, 'active': self.destination is not None,
                    'maneuvers_enabled': navigation_maneuvers_enabled(),
                    'model_decision': model_decision(), 'road_stops': self.road_stops.status(),
                    'mapped_stops': {'enabled': stop_enabled,
                                     'state': self.stop_state if stop_enabled else 'off',
                                     'error': self.stop_error if stop_enabled and self.stop_state in ('unavailable', 'partial') else '',
                                     'loaded_ahead': len([s for s in self.stop_points if s['at_m'] >= self.progress]) if stop_enabled else 0,
                                     'coverage_remaining_m': max(0, round(self.stop_coverage_end-self.progress)) if stop_enabled else 0,
                                     'next_distance_m': next_stop['distance_m'] if next_stop else None,
                                     'last_lookup': self.last_stop_lookup}}

    def next_stop(self):
        for stop in self.stop_points:
            remaining = stop['at_m']-self.progress
            if -8 <= remaining <= 250:
                return {'id': stop['id'], 'distance_m': round(remaining, 1)}
        return None

    def fetch_mapped_stops(self, route, route_id, start):
        try:
            points, end = fetch_stops(route, start)
            state = 'ready'
            error = ''
        except Exception as exc:
            points, end, state = [], start, 'unavailable'
            error = str(exc)[:180]
        with self.lock:
            if route_id == self.route_id:
                if state == 'ready':
                    existing = {point['id']: point for point in self.stop_points if point['at_m'] >= self.progress-20}
                    existing.update({point['id']: point for point in points})
                    self.stop_points = sorted(existing.values(), key=lambda point: point['at_m'])[:500]
                    self.stop_coverage_end = max(self.stop_coverage_end, end)
                    self.stop_state = 'ready'
                    self.save_cache()
                else:
                    self.stop_state = 'partial' if self.stop_coverage_end > self.progress else 'unavailable'
                self.stop_error = error
                self.last_stop_lookup = {'time': time.time(), 'state': state,
                                         'matched': len(points), 'coverage_m': round(end-start),
                                         'error': error}
                try:
                    temp = MAP_STATUS_PATH.with_suffix('.tmp')
                    temp.write_text(json.dumps(self.last_stop_lookup))
                    os.replace(temp, MAP_STATUS_PATH)
                except OSError:
                    pass
            self.stop_fetching = False

    def maybe_fetch_mapped_stops(self, now):
        if not mapped_stops_enabled():
            self.stop_points = []
            self.stop_coverage_end = 0.0
            self.stop_state = 'off'
            self.stop_error = ''
            self.stop_last_request = -1e9
            return
        if (self.route is None or self.stop_fetching or now-self.stop_last_request < 60
                or self.stop_coverage_end >= self.route.length
                or self.stop_coverage_end-self.progress >= 20000):
            return
        self.stop_fetching = True
        self.stop_last_request = now
        self.stop_state = 'loading'
        start = max(0.0, self.stop_coverage_end-50 if self.stop_coverage_end > self.progress else self.progress-50)
        threading.Thread(target=self.fetch_mapped_stops,
                         args=(self.route, self.route_id, start),
                         name='nap-mapped-stops', daemon=True).start()

    def set_enabled(self, enabled):
        if type(enabled) is not bool:
            raise ValueError('enabled must be a boolean')
        if enabled:
            from .vehicle_snapshot import require_park
            require_park()
        from openpilot.common.params import Params
        with self.lock:
            # Keep the paired switch and the model's persisted permission aligned.
            # Set the bridge last when enabling so a failed parameter write cannot
            # advertise navigation authority that the model did not receive.
            if not enabled:
                self.bridge.set_enabled(False)
            Params().put_bool("NAPNavigationManeuvers", enabled)
            if enabled:
                self.bridge.set_enabled(True)
            return self.status()

    def start(self, destination):
        point = coordinate(destination.get('location'))
        label = str(destination.get('label', 'Destination'))[:160]
        with self.lock:
            self.generation += 1
            self.cache_retry_pending = False
            self.bridge.route_active = True
            self.bridge.clear('New destination; waiting for route and GPS')
            if self.clear:
                self.clear()
            self.destination = {'location': point, 'label': label}
            self.clear_cache()
            self.route = None
            self.stop_points = []
            self.stop_state = 'off'
            self.stop_error = ''
            self.progress = 0
            self.off_since = None
            self.nav = {'route_state': 'calculating'}
            self.message = 'Waiting for comma GPS. Turn the car on with a clear view of the sky.'
            return self.status()

    def stop(self):
        with self.lock:
            self.generation += 1
            self.cache_retry_pending = False
            self.destination = None
            self.clear_cache()
            self.route = None
            self.stop_points = []
            self.stop_state = 'off'
            self.stop_error = ''
            self.nav = {'route_state': 'inactive'}
            self.message = 'Guidance stopped.'
            self.bridge.route_active = False
            self.bridge.clear('Route stopped; directions are inactive')
            if self.clear:
                self.clear()
            return self.status()

    def fetch(self, origin, destination, generation):
        try:
            route = Route(self.request(origin, destination['location']))
            with self.lock:
                if generation != self.generation:
                    return
                self.route, self.route_id, self.progress = route, str(uuid.uuid4()), 0
                self.stop_points = []
                self.stop_coverage_end = 0.0
                self.stop_state = 'waiting'
                self.stop_error = ''
                self.last_match = time.monotonic()
                self.off_since = None
                self.reacquire_until = 0.0

                # Successful network request: future reconnects start with
                # the short retry interval again.
                self.retry_delay = 3.0

                self.message = 'Route ready. Waiting for a fresh comma GPS fix.'
                self.save_cache()
        except Exception as exc:
            with self.lock:
                if generation == self.generation:
                    retry_after = self.retry_delay
                    self.message = (
                        'Route request failed. Check the comma internet connection; '
                        f'retrying in {int(retry_after)} seconds.'
                    )
                    self.nav = {'route_state': 'error'}
                    self.bridge.clear('Route calculation failed; retrying')

                    # 3 -> 5 -> 10 -> 20 -> 30 second bounded backoff.
                    if self.retry_delay < 5:
                        self.retry_delay = 5.0
                    elif self.retry_delay < 10:
                        self.retry_delay = 10.0
                    elif self.retry_delay < 20:
                        self.retry_delay = 20.0
                    else:
                        self.retry_delay = 30.0
        finally:
            with self.lock:
                self.fetching = False

    def maybe_fetch(self, now):
        if self.fetching or now-self.last_request < self.retry_delay:
            return
        self.last_request, self.fetching = now, True
        self.message = 'Calculating route online…'
        threading.Thread(target=self.fetch, args=(self.fix['point'], dict(self.destination), self.generation),
                         name='nap-route-request', daemon=True).start()

    def observe_position(self, fix, now):
        # Failures in this diagnostic path cannot clear/advance a route, write a
        # desire, reserve a blinker, or replace production coordinates/progress.
        with self.lock:
            try:
                route = self.route
                self.position_observer.route(self.route_id if route is not None else None,
                                             route.points if route is not None else (),
                                             route.steps if route is not None else ())
                pose_reader = getattr(self.location, 'observation_pose', None)
                pose = pose_reader(now) if pose_reader is not None else None
                self.position_observation = self.position_observer.update(fix, motion_sample(now), pose, now)
            except Exception as exc:
                self.position_observation = {'mode': 'live_guidance', 'used_for_control': False, 'used_for_guidance': False,
                                             'valid': False, 'received_mono': now,
                                             'reason': 'observer_error', 'error': type(exc).__name__}

    def route_match_status(self):
        return {'cross_track_m': getattr(self.route, 'match_error_m', None),
                'heading_error_deg': getattr(self.route, 'heading_error_deg', None),
                'ambiguous': getattr(self.route, 'match_ambiguous', False)}

    def record_trace(self, event, now, fix=None, found=None, nav=None):
        decision=trace_snapshot('/dev/shm/nap_navigation_decision.json',now,'received_mono')
        signal=trace_snapshot('/dev/shm/nap_navigation_signal_status.json',now,'time',1)
        request=trace_snapshot('/dev/shm/nap_navigation_signal_request.json',now,'time',1)
        data={'generation':self.generation,'gps':fix,'match':found,'route_match':self.route_match_status(),'progress_m':self.progress,
              'position_observer':self.position_observation,
              'timing':{'reason':self.timing.reason,'raw_m':self.timing.raw,'innovation_m':self.timing.innovation},
              'guidance':{k:(nav or self.nav).get(k) for k in ('route_state','maneuver_id','maneuver')},
              'model':decision,'signal':signal,'signal_request':request}
        self.trace.write(event,now,self.route_id,data,self.route.raw if self.route is not None else None,
                         continuous=event=='guidance' or self.route is not None)

    def current_guidance(self, now):
        return self.nav

    def publish_guidance(self, was_active):
        # The observer is diagnostic. Publish one route state and maneuver.
        if self.update and self.nav.get('route_state') in ('active', 'arrived'):
            self.update(self.nav)
        elif was_active and self.clear:
            self.clear()

    def tick(self, fix, now=None):
        now = time.monotonic() if now is None else now
        with self.lock:
            was_active = self.nav.get('route_state') in ('active', 'arrived')
            self.control_tick(fix, now)
            self.publish_guidance(was_active)

    def control_tick(self, fix, now=None):
        now = time.monotonic() if now is None else now
        with self.lock:
            fix = select_fix(now, self.route, self.progress, fix, wheel_speed_sample(now))
            if fix is not None and (fix['speed'] < 2 or
                                    fix.get('wheel_speed') is not None and fix['wheel_speed'] < 2):
                fix = dict(fix, bearing_deg=None)
            self.fix = fix
            self.observe_position(fix, now)
            if self.destination is None:
                self.bridge.route_active = False
                self.bridge.clear('No route selected')
                self.record_trace('no_route', now, fix)
                return
            if fix is None or not 0 <= now-fix['mono'] <= 3:
                self.bridge.clear('GPS missing or stale; guidance paused')

                # Keep a long recovery window after GPS returns. The car may
                # have travelled several miles through a cellular dead zone.
                if self.route is not None:
                    self.reacquire_until = now + 120.0

                self.nav = {'route_state': 'calculating' if self.route is None else 'recalculating'}
                self.message = 'Waiting for comma GPS. Turn the car on with a clear view of the sky.'
                self.record_trace('gps_unavailable', now, fix)
                return
            if self.route is None:
                self.bridge.clear('Route calculation pending')
                self.maybe_fetch(now)
                self.record_trace('route_pending', now, fix)
                return
            elapsed = max(1, now-self.last_match)

            # Normal matching remains tightly bounded.
            ahead = min(250, max(120, fix['speed']*elapsed+60))

            # After a GPS/network interruption, search much farther forward
            # on the already-loaded route. This supports long service gaps
            # without forcing a new online route as soon as connectivity returns.
            if now <= self.reacquire_until:
                ahead = max(ahead, 10000)

            # Anchor each GPS observation once. Re-matching a projected old
            # fix against advanced progress can count its projection twice.
            match_key = (id(self.route), self.route_id, fix.get('source'),
                         fix.get('sample_mono', fix['mono']))
            if match_key != getattr(self, '_match_key', None):
                self._match_found = self.route.match(fix['point'], self.progress, ahead,
                                                     fix.get('bearing_deg'))
                self._match_key = match_key
            found = self._match_found
            if found is None or found[0] > 40:
                self.bridge.clear('Off route or uncertain GPS; guidance paused')
                self.nav = {'route_state': 'recalculating'}
                self.message = ('Ambiguous route position; navigation influence paused.'
                                if getattr(self.route, 'match_ambiguous', False) else
                                'Off route or uncertain GPS. Navigation influence paused.')
                # Ambiguity or missing heading is not evidence of leaving the route.
                # Require sustained geometric separation and a new fix to retry.
                if self.route.match_ambiguous or self.route.match_error_m is None:
                    self.off_since = None
                elif self.off_since is None:
                    self.off_since = now
                elif now-self.off_since >= 5 and fix['mono'] > self.last_request:
                    self.maybe_fetch(now)
                self.record_trace('match_uncertain', now, fix, found)
                return
            self.off_since = None
            self.reacquire_until = 0.0
            # Project along route geometry, then advance only within the fresh GPS
            # interval using a fresh vehicle-speed sample. Never integrate a fix twice.
            age = now-fix['mono']
            wheel_speed = fix.get('wheel_speed')
            projection_valid = (0 <= age <= 1.5 and wheel_speed is not None
                                and self.route.heading_error_deg is not None
                                and self.route.heading_error_deg <= 20
                                and not self.route.match_ambiguous)
            timed = self.timing.update(self.route_id, match_key, found[1], fix['mono'], now,
                                       wheel_speed, fix['accuracy_m'], projection_valid, gps_speed=fix['speed'])
            if timed is None:
                self.bridge.clear('GPS progress discontinuity; waiting for a coherent fix')
                self.nav = {'route_state':'recalculating'}
                self.message = 'GPS progress jump rejected; waiting for a coherent position.'
                self.record_trace('gps_jump_rejected', now, fix, found)
                return
            projected = timed
            projection_valid = projection_valid and self.timing.trusted
            self.progress = max(0., min(self.route.length, projected))
            self.last_match = now
            if now-self.last_cache_write >= 60:
                self.save_cache()
            self.maybe_fetch_mapped_stops(now)
            selected = self.completion.index(self.route,self.route_id,self.progress,found[1],
                                             fix.get('bearing_deg'),self.route.heading_error_deg,projection_valid)
            nav = self.route.guidance(self.progress, self.destination['label'], self.route_id, selected)
            if fix['source'] == 'tesla-can':
                nav['source'] = 'tesla-can'
            nav['route_context'].update(projection_valid=projection_valid,
                                        heading_error_deg=self.route.heading_error_deg,
                                        path_ambiguous=self.route.match_ambiguous,
                                        match_error_m=round(found[0], 1),
                                        gps_accuracy_m=fix['accuracy_m'],
                                        gps_age_s=round(now-fix['mono'], 2),
                                        tracking='local GPS / downloaded route',
                                        map_lookup_state=self.stop_state,
                                        map_loaded_ahead=len([s for s in self.stop_points if s['at_m'] >= self.progress]),
                                        map_coverage_remaining_m=max(0, round(self.stop_coverage_end-self.progress)))
            if nav['distance_remaining_m'] < 15 and found[0] < 20:
                nav['route_state'] = 'arrived'
            if mapped_stops_enabled() and self.stop_state in ('ready', 'partial') and self.progress <= self.stop_coverage_end:
                nav['mapped_stop_sign'] = self.next_stop()
            # Bound IPC size regardless of provider road-name lengths.
            self.bridge.route_active = nav['route_state'] != 'arrived'
            self.bridge.publish(nav, (now-fix['mono'])*1000)
            self.nav = nav
            self.record_trace('guidance', now, fix, found, nav)
            self.message = 'Arrived. You can stop guidance.' if nav['route_state'] == 'arrived' else 'The comma is following your route. You can close this page.'
