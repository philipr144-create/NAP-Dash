"""Fixed display preference API. No CAN publisher or driving parameter access."""
import hmac
import json
from urllib.parse import urlparse


def handle(handler, pair_token, params, lock):
    if urlparse(handler.path).path != '/api/assistant/preferences':
        return False

    def reply(value, code=200):
        handler.send_json(value, code)
        return True

    if not pair_token or not hmac.compare_digest(handler.headers.get('X-NAP-Phone-Key', ''), pair_token):
        return reply({'error': 'Pair this browser first.'}, 401)
    origin = handler.headers.get('Origin')
    if origin and urlparse(origin).netloc != handler.headers.get('Host'):
        return reply({'error': 'Same origin required.'}, 403)
    if handler.headers.get('X-NAP-Assistant') != '1' or handler.headers.get('Content-Type', '').split(';')[0] != 'application/json':
        return reply({'error': 'Assistant JSON request required.'}, 415)
    try:
        size = int(handler.headers.get('Content-Length', '0'))
        if not 0 < size <= 256:
            return reply({'error': 'Invalid request size.'}, 400)
        body = json.loads(handler.rfile.read(size))
        if body != {} and (not isinstance(body, dict) or set(body) != {'display_units'} or body['display_units'] not in ('metric', 'imperial')):
            return reply({'error': 'Only metric or imperial display units are supported.'}, 400)
        with lock:
            if body:
                params.put_bool('IsMetric', body['display_units'] == 'metric')
            units = 'metric' if params.get_bool('IsMetric') else 'imperial'
        if body and units != body['display_units']:
            return reply({'error': 'Preference readback failed.'}, 503)
        return reply({'success': True, 'changed': bool(body), 'display_units': units})
    except (ValueError, TypeError, OSError):
        return reply({'error': 'Preference request unavailable.'}, 400)
