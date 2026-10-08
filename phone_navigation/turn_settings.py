"""Paired API settings; enabling either behavior requires fresh Park state."""
import json
import os
import threading
import time
from pathlib import Path
from .vehicle_snapshot import require_park

PATH=Path('/data/nap_turn_settings.json')
LOCK=threading.Lock()
PARAM_KEYS={"corner_assist":"NAPCornerAssist", "manual_blinker_turns":"NAPCityTurns"}


def read():
    result={'corner_assist':False,'blinker_braking':True,'manual_blinker_turns':False,
            'mapped_stop_slowdown':False,'road_stop_slowdown':False,'normal_model_stops':False}
    try:
        with PATH.open() as f:obj=json.loads(f.read(2049))
        for key in result:
            if type(obj.get(key)) is bool:result[key]=obj[key]
    except (OSError,ValueError,TypeError,AttributeError):pass
    from openpilot.common.params import Params
    params=Params()
    for name,key in PARAM_KEYS.items():
        value=params.get(key)
        if type(value) is bool: result[name]=value
    return result


def write(data):
    name,enabled=data.get('name'),data.get('enabled')
    if name not in ('corner_assist','blinker_braking','manual_blinker_turns','mapped_stop_slowdown','road_stop_slowdown','normal_model_stops') or type(enabled) is not bool:
        raise ValueError('Invalid turn setting')
    # Preserve legacy clients; blinker_braking is stored for compatibility only.
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
        if name in PARAM_KEYS:
            from openpilot.common.params import Params
            Params().put_bool(PARAM_KEYS[name],enabled)
    return read()
