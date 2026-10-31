"""Configuration routing through the actual control loop; all robot I/O mocked."""
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict, fields
from io import StringIO
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import numpy as np

from g1d_pico import __main__ as app
from g1d_pico.arm_config import ArmConfig
from g1d_pico.arms import ArmSession
from g1d_pico.body_config import BodyConfig
from g1d_pico.input import PoseSample, StickSample

ROOT = Path(__file__).resolve().parents[1]


class BodyConfigTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'my body.jsonc'

    def write(self, values):
        self.path.write_text('// custom operator settings\n' + json.dumps(values))
        return self.path

    def test_body_and_arms_have_disjoint_fields(self):
        arm = ArmConfig.load(ROOT / 'config/arms.jsonc')
        body = BodyConfig.load(ROOT / 'config/body.jsonc')
        self.assertFalse(set(asdict(arm)) & set(asdict(body)))

    def test_invalid_values_and_existing_caps_rejected(self):
        for field in fields(BodyConfig):
            for value in (True, None, '0.1', float('nan'), float('inf')):
                with self.subTest(field=field.name, value=value), self.assertRaises(ValueError):
                    BodyConfig(**{field.name: value})
        for values in ({'max_forward_mps': 1.51}, {'max_yaw_rps': .61},
                       {'max_lift_normalized': 1.01}, {'deadzone': 1}, {'deadzone': -.1},
                       {'max_forward_mps': -.01}, {'height_min_m': -.1}, {'height_max_m': 0},
                       {'height_min_m': .5}, {'height_max_m': 3.1},
                       {'waist_yaw_speed_rad_s': .3}, {'waist_yaw_limit_rad': 2.71},
                       {'waist_yaw_accel_rad_s2': 1}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                BodyConfig(**values)

    def test_typos_and_old_mixed_arm_file_fail_with_migration_hint(self):
        for values in ([], {'max_forward_typo': .08}, {'joint_speed_rad_s': .2}):
            with self.assertRaises(ValueError): BodyConfig.load(self.write(values))
        with self.assertRaisesRegex(ValueError, 'body.jsonc'):
            ArmConfig.load(self.write({'waist_yaw_speed_rad_s': .06}))

    def test_file_is_the_only_runtime_tuning_source(self):
        self.write({'max_forward_mps': .06, 'max_yaw_rps': .09,
                    'max_lift_normalized': .07, 'deadzone': .2,
                    'height_min_m': .02, 'height_max_m': .35})
        with patch('sys.argv', ['app', '--live', '--with-arms']), \
             patch.object(app, 'BODY_CONFIG', self.path):
            args = app.parse_args()
        expected = BodyConfig.load(self.path)
        self.assertEqual(args.body_settings, expected)
        self.assertEqual((args.height_min_m, args.height_max_m), (.02, .35))
        self.assertEqual(BodyConfig.load(self.path), expected)

    def test_invalid_or_missing_fixed_file_fails_before_robot_creation(self):
        for values in (None, {'max_forward_mps': 1.6}, {'height_min_m': 1}):
            if values is not None:
                self.write(values)
            with patch('sys.argv', ['app']), patch.object(app, 'BODY_CONFIG', self.path), \
                 patch.object(app, 'AgvBridge') as bridge, \
                 patch('g1d_pico.input.FreshTeleVuer') as viewer, redirect_stderr(StringIO()):
                with self.assertRaises(SystemExit): app._run_control()
                bridge.assert_not_called()
                viewer.assert_not_called()

    def test_removed_overrides_fail_before_robot_creation(self):
        for option in ('--body-config', '--arm-config', '--config', '--max-forward-mps',
                       '--max-yaw-rps', '--max-lift', '--deadzone',
                       '--height-min-m', '--height-max-m'):
            with self.subTest(option=option), patch('sys.argv', ['app', option, '0.1']), \
                 patch.object(app, 'AgvBridge') as bridge, \
                 patch('g1d_pico.input.FreshTeleVuer') as viewer, redirect_stderr(StringIO()):
                with self.assertRaises(SystemExit): app._run_control()
                bridge.assert_not_called()
                viewer.assert_not_called()

    def test_arm_session_forwards_body_limits_separately_from_arm_speed(self):
        body = BodyConfig(waist_yaw_limit_rad=.3, waist_yaw_speed_rad_s=.06,
                          waist_yaw_accel_rad_s2=.12, waist_pitch_min_rad=-.01,
                          waist_pitch_max_rad=.4, waist_pitch_speed_rad_s=.04,
                          waist_pitch_accel_rad_s2=.08, waist_pitch_max_error_rad=.12,
                          waist_pitch_stall_timeout_s=2, waist_pitch_min_progress_rad=.006)
        arm = ArmConfig(joint_speed_rad_s=.4)
        with patch('g1d_pico.arms.ArmBridge') as constructor, patch('g1d_pico.arms.DualArmIK'):
            ArmSession(Path('unused'), 'eth0', Path('unused'), config=arm, body_config=body)
        self.assertAlmostEqual(constructor.call_args.args[2], .48)
        for key, value in asdict(body).items():
            if key.startswith('waist_'):
                self.assertEqual(constructor.call_args.kwargs[key.removeprefix('waist_')], value)

    def test_runtime_uses_body_settings_for_base_lift_yaw_pitch_and_recorded_values(self):
        self.write({'max_forward_mps': 1.2, 'max_yaw_rps': .4,
                    'max_lift_normalized': .8, 'deadzone': .25,
                    'height_min_m': .02, 'height_max_m': .35,
                    'waist_yaw_speed_rad_s': .06, 'waist_pitch_speed_rad_s': .04})
        now = time.monotonic()
        viewer, arms, base = Mock(), Mock(), Mock()
        arms.home_complete = True
        # A at 0.2 is neutral only with the configured 0.25 deadzone.
        rows = [(0.2, 0.2, 0.2, 0.2, True, False),
                (1, -1, 1, -1, False, False),
                (0, 0, 0, 0, False, False),
                (0, 0, 0, 0, False, False),
                (0, 0, 1, -1, False, False)]
        viewer.read_sticks.side_effect = [StickSample(*row, received_at=now, mode_button=(i == 2))
                                          for i, row in enumerate(rows)] + [KeyboardInterrupt()]
        eye = np.eye(4)
        viewer.read_poses.return_value = PoseSample(eye, eye, eye, (False, False), now, now)
        ticks = []
        def capture(_):
            ticks.append((base.send.call_args.args[0], arms.step.call_args.kwargs.copy()))
        with patch('sys.argv', ['app', '--no-camera', '--live', '--with-arms', '--skip-arm-home']), \
             patch.object(app, 'BODY_CONFIG', self.path), \
             patch('g1d_pico.input.FreshTeleVuer', return_value=viewer), \
             patch('g1d_pico.arms.ArmSession', return_value=arms) as session, \
             patch.object(app, 'AgvBridge', return_value=base) as bridge, \
             patch.object(app.time, 'sleep', side_effect=capture), \
             patch.object(app, 'emit') as telemetry, redirect_stdout(StringIO()):
            app._run_control()
        arms.start.assert_called_once()
        self.assertEqual(bridge.call_args.args[2:5], (.02, .35, 'both'))
        self.assertEqual(ticks[0][0].forward_mps, 0)
        self.assertEqual(ticks[1][0].forward_mps, 1.2)  # Fixed file value.
        self.assertEqual(ticks[1][0].yaw_rps, -.4)
        self.assertEqual(ticks[1][0].lift_normalized, .8)
        self.assertEqual(ticks[1][1]['waist_rate'], -.06)
        self.assertEqual(ticks[4][0].lift_normalized, 0)
        self.assertEqual(ticks[4][1]['pitch_rate'], .04)
        effective = session.call_args.kwargs['body_config']
        event = next(call.args[1] for call in telemetry.call_args_list if call.args[0] == 'control_start')
        self.assertEqual(event['body_config'], asdict(effective))
        self.assertEqual(event['body_config_path'], str(self.path))
        self.assertNotIn('waist_yaw_speed_rad_s', event['arm_config'])


if __name__ == '__main__':
    unittest.main()
