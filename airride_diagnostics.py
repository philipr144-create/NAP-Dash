"""Read-only EAS DTC transport; CAN output remains owned by card."""
import json
import threading
from collections import deque

from opendbc.car.uds import UdsClient, SERVICE_TYPE, SESSION_TYPE

class DiagnosticReader:
    def __init__(self):
        self.lock = threading.Lock()
        self.ready = threading.Condition(self.lock)
        self.trace = deque(maxlen=16)
        self.rx = deque(maxlen=512)
        self.tx = deque()
        self.busy = False
        self.cancelled = False
        self.result = None

    def can_send(self, address, data, bus, timeout=None):
        allowed = {bytes.fromhex(x) for x in (
            '02 10 03 00 00 00 00 00', '02 10 01 00 00 00 00 00',
            '03 19 02 ff 00 00 00 00', '30 00 00 00 00 00 00 00')}
        with self.lock:
            if self.cancelled:
                raise RuntimeError('DTC read cancelled: vehicle/data/safety state changed')
            if address != 0x64B or bus != 0 or bytes(data) not in allowed:
                raise ValueError('Not a permitted read-only diagnostic packet')
            self.tx.append((address, bytes(data), bus))

    def can_recv(self):
        with self.lock:
            if self.cancelled:
                raise RuntimeError('DTC read cancelled: vehicle/data/safety state changed')
            if not self.rx:
                self.ready.wait(.01)
            frames = list(self.rx)
            self.rx.clear()
            return frames

    def start(self):
        with self.lock:
            if self.busy:
                raise ValueError('DTC read already in progress')
            self.busy = True
            self.cancelled = False
            self.result = None
            self.rx.clear()
            self.tx.clear()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        result = {}
        entered = False
        client = UdsClient(self, 0x64B, rx_addr=0x65B, bus=0, timeout=1.5, response_pending_timeout=3)
        try:
            entered = True
            client.diagnostic_session_control(SESSION_TYPE.EXTENDED_DIAGNOSTIC)
            data = client._uds_request(SERVICE_TYPE.READ_DTC_INFORMATION, subfunction=2, data=b'\xff')
            if not data or (len(data)-1) % 4:
                raise ValueError('Malformed DTC response: ' + data.hex(' '))
            records = [{'code': data[i:i+3].hex().upper(), 'status': f'{data[i+3]:02X}'}
                       for i in range(1, len(data), 4)]
            result = {'response': '59 02 ' + data.hex(' '), 'status_availability_mask': f'{data[0]:02X}',
                      'records': records, 'cleared': False}
        except Exception as exc:
            result = {'error': str(exc), 'cleared': False}
        finally:
            if entered:
                try:
                    client.diagnostic_session_control(SESSION_TYPE.DEFAULT)
                except Exception as exc:
                    result['session_exit_error'] = str(exc)
            with self.lock:
                self.result = result
                self.busy = False
                self.tx.clear()

    def update(self, can, allowed):
        with self.lock:
            if self.busy and not allowed:
                self.cancelled = True
                self.tx.clear()
            if not self.busy or self.cancelled:
                return []
            incoming=[(a,d,b) for a,d,b in can if a == 0x65B and b == 0]
            self.rx.extend(incoming)
            for a,d,b in incoming:self.trace.append({'rx': bytes(d).hex(' ')})
            if incoming:self.ready.notify_all()
            out = list(self.tx)
            self.tx.clear()
            for a,d,b in out:self.trace.append({'tx': d.hex(' ')})
            return out

    def status(self):
        with self.lock:
            return {'busy': self.busy, 'result': self.result, 'trace': list(self.trace)}
