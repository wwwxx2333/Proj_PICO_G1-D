"""Offline geometry, IK and session tests; no robot or DDS initialization."""
from pathlib import Path
import time
import unittest
from unittest.mock import Mock, patch
from unittest.mock import AsyncMock
from contextlib import nullcontext
from types import SimpleNamespace
from g1d_pico.body_config import BodyConfig
import asyncio
import numpy as np
import pinocchio as pin
from g1d_pico.arm_math import Clutch, rigid_pose
from g1d_pico.arm_ik import DualArmIK, IKTargetRejected
from g1d_pico.arm_bridge import ArmState
from g1d_pico.arms import ArmSession
from g1d_pico.input import PoseSample, FreshTeleVuer, TeleVuer
from g1d_pico.input import StickSample
from g1d_pico import __main__ as application

URDF = Path(__file__).resolve().parents[1] / "assets/g1d/g1_d.urdf"


class ArmGeometryTests(unittest.TestCase):
    def test_clutch_does_not_jump_and_maps_forward(self):
        hand = np.eye(4)
        hand[:3, 3] = [0.1, 1.2, -0.3]
        wrist = np.eye(4)
        wrist[:3, 3] = [0.2, 0.2, 0.1]
        clutch = Clutch.capture(hand, wrist, np.eye(4))
        np.testing.assert_allclose(clutch.target(hand), wrist)
        moved = hand.copy()
        moved[2, 3] -= 0.04
        target = clutch.target(moved)
        np.testing.assert_allclose(target[:3, 3], [0.22, 0.2, 0.1])

    def test_pose_validation_and_tracking_jump(self):
        for bad in (np.zeros((4, 4)), np.full((4, 4), np.nan)):
            with self.assertRaises(ValueError):
                rigid_pose(bad)
        clutch = Clutch.capture(np.eye(4), np.eye(4), np.eye(4))
        moved = np.eye(4)
        moved[0, 3] = 0.3
        with self.assertRaises(ValueError):
            clutch.target(moved)

    def test_rotation_mapping(self):
        clutch = Clutch.capture(np.eye(4), np.eye(4), np.eye(4))
        moved = np.eye(4)
        moved[:3, :3] = pin.exp3(np.array([0, 0.1, 0]))
        expected = pin.exp3(np.array([0, 0, 0.1]))
        np.testing.assert_allclose(clutch.target(moved)[:3, :3], expected, atol=1e-9)

    def test_ik_recovers_reachable_pose_and_holds_other_arm(self):
        ik = DualArmIK(URDF)
        seed = np.array([0.1, 0.2, 0., 0.6, 0., 0., 0., 0.1, -0.2, 0., 0.6, 0., 0., 0.])
        goal = seed.copy()
        goal[0] += 0.05
        goal[3] += 0.04
        targets = ik.poses(goal)
        ik.reset(seed)
        solution = seed.copy()
        for _ in range(6):
            solution = ik.solve(targets, solution, enabled=(True, False))
        poses = ik.poses(solution)
        self.assertLess(np.linalg.norm(poses[0][:3, 3] - targets[0][:3, 3]), 0.015)
        np.testing.assert_allclose(solution[7:], seed[7:])
        self.assertTrue(np.all(solution >= ik.lower) and np.all(solution <= ik.upper))

    def test_unreachable_target_is_rejected(self):
        ik = DualArmIK(URDF)
        targets = ik.poses(np.zeros(14))
        targets[0][0, 3] += 2
        with self.assertRaises(ValueError):
            ik.solve(targets, np.zeros(14))


