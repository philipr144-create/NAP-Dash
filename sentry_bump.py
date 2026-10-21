"""Parked jolt detection from fresh native accelerometer samples (m/s²)."""
import math

GRAVITY = 9.81

class BumpDetector:
    def __init__(self):
        self.reset()
    def reset(self):
        self.gravity=None;self.last_ns=0;self.start_ns=0;self.samples=0
        self.pending_ns=0;self.last_hit_ns=0;self.current_g=0.;self.peak_g=0.;self.ready=False
    def update(self,vector,timestamp_ns,now_ns,threshold_g):
        if len(vector)!=3 or not all(math.isfinite(v) for v in vector):
            self.reset();return False
        if not 0<=now_ns-timestamp_ns<=200_000_000 or timestamp_ns<=self.last_ns:
            return False
        if self.last_ns and timestamp_ns-self.last_ns>250_000_000:
            self.reset()
        values=tuple(float(v) for v in vector)
        if self.gravity is None:
            self.gravity=values;self.start_ns=timestamp_ns
        dt=min(.1,(timestamp_ns-self.last_ns)/1e9) if self.last_ns else .01
        self.last_ns=timestamp_ns;self.samples+=1
        dynamic=math.sqrt(sum((v-g)**2 for v,g in zip(values,self.gravity)))/GRAVITY
        self.current_g=dynamic;self.peak_g=max(self.peak_g,dynamic)
        alpha=1-math.exp(-dt/1.5)
        self.gravity=tuple(g+alpha*(v-g) for v,g in zip(values,self.gravity))
        self.ready=timestamp_ns-self.start_ns>=3_000_000_000 and self.samples>=100
        if not self.ready:return False
        hit=False
        if dynamic>=threshold_g:
            hit=dynamic>=2*threshold_g or bool(self.pending_ns and timestamp_ns-self.pending_ns<=40_000_000)
            self.pending_ns=timestamp_ns
        else:self.pending_ns=0
        if hit and (not self.last_hit_ns or timestamp_ns-self.last_hit_ns>=10_000_000_000):
            self.last_hit_ns=timestamp_ns;return True
        return False
    def status(self,now_ns,enabled):
        fresh=bool(self.last_ns and 0<=now_ns-self.last_ns<=250_000_000)
        state='off' if not enabled else 'unavailable' if not fresh else 'ready' if self.ready else 'settling'
        return dict(state=state,current_g=round(self.current_g,3),peak_g=round(self.peak_g,3),last_hit_mono_ns=self.last_hit_ns)
