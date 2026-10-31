import unittest

from g1d_pico.control import MappingConfig, MotionCommand, map_sticks, sticks_neutral, waist_yaw_rate
from g1d_pico.input import StickSample


class JoystickControlTests(unittest.TestCase):
    def test_direction_and_unused_right_x(self):
        self.assertEqual(map_sticks(0, -1, 1, -1), MotionCommand(0.10, 0.0, 0.15))
        self.assertEqual(map_sticks(1, 0, 0, 1), MotionCommand(0.0, -0.15, -0.15))

    def test_deadzone_and_neutral_start(self):
        self.assertEqual(map_sticks(0.1, -0.1, 0, 0.1), MotionCommand())
        self.assertTrue(sticks_neutral(0.1, -0.1, 0.1, 0.15))
        self.assertFalse(sticks_neutral(0.2, 0, 0, 0.15))

    def test_right_stick_yaw_and_lift_are_independent(self):
        self.assertEqual(waist_yaw_rate(1., 0.1), -0.1)
        self.assertEqual(waist_yaw_rate(-1., 0.1), 0.1)
        self.assertEqual(waist_yaw_rate(0.1, 0.1), 0.)
        self.assertEqual(map_sticks(0,0,1,-1).lift_normalized, 0.15)
        self.assertFalse(sticks_neutral(0,0,0,0.15,right_x=1.))
        self.assertFalse(sticks_neutral(0,0,0,0.15,right_x=float('nan')))
        with self.assertRaises(ValueError):
            waist_yaw_rate(float('nan'), 0.1)

    def test_out_of_range_data_fails_closed(self):
        for bad in (float("nan"), float("inf"), 1.5):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                map_sticks(bad, 0, 0, 0)

    def test_expired_controller_sample_is_not_fresh(self):
        sample = StickSample(0, -1, 0, 0, False, False, received_at=10.0)
        self.assertTrue(sample.is_fresh(10.1, timeout_s=0.25))
        self.assertFalse(sample.is_fresh(10.4, timeout_s=0.25))

    def test_mapping_rejects_unsafe_limits(self):
        with self.assertRaises(ValueError):
            MappingConfig(max_forward_mps=1.6)


if __name__ == "__main__":
    unittest.main()