class ArmSessionTests(unittest.TestCase):
    def test_requires_grip_release_and_stops_without_auto_resume(self):
        now = time.monotonic()
        bridge = Mock()
        bridge.wait_ready.return_value = ArmState(now, 0, np.zeros(14), pitch_received_at=now)
        bridge.state.return_value = ArmState(now, 1, np.zeros(14), pitch_received_at=now)
        with patch("g1d_pico.arms.ArmBridge", return_value=bridge):
            session = ArmSession(Path("unused"), "unused", URDF)
        sample = PoseSample(np.eye(4), np.eye(4), np.eye(4), (True, False), now, now)
        with self.assertRaises(ValueError):
            session.start(sample)
        bridge.enable.assert_not_called()
        sample = PoseSample(np.eye(4), np.eye(4), np.eye(4), (False, False), now, now)
        session.start(sample)
        session.step(sample)
        bridge.target.assert_called_once()
        session.stop()
        bridge.hold.assert_called_once()
        session.step(sample)
        self.assertFalse(session.enabled)
        bridge.target.assert_called_once()

    def test_optimizer_failure_holds_then_recovers_without_rearm(self):
        now = time.monotonic()
        bridge = Mock()
        bridge.wait_ready.return_value = ArmState(now, 0, np.zeros(14), pitch_received_at=now)
        bridge.state.return_value = ArmState(now, 1, np.zeros(14), pitch_received_at=now)
        with patch("g1d_pico.arms.ArmBridge", return_value=bridge):
            session = ArmSession(Path("unused"), "unused", URDF)
        released = PoseSample(np.eye(4), np.eye(4), np.eye(4), (False, False), now, now)
        gripped = PoseSample(np.eye(4), np.eye(4), np.eye(4), (True, False), now, now)
        session.start(released)
        with patch.object(session.ik, "solve", side_effect=IKTargetRejected("failed")):
            session.step(gripped)
            self.assertTrue(session.enabled)
            self.assertIn("IK_RETRY", session.status)
            np.testing.assert_array_equal(bridge.target.call_args.args[0], np.zeros(14))
        session.step(gripped)
        self.assertTrue(session.status.startswith("FOLLOW=ON/HOLD"))
        bad_q = np.zeros(14)
        bad_q[0] = 0.31
        bridge.state.return_value = ArmState(time.monotonic(), 1, bad_q, pitch_received_at=time.monotonic())
        with self.assertRaisesRegex(ValueError, "following error"):
            session.step(gripped)

    def test_feedback_timestamp_not_receipt_time(self):
        self.assertFalse(ArmState(10, 1, np.zeros(14), pitch_received_at=10).is_fresh(10.4))
        self.assertFalse(ArmState(10, 1, np.zeros(14), pitch_received_at=10).is_fresh(9.9))

    def test_controller_event_column_major_pose_and_invalid_pose_age(self):
        class Snapshot(list):
            def get_lock(self):
                return nullcontext()
        viewer = object.__new__(FreshTeleVuer)
        viewer._stick_snapshot = Snapshot([0.] * 10)
        viewer._pose_snapshot = Snapshot([0.] * 37)
        viewer._head_snapshot = Snapshot([0.] * 17)
        pose = np.eye(4)
        pose[:3, 3] = [0.1, 0.2, -0.3]
        event = SimpleNamespace(value={
            "left": pose.ravel(order="F"), "right": np.eye(4).ravel(order="F"),
            "leftState": {"thumbstickValue": [0, 0], "squeeze": True},
            "rightState": {"thumbstickValue": [0, 0], "aButton": False},
        })
        with patch.object(TeleVuer, "on_controller_move", new_callable=AsyncMock):
            asyncio.run(viewer.on_controller_move(event, None))
            first = viewer.read_poses()
            np.testing.assert_allclose(first.left, pose)
            self.assertEqual(first.grips, (True, False))
            event.value["left"] = np.zeros(16)
            asyncio.run(viewer.on_controller_move(event, None))
            self.assertEqual(viewer.read_poses().received_at, first.received_at)

    def test_arm_fault_stops_base_and_closes_both_processes(self):
        now = time.monotonic()
        viewer = Mock()
        viewer.read_sticks.side_effect = [
            StickSample(0, 0, 0, 0, True, False, now),
            StickSample(0, -1, 0, 0, False, False, now),
            KeyboardInterrupt(),
        ]
        viewer.read_poses.return_value = PoseSample(np.eye(4), np.eye(4), np.eye(4),
                                                   (False, False), now, now)
        arms, base = Mock(), Mock()
        arms.home_complete = True
        arms.step.side_effect = [None, ValueError("tracking jump")]
        args = SimpleNamespace(body_config=URDF.parents[2] / "config/body.jsonc", body_settings=BodyConfig(),
                               no_camera=True, live=True, base_only=True, lift_only=False,
                               with_arms=True, with_hands=False, skip_arm_home=True, arm_config=URDF.parents[2] / "config/arms.jsonc", cert=None, key=None, bridge_bin=Path("unused"),
                               network_interface="eth0", remote_bridge_port=None,
                               height_min_m=None, height_max_m=None, max_forward_mps=0.1,
                               max_yaw_rps=0.15, max_lift=0.15, arm_bridge_bin=Path("unused"),
                               arm_urdf=URDF)
        with patch.object(application, "parse_args", return_value=args), \
             patch("g1d_pico.input.FreshTeleVuer", return_value=viewer), \
             patch.object(application, "AgvBridge", return_value=base), \
             patch("g1d_pico.arms.ArmSession", return_value=arms), \
             patch.object(application.time, "sleep"):
            application.main()
        self.assertEqual(len(base.send.call_args_list), 2)
        self.assertEqual(base.send.call_args_list[-1].args[0].forward_mps, 0.)
        arms.stop.assert_called_once()
        base.close.assert_called_once()
        arms.close.assert_called_once()
        viewer.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
