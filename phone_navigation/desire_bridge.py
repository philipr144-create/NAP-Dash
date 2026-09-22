"""Single server-owned switch and atomic, short-lived model guidance snapshot."""
import json
import os
import tempfile
import time
from pathlib import Path


class DesireBridge:
    def __init__(self, path='/dev/shm/nap_navigation_desire.json'):
        self.path = Path(path)
        self.enabled = False
        self.route_active = False
        self.last_error = None
        self.clear('Navigation server restarted; influence defaults off')

    def write(self, state):
        name = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', dir=self.path.parent,
                                             prefix='.nap-nav-', delete=False) as stream:
                name = stream.name
                json.dump(state, stream, allow_nan=False)
            os.replace(name, self.path)
            self.last_error = None
        except OSError as exc:
            self.last_error = str(exc)
            raise
        finally:
            if name and os.path.exists(name):
                os.unlink(name)

    def inactive(self, enabled, reason):
        now = time.monotonic()
        return {'enabled': enabled, 'route_active': self.route_active, 'route_state': 'inactive',
                'received_mono': now, 'expires_mono': now+3,
                'pause_reason': reason}

    def clear(self, reason='Waiting for fresh active route guidance'):
        # Withdraw the instruction without silently changing the master switch.
        self.write(self.inactive(self.enabled, reason))

    def set_enabled(self, enabled):
        if type(enabled) is not bool:
            raise ValueError('enabled must be a boolean')
        # Publish the new switch state first; a failed write must not report success.
        self.write(self.inactive(enabled, 'Waiting for fresh active route guidance' if enabled else 'Navigation influence is off'))
        self.enabled = enabled

    def status(self):
        result = {'requested_enabled': self.enabled, 'enabled': False,
                  'state': 'error', 'reason': 'Navigation bridge unavailable'}
        try:
            with self.path.open() as stream:
                raw = stream.read(2049)
            if len(raw) > 2048:
                raise ValueError('Oversized navigation bridge')
            data = json.loads(raw)
            if type(data.get('enabled')) is not bool or data['enabled'] != self.enabled:
                raise ValueError('Switch and model snapshot disagree; reapply the switch')
            if self.last_error:
                raise ValueError('Navigation bridge write failed: '+self.last_error)
            result['enabled'] = data['enabled']
            if not data['enabled']:
                result.update(state='off', reason=data.get('pause_reason', 'Navigation influence is off'))
            elif data.get('pause_reason'):
                result.update(state='paused', reason=data['pause_reason'])
            elif not data['received_mono'] <= time.monotonic() < data['expires_mono']:
                result.update(state='paused', reason='Guidance expired; waiting for a fresh GPS/route update')
            elif data.get('route_state') != 'active':
                result.update(state='paused', reason='No active route instruction')
            else:
                result.update(state='ready', reason='Fresh guidance published; model gates still apply')
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            result.update(enabled=False, state='error', reason=str(exc))
        return result

    def publish(self, nav, age_ms):
        now = time.monotonic()
        context = nav.get('route_context', {})
        self.write({'version': 4, 'enabled': self.enabled, 'route_active': self.route_active, 'received_mono': now,
                    'expires_mono': now + max(0, min(3.0, 5.0 - age_ms / 1000)),
                    'route_id': nav['route_id'], 'route_state': nav['route_state'],
                    'maneuver_id': nav.get('maneuver_id', ''),
                    'maneuver': nav['maneuver'],
                    'mapped_stop_sign': nav.get('mapped_stop_sign'),
                    'map_lookup': {key: context.get(key) for key in
                                   ('map_lookup_state', 'map_loaded_ahead', 'map_coverage_remaining_m')},
                    'position_quality': {key: context.get(key) for key in
                                         ('match_error_m', 'gps_accuracy_m', 'gps_age_s',
                                          'heading_error_deg', 'path_ambiguous', 'projection_valid')}})
