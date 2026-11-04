"""Passive, bounded driving evidence. Never imports a control publisher or writes parameters."""
from collections import deque
import copy
import json
import math
from itertools import islice
import os
from pathlib import Path
import tempfile
import time

IMPLEMENTATION = 'driving-evidence-v1'
DESIRES = ['none', 'turnLeft', 'turnRight', 'laneChangeLeft', 'laneChangeRight', 'keepLeft', 'keepRight']
SERVICES = ['carState', 'carControl', 'carOutput', 'controlsState', 'selfdriveState',
            'modelV2', 'longitudinalPlan', 'radarState', 'liveCalibration']
SIDECARS = ['nap_navigation_decision', 'nap_city_turn_intent', 'nap_turn_longitudinal',
            'nap_brake_resume', 'nap_road_stop', 'nap_tap_lane_change_request', 'nap_tap_lane_change_ack']
NOTICE = ('Model probabilities are producer estimates, not calibrated action-success or safety probabilities. '
          'Controller gates are decisions, not confidence percentages. Missing data stays unavailable. '
          'Actuator output is a reported command; vehicle acceleration is measured separately.')


def get(obj, key, default=None):
    try: return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)
    except Exception: return default


def clean(value, depth=0):
    if depth > 8: return None
    if value is None or type(value) in (bool, int): return value
    if type(value) is str:return value[:512]
    if type(value) is float: return round(value, 7) if math.isfinite(value) else None
    if isinstance(value, dict): return {str(k):clean(v,depth+1) for k,v in list(value.items())[:128]}
    if isinstance(value, (list, tuple)): return [clean(v,depth+1) for v in value[:160]]
    if hasattr(value, 'to_dict'): return clean(value.to_dict(),depth+1)
    try: return [clean(v,depth+1) for v in islice(value,160)]
    except (TypeError, ValueError): return str(value)[:100]


def fields(obj, names):
    return {k:clean(get(obj,k)) for k in names.split()}


def probability(value):
    return round(value*100., 5) if type(value) in (float,int) and math.isfinite(value) and 0<=value<=1 else None


def probabilities(values, source):
    raw=clean(values)
    raw=raw if isinstance(raw,list) else []
    return dict(source=source,raw=raw,percent=[probability(v) for v in raw],
                available=bool(raw) and all(probability(v) is not None for v in raw),calibrated=False)


def trajectory(obj):
    return fields(obj,'t x y z xStd yStd zStd')


def model_view(model):
    meta=get(model,'meta');dis=get(meta,'disengagePredictions')
    state=probabilities(get(meta,'desireState'),'modelV2.meta.desireState')
    state['labels']=[DESIRES[i] if i<len(DESIRES) else 'unmapped_slot_%d'%i for i in range(len(state['raw']))]
    total=sum(v for v in state['raw'] if type(v) in (float,int))
    state['distribution_sum']=round(total,6)
    state['distribution_valid']=state['available'] and .95<=total<=1.05
    state['meaning']='Native model desire output; distinct from selected conditioning input and controller authorization.'
    # No renormalization: invalid/all-zero vectors must not become invented certainty.
    if not state['distribution_valid']: state['available']=False
    predicted=probabilities(get(meta,'desirePrediction'),'modelV2.meta.desirePrediction')
    predicted['layout']='Producer flat array; no guessed horizon or action labels.'
    event_probs={k:probabilities(get(dis,k),'modelV2.meta.disengagePredictions.'+k) for k in
        ['brakeDisengageProbs','gasDisengageProbs','steerOverrideProbs','brake3MetersPerSecondSquaredProbs',
         'brake4MetersPerSecondSquaredProbs','brake5MetersPerSecondSquaredProbs','gasPressProbs','brakePressProbs']}
    leads=[]
    for lead in list(get(model,'leadsV3',[]) or [])[:3]:
        row=fields(lead,'prob probTime t x xStd y yStd v vStd a aStd')
        row.update(probability_pct=probability(get(lead,'prob')),meaning='Model estimate that this candidate is the lead at probTime; not collision risk.')
        leads.append(row)
    return dict(frame_id=get(model,'frameId'),confidence_class=clean(get(model,'confidence')),
                confidence_meaning='Producer red/yellow/green class derived from predicted disengagement; not a safety rating.',
                action_success_probability_pct=None,current_desire=state,future_desire=predicted,
                engaged_probability_pct=probability(get(meta,'engagedProb')),
                engaged_probability_meaning='Model engaged-state estimate, not probability the controller will engage.',
                driver_event_horizons_s=clean(get(dis,'t')),driver_event_probabilities=event_probs,
                hard_brake_predicted=get(meta,'hardBrakePredicted'),
                lane_change=fields(meta,'laneChangeState laneChangeDirection'),
                lane_line_probabilities=probabilities(get(model,'laneLineProbs'),'modelV2.laneLineProbs'),
                lane_line_stds=clean(get(model,'laneLineStds')),road_edge_stds=clean(get(model,'roadEdgeStds')),
                position=trajectory(get(model,'position')),velocity=trajectory(get(model,'velocity')),
                acceleration=trajectory(get(model,'acceleration')),orientation=trajectory(get(model,'orientation')),
                lane_lines=[trajectory(x) for x in list(get(model,'laneLines',[]) or [])[:4]],
                road_edges=[trajectory(x) for x in list(get(model,'roadEdges',[]) or [])[:2]],leads=leads,
                frame_drop_percent=get(model,'frameDropPerc'),execution_time_s=get(model,'modelExecutionTime'))


