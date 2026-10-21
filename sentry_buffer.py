"""Bounded compressed H264 rings. Nothing is written to disk until an event."""
from collections import deque
from dataclasses import dataclass

@dataclass(frozen=True)
class Packet:
    timestamp: int
    key: bool
    payload: bytes
    width: int
    height: int

class VideoRing:
    # Applies independently to each of the fixed three cameras.
    MAX_BYTES = 8 * 1024 * 1024
    KEYFRAME_MARGIN = 3
    def __init__(self):
        self.items=deque();self.bytes=0
    def clear(self):
        self.items.clear();self.bytes=0
    def append(self,packet,seconds):
        if seconds<=0:self.clear();return
        if self.items and packet.timestamp<=self.items[-1].timestamp:return
        if len(packet.payload)>self.MAX_BYTES:self.clear();return
        self.items.append(packet);self.bytes+=len(packet.payload)
        cutoff=packet.timestamp-int((seconds+self.KEYFRAME_MARGIN)*1e9)
        while self.items and (self.bytes>self.MAX_BYTES or self.items[0].timestamp<cutoff):
            self.bytes-=len(self.items.popleft().payload)
        # A ring is always independently decodable, even after hitting a cap.
        while self.items and not self.items[0].key:
            self.bytes-=len(self.items.popleft().payload)
    def snapshot(self,trigger_ns,seconds):
        cutoff=trigger_ns-int(seconds*1e9)
        packets=[p for p in self.items if p.timestamp<=trigger_ns]
        keys=[i for i,p in enumerate(packets) if p.key and p.timestamp<=cutoff]
        if keys:packets=packets[keys[-1]:]
        return packets
    def seconds(self):
        return (self.items[-1].timestamp-self.items[0].timestamp)/1e9 if len(self.items)>1 else 0.
