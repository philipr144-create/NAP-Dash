import unittest
from phone_navigation.shadow_localization import evaluate, project, ShadowSession

class Route:
    points = [([-92.201, 37.8], 0.), ([-92.199, 37.8], 180.)]

def fixes():
    comma = {'point': [-92.2005, 37.8], 'mono': 9.9, 'speed': 20.,
             'bearing_deg': 90., 'accuracy_m': 15.}
    tesla = ({'point': [-92.2004, 37.8], 'mono': 9.9},
             {'mono': 9.9, 'speed_mps': 20., 'heading_deg': 90.})
    return comma, tesla

class ShadowTests(unittest.TestCase):
    def test_consistent_candidates_have_no_authority(self):
        comma, tesla = fixes()
        result = evaluate(10., comma, tesla, 20., Route(), 45.)
        self.assertEqual(result['result'], 'route_candidates_consistent')
        self.assertFalse(result['map_control_authorized'])
        self.assertFalse(result['used_for_guidance'])
        self.assertLess(result['projected_separation_m'], 10)

    def test_gps_conflict_is_detected(self):
        comma, tesla = fixes()
        tesla[0]['point'] = [-92.2001, 37.8]
        comma['accuracy_m'] = 8.
        result = evaluate(10., comma, tesla, 20., Route(), 45.)
        self.assertEqual(result['result'], 'receiver_conflict')
        self.assertFalse(result['map_control_authorized'])

    def test_missing_tesla_is_insufficient(self):
        comma, _ = fixes()
        result = evaluate(10., comma, None, 20., Route(), 45.)
        self.assertEqual(result['result'], 'insufficient_evidence')

    def test_old_fix_is_not_projected_indefinitely(self):
        comma, _ = fixes()
        comma['mono'] = 8.1
        r = project(10., comma, 20.)
        self.assertFalse(r['valid'])
        self.assertEqual(r['reason'], 'stale_or_future')

    def test_late_fix_motion_projection(self):
        comma, _ = fixes()
        r = project(10., comma, 20.)
        self.assertTrue(r['valid'])
        self.assertAlmostEqual(r['travel_projected_m'], 2., places=1)
        self.assertGreater(r['point'][0], comma['point'][0])

    def test_speed_disagreement(self):
        comma, tesla = fixes()
        r = evaluate(10., comma, tesla, 30.)
        self.assertFalse(r['comma']['valid'])
        self.assertEqual(r['result'], 'insufficient_evidence')

    def test_timestamp_skew(self):
        comma, tesla = fixes()
        tesla[1]['mono'] = 9.
        r = evaluate(10., comma, tesla, 20.)
        self.assertEqual(r['tesla']['reason'], 'position_motion_timestamp_skew')
        self.assertFalse(r['tesla']['valid'])

    def test_pair_is_counted_only_once(self):
        comma, tesla = fixes()
        r = evaluate(10., comma, tesla, 20., Route(), 45.)
        stats = ShadowSession()
        stats.update(r)
        stats.update(r)
        self.assertEqual(stats.counts['route_candidates_consistent'], 1)

    def test_no_route_is_not_called_verified(self):
        comma, tesla = fixes()
        r = evaluate(10., comma, tesla, 20.)
        self.assertEqual(r['result'], 'receiver_consistent_route_unverified')

if __name__ == '__main__':
    unittest.main()
