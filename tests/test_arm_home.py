"""Offline startup-motion and input freshness regression tests."""
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import numpy as np
from g1d_pico.arm_home import HomeTrajectory
from g1d_pico.arms import ArmSession
from g1d_pico.arm_bridge import ArmState
from g1d_pico.input import PoseSample
from g1d_pico.arm_ik import DualArmIK

URDF = Path(__file__).resolve().parents[1] / 'assets/g1d/g1_d.urdf'


def sample(now, grips=(False, False), head_time=None):
    return PoseSample(np.eye(4), np.eye(4), np.eye(4), grips, now,
                      now if head_time is None else head_time)


class HomeTests(unittest.TestCase):
    def test_quintic_starts_at_actual_pose_and_respects_speed(self):
        start = np.linspace(-1., 1., 14)
        home = HomeTrajectory(start, np.zeros(14), 10.)
        previous, done = home.step(10., start)
        np.testing.assert_array_equal(previous, start)
        for t in np.arange(10.01, 10. + home.duration, 0.01):
            q, done = home.step(t, previous)
            self.assertLessEqual(np.max(np.abs(q - previous)) / 0.01, 0.15001)
            previous = q
        t = 10 + home.duration
        _, done = home.step(t, np.zeros(14))
        self.assertFalse(done)
        _, done = home.step(t + 0.6, np.zeros(14))
        self.assertTrue(done)
        with self.assertRaisesRegex(ValueError, 'timed out'):
            home.step(t + 9., start)

    def make_session(self):
        bridge = Mock()
        bridge.wait_ready.return_value = ArmState(10., 0, np.zeros(14), pitch_received_at=10.)
        with patch('g1d_pico.arms.ArmBridge', return_value=bridge):
            session = ArmSession(Path('unused'), 'unused', URDF, startup_home=True)
        return session, bridge

    def test_startup_wait_countdown_cancel_requires_explicit_retry(self):
        session, bridge = self.make_session()
        def step(t, **kwargs):
            bridge.state.return_value = ArmState(t, 0, np.zeros(14), pitch_received_at=t)
            with patch('g1d_pico.arms.time.monotonic', return_value=t):
                session.step_home(sample(t), neutral=True, **kwargs)
        step(10.)
        bridge.enable.assert_not_called()
        step(11., stop=True)
        step(20.)
        bridge.enable.assert_not_called()
        step(21., retry=True)
        step(24.1)
        bridge.enable.assert_called_once()
        self.assertFalse(session.home_complete)

    def test_complete_home_uses_measured_settle_then_waits_for_a(self):
        session, bridge = self.make_session()
        for t in (10., 13.1, 13.2, 17.3, 17.9):
            bridge.state.return_value = ArmState(t, 1, np.zeros(14), pitch_received_at=t)
            with patch('g1d_pico.arms.time.monotonic', return_value=t):
                session.step_home(sample(t), neutral=True)
        self.assertTrue(session.home_complete)
        self.assertFalse(session.enabled)
        self.assertIn('WAITING_FOR_A', session.status)
        bridge.hold.assert_called_once()

    def test_no_head_refresh_needed_after_clutch_but_controller_is_required(self):
        session, bridge = self.make_session()
        session.home_complete = True
        bridge.state.return_value = ArmState(10., 1, np.zeros(14), pitch_received_at=10.)
        with patch('g1d_pico.arms.time.monotonic', return_value=10.):
            session.start(sample(10.))
            session.step(sample(10., (True, False)))
        bridge.state.return_value = ArmState(11., 1, np.zeros(14), pitch_received_at=11.)
        with patch('g1d_pico.arms.time.monotonic', return_value=11.):
            session.step(sample(11., (True, False), head_time=10.))
            with self.assertRaisesRegex(ValueError, 'controller poses'):
                session.step(sample(10., (True, False)))
            session.step(sample(11., (False, False), head_time=10.))
            with self.assertRaisesRegex(ValueError, 'head pose'):
                session.step(sample(11., (True, False), head_time=10.))

    def test_cartesian_raise_from_home_converges_without_moving_left_arm(self):
        ik = DualArmIK(URDF)
        q = np.zeros(14)
        ik.reset(q)
        desired = ik.poses(q)
        desired[1][2, 3] += 0.05
        for _ in range(100):
            targets = [ArmSession._bounded_target(c, t) for c, t in zip(ik.poses(q), desired)]
            solution = ik.solve(targets, q, (False, True))
            delta = solution - q
            q += delta * min(1., 0.0125 / max(np.max(np.abs(delta)), 1e-12))
        np.testing.assert_array_equal(q[:7], np.zeros(7))
        self.assertLess(np.linalg.norm(ik.poses(q)[1][:3, 3] - desired[1][:3, 3]), 0.01)

    def test_application_keeps_base_zero_during_and_after_home_until_a(self):
        from g1d_pico import __main__ as app
        from g1d_pico.input import StickSample
        now = time.monotonic()
        viewer, arms, base = Mock(), Mock(), Mock()
        arms.home_complete = False
        def finish(*args, **kwargs):
            arms.home_complete = True
        arms.step_home.side_effect = finish
        viewer.read_sticks.side_effect = [
            StickSample(0, -1, 0, 0, False, False, now),
            StickSample(0, -1, 0, 0, False, False, now), KeyboardInterrupt()]
        viewer.read_poses.return_value = sample(now)
        with patch('sys.argv', ['g1d_pico', '--no-camera', '--live', '--base-only', '--with-arms']), \
             patch('g1d_pico.input.FreshTeleVuer', return_value=viewer), \
             patch('g1d_pico.arms.ArmSession', return_value=arms), \
             patch.object(app, 'AgvBridge', return_value=base), \
             patch.object(app.time, 'sleep'):
            app.main()
        self.assertEqual(len(base.send.call_args_list), 2)
        for call in base.send.call_args_list:
            self.assertEqual(call.args[0].forward_mps, 0.)
        arms.start.assert_not_called()

    def test_gravity_finite_bounded_and_accounts_for_waist(self):
        ik = DualArmIK(URDF)
        q = np.zeros(14)
        zero = ik.gravity(q, [0., 0.])
        tilted = ik.gravity(q, [0., 0.2])
        self.assertTrue(np.all(np.abs(zero) <= ik.torque_limits))
        self.assertFalse(np.allclose(zero, tilted))
        self.assertTrue(np.any(np.abs(zero) > 0.1))


if __name__ == '__main__':
    unittest.main()
