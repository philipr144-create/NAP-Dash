"""Synthetic unified-navigation tests: GPS, yaw, OSM, lane geometry.

No car access, vehicle control, network, or installed openpilot dependencies.
"""
import json
import math
import tempfile
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace

from phone_navigation.position_observer import PositionObserver
from phone_navigation.unified_nav_shadow import (
    UnifiedNavigationShadow, road_candidates, tesla_motion_pose, vision_lane,
)
from phone_navigation.vehicle_snapshot import _model_snapshot


def world(exported=1000., parallel=False, two_way=False, lanes=3):
    roads = [{
        'id':'eastbound:0','way_id':'eastbound','name':'Sample Road',
        'highway':'primary','points':[[-100,0],[100,0]],'oneway':not two_way,
        'oneway_reverse':False,'lanes_mapped':lanes,'width_m':10.5,
        'width_source':'estimated_from_osm_lane_count'
    }]
    if parallel:
        roads.append({
            'id':'other:0','way_id':'other','name':'Parallel Road','highway':'primary',
            'points':[[-100,4],[100,4]],'oneway':True,
            'oneway_reverse':False,'lanes_mapped':2, 'width_m':7.
        })
    return {'version':1,'coordinate_system':'enu_m',
            'origin':{'lat':37.8,'lon':-92.},'radius_m':200,
            'exported_at':exported,'roads':roads}


def model(mono=1., width=10.2, std=.2, p=.95):
    return {'mono':mono,'valid':True,
            'lane_lines':[{'idx':1,'y_m':-1.7,'prob':p},
                          {'idx':2,'y_m':1.7,'prob':p}],
            'road_edges':[{'idx':0,'y_m':-width/2,'std_m':std},
                          {'idx':1,'y_m':width/2,'std_m':std}]}


def fallback(now):
    return {'valid':True,'received_mono':now,'point':[-92.,37.8],
            'heading_deg':90.,'error_budget_m':15.}


def shadow_samples(path, samples=6, model_provider=None, now_wall=1000.):
    observer = UnifiedNavigationShadow(path)
    results=[]
    for i in range(samples):
        now=float(i+1)
        m=model(now) if model_provider is None else model_provider(now)
        r=observer.update(now,None,None,fallback(now),m,wall=now_wall)
        results.append(r)
    return results


class UnifiedMapTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'nap_world_geometry.json'

    def write(self, **kwargs):
        self.path.write_text(json.dumps(world(**kwargs)))

    def test_stable_road_and_second_lane_after_distinct_frames(self):
        self.write()
        results=shadow_samples(self.path)
        self.assertEqual(results[0]['road']['state'],'candidate')
        self.assertEqual(results[1]['road']['state'],'candidate')
        self.assertEqual(results[2]['road']['state'],'stable_geometry_match')
        self.assertEqual(results[-1]['lane']['state'],'estimated')
        self.assertEqual(results[-1]['lane']['lane_count'],3)
        self.assertEqual(results[-1]['lane']['lane_index_from_left'],2)
        self.assertEqual(results[-1]['display_pose']['state'], 'illustrative_lane_center_alignment')
        self.assertFalse(results[-1]['display_pose']['used_for_control'])
        self.assertEqual(results[-1]['pose']['source'], 'existing_position_observer_fallback')
        self.assertFalse(results[-1]['map_control_authorized'])
        self.assertFalse(results[-1]['used_for_guidance'])
        self.assertFalse(results[-1]['used_for_control'])

    def test_parallel_roads_are_ambiguous(self):
        self.write(parallel=True)
        results=shadow_samples(self.path)
        self.assertEqual(results[-1]['road']['state'],'ambiguous')
        self.assertEqual(results[-1]['lane']['state'],'unknown')

    def test_two_way_osm_count_cannot_identify_one_lane(self):
        self.write(two_way=True)
        result=shadow_samples(self.path)[-1]
        self.assertEqual(result['road']['state'],'stable_geometry_match')
        self.assertEqual(result['lane']['state'],'unknown')
        self.assertEqual(result['lane']['reason'],'road_direction_unknown_or_two_way')

    def test_osm_missing_lane_count(self):
        self.write(lanes=None)
        result=shadow_samples(self.path)[-1]
        self.assertEqual(result['lane']['reason'],'osm_lane_count_missing')

    def test_vision_uncertain_edges_suppress_lane_number(self):
        self.write()
        result=shadow_samples(self.path,model_provider=lambda now:model(now,std=2.5))[-1]
        self.assertEqual(result['lane']['reason'],'road_edges_uncertain')

    def test_vision_low_probability_suppresses_lane_number(self):
        self.write()
        result=shadow_samples(self.path,model_provider=lambda now:model(now,p=.1))[-1]
        self.assertEqual(result['lane']['reason'],'ego_lane_lines_low_quality')

    def test_visual_width_must_reconcile_with_osm_lane_count(self):
        self.write()
        result=shadow_samples(self.path,model_provider=lambda now:model(now,width=20.))[-1]
        self.assertEqual(result['lane']['reason'],'osm_camera_width_disagreement')

    def test_missing_camera_returns_unknown(self):
        self.write()
        result=shadow_samples(self.path,model_provider=lambda now:None)[-1]
        self.assertEqual(result['lane']['reason'],'vision_stale_or_unavailable')

    def test_stale_world_cache_rejected(self):
        self.write(exported=950.)
        result=shadow_samples(self.path)[-1]
        self.assertEqual(result['road']['state'],'unavailable')
        self.assertEqual(result['lane']['state'],'unknown')

    def test_missing_world_does_not_emit_road_match(self):
        result=shadow_samples(self.path)[-1]
        self.assertEqual(result['road']['state'],'unavailable')

    def test_no_position_does_not_guess_a_road(self):
        self.write()
        u=UnifiedNavigationShadow(self.path)
        r=u.update(1.,None,None,{},model(1.),wall=1000.)
        self.assertEqual(r['reason'],'position_unavailable')
        self.assertEqual(r['lane']['state'],'unknown')

    def test_opposite_heading_rejects_oneway_road(self):
        w=world()
        self.assertEqual(road_candidates([-92,37.8],270,w),[])

    def test_matching_heading_finds_oneway_road(self):
        matches=road_candidates([-92,37.8],90.,world())
        self.assertEqual(len(matches),1)
        self.assertEqual(matches[0]['way_id'],'eastbound')


