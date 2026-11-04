"""Publish bounded, read-only MCU map hints for the existing MCP catalog."""
import json
import os
import time
from pathlib import Path

OUTPUT = Path('/dev/shm/nap_mcu_route.json')
MAX_AGE = 2.0
CAN_INDEX = Path('/dev/shm/nap_can_address_index.json')
CAN_PAYLOAD_ADDRESSES = {0x2F8, 0x3C8, 0x3D8}


def observe_address(index, frame, stamp):
    if not (0 <= frame.src < 4 and 0 <= frame.address <= 0x7FF):
        return
    key = (frame.address, frame.src)
    previous = index.get(key)
    sample = {'frame_mono': stamp, 'length': len(frame.dat),
              'count': previous['count'] + 1 if previous else 1}
    if frame.address in CAN_PAYLOAD_ADDRESSES:
        sample['payload_hex'] = frame.dat.hex()
    index[key] = sample


def can_snapshot(index, now, boot_id):
    addresses = {}
    recent = sorted(index.items(), key=lambda entry: entry[1]['frame_mono'], reverse=True)[:512]
    for (address, bus), sample in recent:
        age = now - sample['frame_mono']
        if 0 <= age <= MAX_AGE:
            buses = addresses.setdefault(f'0x{address:03X}', {})
            buses[str(bus)] = dict(sample, age_at_publish_s=round(age, 3))
    return dict(boot_id=boot_id, producer_mono=now, max_frame_age_s=MAX_AGE,
                addresses=addresses,
                note='Up to 512 recently observed address/bus pairs on receive buses 0–3. Payloads limited to known GPS/map addresses.')

GPS_OUTPUT = Path('/dev/shm/tesla_gps_assist.json')
GPS_TOGGLE = Path('/data/nap_tesla_gps_assist')


def signed_coordinate(raw):
    return (raw - (1 << 28) if raw & (1 << 27) else raw) / 1e6


def gps_fields(address, data):
    if address == 984:
        packed = int.from_bytes(data[:7], 'little')
        return 'position', {'point': [signed_coordinate(packed >> 28),
                                      signed_coordinate(packed & ((1 << 28) - 1))]}
    return 'motion', {'hdop': data[0] / 10,
                      'heading_deg': int.from_bytes(data[1:3], 'little') / 128,
                      'speed_mps': int.from_bytes(data[3:5], 'little') / (256 * 3.6),
                      'nmea_missing': bool(data[6] & 32)}


def decode(payload):
    return {
        'route_active': bool(payload[3] & 32),
        'gps_road_match': bool(payload[3] & 16),
        'branch_distance_raw': payload[4] & 31,
        'branch_distance_m': (payload[4] & 31) * 10,
        'controlled_access': bool(payload[4] & 32),
        'left_off_ramp': bool(payload[4] & 64),
        'right_off_ramp': bool(payload[4] & 128),
    }

def frames(message):
    for frame in message.can:
        if frame.src < 4 and frame.address == 968 and len(frame.dat) == 8:
            yield frame.src, decode(frame.dat)

def main():
    from cereal import messaging

    socket = messaging.sub_sock('can', conflate=False, timeout=100)
    boot_id = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    sources = {}
    gps = {}
    index = {}
    gps_enabled = False
    publish_at = 0
    while True:
        message = messaging.recv_one(socket)
        if message is not None and message.valid:
            stamp = message.logMonoTime / 1e9
            for bus, fields in frames(message):
                sources[bus] = dict(fields, frame_mono=stamp)
            for frame in message.can:
                observe_address(index, frame, stamp)
                if gps_enabled:
                    if frame.src == 0 and frame.address in (760, 984) and len(frame.dat) == 8:
                        kind, values = gps_fields(frame.address, frame.dat)
                        gps[kind] = dict(values, mono=stamp)
        now = time.monotonic()
        if now < publish_at:
            continue
        buses = {}
        for bus, sample in sources.items():
            age = now - sample['frame_mono']
            fresh = 0 <= age <= MAX_AGE
            buses[bus] = dict(status='fresh' if fresh else 'stale',
                              age_at_publish_s=round(age, 3),
                              frame_mono=sample['frame_mono'],
                              fields={k:v for k,v in sample.items() if k != 'frame_mono'} if fresh else None)
        snapshot = dict(boot_id=boot_id, producer_mono=now, max_frame_age_s=MAX_AGE,
                        status='observed' if buses else 'no_frames_seen',
                        can_address='0x3C8', buses=buses,
                        note='Freshness describes CAN delivery. Branch values and route state require validation against MCU guidance.')
        temporary = OUTPUT.with_suffix('.tmp')
        temporary.write_text(json.dumps(snapshot))
        temporary.replace(OUTPUT)
        gps_enabled = GPS_TOGGLE.is_file()
        if gps_enabled:
            temporary = GPS_OUTPUT.with_suffix('.tmp')
            temporary.write_text(json.dumps(dict(gps, boot_id=boot_id)))
            temporary.replace(GPS_OUTPUT)
        else:
            gps.clear()
            GPS_OUTPUT.unlink(missing_ok=True)
        temporary = CAN_INDEX.with_suffix('.tmp')
        temporary.write_text(json.dumps(can_snapshot(index, now, boot_id), separators=(',', ':')))
        temporary.replace(CAN_INDEX)
        publish_at = now + .5

if __name__ == '__main__':
    main()
