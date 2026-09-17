"""Paired API settings; enabling either behavior requires fresh Park state."""
import json
import os
import threading
import time
from pathlib import Path

PATH=Path('/data/nap_turn_settings.json')
LOCK=threading.Lock()


def read():
    result={'corner_assist':False,'blinker_braking':True,'manual_blinker_turns':False,
            'mapped_stop_slowdown':False}
    try:
        with PATH.open() as f:obj=json.loads(f.read(2049))
        for key in result:
            if type(obj.get(key)) is bool:result[key]=obj[key]
    except (OSError,ValueError,TypeError,AttributeError):pass
    return result


def require_park():
    import cereal.messaging as messaging
    sm=messaging.SubMaster(['carState','carControl'])
    until=time.monotonic()+1
    while time.monotonic()<until:sm.update(100)
    if not all(sm.valid[s] and sm.alive[s] and 0 <= time.monotonic()-sm.logMonoTime[s]/1e9 < .25 for s in ('carState','carControl')):
        raise ValueError('Wake the car and remain in Park to enable this setting.')
    cs,cc=sm['carState'],sm['carControl']
    if (str(cs.gearShifter)!='park' or not cs.canValid or not abs(cs.vEgo)<=.05 or not abs(cs.vEgoRaw)<=.05
        or cc.enabled or cc.latActive or cc.longActive or cs.cruiseState.enabled):
        raise ValueError('Park and disengage all controls to enable this setting.')


def write(data):
    name,enabled=data.get('name'),data.get('enabled')
    if name not in ('corner_assist','blinker_braking','manual_blinker_turns','mapped_stop_slowdown') or type(enabled) is not bool:
        raise ValueError('Invalid turn setting')
    if enabled:require_park()
    with LOCK:
        obj={}
        if PATH.exists():
            obj=json.loads(PATH.read_text())
            if not isinstance(obj,dict):raise ValueError('Turn settings need repair before changing them')
        obj[name]=enabled
        temp=PATH.with_suffix('.tmp')
        temp.write_text(json.dumps(obj,indent=2)+'\n')
        os.replace(temp,PATH)
    return read()
