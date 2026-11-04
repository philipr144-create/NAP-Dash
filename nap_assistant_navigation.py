"""Fixed assistant actions over the existing paired navigation backend. No CAN access."""
import hmac
import json
import secrets
import threading
import time
from urllib.parse import urlparse


def summary(raw):
    nav = raw.get('navigation') or {}
    bridge = raw.get('bridge') or {}
    destination = raw.get('destination') or {}
    maneuver = nav.get('maneuver') or {}
    return {'success': raw.get('ok') is True, 'route_active': raw.get('active') is True,
            'destination_label': destination.get('label'), 'gps_ready': raw.get('gps_ready') is True,
            'navigation_influence_enabled': raw.get('enabled') is True,
            'guidance_state': bridge.get('state', 'unavailable'),
            'guidance_reason': bridge.get('reason', ''), 'route_state': nav.get('route_state', 'inactive'),
            'message': raw.get('message', ''),
            'navigation': {k:nav[k] for k in ('distance_remaining_m','time_remaining_s','eta_unix_ms') if k in nav},
            'maneuver': {k:maneuver[k] for k in ('type','modifier','distance_m','primary_text') if k in maneuver},
            'checked_at_unix_s': time.time(),
            'scope': 'Comma navigation. Guidance remains subject to GPS, route and existing model/control gates.'}


class AssistantNavigation:
    def __init__(self, backend, clock=time.monotonic):
        self.backend, self.clock = backend, clock
        self.lock = threading.Lock()
        self.candidates = {}

    def prune(self):
        now = self.clock()
        self.candidates = {k:v for k,v in self.candidates.items() if 0 <= now-v['created'] <= 300}
        while len(self.candidates) > 20:
            self.candidates.pop(next(iter(self.candidates)))

    def action(self, data):
        if not isinstance(data, dict):
            raise ValueError('A navigation request object is required')
        action = data.get('action')
        if action == 'search':
            if set(data) != {'action','query'} or not isinstance(data['query'], str) or not 3 <= len(data['query'].strip()) <= 180:
                raise ValueError('Search needs a place or full address, including the city')
            raw = self.backend.action('/phone-nav/api/search', {'query':data['query'].strip()})
            places = []
            with self.lock:
                self.prune()
                for place in raw.get('places', [])[:5]:
                    candidate = secrets.token_urlsafe(18)
                    self.candidates[candidate] = {'created':self.clock(), 'destination':place, 'started':False}
                    places.append({'candidate_id':candidate, 'label':place['label']})
                self.prune()
            return {'success':True, 'places':places, 'expires_in_seconds':300,
                    'message':'Choose the intended result. Search alone does not change the route.'}
        if action == 'status' and set(data) == {'action'}:
            return summary(self.backend.action('/phone-nav/api/status', {}))
        if action == 'start':
            if set(data) != {'action','candidate_id'} or not isinstance(data['candidate_id'], str) or not 1 <= len(data['candidate_id']) <= 64:
                raise ValueError('Start needs a candidate returned by a recent search')
            with self.lock:
                self.prune()
                chosen = self.candidates.get(data['candidate_id'])
                if chosen is None:
                    raise ValueError('Destination expired or was not returned by search. Search again.')
                if chosen['started']:
                    result=summary(self.backend.action('/phone-nav/api/status', {}))
                    still_active = result['route_active'] and result['destination_label'] == chosen['destination']['label']
                    result.update(success=still_active, route_request_accepted=still_active, duplicate_request=True, requested_destination_label=chosen['destination']['label'])
                    if not still_active:
                        result['message']='That destination request was already used and is no longer active. Search again to start a new route.'
                    return result
                result = summary(self.backend.action('/phone-nav/api/start', {'destination':chosen['destination']}))
                if result['success']:
                    chosen['started'] = True
                result['route_request_accepted'] = result['success']
                # Starting a destination never calls the influence endpoint.
                return result
        if action == 'stop' and set(data) == {'action'}:
            return summary(self.backend.action('/phone-nav/api/stop', {}))
        if action == 'influence' and set(data) == {'action','enabled'} and type(data['enabled']) is bool:
            result = summary(self.backend.action('/phone-nav/api/influence', {'enabled':data['enabled']}))
            if result['navigation_influence_enabled'] is not data['enabled']:
                result.update(success=False, message='Navigation influence readback did not match the request. No success is confirmed.')
            return result
        raise ValueError('Unsupported navigation action or arguments')

    def handle(self, handler):
        if urlparse(handler.path).path != '/api/assistant/navigation':
            return False
        def reply(value, status=200):
            handler.send_json(value,status)
            return True
        if handler.command != 'POST':
            return reply({'error':'POST required'},405)
        token=self.backend.token
        if not token or not hmac.compare_digest(handler.headers.get('X-NAP-Phone-Key',''),token):
            return reply({'error':'Pair this browser using the existing Phone Navigation code first.'},401)
        origin=handler.headers.get('Origin')
        if origin and urlparse(origin).netloc != handler.headers.get('Host'):
            return reply({'error':'Same origin required'},403)
        if handler.headers.get('X-NAP-Assistant')!='1' or handler.headers.get('Content-Type','').split(';')[0]!='application/json':
            return reply({'error':'Assistant JSON request required'},415)
        try:
            size=int(handler.headers.get('Content-Length','0'))
            if not 0<size<=1024:
                raise ValueError('Invalid navigation request size')
            return reply(self.action(json.loads(handler.rfile.read(size))))
        except (ValueError,TypeError,KeyError) as exc:
            return reply({'error':str(exc)},400)
        except Exception:
            return reply({'error':'Navigation backend unavailable. No success is confirmed.'},503)
