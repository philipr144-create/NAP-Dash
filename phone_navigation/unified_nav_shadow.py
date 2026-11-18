"""NAP unified localization *observer*. It has no authority over vehicle controls.

Tesla GNSS anchors a vehicle-motion estimate when both CAN clocks can be
aligned with the OpenPilot livePose history. OSM supplies *road centerlines* and
mapped lane counts; modelV2 supplies ego-relative lane lines and road edges.

A road centerline is not a lane-level survey, and two agreeing GNSS receivers
do not establish a correct roadway. All estimates are display/diagnostic only.
Never feed this module into a planner, a speed target, a turn intent or an IPC
guidance bridge until separately validated.
"""
import json
import math
import time
from pathlib import Path

from .position_observer import EARTH_M, rotate

WORLD_PATH = Path('/dev/shm/nap_world_geometry.json')
WORLD_READ_LIMIT = 320 * 1024


def finite(v):
    return type(v) in (int, float) and math.isfinite(v)


def bearing_error(a, b):
    return abs((a - b + 180) % 360 - 180)


def meters_xy(point, origin):
    lon, lat = point
    lat0, lon0 = origin['lat'], origin['lon']
    return (EARTH_M * math.radians(lon - lon0) * math.cos(math.radians(lat0)),
            EARTH_M * math.radians(lat - lat0))


def position_from_offset(point, east, north):
    lon, lat = point
    nlat = lat + math.degrees(north / EARTH_M)
    return [lon + math.degrees(east / (EARTH_M * math.cos(math.radians((lat+nlat)/2)))), nlat]


def tesla_motion_pose(sample, history, now):
    """Estimate current position by replaying observed wheel/yaw *changes*.

    Tesla position and heading CAN packets arrive at different times, so heading
    is aligned to pose yaw at its own timestamp, not naively assigned to GPS time.
    Does not convert HDOP into accuracy metres.
    """
    invalid = lambda reason: {'valid': False, 'reason': reason}
    if sample is None or history is None:
        return invalid('tesla_or_motion_unavailable')
    try:
        position, motion = sample
        lon, lat = position['point']
        pm, hm = position['mono'], motion['mono']
        speed = motion['speed_mps']
        hdg = motion['heading_deg']
    except (KeyError, TypeError, ValueError):
        return invalid('invalid_tesla_sample')
    if not all(finite(v) for v in (lon, lat, pm, hm, speed, hdg, now)):
        return invalid('nonfinite_tesla_sample')
    if not (-180 <= lon <= 180 and -85 <= lat <= 85 and 0 <= speed <= 45
            and 0 <= hdg < 360 and 0 <= now-pm <= 2 and 0 <= now-hm <= 2.5):
        return invalid('out_of_range_or_stale_tesla')
    if abs(pm-hm) > 1.1:
        return invalid('tesla_can_clock_skew')
    rows = getattr(history, 'history', ())
    if not rows or now-rows[-1][0] > .4 or rows[-1][0] > now+.05:
        return invalid('motion_history_stale')
    at = getattr(history, 'at', None)
    if not callable(at):
        return invalid('motion_history_unavailable')
    pos_row = at(pm)
    heading_row = at(hm)
    if pos_row is None or heading_row is None:
        return invalid('motion_history_not_primed')
    # NED yaw increases toward east; existing observer uses this same rotation.
    offset = (math.radians(hdg)-heading_row[4]+math.pi) % (2*math.pi)-math.pi
    last = rows[-1]
    east, north = rotate(last[1]-pos_row[1], last[2]-pos_row[2], offset)
    current_heading = math.degrees((last[4]+offset) % (2*math.pi))
    est = position_from_offset((lon, lat), east, north)
    reported_accuracy = position.get('accuracy_m')
    accuracy_m = round(reported_accuracy, 2) if finite(reported_accuracy) and 0 < reported_accuracy <= 50 else None
    distance = math.hypot(east, north)
    return {'valid': True, 'point': [round(v, 8) for v in est],
            'heading_deg': round(current_heading, 2),
            'source': 'tesla_can_odometry_aligned',
            'tesla_position_age_s': round(now-pm, 3),
            'tesla_heading_age_s': round(now-hm, 3),
            'position_heading_can_skew_s': round(abs(pm-hm), 3),
            'distance_predicted_m': round(distance, 2),
            'reported_accuracy_m': accuracy_m,
            'accuracy_note': 'receiver_reported_not_verified' if accuracy_m is not None else 'unknown',
            'motion_source': 'wheel_speed_and_live_pose',
            'motion_age_s': round(now-last[0], 3)}


