"""GPS progress continuity and evidence-based route-step completion.

This module changes no steering/signal authority, speed or freshness limit.
"""
import math


def finite(x):
    return type(x) in (int,float) and math.isfinite(x)


class ProgressTiming:
    def __init__(self):
        self.key=None
        self.sensor=None
        self.anchor=None
        self.speed=None
        self.previous_sample=None
        self.last_now=None
        self.progress=None
        self.reason='waiting'
        self.raw=None
        self.innovation=None
        self.trusted=False

    def update(self,route_key,sample_key,raw,sensor,now,speed,accuracy,projection_valid,gps_speed=None):
        self.raw=raw
        self.innovation=None
        if not all(finite(v) for v in (raw,sensor,now)) or not 0<=now-sensor<=3:
            self.reason='invalid_source_time';return None
        motion=projection_valid and finite(speed) and 0<=speed<=45
        sensor_speed=gps_speed if finite(gps_speed) and 0<=gps_speed<=45 else speed
        new_route=route_key!=self.key
        gap=self.sensor is not None and sensor-self.sensor>3
        if new_route or self.sensor is None or gap:
            initial=self.sensor is None
            self.key=route_key;self.sensor=sensor;self.anchor=raw
            self.speed=sensor_speed if motion else None;self.previous_sample=sample_key
            self.reason='new_route' if new_route else 'reacquired'
            self.trusted=bool(motion and (new_route or initial))
        elif sample_key!=self.previous_sample:
            dt=sensor-self.sensor
            if not 0<dt<=3:
                self.reason='nonforward_sensor_time';return None
            if not motion and self.speed is not None:
                self.reason='odometry_or_projection_unavailable';return None
            if motion and self.speed is not None:
                expected=self.anchor+(self.speed+sensor_speed)*.5*dt
                innovation=raw-expected;self.innovation=innovation
                gate=max(6.,min(15.,(accuracy if finite(accuracy) and accuracy>0 else 8.)*.75))
                if abs(innovation)>gate:
                    # Do not publish a guessed position as trustworthy. Wait
                    # for a coherent new GPS fix; source timestamps stay old.
                    self.reason='gps_jump_rejected';self.trusted=False;return None
                correction=max(-3.*dt,min(3.*dt,.35*innovation))
                self.anchor=expected+correction
                self.reason='bounded_gps_correction'
                self.trusted=True
            else:
                self.anchor=raw;self.reason='gps_only_no_odometry';self.trusted=False
            self.sensor=sensor;self.speed=sensor_speed if motion else None
            self.previous_sample=sample_key
        # Each tick projects from one sensor-time anchor, never from the last
        # already-projected output. Corrections can unwind an earlier overshoot.
        value=self.anchor+((sensor_speed+speed)*.5*(now-sensor) if motion else 0.)
        if not motion and self.progress is not None and not new_route:
            value=self.progress  # Historic hold only; projection permission is false.
        self.last_now=now;self.progress=value
        return value


class ManeuverCompletion:
    def __init__(self):
        self.route_key=None
        self.completed=0
        self.nearby=set()

    def index(self,route,route_key,progress,raw_progress,bearing,heading_error,projection_valid):
        if route_key!=self.route_key:
            self.route_key=route_key;self.completed=0;self.nearby=set()
            # Initial/replaced routes may begin part way through a cached path.
            for i,s in enumerate(route.steps[1:-1],1):
                if s['at']<progress-8:self.completed=i
        for i in range(self.completed+1,len(route.steps)-1):
            s=route.steps[i];at=s['at'];m=s['maneuver']
            if at-80<=progress<=at+40:self.nearby.add(i)
            before,after=m.get('bearing_before'),m.get('bearing_after')
            delta=lambda a,b:abs((a-b+180)%360-180)
            turn=(finite(before) and finite(after) and delta(before,after)>=35)
            outgoing=(turn and finite(bearing) and delta(bearing,after)<=20
                      and delta(bearing,before)>=35 and projection_valid
                      and finite(heading_error) and heading_error<=20)
            # A sharp turn only completes early when position AND the changed
            # outgoing heading agree. Forward GPS noise on approach cannot do it.
            confirmed=(i in self.nearby and outgoing and progress>=at+2 and raw_progress>=at)
            if confirmed or (progress>at+8 and (not turn or outgoing)):
                self.completed=i
            else:break
        return min(len(route.steps)-1,self.completed+1)
