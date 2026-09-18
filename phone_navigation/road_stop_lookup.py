"""Optional road-only OSM stop hints. No vehicle control or route instruction."""
import json
import math
import os
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .mapped_stop_signs import OVERPASS, angle_gap, allowed_heading, heading, meters

SETTINGS = Path('/data/nap_turn_settings.json')
SNAPSHOT = Path('/dev/shm/nap_road_stop.json')
MAX_RESPONSE = 1_000_000
RADIUS_M = 600


def road_setting_enabled(path=SETTINGS):
    try:
        raw = path.read_text()[:2049]
        return len(raw) <= 2048 and json.loads(raw).get('road_stop_slowdown') is True
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def experimental_enabled(path=Path('/data/params/d/ExperimentalMode')):
    try:
        return path.read_bytes() == b'1'
    except OSError:
        return False


def fetch_roads(point, opener=urlopen):
    lat, lon = point[1], point[0]
    query = (f'[out:json][timeout:15];way(around:{RADIUS_M},{lat:.6f},{lon:.6f})'
             '["highway"~"^(residential|tertiary|secondary|primary|trunk|unclassified|living_street|service|'
             'tertiary_link|secondary_link|primary_link|trunk_link)$"]->.roads;'
             'node(w.roads)["highway"="stop"]->.stops;(.roads;.stops;);out body geom;')
    errors = []
    for endpoint in OVERPASS:
        request = Request(endpoint, data=urlencode({'data': query}).encode(),
                          headers={'User-Agent': 'NAP-Road-Stops/0.1 personal-navigation', 'Accept': 'application/json'})
        try:
            with opener(request, timeout=12) as response:
                raw = response.read(MAX_RESPONSE+1)
            if len(raw) > MAX_RESPONSE:
                raise ValueError('Road lookup response too large')
            elements = json.loads(raw)['elements']
            if not isinstance(elements, list):
                raise ValueError('Invalid road lookup response')
            return elements, len(raw)
        except HTTPError as exc:
            errors.append(endpoint.split('/')[2] + ': HTTP ' + str(exc.code))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors.append(endpoint.split('/')[2] + ': ' + type(exc).__name__)
    raise ValueError('Road lookup unavailable: ' + ', '.join(errors))


def nearest_way(point, travel_heading, elements):
    candidates = []
    for way in elements:
        if way.get('type') != 'way' or not isinstance(way.get('nodes'), list):
            continue
        geometry = way.get('geometry')
        if not isinstance(geometry, list) or len(geometry) != len(way['nodes']) or len(geometry) < 2:
            continue
        along = 0.0
        best = None
        for a, b in zip(geometry, geometry[1:]):
            try:
                pa, pb = [float(a['lon']), float(a['lat'])], [float(b['lon']), float(b['lat'])]
                length = meters(pa, pb)
                if length < 1:
                    continue
                scale = math.cos(math.radians(point[1]))
                x, y = (pb[0]-pa[0])*scale, pb[1]-pa[1]
                u, v = (point[0]-pa[0])*scale, point[1]-pa[1]
                t = max(0, min(1, (u*x+v*y)/(x*x+y*y)))
                projected = [pa[0]+(pb[0]-pa[0])*t, pa[1]+(pb[1]-pa[1])*t]
                gap = angle_gap(travel_heading, heading(pa, pb))
                reverse_gap = angle_gap(travel_heading, heading(pb, pa))
                if min(gap, reverse_gap) <= math.radians(25):
                    item = (meters(point, projected), along+length*t, 1 if gap < reverse_gap else -1,
                            min(gap, reverse_gap))
                    if best is None or item[0] < best[0]:
                        best = item
                along += length
            except (KeyError, TypeError, ValueError, ZeroDivisionError):
                continue
        if best is not None and best[0] <= 8:
            candidates.append((best[0], way, best[1], best[2], best[3]))
    candidates.sort(key=lambda item: item[0])
    if not candidates or (len(candidates) > 1 and candidates[1][0] <= candidates[0][0]+8):
        return None
    return candidates[0]


