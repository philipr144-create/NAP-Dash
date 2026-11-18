"""Shadow-only diagnostic tests. No vehicle, CAN, GPS, or control access."""
import unittest
from unittest.mock import patch

from phone_navigation.tesla_gps_assist import diagnostic_status


class DualGpsDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.position = {'point': [-92.2, 37.8], 'mono': 99.9}
        self.motion = {'mono': 99.9, 'hdop': 1.2, 'speed_mps': 20.0,
                       'heading_deg': 90.0}
        self.comma = {'point': [-92.1999, 37.8], 'mono': 99.9, 'speed': 20.0,
                      'bearing_deg': 90.0, 'accuracy_m': 15.0}

    def inspect(self, enabled=True, wheel_speed=20.0, comma=True, sample=True):
        with patch('phone_navigation.tesla_gps_assist.enabled', return_value=enabled), \
             patch('phone_navigation.tesla_gps_assist.read_fix',
                   return_value=(self.position, self.motion) if sample else None):
            return diagnostic_status(self.now, self.comma if comma else None, wheel_speed)

    def test_two_receivers_are_compared_without_authorizing_controls(self):
        result = self.inspect()
        self.assertTrue(result['tesla_snapshot_valid'])
        self.assertTrue(result['compared'])
        self.assertEqual(result['selection_reason'], 'eligible_for_legacy_source_selection')
        self.assertGreater(result['position_separation_m'], 8)
        self.assertLess(result['position_separation_m'], 10)
        self.assertEqual(result['receiver_heading_difference_deg'], 0)
        self.assertEqual(result['receiver_speed_difference_mps'], 0)
        self.assertFalse(result['used_for_control'])
        self.assertEqual(result['mode'], 'shadow')

    def test_disabled_toggle_does_not_hide_receiver_comparison(self):
        result = self.inspect(enabled=False)
        self.assertFalse(result['assist_enabled'])
        self.assertEqual(result['selection_reason'], 'assist_disabled')
        self.assertTrue(result['compared'])

    def test_wheel_speed_gates_reported_without_changing_selection(self):
        self.assertEqual(self.inspect(wheel_speed=None)['selection_reason'],
                         'wheel_speed_unavailable')
        self.assertEqual(self.inspect(wheel_speed=15)['selection_reason'],
                         'tesla_wheel_speed_disagreement')

    def test_tesla_snapshot_missing_and_comma_missing(self):
        missing = self.inspect(sample=False)
        self.assertFalse(missing['tesla_snapshot_valid'])
        self.assertFalse(missing['compared'])
        no_comma = self.inspect(comma=False)
        self.assertTrue(no_comma['tesla_snapshot_valid'])
        self.assertEqual(no_comma['comparison_reason'], 'comma_fix_unavailable')
        self.assertFalse(no_comma['compared'])

    def test_stale_comma_fix_cannot_be_reported_as_agreement(self):
        self.comma['mono'] = 95.0
        result = self.inspect()
        self.assertFalse(result['compared'])
        self.assertEqual(result['comparison_reason'], 'comma_fix_invalid_or_stale')

    def test_tesla_position_motion_timestamp_skew(self):
        self.motion['mono'] = 98.7
        result = self.inspect()
        self.assertEqual(result['selection_reason'], 'tesla_position_motion_skew')


if __name__ == '__main__':
    unittest.main()