def road_candidates(point, heading_deg, world, limit=4):
    """Pure read-only geometric matching; score is NOT confidence in percent."""
    origin = world.get('origin')
    roads = world.get('roads', [])
    if not (isinstance(origin, dict) and all(finite(origin.get(k)) for k in ('lat', 'lon'))
            and isinstance(roads, list) and len(roads) <= 1000
            and world.get('coordinate_system') == 'enu_m'):
        return []
    px, py = meters_xy(point, origin)
    radius = world.get('radius_m')
    if not finite(radius) or math.hypot(px, py) > radius+30:
        return []
    options = []
    for road in roads:
        if not isinstance(road, dict):
            continue
        vertices = road.get('points', [])
        if not isinstance(vertices, list) or len(vertices) < 2 or len(vertices) > 500:
            continue
        best = None
        for a, b in zip(vertices[:-1], vertices[1:]):
            if not (isinstance(a, (tuple, list)) and isinstance(b, (tuple, list))
                    and len(a) >= 2 and len(b) >= 2 and
                    all(finite(v) for v in (a[0], a[1], b[0], b[1]))):
                continue
            dx, dy = b[0]-a[0], b[1]-a[1]
            norm2 = dx*dx + dy*dy
            if norm2 < .25:
                continue
            t = max(0., min(1., ((px-a[0])*dx+(py-a[1])*dy)/norm2))
            distance = math.hypot(px-a[0]-t*dx, py-a[1]-t*dy)
            direction = math.degrees(math.atan2(dx, dy)) % 360
            if road.get('oneway_reverse'):
                direction = (direction+180) % 360
            err = bearing_error(heading_deg, direction)
            if not road.get('oneway'):
                err = min(err, bearing_error(heading_deg, (direction+180) % 360))
            if road.get('oneway') and err > 60:
                continue
            score = distance+.15*err
            if best is None or score < best['score']:
                best = {'id': str(road.get('id',''))[:80],
                        'way_id': str(road.get('way_id',''))[:80],
                        'name': str(road.get('name',''))[:90],
                        'highway': str(road.get('highway',''))[:30],
                        'distance_m': round(distance,2),
                        'heading_error_deg': round(err,1),
                        'score': round(score,2),
                        'oneway': road.get('oneway') is True,
                        'lanes_mapped': road.get('lanes_mapped'),
                        'width_m': road.get('width_m'),
                        'width_source': road.get('width_source')}
        if best and best['distance_m'] <= 45 and best['heading_error_deg'] <= 65:
            options.append(best)
    options.sort(key=lambda x:x['score'])
    # Duplicate portions of the same OSM way do not count as independent roads.
    unique = []
    seen = set()
    for candidate in options:
        key = candidate['way_id'] or candidate['id']
        if key not in seen:
            unique.append(candidate)
            seen.add(key)
        if len(unique) >= limit:
            break
    return unique


