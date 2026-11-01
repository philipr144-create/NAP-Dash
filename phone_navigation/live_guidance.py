"""Render motion-assisted navigation for people; never publish control IPC."""
import math


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def render(route, route_id, destination, observation, completion, now):
    if route is None or not destination or not isinstance(observation, dict) or not finite(now):
        return None
    match = observation.get('route', {})
    if not (observation.get('mode') == 'live_guidance'
            and observation.get('fresh') is True and observation.get('valid') is True
            and observation.get('used_for_control') is False and match.get('state') == 'matched'
            and all(finite(observation.get(k)) for k in ('gps_sensor_age_s', 'error_budget_m', 'heading_deg'))
            and 0 <= observation['gps_sensor_age_s'] <= 6 and 0 < observation['error_budget_m'] <= 40
            and all(finite(match.get(k)) for k in ('progress_m', 'cross_track_m', 'heading_error_deg'))
            and 0 <= match['progress_m'] <= route.length
            and 0 <= match['cross_track_m'] <= 25 and 0 <= match['heading_error_deg'] <= 35):
        return None
    progress = match['progress_m']
    selected = completion.index(route, route_id, progress, progress, observation['heading_deg'],
                                match['heading_error_deg'], True)
    nav = route.guidance(progress, destination['label'], route_id, selected)
    nav['source'] = 'comma-motion-estimate'
    nav['position_estimate'] = {'mode': 'live_guidance', 'used_for_control': False,
                                'gps_sensor_age_s': observation['gps_sensor_age_s'],
                                'error_budget_m': observation['error_budget_m'], 'received_mono': now,
                                'point': observation.get('point')}
    # Even an accidental future copy into the bridge cannot satisfy its v4
    # projection gate. The existing raw-GPS control payload is separate.
    nav['route_context'].update(tracking='GPS + vehicle motion', projection_valid=False,
                                used_for_control=False)
    if nav['distance_remaining_m'] < 15 and match['cross_track_m'] < 20:
        nav['route_state'] = 'arrived'
    return nav
