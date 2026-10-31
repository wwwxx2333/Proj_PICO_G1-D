"""Offline PICO X mapping, mode routing and torso kinematics; no robot I/O."""
import asyncio
from contextlib import nullcontext, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
import time
import unittest
from unittest.mock import Mock, AsyncMock, patch
import numpy as np
import pinocchio as pin

from g1d_pico import __main__ as app
from g1d_pico.arm_bridge import ArmBridge, ArmState, parse_arm_state
from g1d_pico.body_config import BodyConfig
from g1d_pico.arms import ArmSession
from g1d_pico.control import waist_pitch_rate
from g1d_pico.input import FreshTeleVuer, TeleVuer, StickSample, PoseSample
from g1d_pico.right_stick import RightStickMode, VerticalMode

ROOT = Path(__file__).resolve().parents[1]


class PitchModeTests(unittest.TestCase):
    def update(self, mode, pressed, rx=0, ry=0, **options):
        return mode.update(pressed, rx, ry, fresh=options.get('fresh', True),
                           allow_switch=options.get('allowed', True), deadzone=0.15)

    def test_toggle_is_edge_triggered_and_requires_later_centered_sample(self):
        mode = RightStickMode()
        self.assertTrue(mode.lift_selected)
        self.assertTrue(self.update(mode, True, ry=-1))
        self.assertFalse(mode.lift_selected or mode.pitch_selected)
        for _ in range(10):
            self.assertFalse(self.update(mode, True, ry=-1))
            self.assertTrue(mode.waiting_for_center)
        self.update(mode, False, rx=1)  # Require both right-stick axes centred.
        self.assertTrue(mode.waiting_for_center)
        self.update(mode, False, fresh=False)
        self.assertTrue(mode.waiting_for_center)
        self.update(mode, False)
        self.assertTrue(mode.pitch_selected)
        self.assertTrue(self.update(mode, True))
        self.assertTrue(mode.waiting_for_center)  # Even when X was pressed at centre.
        self.update(mode, True)
        self.assertTrue(mode.lift_selected)

    def test_stale_or_inhibited_x_is_not_replayed_when_input_recovers(self):
        for options in ({'fresh': False}, {'allowed': False}):
            mode = RightStickMode()
            self.assertFalse(self.update(mode, True, **options))
            self.assertFalse(self.update(mode, True))
            self.assertEqual(mode.mode, VerticalMode.LIFT)
            self.update(mode, False)
            self.assertTrue(self.update(mode, True))

    def test_x_vuer_alias_does_not_enable_a_or_request_home(self):
        class Snapshot(list):
            def get_lock(self):
                return nullcontext()
        viewer = object.__new__(FreshTeleVuer)
        viewer._stick_snapshot = Snapshot([0.] * 10)
        viewer._pose_snapshot = Snapshot([0.] * 37)
        event = SimpleNamespace(value={
            'left': np.eye(4).ravel(order='F'), 'right': np.eye(4).ravel(order='F'),
            'leftState': {'thumbstickValue': [0, 0]},
            'rightState': {'thumbstickValue': [0, 0]}})
        with patch.object(TeleVuer, 'on_controller_move', new_callable=AsyncMock):
            for buttons in ({'aButton': True}, {'xButton': True}, {'xButton': False, 'aButton': True}):
                event.value['leftState'] = {'thumbstickValue': [0, 0], **buttons}
                asyncio.run(viewer.on_controller_move(event, None))
                sample = viewer.read_sticks()
                self.assertEqual(sample.mode_button, buttons.get('xButton', buttons.get('aButton')))
                self.assertFalse(sample.start_button or sample.home_button or sample.stop_button)

    def test_pitch_direction_deadzone_and_configuration_bounds(self):
        self.assertEqual(waist_pitch_rate(-1, 0.08), 0.08)
        self.assertEqual(waist_pitch_rate(1, 0.08), -0.08)
        self.assertEqual(waist_pitch_rate(-0.1, 0.08), 0)
        for axis in (float('nan'), float('inf'), 2):
            with self.assertRaises(ValueError): waist_pitch_rate(axis, 0.08)
        for options in ({'waist_pitch_min_rad': -0.05}, {'waist_pitch_max_rad': 2.4},
                        {'waist_pitch_speed_rad_s': .2}, {'waist_pitch_accel_rad_s2': float('nan')}):
            with self.assertRaises(ValueError): BodyConfig(**options)

    def run_app(self, rows, *, lift=True, with_arms=True, homing=False):
        now = time.monotonic()
        viewer, arms, base = Mock(), Mock(), Mock()
        arms.home_complete, arms.home_cancelled = not homing, False
        arms.request_home.side_effect = lambda: setattr(arms, 'home_complete', False)
        viewer.read_sticks.side_effect = [StickSample(0, row.get('ly', 0), row.get('rx', 0), row.get('ry', 0),
            row.get('a', False), row.get('b', False), now if not row.get('stale') else now-1,
            row.get('y', False), row.get('x', False)) for row in rows] + [KeyboardInterrupt()]
        viewer.read_poses.return_value = PoseSample(np.eye(4), np.eye(4), np.eye(4), (False, False), now, now)
        argv = ['app', '--no-camera', '--live']
        if with_arms: argv += ['--with-arms', '--skip-arm-home']
        if not lift: argv += ['--base-only']
        ticks, output = [], StringIO()
        def capture(_):
            ticks.append((base.send.call_args.args[0],
                          arms.step.call_args.kwargs.copy() if arms.step.call_args else {}))
        with patch.object(app.BodyConfig, 'load', return_value=BodyConfig()), \
             patch('sys.argv', argv), patch('g1d_pico.input.FreshTeleVuer', return_value=viewer), \
             patch('g1d_pico.arms.ArmSession', return_value=arms), \
             patch.object(app, 'AgvBridge', return_value=base), patch.object(app.time, 'sleep', side_effect=capture), \
             redirect_stdout(output):
            app.main()
        return ticks, arms, output.getvalue()

    def test_switching_routes_one_vertical_axis_and_preserves_base_and_yaw(self):
        rows = [dict(a=True), dict(ly=-1, rx=1, ry=-1), dict(x=True, rx=1, ry=-1),
                dict(x=True, ry=-1), dict(x=True), dict(ly=-1, rx=1, ry=-1),
                dict(), dict(x=True, ry=1), dict(ry=1), dict(), dict(ry=1), dict(b=True)]
        ticks, arms, log = self.run_app(rows)
        for index in (2, 3, 7, 8):
            self.assertEqual(ticks[index][0].lift_normalized, 0)
            self.assertFalse(ticks[index][1]['pitch_enabled'])
            self.assertEqual(ticks[index][1]['pitch_rate'], 0)
        self.assertEqual(ticks[1][0].lift_normalized, .15)
        self.assertEqual(ticks[5][0].lift_normalized, 0)
        self.assertEqual(ticks[5][1]['pitch_rate'], .08)
        self.assertEqual(ticks[5][0].forward_mps, .1)
        self.assertEqual(ticks[5][1]['waist_rate'], -.1)
        self.assertTrue(ticks[6][1]['pitch_enabled'])  # Centre decelerates in pitch mode.
        self.assertEqual(ticks[6][1]['pitch_rate'], 0)
        self.assertEqual(ticks[10][0].lift_normalized, -.15)
        self.assertFalse(ticks[10][1]['pitch_enabled'])
        self.assertEqual(ticks[11][0].lift_normalized, 0)
        arms.start.assert_called_once()  # Switching never requires another A.
        self.assertEqual(arms.hold_pitch.call_count, 2)
        self.assertIn('RIGHT_Y=PITCH / WAIT_CENTER', log)
        self.assertIn('RIGHT_Y=LIFT / WAIT_CENTER', log)

    def test_x_does_not_arm_and_without_lift_still_allows_pitch(self):
        ticks, arms, _ = self.run_app([dict(x=True), dict(), dict(ry=-1), dict(a=True), dict(ry=-1)], lift=False)
        self.assertFalse(ticks[2][1]['pitch_enabled'])
        self.assertEqual(ticks[4][1]['pitch_rate'], .08)
        self.assertTrue(all(c.lift_normalized == 0 for c, _ in ticks))
        arms.start.assert_called_once()

    def test_stop_stale_and_homing_take_priority_over_x(self):
        for stop in ({'b': True}, {'stale': True}, {'y': True}):
            ticks, arms, _ = self.run_app([dict(a=True), dict(x=True, **stop)])
            arms.hold_pitch.assert_not_called()
            self.assertEqual(ticks[-1][0].lift_normalized, 0)
        _, arms, _ = self.run_app([dict(x=True), dict(x=True)], homing=True)
        arms.hold_pitch.assert_not_called()

    def test_b_and_a_keep_selected_pitch_but_do_not_auto_resume(self):
        ticks, arms, _ = self.run_app([dict(a=True), dict(x=True), dict(), dict(ry=-1),
                                     dict(b=True), dict(ry=-1), dict(a=True), dict(ry=-1)])
        self.assertFalse(ticks[5][1]['pitch_enabled'])
        self.assertEqual(ticks[7][1]['pitch_rate'], .08)
        self.assertEqual(arms.start.call_count, 2)

    def test_base_only_program_ignores_x(self):
        ticks, arms, _ = self.run_app([dict(a=True), dict(x=True, ly=-1)], lift=False, with_arms=False)
        self.assertEqual(ticks[1][0].forward_mps, .1)
        arms.hold_pitch.assert_not_called()

    def test_packet_serialization_and_gravity_use_actual_pitch(self):
        bridge = object.__new__(ArmBridge)
        bridge._send = Mock()
        bridge.target(np.zeros(14), 100, np.ones(14), .1, pitch_rate=.08, pitch_enabled=True)
        fields = bridge._send.call_args.args[0].split()
        self.assertEqual(fields[0], 'TARGET6')
        self.assertEqual(len(fields), 33)
        self.assertEqual([float(x) for x in fields[-3:]], [.1, .08, 1])
        bridge._error = None
        bridge.process = Mock(returncode=2)
        bridge.process.poll.return_value = 2
        with self.assertRaisesRegex(RuntimeError, 'rebuild'): bridge.state()
        session = object.__new__(ArmSession)
        session.ik, session.bridge, session.command = Mock(), Mock(), np.zeros(14)
        session.ik.gravity.return_value = np.ones(14)
        state = ArmState(99.9, 1, np.zeros(14), np.array([.1, .3]), pitch_received_at=99.9)
        sample = SimpleNamespace(received_at=100)
        session._send_target(sample, state, .1, 99.8, pitch_rate=.08, pitch_enabled=True)
        np.testing.assert_array_equal(session.ik.gravity.call_args.args[1], [.1, .3])
        self.assertEqual(session.bridge.target.call_args.args[1], 99.8)
        self.assertEqual(session.bridge.target.call_args.kwargs, dict(pitch_rate=.08, pitch_enabled=True))
        for value in ('nan', 'inf'):
            fields = ('STATE6 100 1 '+'0 '*14+'0 0 0 0 0 '+value+' 0 0 99.98 0 0 0 0 0').split()
            with self.assertRaises(ValueError): parse_arm_state(fields)

    def test_pitch_moves_both_arms_with_torso_and_positive_pitch_leans_forward(self):
        model = pin.buildModelFromUrdf(str(ROOT / 'assets/g1d/g1_d.urdf'))
        data = model.createData()
        q = pin.neutral(model)
        torso = model.getFrameId('torso_link')
        wrists = [model.getFrameId(side+'_wrist_yaw_link') for side in ('left', 'right')]
        pin.framesForwardKinematics(model, data, q)
        relatives = [(data.oMf[torso].inverse()*data.oMf[w]).homogeneous.copy() for w in wrists]
        worlds = [data.oMf[w].translation.copy() for w in wrists]
        q[model.joints[model.getJointId('Yaw_Joint')].idx_q] = .3
        pin.framesForwardKinematics(model, data, q)
        self.assertGreater(data.oMf[torso].rotation[0, 2], 0)  # Torso up tilts forward (+X).
        for w, relative, world in zip(wrists, relatives, worlds):
            np.testing.assert_allclose((data.oMf[torso].inverse()*data.oMf[w]).homogeneous, relative, atol=1e-12)
            self.assertGreater(np.linalg.norm(data.oMf[w].translation-world), .01)


if __name__ == '__main__':
    unittest.main()
