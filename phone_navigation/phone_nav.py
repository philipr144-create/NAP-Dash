"""Paired navigation remote; route tracking and GPS run on the comma."""
import hmac
import json
import os
import threading
import time
from pathlib import Path
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen
from urllib.error import URLError

from .desire_bridge import DesireBridge
from .route_tracking import NavigationTracker, coordinate
from . import pairing, turn_settings

ROOT = Path(__file__).resolve().parent
PRESETS_PATH = Path('/data/nap_destination_presets.json')


def read_presets():
    try:
        data = json.loads(PRESETS_PATH.read_text())
        return data.get('presets', []) if isinstance(data, dict) and isinstance(data.get('presets'), list) else []
    except (OSError, ValueError):
        return []


def validated_preset(data):
    name = data.get('name')
    destination = data.get('destination')
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 48 or not isinstance(destination, dict):
        raise ValueError('Choose a destination and enter a short name')
    label = destination.get('label')
    if not isinstance(label, str) or not 1 <= len(label.strip()) <= 240:
        raise ValueError('Invalid destination label')
    location = coordinate(destination.get('location'))
    return {'name': name.strip(), 'label': label.strip(), 'location': location}


def write_presets(presets):
    temporary = PRESETS_PATH.with_suffix('.tmp')
    temporary.write_text(json.dumps({'version': 1, 'presets': presets}))
    os.replace(temporary, PRESETS_PATH)