class TeslaPoseTests(unittest.TestCase):
    def history(self):
        p=PositionObserver()
        # Observer rejects motion gaps above 450 ms, so reproduce normal
        # high-frequency vehicle odometry rather than discontinuous 500 ms ticks.
        p.history=deque([[i*.25,0.,i*5.,i*5.,0.,20.,.02]
                         for i in range(7)],maxlen=64)
        return p

    def sample(self, pos_stamp=.5, heading_stamp=1.):
        return ({'point':[-92.,37.8],'mono':pos_stamp,'accuracy_m':2.},
                {'mono':heading_stamp,'speed_mps':20.,'heading_deg':0.})

    def test_1hz_can_position_is_interpolated_with_vehicle_odometry(self):
        r=tesla_motion_pose(self.sample(),self.history(),1.5)
        self.assertTrue(r['valid'],r)
        self.assertEqual(r['source'],'tesla_can_odometry_aligned')
        self.assertAlmostEqual(r['distance_predicted_m'],20.,places=2)
        self.assertAlmostEqual(r['position_heading_can_skew_s'],.5,places=3)
        self.assertGreater(r['point'][1],37.8)

    def test_old_heading_is_corrected_by_new_yaw(self):
        p=self.history()
        p.history[4][4]=.1
        p.history[6][4]=.2
        sample=self.sample()
        sample[1]['heading_deg']=45.
        r=tesla_motion_pose(sample,p,1.5)
        self.assertTrue(r['valid'],r)
        self.assertGreater(r['heading_deg'],50)
        self.assertLess(r['heading_deg'],51)

    def test_650ms_can_stagger_is_aligned_not_discarded(self):
        r=tesla_motion_pose(self.sample(pos_stamp=.5,heading_stamp=1.15),
                            self.history(),1.5)
        self.assertTrue(r['valid'],r)
        self.assertAlmostEqual(r['position_heading_can_skew_s'],.65,places=2)
        self.assertEqual(r['heading_deg'],0)

    def test_tesla_position_heading_skew_fails_closed(self):
        r=tesla_motion_pose(self.sample(pos_stamp=0.,heading_stamp=1.5),self.history(),1.5)
        self.assertFalse(r['valid'])
        self.assertEqual(r['reason'],'tesla_can_clock_skew')

    def test_no_motion_history_does_not_fake_smooth_position(self):
        r=tesla_motion_pose(self.sample(),None,1.5)
        self.assertFalse(r['valid'])


class OpenPilotSampleTests(unittest.TestCase):
    def test_camera_y_interpolated_at_ten_metres(self):
        def ln(y):
            return SimpleNamespace(x=[0.,20.], y=[y,y])
        m=SimpleNamespace(laneLines=[ln(-3.5),ln(-1.7),ln(1.7),ln(5.2)],
                          laneLineProbs=[.2,.9,.9,.1],
                          roadEdges=[ln(-5.1),ln(5.1)],
                          roadEdgeStds=[.3,.3])
        sample=_model_snapshot(m,12.)
        self.assertTrue(sample['valid'])
        self.assertEqual(sample['mono'],12.)
        self.assertEqual(sample['lane_lines'][1]['y_m'],-1.7)
        self.assertEqual(sample['road_edges'][1]['y_m'],5.1)

    def test_no_vision_index_when_line_incomplete(self):
        wrong={'mono':10.,'valid':True,'lane_lines':[],
               'road_edges':[{'idx':0,'y_m':-5,'std_m':.1},
                             {'idx':1,'y_m':5,'std_m':.1}]}
        result=vision_lane(wrong,road_candidates([-92.,37.8],90.,world())[0])
        self.assertEqual(result['reason'],'ego_lane_lines_missing')


if __name__ == '__main__':
    unittest.main()