def vision_lane(model, road):
    """Estimate lane index only with clear OpenPilot lines AND road edges.

    A lane index is relative to the visible roadway, left-to-right in travel
    direction. OSM lane count can be missing or total both directions; in that
    case we refuse to label a specific lane.
    """
    reject = lambda reason: {'state': 'unknown', 'reason': reason}
    if not isinstance(model, dict) or not model.get('valid'):
        return reject('vision_stale_or_unavailable')
    if not road or road.get('oneway') is not True:
        return reject('road_direction_unknown_or_two_way')
    count = road.get('lanes_mapped')
    if type(count) is not int or not 1 <= count <= 6:
        return reject('osm_lane_count_missing')
    lines = {v.get('idx'):v for v in model.get('lane_lines', []) if isinstance(v,dict)}
    left,right = lines.get(1),lines.get(2)
    if not left or not right:
        return reject('ego_lane_lines_missing')
    if not all(finite(v) for v in (left.get('y_m'),right.get('y_m'),
                                  left.get('prob'),right.get('prob'))):
        return reject('ego_lane_lines_invalid')
    yl,yr = left['y_m'],right['y_m']
    if not (left['prob'] >= .65 and right['prob'] >= .65 and -5.2 <= yl <= -.3
            and .3 <= yr <= 5.2 and 2.5 <= yr-yl <= 4.8):
        return reject('ego_lane_lines_low_quality')
    edges = {v.get('idx'):v for v in model.get('road_edges', []) if isinstance(v,dict)}
    le,re = edges.get(0),edges.get(1)
    if not le or not re or not all(finite(v) for v in
                               (le.get('y_m'),re.get('y_m'),le.get('std_m'),re.get('std_m'))):
        return reject('road_edges_missing')
    if le['std_m'] > 1.25 or re['std_m'] > 1.25 or le['std_m'] < 0 or re['std_m'] < 0:
        return reject('road_edges_uncertain')
    if not le['y_m'] < yl < 0 < yr < re['y_m']:
        return reject('edges_inconsistent_with_lane_lines')
    total_vision_width = re['y_m']-le['y_m']
    expected = (yr-yl)*count
    # Road edges may contain shoulders, but should remain compatible with the
    # OSM lane count and observed ego-lane width.
    if abs(total_vision_width-expected) > max(3.5,.35*expected):
        return reject('osm_camera_width_disagreement')
    cell = total_vision_width/count
    # Exact boundaries are not surveyed; avoid reporting a lane when the ego
    # appears near an inferred lane separator.
    raw = -le['y_m']/cell
    if raw <= 0 or raw >= count:
        return reject('vehicle_outside_inferred_road')
    if count > 1 and abs(raw-round(raw))*cell < .6:
        return reject('near_inferred_lane_boundary')
    index = int(math.floor(raw))+1
    return {'state':'candidate', 'lane_index_from_left': index, 'lane_count': count,
            'lane_width_vision_m': round(yr-yl,2), 'visible_road_width_m': round(total_vision_width,2),
            'position_in_lane_m': round(-(yl+yr)/2,2),
            'basis': 'modelV2_laneLines_roadEdges_plus_osm_lane_count',
            'note': 'inferred lanes, not verified physical lane boundaries'}


