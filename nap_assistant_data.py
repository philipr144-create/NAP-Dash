"""Bounded read-only assistant telemetry; source age is never HTTP response age."""
import copy
import math
import time


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def freshness(mono, limit, now=None, wall=None, valid=True):
    now = time.monotonic() if now is None else now
    wall = time.time() if wall is None else wall
    age = now - mono if finite(mono) and mono > 0 else None
    if age is None or age < 0:
        return {'status': 'unavailable', 'available': False, 'age_seconds': None, 'observed_at_unix_s': None}
    fresh = valid and age <= limit
    return {'status': 'fresh' if fresh else 'stale', 'available': bool(fresh),
            'age_seconds': round(age, 3), 'observed_at_unix_s': round(wall-age, 3) if wall > 1700000000 else None}


def mark_battery(state, fields, mono, address, bus):
    # One entry per supported signal, never a history. Ignore reflected TX and stale events.
    if bus not in (0, 1, 2) or not finite(mono) or not 0 <= time.monotonic()-mono <= .5:
        return
    stamps = state.setdefault('_assistant_battery_stamps', {})
    for field in fields:
        previous = stamps.get(field, {})
        if mono >= previous.get('mono', 0):
            stamps[field] = {'mono': mono, 'address': address, 'bus': bus}


def telemetry_view(state, now=None, wall=None):
    now = time.monotonic() if now is None else now
    wall = time.time() if wall is None else wall
    result = {k: copy.deepcopy(state.get(k, {})) for k in ('car','drive','control_modes','bms','settings','health','navigation')}
    result['ts'] = wall
    feeds = state.get('_assistant_sources', {}).get('feeds', {})
    def feed(name, limit=2):
        info = feeds.get(name, {})
        return freshness(info.get('mono'), limit, now, wall, info.get('valid') is True and info.get('alive') is True)
    status = {'vehicle': feed('carState'), 'engagement': feed('selfdriveState'), 'control_modes': feed('carControl'), 'system': feed('deviceState', 5),
              'settings': freshness(state.get('_assistant_settings_mono'), 5, now, wall),
              'navigation': freshness(state.get('navigation', {}).get('received_mono'), 5, now, wall)}
    for key, group in [('car','vehicle'),('drive','engagement'),('control_modes','control_modes'),('health','system'),('settings','settings'),('navigation','navigation')]:
        if not status[group]['available']:
            result[key] = {}
    stamps = state.get('_assistant_battery_stamps', {})
    bms = result['bms']; battery = {}
    def reading(field):
        meta = stamps.get(field, {})
        out = freshness(meta.get('mono'), 5 if field in ('pack_v','pack_i') else 30, now, wall)
        return out
    for field in list(bms):
        if field in ('bricks','temps_dict','min_v','max_v','min_t','max_t'):
            continue
        battery[field] = reading(field)
        if not battery[field]['available']:
            bms[field] = None
    bms['bricks'] = [value if reading('brick_'+str(i))['available'] else None for i,value in enumerate(bms.get('bricks', []))]
    bms['temps_dict'] = {key:value for key,value in bms.get('temps_dict', {}).items() if reading('temp_'+key)['available']}
    # Aggregated extrema from the old cache cannot establish cell-level freshness.
    for field in ('min_v','max_v','min_t','max_t'):
        bms.pop(field, None)
    for field in stamps:
        if field.startswith(('brick_','temp_')):
            battery[field] = reading(field)
    status['battery'] = battery
    result['freshness'] = status
    result['read_only'] = True
    v, current = bms.get('pack_v'), bms.get('pack_i')
    power = {'available': False, 'direction': 'unknown', 'magnitude_kW': None, 'signed_kW_positive_discharge': None,
             'reason': 'Fresh, plausible pack voltage and current are required.'}
    if finite(v) and 100 <= v <= 500 and finite(current) and abs(current) <= 2000:
        signed = -v*current/1000
        direction = 'near_zero' if abs(signed) < .5 else 'out_of_pack' if signed > 0 else 'into_pack'
        power.update(available=True, direction=direction, magnitude_kW=round(abs(signed),1), signed_kW_positive_discharge=round(signed,1), reason=None,
                     description='Net battery power; pack inflow alone does not distinguish external charging from regeneration or measure charger input.',
                     freshness=battery['pack_i'])
    result['battery_power'] = power
    return result
