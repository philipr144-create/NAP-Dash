"""Continental EAS hold controller. Transport is card's existing sendcan publisher.
No Panda mode changes, second publisher, arbitrary UDS, or inflation commands.
"""
import json
import math
import os
import socket
import tempfile
import time
from collections import deque

SOCKET = '/dev/shm/nap-airride.sock'
RECOVERY = '/data/nap-airride-recovery.json'
LIMIT = 40 * 0.44704
GROUPS = {'front': (0, 1), 'rear': (2, 3), 'pancake': (0, 1, 2, 3),
          'fl': (0,), 'fr': (1,), 'rl': (2,), 'rr': (3,)}
STOP = bytes.fromhex('31 02 03 34')
RESTORE = [STOP, bytes.fromhex('31 01 03 09'), bytes.fromhex('31 02 03 09'), bytes.fromhex('31 03 03 09'), bytes.fromhex('10 01')]
SETUP = [bytes.fromhex('10 03'), bytes.fromhex('31 01 03 10'), bytes.fromhex('31 02 03 10')]


def frame(payload):
    return (0x64B, (bytes([len(payload)]) + payload).ljust(8, b'\x00'), 0)


class Controller:
    def __init__(self):
        self.hold = None
        self.token = None
        self.closed = deque(maxlen=256)
        self.deadline = 0
        self.queue = deque()
        self.pending = None
        self.sent_at = 0
        self.send_after = 0
        self.next_pulse = 0
        self.corner = 0
        self.manual = False
        self.releasing = False
        self.stopping = False
        self.service_ready = False
        self.vent_stopped = False
        self.fault = ''
        self.reason = 'Ready'
        self.speed = None
        self.speed_at = -math.inf
        self.restored = None
        self.last_request = None
        self.last_response = None
        self.trace = deque(maxlen=32)

    def permitted(self, now):
        return self.speed is not None and now - self.speed_at < .25 and self.speed < LIMIT

    def stop_vent(self, reason):
        if self.token:
            self.closed.append(self.token)
        self.token = self.hold = None
        self.reason = reason
        if self.manual and not self.releasing:
            self.pending = None
            self.send_after = 0
            self.queue = deque([STOP])
            self.stopping = True

    def release(self, reason):
        if self.token:
            self.closed.append(self.token)
        self.token = self.hold = None
        self.reason = reason
        if self.manual and not self.releasing:
            self.pending = None
            self.send_after = 0
            self.queue = deque(RESTORE[1:] if self.vent_stopped else RESTORE)
            self.releasing = True
            self.stopping = False

    def request(self, d, now):
        action = d.get('action')
        if action == 'status':
            return self.status(now)
        if action == 'reset':
            if not self.reset_available(now):
                raise ValueError('Reset unavailable: wait for commands to finish and valid speed below 40 mph')
            # Explicit error acknowledgement only; no CAN and no restoration claim.
            self.fault = ''
            self.releasing = self.stopping = self.service_ready = False
            self.manual = self.restored is False
            self.reason = 'Error reset; press a hold button to retry'
            return self.status(now)
        if action in ('stop', 'restore'):
            token = d.get('token')
            if isinstance(token, str):
                self.closed.append(token)
            if token is None or token == self.token:
                if action == 'restore' and self.restored is False:
                    self.manual = True
                if self.fault and self.manual:
                    self.fault = ''
                    self.releasing = False
                if action == 'restore':
                    was_releasing = self.releasing
                    self.release('Restoring factory control')
                    if not was_releasing and self.queue and self.permitted(now):
                        self.queue.appendleft(SETUP[0])
                else:
                    self.stop_vent('Stopping venting')
            return self.status(now)
        token = d.get('token')
        if not isinstance(token, str) or not 16 <= len(token) <= 80:
            raise ValueError('Hold token required')
        if action not in ('start', 'heartbeat') or d.get('group') not in GROUPS:
            raise ValueError('Invalid hold command')
        if token in self.closed or not self.permitted(now) or self.releasing or self.stopping or self.fault:
            raise ValueError('Manual air ride unavailable')
        if action == 'heartbeat':
            if token != self.token or d['group'] != self.hold:
                raise ValueError('Hold expired; press again')
        elif self.token is not None:
            raise ValueError('Another hold is active')
        else:
            self.token, self.hold = token, d['group']
            self.manual = True
            self.restored = False
            self.queue = deque([SETUP[0]] if self.service_ready else SETUP)
            self.corner = 0
            self.next_pulse = now
            self.reason = 'Holding ' + self.hold
        self.deadline = now + .8
        return self.status(now)

    def reset_available(self, now):
        return bool(self.fault) and self.permitted(now) and not self.hold and not self.pending and not self.queue

    def status(self, now):
        return {'reset_available': self.reset_available(now), 'available': self.permitted(now) and not self.releasing and not self.stopping and not self.fault,
                'speed_mph': None if self.speed is None else self.speed / .44704,
                'hold': self.hold, 'stopping': self.stopping, 'manual': self.manual, 'releasing': self.releasing, 'reason': self.reason,
                'fault': self.fault, 'factory_restored': self.restored, 'last_request': self.last_request,
                'last_response': self.last_response, 'trace': list(self.trace)}

    def tick(self, now, speed, valid, can):
        # Use fresh raw ESP_B reception as well as parsed carState.canValid.
        if any(a == 0x155 and bus == 0 for a, data, bus in can):
            self.speed_at = now
        self.speed = speed if valid and math.isfinite(speed) and speed >= 0 else None
        if self.manual and not self.permitted(now):
            self.release('40 mph cutoff / speed unavailable')
        elif self.hold and now >= self.deadline:
            self.release('Hold heartbeat expired')
        for a, data, bus in can:
            if a == 0x65B and bus == 0 and len(data) >= 3 and 0 < data[0] <= 7:
                self.trace.append({'at': now, 'event': 'rx', 'payload': data[1:1 + data[0]].hex(' '),
                                   'pending': self.pending.hex(' ') if self.pending else None})
        if self.pending:
            payload = self.pending
            for a, data, bus in can:
                if a != 0x65B or bus != 0 or len(data) < 3 or data[0] > 7:
                    continue
                uds = data[1:1 + data[0]]
                self.last_response = uds.hex(' ')
                expected = bytes([payload[0] + 0x40]) + payload[1:4]
                if uds.startswith(expected):
                    self.pending = None
                    if payload == STOP:
                        self.vent_stopped = True
                    if payload == SETUP[1]:
                        self.send_after = now + .4
                    if payload == SETUP[2]:
                        self.service_ready = True
                    if payload == bytes.fromhex('31 01 03 09'):
                        self.send_after = now + 1.0
                    if self.stopping and not self.queue:
                        self.stopping = False
                        self.reason = 'Venting stopped; use Restore Factory Control to resume leveling'
                    if self.releasing and not self.queue:
                        self.manual = self.releasing = self.service_ready = False
                        self.restored = not bool(self.fault)
                        self.reason = 'Factory EAS control restored' if self.restored else 'Cleanup finished; EAS fault remains'
                    break
                if uds[:2] == bytes([0x7f, payload[0]]) and len(uds) >= 3 and uds[2] != 0x78:
                    self.fault = 'EAS rejected ' + payload.hex(' ') + ': ' + uds.hex(' ')
                    self.restored = False
                    self.pending = None
                    self.release('EAS command rejected')
                    break
            if self.pending and now - self.sent_at >= .6:
                self.trace.append({'at': now, 'event': 'timeout', 'payload': payload.hex(' ')})
                self.fault = 'EAS response timeout for ' + payload.hex(' ') + '; factory restoration unconfirmed'
                self.restored = False
                self.pending = None
                if self.releasing:
                    # Continue bounded cleanup; do not claim successful restoration.
                    pass
                else:
                    self.release('EAS response timeout')
        if self.pending:
            return []
        if now < self.send_after:
            return []
        if self.queue:
            payload = self.queue.popleft()
        elif self.hold and now >= self.next_pulse and self.permitted(now):
            corners = GROUPS[self.hold]
            payload = bytes([0x31, 1, 3, 0x34, corners[self.corner % len(corners)], 0])
            self.corner += 1
            self.next_pulse = now + .8
        else:
            return []
        if payload[:4] == bytes.fromhex('31 01 03 34'):
            # A transmitted start invalidates the previous confirmed stop, even without a reply.
            self.vent_stopped = False
        self.pending, self.sent_at = payload, now
        self.last_request = payload.hex(' ')
        self.trace.append({'at': now, 'event': 'tx', 'payload': self.last_request})
        return [frame(payload)]


