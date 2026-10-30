"""Offline reset-button priority and configuration regression checks."""
from pathlib import Path
from unittest.mock import Mock, patch, AsyncMock
from contextlib import nullcontext
from types import SimpleNamespace
import asyncio
import time
import unittest
import numpy as np
from g1d_pico.arm_config import ArmConfig
from g1d_pico.input import FreshTeleVuer, TeleVuer, StickSample, PoseSample
from g1d_pico.arm_bridge import ArmState
from g1d_pico.arms import ArmSession
from g1d_pico import __main__ as app

URDF = Path(__file__).resolve().parents[1] / 'assets/g1d/g1_d.urdf'


class ConfigTests(unittest.TestCase):
    def test_validate_limits_and_automatically_match_bridge_speed(self):
        self.assertEqual(ArmConfig().bridge_speed_rad_s, 0.3)
        self.assertAlmostEqual(ArmConfig(joint_speed_rad_s=0.5).bridge_speed_rad_s, 0.6)
        self.assertEqual(ArmConfig(joint_speed_rad_s=0.5).home_speed_rad_s, 0.15)
        for bad in ({'joint_speed_rad_s': 2.}, {'joint_speed_rad_s': float('nan')},
                    {'waist_yaw_speed_rad_s': 0.3}, {'waist_yaw_limit_rad': 2.7},
                    {'waist_yaw_accel_rad_s2': float('nan')}, {'control_hz': 0.}, {'filter_weights': [0., 1.]},
                    {'filter_weights': [0.4, 0.4]}, {'home_speed_rad_s': True}):
            with self.assertRaises(ValueError):
                ArmConfig(**bad)

    def test_left_y_vuer_alias_is_not_right_b(self):
        class Snapshot(list):
            def get_lock(self):
                return nullcontext()
        viewer = object.__new__(FreshTeleVuer)
        viewer._stick_snapshot = Snapshot([0.] * 9)
        viewer._pose_snapshot = Snapshot([0.] * 35)
        event = SimpleNamespace(value={
            'left': np.eye(4).ravel(order='F'), 'right': np.eye(4).ravel(order='F'),
            'leftState': {'thumbstickValue': [0, 0], 'bButton': True},
            'rightState': {'thumbstickValue': [0, 0], 'bButton': False}})
        with patch.object(TeleVuer, 'on_controller_move', new_callable=AsyncMock):
            asyncio.run(viewer.on_controller_move(event, None))
            self.assertTrue(viewer.read_sticks().home_button)
            self.assertFalse(viewer.read_sticks().stop_button)
            event.value['leftState']['bButton'] = False
            event.value['rightState']['bButton'] = True
            asyncio.run(viewer.on_controller_move(event, None))
            self.assertFalse(viewer.read_sticks().home_button)
            self.assertTrue(viewer.read_sticks().stop_button)

    def test_y_stops_base_and_is_edge_triggered_b_has_priority(self):
        for stop in (False, True):
            now = time.monotonic()
            viewer, arms, base = Mock(), Mock(), Mock()
            arms.home_complete, arms.home_cancelled = True, False
            def request_home():
                arms.home_complete = False
            arms.request_home.side_effect = request_home
            viewer.read_sticks.side_effect = [
                StickSample(0, 0, 0, 0, True, False, now),
                StickSample(0, -1, 0, 0, False, stop, now, True),
                StickSample(0, -1, 0, 0, False, stop, now, True), KeyboardInterrupt()]
            viewer.read_poses.return_value = PoseSample(np.eye(4), np.eye(4), np.eye(4),
                                                        (False, False), now, now)
            with patch('sys.argv', ['app', '--no-camera', '--live', '--base-only', '--with-arms', '--skip-arm-home']), \
                 patch('g1d_pico.input.FreshTeleVuer', return_value=viewer), \
                 patch('g1d_pico.arms.ArmSession', return_value=arms), \
                 patch.object(app, 'AgvBridge', return_value=base), \
                 patch.object(app.time, 'sleep'):
                app.main()
            self.assertEqual(arms.request_home.call_count, 0 if stop else 1)
            for call in base.send.call_args_list:
                self.assertEqual(call.args[0].forward_mps, 0.)

    def test_request_home_resets_trajectory_and_disarms_tracking(self):
        bridge = Mock()
        bridge.wait_ready.return_value = ArmState(time.monotonic(), 0, np.zeros(14), pitch_received_at=time.monotonic())
        with patch('g1d_pico.arms.ArmBridge', return_value=bridge):
            session = ArmSession(Path('unused'), 'unused', URDF)
        session.enabled = True
        session.request_home()
        self.assertFalse(session.home_complete)
        self.assertFalse(session.enabled)
        self.assertFalse(session.home_cancelled)
        self.assertIsNone(session.countdown_since)
        bridge.hold.assert_called_once()

    def test_left_and_right_joint_scaling_are_independent(self):
        bridge = Mock()
        bridge.wait_ready.return_value = ArmState(10., 0, np.zeros(14), pitch_received_at=10.)
        bridge.state.return_value = ArmState(10.05, 1, np.zeros(14), pitch_received_at=10.05)
        with patch('g1d_pico.arms.ArmBridge', return_value=bridge):
            session = ArmSession(Path('unused'), 'unused', URDF)
        session.enabled, session.waiting = True, False
        session.last_step = 10.
        target = np.zeros(14)
        target[0], target[7] = 0.5, 0.05
        sample = PoseSample(np.eye(4), np.eye(4), np.eye(4), (True, True), 10.05, 10.05)
        with patch('g1d_pico.arms.time.monotonic', return_value=10.05), \
             patch.object(session.ik, 'solve', return_value=target):
            session.step(sample)
        self.assertAlmostEqual(session.command[0], 0.0125)
        self.assertAlmostEqual(session.command[7], 0.0125)


if __name__ == '__main__':
    unittest.main()