class UnifiedNavigationShadow:
    def __init__(self, geometry_path=WORLD_PATH):
        self.geometry_path = Path(geometry_path)
        self.world = None
        self.world_checked_at = -1e9
        self.road_pending = None
        self.road_count = 0
        self.last_road = None
        self.lane_pending = None
        self.lane_count = 0
        self.last_model_mono = None

    def load_world(self, now, wall):
        if now-self.world_checked_at < 1.:
            return self.world
        self.world_checked_at = now
        self.world = None
        try:
            with self.geometry_path.open('rb') as f:
                raw = f.read(WORLD_READ_LIMIT+1)
            if len(raw) > WORLD_READ_LIMIT:
                return None
            world = json.loads(raw)
            stamp = world.get('exported_at')
            if (not finite(stamp) or not 0 <= wall-stamp <= 15
                    or world.get('coordinate_system') != 'enu_m'):
                return None
            self.world = world
        except (OSError, ValueError, TypeError, AttributeError):
            return None
        return self.world

    def update(self, now, tesla_sample, observer, position_observation,
               model_snapshot, wall=None):
        wall = time.time() if wall is None else wall
        result = {'mode': 'unified_nav_shadow_v1', 'used_for_control': False,
                  'used_for_guidance': False, 'map_control_authorized': False,
                  'received_mono': now, 'source_policy': 'tesla_first_comma_observer_fallback',
                  'road': {'state':'unknown'}, 'lane': {'state':'unknown'}}
        pose = tesla_motion_pose(tesla_sample, observer, now)
        if not pose['valid']:
            result['tesla_rejection'] = pose['reason']
            previous = position_observation
            if (isinstance(previous, dict) and previous.get('valid') is True
                    and finite(previous.get('received_mono'))
                    and 0 <= now-previous['received_mono'] <= .5
                    and isinstance(previous.get('point'), list)
                    and len(previous['point']) == 2
                    and all(finite(x) for x in previous['point'])
                    and finite(previous.get('heading_deg'))):
                pose = {'valid': True,'source':'existing_position_observer_fallback',
                        'point': previous['point'],
                        'heading_deg':previous['heading_deg'],
                        'reported_accuracy_m': None,
                        'accuracy_note':'conservative_observer_budget_not_receiver_accuracy',
                        'observer_error_budget_m':previous.get('error_budget_m')}
        result['pose'] = pose
        if not pose['valid']:
            result['reason'] = 'position_unavailable'
            self.road_pending,self.road_count = None,0
            self.lane_pending,self.lane_count = None,0
            return result
        world = self.load_world(now,wall)
        if not world:
            result['road'] = {'state':'unavailable','reason':'osm_world_cache_stale_or_missing'}
            return result
        choices = road_candidates(pose['point'],pose['heading_deg'],world)
        result['road_candidates'] = choices
        if not choices:
            self.road_pending,self.road_count=None,0
            result['road'] = {'state':'unknown','reason':'no_nearby_direction_compatible_road'}
            return result
        first = choices[0]
        separation = choices[1]['score']-first['score'] if len(choices)>1 else None
        is_ambiguous = (separation is not None and separation < 6.) or first['distance_m'] > 20
        if is_ambiguous:
            self.road_pending,self.road_count=None,0
            result['road'] = {'state':'ambiguous','candidate':first,
                              'score_margin_m': round(separation,2) if separation is not None else None}
            return result
        ident = first['way_id'] or first['id']
        if ident == self.road_pending:
            self.road_count += 1
        else:
            self.road_pending,self.road_count=ident,1
            self.lane_pending,self.lane_count=None,0
        if self.road_count < 3:
            result['road'] = {'state':'candidate','candidate':first,'observations':self.road_count}
            return result
        self.last_road=ident
        result['road']={'state':'stable_geometry_match','candidate':first,'observations':self.road_count,
                        'score_margin_m':round(separation,2) if separation is not None else None,
                        'confidence_kind':'relative_geometry_not_absolute_accuracy'}
        lane=vision_lane(model_snapshot,first)
        if lane.get('state') != 'candidate':
            self.lane_pending,self.lane_count=None,0
            result['lane']=lane
            return result
        candidate_key=(ident,lane['lane_index_from_left'],lane['lane_count'])
        mono=model_snapshot.get('mono')
        if not finite(mono) or mono == self.last_model_mono:
            result['lane']={'state':'candidate','reason':'waiting_for_new_camera_frame',**{
                k:v for k,v in lane.items() if k!='state'}}
            return result
        self.last_model_mono=mono
        if candidate_key==self.lane_pending:
            self.lane_count+=1
        else:
            self.lane_pending,self.lane_count=candidate_key,1
        if self.lane_count < 3:
            lane['state']='candidate'
            lane['observations']=self.lane_count
        else:
            lane['state']='estimated'
            lane['observations']=self.lane_count
            lane['confidence_kind']='camera_relative_plus_osm_count_not_surveyed'
        result['lane']=lane
        return result
