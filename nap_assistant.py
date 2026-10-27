"""Read-only assistant view. No subscriptions, workers, credentials or writes."""
import json
import math
import time
from pathlib import Path
from server.nap_body_telemetry import body_view


def snapshot(state, now=None):
    now = time.monotonic() if now is None else now
    meta = state.get('_assistant_sources', {})
    result = {'read_only': True, 'lead': {'available': False, 'reason': 'Radar data unavailable or stale'}}
    received = meta.get('radar_received', 0)
    age = now - received
    if not received or not 0 <= age <= 2 or not meta.get('radar_valid') or not meta.get('radar_alive'):
        return result
    lead = state.get('lead1', {})
    if lead.get('status') is not True:
        result['lead']['reason'] = 'No lead vehicle detected'
        return result
    distance = lead.get('dRel')
    if isinstance(distance, bool) or not isinstance(distance, (int, float)) or not math.isfinite(distance) or distance < 0:
        return result
    result['lead'] = {'available': True, 'distance_m': round(distance, 1),
                      'distance_ft': round(distance * 3.28084, 1), 'age_seconds': round(age, 2),
                      'description': 'Estimated longitudinal distance to the detected lead vehicle; not a safety guarantee'}
    return result


def handle(handler, path, state, lock):
    if path in ('/assistant', '/assistant/'):
        handler.send_bytes(Path(__file__).with_name('nap_assistant.html').read_bytes(), 'text/html; charset=utf-8')
        return True
    if path == '/api/assistant/body':
        with lock:
            value = dict(state.get('_assistant_body', {}))
        value = body_view(value)
        handler.send_json(value)
        return True
    if path == '/api/assistant/grade':
        try:
            data = json.loads(Path('/dev/shm/nap_predictive_grade_status.json').read_text())
            age = time.monotonic() - data['time']
            keys = ('controller_version', 'preview_trigger_deg', 'confirmation_seconds', 'assist_enabled', 'reason', 'blocks', 'pitch_rise_deg', 'climb_active', 'proposed_add', 'applied_add', 'speed_mps', 'target_mps')
            value = {'read_only': True, 'available': 0 <= age <= 3, 'age_seconds': round(age, 2)}
            if value['available']:
                value['diagnostics'] = {k: data[k] for k in keys if k in data}
        except (OSError, ValueError, KeyError, TypeError):
            value = {'read_only': True, 'available': False, 'reason': 'Predictive hill diagnostics unavailable.'}
        handler.send_json(value)
        return True
    if path == '/api/assistant/state':
        with lock:
            value = snapshot(state)
        handler.send_json(value)
        return True
    return False