class PhoneNavigation:
    def __init__(self, update=None, clear=None, token=None, bridge=None, location=None):
        self.token = token or os.environ.get('NAP_PHONE_TOKEN', '')
        if not self.token and (ROOT / '.pairing-key').is_file():
            self.token = (ROOT / '.pairing-key').read_text().strip()
        self.lock = threading.Lock()
        self.provider_lock = threading.Lock()
        self.last_request = 0.0
        self.cache = {}
        self.photon = os.environ.get('NAP_PHONE_SEARCH_URL', 'https://photon.komoot.io/api/')
        self.router = os.environ.get('NAP_PHONE_ROUTE_URL', 'https://routing.openstreetmap.de/routed-car/route/v1/driving/')
        self.bridge = bridge if bridge is not None else DesireBridge()
        self.tracker = NavigationTracker(self.route_request, self.bridge, update, clear, location)

    def start(self):
        self.tracker.start_worker()

    def provider(self, url):
        with self.provider_lock:
            now = time.monotonic()
            if url in self.cache and now-self.cache[url][0] < 300:
                return self.cache[url][1]
            pause = 1.1-(now-self.last_request)
            if pause > 0:
                time.sleep(pause)
            self.last_request = time.monotonic()
            req = Request(url, headers={'User-Agent': 'NAP-Phone-Guidance/0.2 personal-prototype', 'Accept': 'application/json'})
            try:
                with urlopen(req, timeout=12) as response:
                    raw = response.read(4_000_001)
            except (URLError, TimeoutError, OSError) as exc:
                raise ValueError('Online search/routing is unavailable. Check the comma cellular or hotspot connection and try again.') from exc
            if len(raw) > 4_000_000:
                raise ValueError('Route too large; choose a closer destination')
            result = json.loads(raw)
            if len(self.cache) >= 32:
                self.cache.pop(next(iter(self.cache)))
            self.cache[url] = (time.monotonic(), result)
            return result

    def route_request(self, origin, destination):
        coords = ';'.join(','.join(str(n) for n in coordinate(p)) for p in (origin, destination))
        raw = self.provider(self.router.rstrip('/')+'/'+coords+'?steps=true&overview=false&geometries=geojson')
        if raw.get('code') != 'Ok' or not raw.get('routes'):
            raise ValueError('No driving route found')
        return raw['routes'][0]

    def action(self, path, data):
        if path == '/phone-nav/api/presets':
            with self.lock:
                return {'presets': read_presets()}
        if path == '/phone-nav/api/preset-save':
            preset = validated_preset(data)
            with self.lock:
                presets = [p for p in read_presets() if p.get('name', '').casefold() != preset['name'].casefold()]
                if len(presets) >= 6:
                    raise ValueError('Remove a comma preset first (six maximum)')
                presets.append(preset)
                write_presets(presets)
                return {'presets': presets}
        if path == '/phone-nav/api/preset-remove':
            name = data.get('name')
            if not isinstance(name, str):
                raise ValueError('Choose a preset to remove')
            with self.lock:
                presets = [p for p in read_presets() if p.get('name') != name]
                write_presets(presets)
                return {'presets': presets}
        if path == '/phone-nav/api/turn-settings':
            return turn_settings.read()
        if path == '/phone-nav/api/turn-setting':
            return turn_settings.write(data)
        if path in ('/phone-nav/api/status', '/phone-nav/api/influence-status'):
            return self.tracker.status()
        if path == '/phone-nav/api/influence':
            return self.tracker.set_enabled(data.get('enabled'))
        if path == '/phone-nav/api/start':
            destination = data.get('destination')
            if not isinstance(destination, dict):
                raise ValueError('Choose a destination from the search results first')
            return self.tracker.start(destination)
        if path == '/phone-nav/api/stop':
            return self.tracker.stop()
        if path != '/phone-nav/api/search':
            raise ValueError('Reload the navigation page. Navigation now runs on the comma.')
        query = str(data.get('query', '')).strip()
        if not 3 <= len(query) <= 180:
            raise ValueError('Enter a place or full address, including the city')
        params = {'q': query, 'limit': 5, 'lang': 'en'}
        with self.tracker.lock:
            fix = self.tracker.fix
            if fix and 0 <= time.monotonic()-fix['mono'] <= 3:
                params.update(lon=fix['point'][0], lat=fix['point'][1])
        raw = self.provider(self.photon+'?'+urlencode(params))
        places = []
        for feature in raw.get('features', []):
            try:
                p = feature['properties']
                label = ', '.join(str(p[k]) for k in ('name', 'housenumber', 'street', 'city', 'state', 'country') if p.get(k))
                places.append({'label': label[:240], 'location': coordinate(feature['geometry']['coordinates'])})
            except (KeyError, TypeError, ValueError):
                continue
        return {'places': places}

    def handle(self, handler):
        path = urlparse(handler.path).path
        if not (path == '/phone-nav' or path.startswith('/phone-nav/')):
            return False

        def reply(value, code=200, mime='application/json'):
            body = value if isinstance(value, bytes) else json.dumps(value).encode()
            handler.send_response(code)
            handler.send_header('Content-Type', mime)
            handler.send_header('Content-Length', str(len(body)))
            handler.send_header('Cache-Control', 'no-store')
            handler.send_header('X-Content-Type-Options', 'nosniff')
            handler.send_header('Referrer-Policy', 'no-referrer')
            handler.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'self'; base-uri 'none'; form-action 'self'")
            handler.end_headers()
            handler.wfile.write(body)

        if handler.command == 'GET':
            files = {'/phone-nav': ('index.html', 'text/html'), '/phone-nav/': ('index.html', 'text/html'),
                     '/phone-nav/app.js': ('app.js', 'text/javascript'), '/phone-nav/style.css': ('style.css', 'text/css')}
            if path in files:
                filename, mime = files[path]
                reply((ROOT/'static'/filename).read_bytes(), mime=mime+'; charset=utf-8')
            else:
                reply({'error': 'Not found. Reload the navigation page.'}, 404)
            return True
        if handler.command != 'POST':
            reply({'error': 'Method not allowed'}, 405)
            return True
        if path != '/phone-nav/api/pair' and (not self.token or not hmac.compare_digest(handler.headers.get('X-NAP-Phone-Key', ''), self.token)):
            reply({'error': 'Connect this browser first using the six-digit navigation setup code.'}, 401)
            return True
        try:
            size = int(handler.headers.get('Content-Length', '0'))
            if not 0 < size <= 16384:
                raise ValueError('Invalid request size')
            data = json.loads(handler.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError('JSON object required')
            if path == '/phone-nav/api/pair':
                with self.lock:
                    if not self.token:
                        raise ValueError('Navigation pairing has not been configured on this comma')
                    pairing.redeem(data.get('code', ''))
                    result = {'ok': True, 'key': self.token}
            else:
                result = self.action(path, data)
            reply(result)
        except (ValueError, TypeError, KeyError) as exc:
            reply({'error': str(exc)}, 400)
        except Exception:
            reply({'error': 'Navigation service unavailable. Check the comma connection and try again.'}, 503)
        return True