class DrivingEvidence:
    SNAPSHOT_BYTES=96000
    CHUNK_BYTES=240000
    HISTORY_LIMIT=720

    def __init__(self, history, directory=Path('/dev/shm')):
        self.history=history;self.directory=Path(directory)
        self.next_observe=0.;self.next_history=0.;self.last_flush=0.
        self.pending=[];self.pending_bytes=0;self.recent=deque(maxlen=40)
        self.saved=0;self.dropped=0;self.omitted=0;self.last_error='';self.sequence=0
        self.previous=None
        try:self.boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        except OSError:self.boot=None

    def status(self):
        return dict(implementation=IMPLEMENTATION,used_for_control=False,snapshot_hz=5,history_hz=2,
                    history_chunk_s=5,history_limit=self.HISTORY_LIMIT,max_chunk_bytes=self.CHUNK_BYTES,
                    pending_samples=len(self.pending),saved=self.saved,dropped=self.dropped,
                    omitted_oversized=self.omitted,last_error=self.last_error)

    def sidecar(self,name,now):
        path=self.directory/(name+'.json')
        try:
            if path.is_symlink():raise ValueError('symlink excluded')
            with path.open('rb') as stream:raw=stream.read(16385)
            if len(raw)>16384:raise ValueError('diagnostic too large')
            value=json.loads(raw)
            if not isinstance(value,dict):raise ValueError('diagnostic is not an object')
            if name=='nap_city_turn_intent' and isinstance(value.get('authorization_trace'),dict):
                trace=value.pop('authorization_trace')
                # Public decision evidence, not HTTP/OAuth authorization. Keep
                # the MCP credential filter intact and expose only known keys.
                value['direction_trace']={k:trace.get(k) for k in ['raw_classified_direction','physical_direction',
                    'effective_direction','direction_retained','request_valid','request_controller','reason']}
            stamp=value.get('producer_mono',value.get('received_mono',value.get('time')))
            age=now-stamp if type(stamp) in (float,int) and math.isfinite(stamp) else None
            boot=value.get('boot_id');same_boot=(boot==self.boot) if boot else None
            fresh=age is not None and 0<=age<=.5 and same_boot is not False
            return dict(status='fresh' if fresh else 'stale',producer_mono=stamp,age_s=age,
                        boot_verified=same_boot,data=clean(value))
        except (OSError,ValueError,TypeError) as exc:
            return dict(status='unavailable',data=None,reason=type(exc).__name__)

    def write(self,name,value):
        temporary=None
        try:
            raw=json.dumps(clean(value),allow_nan=False,separators=(',',':')).encode()
            if len(raw)>self.SNAPSHOT_BYTES:raise ValueError('evidence snapshot exceeds bound')
            fd,temporary=tempfile.mkstemp(prefix='.nap-evidence-',dir=self.directory)
            with os.fdopen(fd,'wb') as stream:stream.write(raw)
            os.replace(temporary,self.directory/(name+'.json'))
        except (OSError,ValueError,TypeError) as exc:self.last_error=str(exc)[:160]
        finally:
            if temporary and os.path.exists(temporary):os.unlink(temporary)

    def observe(self,sm,now):
        if now<self.next_observe:return
        sources={};messages={}
        for name in SERVICES:
            seen=bool(get(sm.seen,name,False));valid=bool(get(sm.valid,name,False))
            stamp=get(sm.logMonoTime,name,0)/1e9 if seen else None
            age=now-stamp if stamp is not None else None
            fresh=seen and valid and age is not None and 0<=age<=.5
            sources[name]=dict(status='fresh' if fresh else 'stale' if seen else 'unavailable',
                               valid=valid,producer_mono=stamp,age_s=age)
            try:messages[name]=sm[name] if seen else None
            except Exception:messages[name]=None
        moving_feed=sources['carState']['status']=='fresh'
        self.next_observe=now+(.2 if moving_feed else 1.)
        cs=messages['carState'];cc=messages['carControl'];ctl=messages['controlsState'];sd=messages['selfdriveState']
        model=messages['modelV2'];plan=messages['longitudinalPlan'];radar=messages['radarState']
        decisions={k:self.sidecar(k,now) for k in SIDECARS}
        decision=(decisions['nap_navigation_decision'].get('data') or {})
        model_frame=get(model,'frameId');selected_frame=decision.get('frame_id')
        skew=model_frame-selected_frame if type(model_frame) is int and type(selected_frame) is int else None
        pair_fresh=(sources['modelV2']['status']=='fresh' and decisions['nap_navigation_decision']['status']=='fresh')
        self.sequence+=1
        lateral=get(ctl,'lateralControlState');lat_type=None
        try:lat_type=lateral.which()
        except (AttributeError,ValueError):pass
        point=dict(version=1,implementation=IMPLEMENTATION,producer_mono=now,recorded_at=time.time(),boot_id=self.boot,
            sequence=self.sequence,used_for_control=False,notice=NOTICE,sources=sources,
            units=dict(speed='m/s',acceleration='m/s^2',curvature='1/m',steering='degrees',position='metres',orientation='radians',
                       uncertainty='Producer standard deviations in the corresponding quantity units; not percentages.'),
            alignment=dict(model_to_selection_frame_skew=skew,paired=pair_fresh and skew is not None and abs(skew)<=2),
            vehicle=fields(cs,'vEgo aEgo steeringAngleDeg steeringTorque steeringPressed steeringDisengage brakePressed brake gasPressed gas leftBlinker rightBlinker canValid gearShifter leftBlindspot rightBlindspot'),
            engagement=fields(sd,'enabled active experimentalMode state alertType alertText1 alertText2'),
            model=model_view(model),
            requests=dict(model=fields(get(model,'action'),'desiredCurvature desiredAcceleration shouldStop'),
                          car_control=fields(cc,'latActive longActive'),
                          actuators=fields(get(cc,'actuators'),'steeringAngleDeg steer accel curvature longControlState'),
                          reported_actuator_output=fields(get(messages['carOutput'],'actuatorsOutput'),'steeringAngleDeg steer accel curvature longControlState')),
            controller=dict(state=fields(ctl,'curvature desiredCurvature longControlState vCruise'),
                            lateral_type=lat_type,lateral=clean(get(lateral,lat_type)) if lat_type else None),
            planner=fields(plan,'modelMonoTime hasLead fcw longitudinalPlanSource processingDelay accels speeds jerks aTarget shouldStop allowThrottle allowBrake solverExecutionTime'),
            radar=dict(lead_one=fields(get(radar,'leadOne'),'status dRel yRel vRel aRel vLead aLeadK modelProb radar'),
                       lead_two=fields(get(radar,'leadTwo'),'status dRel yRel vRel aRel vLead aLeadK modelProb radar')),
            calibration=fields(messages['liveCalibration'],'calStatus validBlocks rpyCalib'),decisions=decisions)
        point=clean(point)
        point['model']['source_status']=sources['modelV2']
        signature=(get(sd,'experimentalMode'),get(cc,'latActive'),get(cc,'longActive'),get(cs,'steeringPressed'),
                   get(cs,'brakePressed'),get(cs,'gasPressed'),decision.get('final_desire'),decision.get('manual_turn_reason'),
                   decision.get('source'),point['model']['confidence_class'])
        changed=self.previous is not None and signature!=self.previous
        self.previous=signature
        point['transition_observed']=changed
        self.write('nap_driving_evidence',dict(point,recorder=self.status()))
        if moving_feed and (now>=self.next_history or changed):
            self.next_history=now+.5
            compact=self.compact(point);size=len(json.dumps(compact,separators=(',',':')).encode())
            if size>self.CHUNK_BYTES-2000:self.omitted+=1;return
            if self.pending and self.pending_bytes+size>self.CHUNK_BYTES-2000:self.flush()
            if not self.pending:self.last_flush=now
            self.pending.append(compact);self.pending_bytes+=size
            self.recent.append(compact)
            while self.recent and len(json.dumps(list(self.recent),separators=(',',':')).encode()) > 80000:self.recent.popleft()
            self.write('nap_driving_evidence_recent',dict(version=1,implementation=IMPLEMENTATION,producer_mono=now,
                       boot_id=self.boot,used_for_control=False,samples=list(self.recent),target_sample_hz=2,
                       events_can_add_samples=True,retained_duration_s=self.recent[-1]['producer_mono']-self.recent[0]['producer_mono'] if self.recent else 0.,
                       recorder=self.status()))
        if self.pending and (now-self.last_flush>=5 or not moving_feed):self.flush()

    @staticmethod
    def compact(point):
        value=copy.deepcopy(point)
        m=value['model']
        # Complete geometry is in the live snapshot/raw model topic. Retain the
        # planned path and its uncertainty, probabilities and all decision gates.
        for name in ['lane_lines','road_edges','velocity','acceleration','orientation']:m.pop(name,None)
        value.pop('notice',None);value.pop('units',None)
        return value

    def flush(self):
        if not self.pending:return
        row=dict(version=1,implementation=IMPLEMENTATION,recorded_at=time.time(),boot_id=self.boot,
                 start_mono=self.pending[0]['producer_mono'],end_mono=self.pending[-1]['producer_mono'],
                 used_for_control=False,notice=NOTICE,samples=self.pending)
        self.pending=[];self.pending_bytes=0
        try:
            if self.history.append('driving_evidence',row):self.saved+=1
            else:self.dropped+=1
        except Exception as exc:self.dropped+=1;self.last_error=str(exc)[:160]
