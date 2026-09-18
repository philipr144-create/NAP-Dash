"""Conservative, route-direction-matched OpenStreetMap stop-sign hints."""
import json
import math
from urllib.parse import urlencode
from urllib.error import HTTPError
from urllib.request import Request, urlopen

OVERPASS = ('https://overpass-api.de/api/interpreter',
            'https://lz4.overpass-api.de/api/interpreter',
            'https://overpass.private.coffee/api/interpreter')
MAX_LOOKAHEAD_M = 5000.0


def meters(a, b):
    lat = math.radians(b[1]-a[1]); lon = math.radians(b[0]-a[0])
    h = math.sin(lat/2)**2 + math.cos(math.radians(a[1]))*math.cos(math.radians(b[1]))*math.sin(lon/2)**2
    return 12742000*math.asin(math.sqrt(min(1, max(0, h))))


def heading(a, b):
    x = (b[0]-a[0])*math.cos(math.radians((a[1]+b[1])/2))
    return math.atan2(b[1]-a[1], x)


def angle_gap(a, b):
    return abs((a-b+math.pi) % (2*math.pi)-math.pi)


def route_position(route, point, start, end):
    best = None
    for (a, aa), (b, ba) in zip(route.points, route.points[1:]):
        if ba < start-25 or aa > end+25 or ba-aa < .01:
            continue
        scale = math.cos(math.radians(point[1]))
        x, y = (b[0]-a[0])*scale, b[1]-a[1]
        u, v = (point[0]-a[0])*scale, point[1]-a[1]
        t = max(0, min(1, (u*x+v*y)/(x*x+y*y)))
        projected = [a[0]+(b[0]-a[0])*t, a[1]+(b[1]-a[1])*t]
        error = meters(point, projected)
        if best is None or error < best[0]:
            best = (error, aa+(ba-aa)*t, heading(a, b))
    return best


def route_samples(route, start, end):
    samples = []
    points = route.points
    for target in [start + i*120 for i in range(int((end-start)/120)+1)] + [end]:
        for (a, aa), (b, ba) in zip(points, points[1:]):
            if aa <= target <= ba and ba > aa:
                t = (target-aa)/(ba-aa)
                samples.append([a[0]+(b[0]-a[0])*t, a[1]+(b[1]-a[1])*t])
                break
    return samples[:50]


def way_heading(way, node_id):
    ids, geometry = way.get('nodes'), way.get('geometry')
    if not isinstance(ids, list) or not isinstance(geometry, list) or len(ids) != len(geometry):
        return None
    try:
        i = ids.index(node_id)
        a, b = geometry[max(0, i-1)], geometry[min(len(ids)-1, i+1)]
        if a is b:
            return None
        return heading([a['lon'], a['lat']], [b['lon'], b['lat']])
    except (ValueError, KeyError, TypeError, IndexError):
        return None


def allowed_heading(node, way, route_heading):
    bearing = way_heading(way, node['id'])
    if bearing is None:
        return False
    tags, way_tags = node.get('tags', {}), way.get('tags', {})
    direction = tags.get('stop:direction', tags.get('direction', ''))
    if direction == 'forward' or (not direction and way_tags.get('oneway') in ('yes', '1', 'true')):
        return angle_gap(route_heading, bearing) <= math.radians(35)
    if direction == 'backward' or (not direction and way_tags.get('oneway') == '-1'):
        return angle_gap(route_heading, bearing+math.pi) <= math.radians(35)
    if tags.get('stop') == 'all':
        return min(angle_gap(route_heading, bearing), angle_gap(route_heading, bearing+math.pi)) <= math.radians(35)
    # An unmarked stop on a two-way road may apply only to the other approach.
    return False


def match_stops(route, elements, start, end):
    nodes = {e['id']: e for e in elements if e.get('type') == 'node' and e.get('tags', {}).get('highway') == 'stop'
             and type(e.get('id')) is int and type(e.get('lon')) in (int, float) and type(e.get('lat')) in (int, float)}
    ways = [e for e in elements if e.get('type') == 'way' and isinstance(e.get('nodes'), list)]
    stops = []
    for node_id, node in nodes.items():
        point = [node['lon'], node['lat']]
        position = route_position(route, point, start, end)
        if position is None or position[0] > 8 or not start <= position[1] <= end:
            continue
        if any(node_id in way['nodes'] and allowed_heading(node, way, position[2]) for way in ways):
            stops.append({'id': str(node_id), 'at_m': round(position[1], 1)})
    return sorted(stops, key=lambda item: item['at_m'])[:100]


def fetch_stops(route, start, opener=urlopen):
    end = min(route.length, start+MAX_LOOKAHEAD_M)
    samples = route_samples(route, start, end)
    if len(samples) < 2:
        return [], end
    coords = ','.join(f'{p[1]:.6f},{p[0]:.6f}' for p in samples)
    query = f'[out:json][timeout:15];node["highway"="stop"](around:35,{coords})->.stops;(.stops;way(bn.stops)[highway];);out body geom;'
    failures = []
    for endpoint in OVERPASS:
        request = Request(endpoint, data=urlencode({'data': query}).encode(),
                          headers={'User-Agent': 'NAP-Mapped-Stops/0.1 personal-navigation', 'Accept': 'application/json'})
        try:
            with opener(request, timeout=12) as response:
                raw = response.read(1_000_001)
            if len(raw) > 1_000_000:
                raise ValueError('OpenStreetMap stop response too large')
            elements = json.loads(raw)['elements']
            if not isinstance(elements, list):
                raise ValueError('Invalid stop response')
            return match_stops(route, elements, start, end), end
        except HTTPError as exc:
            failures.append(endpoint.split('/')[2] + ': HTTP ' + str(exc.code))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            failures.append(endpoint.split('/')[2] + ': ' + type(exc).__name__)
    raise ValueError('OpenStreetMap stop lookup unavailable (' + ', '.join(failures) + ')')
