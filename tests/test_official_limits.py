"""Cross-language bounds and startup contract regressions, without robot I/O."""
import json
import io
from contextlib import redirect_stderr
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

from g1d_pico.arm_bridge import ArmBridge
from g1d_pico.__main__ import parse_args
from g1d_pico.body_config import BodyConfig
from g1d_pico.bridge import AgvBridge
from g1d_pico.bridge_contract import require_current_limits
from g1d_pico.control import MappingConfig, MotionCommand, map_sticks
from g1d_pico.limits import BRIDGE_LIMITS, valid_height_window
from g1d_pico.recording.runner import load_settings

ROOT = Path(__file__).resolve().parents[1]


class OfficialLimitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.directory.cleanup)
        cls.binary = Path(cls.directory.name) / 'offline_limits'
        subprocess.run(['c++', '-std=c++17', '-O2', str(ROOT/'tests/agv_limits_test.cpp'),
                        '-o', str(cls.binary)], check=True, capture_output=True)

    def test_cpp_contract_equals_python_and_cpp_boundary_regressions_pass(self):
        require_current_limits(self.binary, None)
        subprocess.run([str(self.binary)], check=True, timeout=5)

    def test_cli_defaults_to_pc2_local_control_without_arming(self):
        with patch('sys.argv', ['g1d_pico']):
            args = parse_args()
        self.assertEqual(args.bridge_bin, ROOT/'bridge/build-arm64/g1d_agv_bridge')
        self.assertEqual(args.network_interface, 'eth0')
        self.assertEqual(args.robot_ip, '127.0.0.1')
        self.assertFalse(args.live)

    def test_retired_remote_control_option_is_rejected(self):
        error = io.StringIO()
        with patch('sys.argv', ['g1d_pico', '--live', '--remote-bridge-port', '8765']), \
                redirect_stderr(error), self.assertRaises(SystemExit) as raised:
            parse_args()
        self.assertEqual(raised.exception.code, 2)
        self.assertIn('unrecognized arguments', error.getvalue())

    def test_full_sdk_endpoints_survive_mapping_and_real_cpp_parser(self):
        settings = BodyConfig(max_forward_mps=1.5, max_yaw_rps=.6, max_lift_normalized=1)
        for sign in (-1, 1):
            mapped = map_sticks(-sign, -sign, 0, -sign, settings)
            self.assertEqual(mapped, MotionCommand(sign*1.5, sign*.6, sign*1.))
            values = [str(mapped.forward_mps), str(mapped.yaw_rps), str(mapped.lift_normalized)]
            result = subprocess.run([str(self.binary), '--command', *values],
                                    check=True, text=True, capture_output=True)
            self.assertEqual(list(map(float, result.stdout.split())), list(map(float, values)))

    def test_zero_amplitudes_disable_outputs_without_invalidating_configuration(self):
        settings = MappingConfig(max_forward_mps=0, max_yaw_rps=0, max_lift_normalized=0)
        self.assertEqual(map_sticks(1, 1, 1, 1, settings), MotionCommand())

    def test_official_endpoints_accepted_outside_and_nonfinite_values_rejected(self):
        for field, cap in [('max_forward_mps', 1.5), ('max_yaw_rps', .6),
                           ('max_lift_normalized', 1), ('waist_yaw_limit_rad', 2.7053)]:
            BodyConfig(**{field: cap})
            for bad in (cap+1e-5, -.001, float('nan'), float('inf'), True, '1'):
                with self.subTest(field=field, value=bad), self.assertRaises(ValueError):
                    BodyConfig(**{field: bad})
        BodyConfig(waist_pitch_min_rad=-.04363, waist_pitch_max_rad=2.3562)
        for low, high in [(-.04364, 2), (0, 2.35621), (.1, .1), (.2, .1)]:
            with self.assertRaises(ValueError):
                BodyConfig(waist_pitch_min_rad=low, waist_pitch_max_rad=high)

    def test_height_window_has_travel_bound(self):
        for low, high in [(0,.42), (.05,.47)]:
            BodyConfig(height_min_m=low, height_max_m=high)
            self.assertTrue(valid_height_window(low, high))
        for low, high in [(0,.421), (.5,.4), (-.1,.3), (0,float('inf'))]:
            self.assertFalse(valid_height_window(low,high))
            with self.assertRaises(ValueError):
                BodyConfig(height_min_m=low, height_max_m=high)

    def test_checked_waist_positions_match_bundled_official_model(self):
        joints = {j.attrib['name']: j.find('limit').attrib
                  for j in ET.parse(ROOT/'assets/g1d/g1_d.urdf').getroot().findall('joint')
                  if j.find('limit') is not None}
        self.assertEqual(float(joints['torso_Joint']['upper']), BRIDGE_LIMITS['waist_yaw_rad'])
        self.assertEqual(float(joints['torso_Joint']['lower']), -BRIDGE_LIMITS['waist_yaw_rad'])
        self.assertEqual(float(joints['Yaw_Joint']['lower']), BRIDGE_LIMITS['waist_pitch_min_rad'])
        self.assertEqual(float(joints['Yaw_Joint']['upper']), BRIDGE_LIMITS['waist_pitch_max_rad'])

    def test_incompatible_or_unreadable_binary_fails_before_control_popen(self):
        for stdout in ('{}', 'not-json', json.dumps({**BRIDGE_LIMITS, 'agv_forward_mps': .1})):
            with patch('g1d_pico.bridge_contract.subprocess.run', return_value=Mock(stdout=stdout)):
                with self.assertRaisesRegex(RuntimeError, 'rebuild'):
                    require_current_limits(self.binary, {})
        for constructor, args in [(AgvBridge, (self.binary, 'eth0', 0, .42, 'both')),
                                  (ArmBridge, (self.binary, 'eth0'))]:
            with patch('g1d_pico.bridge_contract.subprocess.run',
                       side_effect=subprocess.CalledProcessError(2, 'old_bridge')), \
                 patch('subprocess.Popen') as popen:
                with self.assertRaisesRegex(RuntimeError, 'rebuild'): constructor(*args)
                popen.assert_not_called()

    def test_recording_paths_are_validated_and_all_config_values_still_load(self):
        path = ROOT/'config/recording.jsonc'
        settings = load_settings(path)
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory)/'recording.jsonc'
            for bad in ('', '   ', None, 12, []):
                config.write_text(json.dumps({**settings, 'spool': bad}))
                with self.assertRaises(ValueError): load_settings(config)
        BodyConfig.load(ROOT/'config/body.jsonc')


if __name__ == '__main__':
    unittest.main()