class Endpoint:
    """Nonblocking local control channel, serviced by card at its normal cadence."""
    def __init__(self):
        self.controller = Controller()
        if os.path.exists(RECOVERY):
            with open(RECOVERY) as f:
                saved = json.load(f)
            self.controller.restored = False
            self.controller.vent_stopped = bool(saved.get('vent_stopped', False))
            self.controller.fault = 'Factory restoration unconfirmed after reload; use Restore Factory Control'
        self.saved_recovery = None
        from server.airride_diagnostics import DiagnosticReader
        self.diagnostics = DiagnosticReader()
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        if os.path.exists(SOCKET):
            os.unlink(SOCKET)
        self.sock.bind(SOCKET)
        os.chmod(SOCKET, 0o600)
        self.sock.setblocking(False)

    def update(self, speed, valid, can, active=True):
        now = time.monotonic()
        if active:
            if not self.controller.manual and not self.controller.fault and self.controller.reason == 'Waiting for Tesla Pre-AP safety mode':
                self.controller.reason = 'Ready'
            output = self.controller.tick(now, speed, valid, can)
        else:
            self.controller.speed = None
            self.controller.release('Waiting for Tesla Pre-AP safety mode')
            output = []
        for _ in range(16):
            try:
                raw, peer = self.sock.recvfrom(2048)
            except BlockingIOError:
                break
            try:
                d = json.loads(raw)
                if not isinstance(d, dict):
                    raise ValueError('JSON object required')
                if d.get('action') in ('start', 'heartbeat') and not 0 <= now - d.get('_received_at', -1) < .4:
                    raise ValueError('Request expired; press again')
                if d.get('action') == 'read_dtc':
                    c = self.controller
                    if not active or not c.permitted(now) or abs(c.speed) >= .1 or c.manual or c.hold or c.pending or c.queue:
                        raise ValueError('DTC read requires stationary valid data, active safety, and no manual command')
                    self.diagnostics.start()
                    result = c.status(now)
                else:
                    if self.diagnostics.busy and d.get('action') != 'status':
                        raise ValueError('DTC read in progress; wait for it to finish')
                    result = self.controller.request(d, now)
                result['dtc'] = self.diagnostics.status()
            except (ValueError, TypeError) as exc:
                result = {'error': str(exc)}
            try:
                self.sock.sendto(json.dumps(result).encode(), peer)
            except OSError:
                pass
        # A stop may have arrived in this cycle; never send the earlier pulse.
        if output and output[0][1][1:5] == bytes.fromhex('31 01 03 34') and not self.controller.hold:
            output = []
        c = self.controller
        output += self.diagnostics.update(can, active and c.permitted(now) and abs(c.speed) < .1 and not c.manual)
        if c.manual or c.restored is False:
            state = {'vent_stopped': c.vent_stopped}
            if state != self.saved_recovery:
                with open(RECOVERY + '.tmp', 'w') as f:
                    json.dump(state, f)
                os.replace(RECOVERY + '.tmp', RECOVERY)
                self.saved_recovery = state
        elif c.restored is True and os.path.exists(RECOVERY):
            os.unlink(RECOVERY)
            self.saved_recovery = None
        return output


def request(payload):
    with tempfile.TemporaryDirectory(prefix='nap-airride-', dir='/dev/shm') as directory:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.bind(directory + '/client')
            sock.settimeout(.3)
            try:
                sock.sendto(json.dumps(dict(payload, _received_at=time.monotonic())).encode(), SOCKET)
                return json.loads(sock.recv(4096))
            except OSError:
                return {'available': False, 'error': 'Air Ride controller unavailable; off-road controls remain disabled'}
