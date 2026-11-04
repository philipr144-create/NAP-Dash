"""Receive-only selected body signals; called by the existing telemetry worker."""
import time
import math

MAX_AGE = {0x318: 2.0, 0x45: 2.0, 0x283: 3.0}
FIELDS = {792: {'DOOR_STATE_FL': [12, 2, 1, 1.0, 0.0], 'DOOR_STATE_FR': [14, 2, 1, 1.0, 0.0], 'DOOR_STATE_RL': [22, 2, 1, 1.0, 0.0], 'DOOR_STATE_RR': [29, 2, 1, 1.0, 0.0], 'BOOT_STATE': [46, 2, 1, 1.0, 0.0], 'DOOR_STATE_FrontTrunk': [50, 2, 1, 1.0, 0.0], 'BC_headLightLStatus': [55, 2, 0, 1.0, 0.0], 'BC_headLightRStatus': [57, 2, 0, 1.0, 0.0], 'BC_indicatorLStatus': [59, 2, 0, 1.0, 0.0], 'BC_indicatorRStatus': [61, 2, 0, 1.0, 0.0]}, 69: {'HiBmLvr_Stat': [18, 2, 1, 1.0, 0.0], 'WprWashSw_Psd': [20, 2, 1, 1.0, 0.0], 'HrnSw_Psd': [30, 2, 1, 1.0, 0.0], 'WprSw6Posn': [48, 3, 1, 1.0, 0.0]}, 643: {'AirTemp_Insd': [47, 8, 0, 0.25, 0.0], 'AirTemp_Outsd': [63, 8, 0, 0.5, -40.0], 'LoBm_On_Rq': [32, 1, 0, 1.0, 0.0], 'HiBm_On': [33, 1, 0, 1.0, 0.0], 'Hrn_On': [26, 1, 0, 1.0, 0.0], 'LgtSens_Night': [22, 1, 0, 1.0, 0.0]}}


def raw_signal(data, start, size, little):
    if little:
        return (int.from_bytes(data, 'little') >> start) & ((1 << size) - 1)
    value = 0
    bit = start
    for _ in range(size):
        value = (value << 1) | ((data[bit // 8] >> (bit % 8)) & 1)
        bit = bit + 15 if bit % 8 == 0 else bit - 1
    return value


def ingest(cache, messages, now=None):
    now = time.monotonic() if now is None else now
    for frame in messages:
        if frame.src != 0 or frame.address not in FIELDS or len(frame.dat) != 8:
            continue
        previous = cache.get(str(frame.address))
        if previous is not None and previous['received'] > now:
            continue
        values = {}
        for name, (start, size, little, factor, offset) in FIELDS[frame.address].items():
            raw = raw_signal(frame.dat, start, size, little)
            if name.startswith('DOOR_STATE') or name == 'BOOT_STATE':
                value = {0: 'closed', 1: 'open'}.get(raw)
            elif name == 'WprSw6Posn':
                value = {0:'off',1:'intermittent 1',2:'intermittent 2',3:'intermittent 3',4:'intermittent 4',5:'stage 1',6:'stage 2'}.get(raw)
            elif name in ('HiBmLvr_Stat', 'WprWashSw_Psd', 'HrnSw_Psd'):
                labels = {'HiBmLvr_Stat': {0:'idle',1:'high beam switch pressed',2:'high beam flash pressed'},
                          'WprWashSw_Psd': {0:'not pressed',1:'tip wipe',2:'wash'},
                          'HrnSw_Psd': {0:'not pressed',1:'pressed'}}
                value = labels[name].get(raw)
            elif name.startswith('AirTemp'):
                invalid = raw == 255 if name == 'AirTemp_Insd' else raw >= 254
                value = None if invalid else raw * factor + offset
            else:
                value = raw * factor + offset
            values[name] = value
        cache[str(frame.address)] = {'received': now, 'signals': values}


def snapshot(cache, now=None):
    now = time.monotonic() if now is None else now
    messages = {}
    for address, item in cache.items():
        age = now - item['received']
        fresh = 0 <= age <= MAX_AGE[int(address)]
        messages[address] = {'available': fresh, 'age_seconds': round(age, 2),
                             'signals': item['signals'] if fresh else {}}
    return {'read_only': True, 'feed_version': 2, '_snapshot_time': now, 'available': any(x['available'] for x in messages.values()),
            'source': 'Received bus-zero CAN body messages; hardware meanings require validation. Headlight and indicator numeric values are raw status codes, not verified on/off. Wiper and lever values describe switch inputs. Low beam request is not lamp feedback.',
            'messages': messages,
            'reason': None if any(x['available'] for x in messages.values()) else 'No fresh body readings are arriving. The car may be asleep or the feed may be interrupted. Battery readings use a separate feed.',
            'vehicle_commands': {'available': False, 'reason': 'The assistant has no vehicle actuation tools. Telemetry availability does not enable controls.'}}


class BodyCanReceiver:
    """Drain CAN events without conflation in the existing telemetry thread.

    Keep source timestamps so queued packets cannot make stale readings fresh.
    Bound work per tick; no publishers, threads, USB access or settings writes.
    """
    def __init__(self, messaging, max_batches=256, budget_seconds=.008):
        self.messaging = messaging
        self.socket = messaging.sub_sock('can', conflate=False, timeout=0)
        self.max_batches = max_batches
        self.budget_seconds = budget_seconds

    def drain(self, cache):
        deadline = time.monotonic() + self.budget_seconds
        for _ in range(self.max_batches):
            if time.monotonic() >= deadline:
                break
            event = self.messaging.recv_one_or_none(self.socket)
            if event is None:
                break
            source_time = event.logMonoTime / 1e9
            age = time.monotonic() - source_time
            if math.isfinite(source_time) and 0 <= age <= .5:
                ingest(cache, event.can, now=source_time)


def body_view(value, now=None):
    """Re-age the snapshot at request time and withhold expired signals."""
    now = time.monotonic() if now is None else now
    value = dict(value or {})
    stamp = value.pop('_snapshot_time', None)
    if not isinstance(stamp, (int, float)) or not math.isfinite(stamp) or not 0 <= now - stamp <= 3:
        return {'read_only': True, 'feed_version': 2, 'available': False,
                'reason': 'The body-data feed is unavailable or stale. Battery readings use a separate feed.',
                'messages': {}, 'vehicle_commands': {'available': False, 'reason': 'Vehicle actuation tools are not installed.'}}
    elapsed = now - stamp
    messages = {}
    for address, message in value.get('messages', {}).items():
        if int(address) not in MAX_AGE:
            continue
        item = dict(message)
        age = item.get('age_seconds')
        age = age + elapsed if isinstance(age, (int, float)) else math.inf
        fresh = item.get('available') is True and math.isfinite(age) and 0 <= age <= MAX_AGE[int(address)]
        item.update(available=fresh, age_seconds=round(age, 2) if math.isfinite(age) else None,
                    signals=item.get('signals', {}) if fresh else {})
        messages[address] = item
    value['messages'] = messages
    value['available'] = any(m['available'] for m in messages.values())
    if not value['available']:
        value['reason'] = 'No fresh body readings are arriving. The car may be asleep or the feed may be interrupted. Battery readings use a separate feed.'
    return value
