"""Offline application/transport/launcher checks. Robot clients are all mocked."""
from pathlib import Path
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
import numpy as np
import pinocchio as pin
from g1d_pico import __main__ as app
from g1d_pico.arm_bridge import parse_arm_state
from g1d_pico.arms import ArmSession
from g1d_pico.body_config import BodyConfig
from g1d_pico.input import PoseSample, StickSample

ROOT=Path(__file__).resolve().parents[1]

def pose(timestamp):
    eye=np.eye(4)
    return PoseSample(eye,eye,eye,(False,False),timestamp,timestamp)

class TorsoLiftTests(unittest.TestCase):
    def run_application(self, sticks, *, lift=True):
        now=time.monotonic()
        viewer,arms,base=Mock(),Mock(),Mock()
        arms.home_complete=True
        viewer.read_sticks.side_effect=[StickSample(*row,received_at=now) for row in sticks]+[KeyboardInterrupt()]
        viewer.read_poses.return_value=pose(now)
        argv=['app','--no-camera','--live','--with-arms','--skip-arm-home']
        if not lift: argv += ['--base-only']
        with patch.object(app.BodyConfig, 'load', return_value=BodyConfig()), \
             patch('sys.argv',argv), patch('g1d_pico.input.FreshTeleVuer',return_value=viewer), \
             patch('g1d_pico.arms.ArmSession',return_value=arms), \
             patch.object(app,'AgvBridge',return_value=base), patch.object(app.time,'sleep'), \
             redirect_stdout(StringIO()):
            app.main()
        return arms,base

    def test_right_stick_can_turn_waist_and_lift_in_same_cycle_and_b_stops_both(self):
        arms,base=self.run_application([(0,0,0,0,True,False),
                                       (0,-1,1,-1,False,False),
                                       (0,0,1,-1,False,True)])
        commands=[c.args[0] for c in base.send.call_args_list]
        self.assertEqual(commands[1].forward_mps,0.1)
        self.assertEqual(commands[1].lift_normalized,0.15)
        self.assertEqual(arms.step.call_args_list[1].kwargs['waist_rate'],-0.1)
        self.assertEqual(commands[2].lift_normalized,0)
        self.assertEqual(commands[2].forward_mps,0)
        self.assertEqual(arms.step.call_args_list[2].kwargs['waist_rate'],0)
        arms.stop.assert_called_once()

    def test_base_only_disables_lift_but_keeps_waist(self):
        arms,base=self.run_application([(0,0,0,0,True,False),
                                       (0,0,-1,-1,False,False)],lift=False)
        self.assertEqual(base.send.call_args_list[1].args[0].lift_normalized,0)
        self.assertEqual(arms.step.call_args_list[1].kwargs['waist_rate'],0.1)

    def test_a_cannot_arm_while_right_stick_is_deflected(self):
        arms,base=self.run_application([(0,0,1,0,True,False)])
        arms.start.assert_not_called()
        self.assertEqual(arms.step.call_args.kwargs['waist_rate'],0)
        self.assertEqual(base.send.call_args.args[0].lift_normalized,0)

    def test_arms_and_lift_use_fixed_file_bounds(self):
        body = BodyConfig(height_min_m=.02, height_max_m=.08)
        with patch('sys.argv', ['app', '--live', '--with-arms']), \
             patch.object(app.BodyConfig, 'load', return_value=body) as loader:
            args = app.parse_args()
        loader.assert_called_once_with(ROOT / 'config/body.jsonc')
        self.assertEqual((args.height_min_m, args.height_max_m), (.02, .08))
        self.assertFalse(args.base_only)

    def test_older_joystick_timestamp_goes_with_atomic_upper_body_command(self):
        session=object.__new__(ArmSession)
        session.ik,session.bridge=Mock(),Mock()
        session.ik.gravity.return_value=np.zeros(14)
        session.command=np.zeros(14)
        state=Mock(q=np.zeros(14),waist=np.zeros(2))
        session._send_target(pose(100),state,0.1,99.8)
        self.assertEqual(session.bridge.target.call_args.args[1],99.8)
        self.assertEqual(session.bridge.target.call_args.args[3],0.1)

    def test_versioned_waist_telemetry_and_old_bridge_rejection(self):
        fields=('STATE6 100 1 '+'0 '*14+'0.2 0.3 0.25 0.1 1 0.35 0.08 0 99.98 0 0 0 0 0').split()
        state=parse_arm_state(fields)
        np.testing.assert_allclose(state.waist,[0.2,0.3])
        self.assertEqual(state.waist_target,0.25)
        self.assertEqual(state.waist_velocity,0.1)
        self.assertTrue(state.waist_limited)
        self.assertEqual(state.pitch_target,0.35)
        self.assertEqual(state.pitch_velocity,0.08)
        self.assertFalse(state.pitch_limited)
        self.assertEqual(state.pitch_received_at,99.98)
        with self.assertRaisesRegex(ValueError,'rebuild'): parse_arm_state(['STATE4'])
        with self.assertRaisesRegex(ValueError,'rebuild'): parse_arm_state(['STATE3'])
        with self.assertRaisesRegex(ValueError,'rebuild'): parse_arm_state(['STATE2'])
        fields[19]='nan'
        with self.assertRaises(ValueError): parse_arm_state(fields)

    def test_arms_follow_torso_without_world_position_compensation(self):
        model=pin.buildModelFromUrdf(str(ROOT/'assets/g1d/g1_d.urdf'))
        data=model.createData()
        neutral=pin.neutral(model)
        yaw=model.joints[model.getJointId('torso_Joint')].idx_q
        torso=model.getFrameId('torso_link')
        wrist=model.getFrameId('left_wrist_yaw_link')
        pin.framesForwardKinematics(model,data,neutral)
        reference=(data.oMf[torso].inverse()*data.oMf[wrist]).homogeneous.copy()
        world=data.oMf[wrist].translation.copy()
        moved=neutral.copy();moved[yaw]=0.2
        pin.framesForwardKinematics(model,data,moved)
        relative=(data.oMf[torso].inverse()*data.oMf[wrist]).homogeneous
        np.testing.assert_allclose(relative,reference,atol=1e-12)
        self.assertGreater(np.linalg.norm(data.oMf[wrist].translation-world),0.01)


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / 'deploy').mkdir()
        for name in ('run_pc2_arms.sh', 'run_pc2_base.sh', 'record_pc2.sh', 'runtime_logging.sh'):
            shutil.copy2(ROOT / 'deploy' / name, self.root / 'deploy' / name)

        def executable(path, text):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('#!/usr/bin/env bash\n' + text + '\n')
            path.chmod(0o755)

        fakebin = self.root / 'bin'
        executable(fakebin / 'uname', 'echo aarch64')
        executable(fakebin / 'ip', 'if [[ $1 == -4 ]]; then echo "4: wlan0 inet 192.168.1.118/24"; fi')
        executable(fakebin / 'openssl', 'exit 0')
        executable(fakebin / 'runtime', 'if [[ ${1:-} == -u ]]; then printf "%s\\n" "$@"; fi')
        for name in ('g1d_agv_bridge', 'g1d_arm_bridge', 'g1d_hand_bridge'):
            executable(self.root / 'bridge/build-arm64' / name, 'exit 99')
        tls = self.root / '.local/https'
        tls.mkdir(parents=True)
        (tls / 'cert.pem').touch()
        (tls / 'key.pem').touch()
        self.env = os.environ.copy()
        self.env['G1D_RUNTIME_LOG_ACTIVE'] = 'argument-fixture'
        self.env['PATH'] = str(fakebin) + os.pathsep + self.env['PATH']
        self.env['PICO_G1D_PYTHON'] = str(fakebin / 'runtime')

    def run_launcher(self, *options, script='run_pc2_arms.sh'):
        return subprocess.run(['bash', str(self.root / 'deploy' / script), *options],
                              env=self.env, capture_output=True, text=True)

    def arguments(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.splitlines()

    def test_low_level_launcher_reads_height_limits_from_body_config(self):
        result = self.run_launcher('--with-lift', script='run_pc2_base.sh')
        args = self.arguments(result)
        self.assertNotIn('--base-only', args)
        self.assertNotIn('--height-min-m', args)
        self.assertNotIn('--height-max-m', args)

    def test_arms_default_launch_enables_confirmed_column_travel(self):
        for options in ((), ('--with-hands',), ('--with-lift',)):
            with self.subTest(options=options):
                args = self.arguments(self.run_launcher(*options))
                self.assertIn('--with-arms', args)
                self.assertNotIn('--base-only', args)
                self.assertNotIn('--height-min-m', args)
                self.assertNotIn('--height-max-m', args)
                self.assertEqual('--with-hands' in args, '--with-hands' in options)

    def test_without_lift_preserves_base_arms_and_optional_hands(self):
        args = self.arguments(self.run_launcher('--without-lift', '--with-hands'))
        self.assertIn('--base-only', args)
        self.assertIn('--with-arms', args)
        self.assertIn('--with-hands', args)
        self.assertNotIn('--height-min-m', args)
        self.assertNotIn('--height-max-m', args)

    def test_launchers_reject_removed_config_and_tuning_options(self):
        for script in ('run_pc2_arms.sh', 'run_pc2_base.sh', 'record_pc2.sh'):
            for option in ('--config', '--body-config', '--arm-config', '--max-forward-mps',
                           '--max-yaw-rps', '--max-lift', '--deadzone',
                           '--height-min-m', '--height-max-m'):
                with self.subTest(script=script, option=option):
                    result = self.run_launcher(option, '0.1', script=script)
                    self.assertEqual(result.returncode, 2)
                    self.assertIn('Edit project config/', result.stderr)

    def test_recording_passes_task_metadata_and_feature_flags_only(self):
        args = self.arguments(self.run_launcher('--instruction', '实验室取物', '--scene', 'lab209',
                                               '--objects', 'cup', '--', '--with-hands',
                                               script='record_pc2.sh'))
        self.assertIn('g1d_pico.recording', args)
        self.assertIn('实验室取物', args)
        self.assertIn('--with-hands', args)
        self.assertNotIn('--config', args)

    def test_skip_home_is_still_an_explicit_feature_option(self):
        args = self.arguments(self.run_launcher('--skip-home'))
        self.assertIn('--skip-arm-home', args)

    def test_base_default_remains_base_only(self):
        args = self.arguments(self.run_launcher(script='run_pc2_base.sh'))
        self.assertIn('--base-only', args)
        self.assertNotIn('--with-arms', args)

if __name__ == '__main__':
    unittest.main()