def stop_ahead(point, bearing_deg, elements):
    travel_heading = math.radians(90-bearing_deg)
    match = nearest_way(point, travel_heading, elements)
    if match is None:
        return None
    error, way, progress, direction, heading_error = match
    nodes, geometry = way['nodes'], way['geometry']
    node_positions = {}
    along = 0.0
    for i, (node_id, item) in enumerate(zip(nodes, geometry)):
        node_positions[node_id] = along
        if i+1 < len(geometry):
            try:
                along += meters([item['lon'], item['lat']],
                                [geometry[i+1]['lon'], geometry[i+1]['lat']])
            except (KeyError, TypeError, ValueError):
                return None
    best_stop = None
    for node in elements:
        if node.get('type') != 'node' or node.get('id') not in node_positions or node.get('tags', {}).get('highway') != 'stop':
            continue
        if not allowed_heading(node, way, travel_heading):
            continue
        ahead = (node_positions[node['id']]-progress)*direction
        if -8 <= ahead <= 250 and (best_stop is None or ahead < best_stop['distance_m']):
            best_stop = {'id': str(node['id']), 'distance_m': round(ahead, 1)}
    return {'road': str(way.get('tags', {}).get('name', 'current road'))[:80],
            'match_error_m': round(error, 1), 'heading_error_deg': round(math.degrees(heading_error), 1),
            'stop': best_stop}


class RoadStopLookup:
    def __init__(self):
        self.lock = threading.Lock()
        self.elements = []
        self.center = None
        self.fetched_at = 0.0
        self.last_request = -1e9
        self.fetching = False
        self.state = 'off'
        self.error = ''
        self.last_bytes = 0
        self.last_match = None
        self.write(None)

    def write(self, payload):
        now = time.monotonic()
        data = {'received_mono': now, 'expires_mono': now+2.5}
        if payload is not None:
            data.update(payload)
        name = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', dir=SNAPSHOT.parent, prefix='.nap-road-', delete=False) as stream:
                name = stream.name
                json.dump(data, stream, allow_nan=False)
            os.replace(name, SNAPSHOT)
        except (OSError, ValueError, TypeError):
            pass
        finally:
            if name and os.path.exists(name):
                os.unlink(name)

    def fetch(self, point):
        try:
            elements, size = fetch_roads(point)
            with self.lock:
                self.elements, self.center, self.fetched_at = elements, point, time.monotonic()
                self.state, self.error, self.last_bytes = 'ready', '', size
        except Exception as exc:
            with self.lock:
                self.state, self.error = 'unavailable', str(exc)[:120]
        finally:
            with self.lock:
                self.fetching = False

    def tick(self, fix, route_stop_covered):
        if route_stop_covered or not road_setting_enabled() or not experimental_enabled():
            with self.lock:
                self.state = ('off' if not road_setting_enabled() else
                              'route stop coverage available' if route_stop_covered else 'experimental off')
                self.last_match = None
            self.write(None)
            return
        now = time.monotonic()
        if (fix is None or now-fix['mono'] > 1.5 or fix.get('accuracy_m') is None
                or fix['accuracy_m'] > 8 or fix['speed'] < 3 or fix['speed'] > 25
                or fix.get('bearing_deg') is None):
            with self.lock:
                self.state = ('waiting for GPS accuracy' if fix and fix.get('accuracy_m') is None else
                              'waiting for GPS and motion')
                self.last_match = None
            self.write(None)
            return
        with self.lock:
            stale = self.center is None or meters(self.center, fix['point']) > 200 or now-self.fetched_at > 180
            if stale and not self.fetching and now-self.last_request >= 45:
                self.fetching, self.last_request = True, now
                self.state = 'loading'
                threading.Thread(target=self.fetch, args=(list(fix['point']),), daemon=True,
                                 name='nap-road-stop-lookup').start()
            match = None if stale else stop_ahead(fix['point'], fix['bearing_deg'], self.elements)
            self.last_match = match
            if stale and not self.fetching:
                self.state = 'waiting for lookup'
            if not stale and match is None:
                self.state = 'road uncertain'
        if match and match['stop']:
            self.write({'road': match['road'], 'mapped_stop_sign': match['stop'],
                        'position_quality': {'match_error_m': match['match_error_m'],
                                             'gps_accuracy_m': fix['accuracy_m'],
                                             'gps_age_s': round(now-fix['mono'], 2),
                                             'heading_error_deg': match['heading_error_deg']}})
        else:
            self.write(None)

    def status(self):
        with self.lock:
            return {'enabled': road_setting_enabled(), 'state': self.state, 'error': self.error,
                    'road': self.last_match['road'] if self.last_match else None,
                    'next_distance_m': self.last_match['stop']['distance_m'] if self.last_match and self.last_match['stop'] else None,
                    'last_response_bytes': self.last_bytes}
