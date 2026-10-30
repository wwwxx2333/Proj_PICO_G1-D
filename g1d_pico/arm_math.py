"""Coordinate transforms and clutch-relative targets, without robot I/O."""

from dataclasses import dataclass
from .arm_config import ArmConfig
import numpy as np

# OpenXR (right, up, back) -> robot (forward, left, up).
XR_TO_ROBOT = np.array([[0., 0., -1.], [-1., 0., 0.], [0., 1., 0.]])


def rigid_pose(value) -> np.ndarray:
    pose = np.asarray(value, dtype=float)
    if pose.shape != (4, 4) or not np.isfinite(pose).all():
        raise ValueError("pose must be a finite 4x4 matrix")
    rotation = pose[:3, :3]
    if (not np.allclose(pose[3], [0, 0, 0, 1], atol=1e-4)
            or not np.allclose(rotation.T @ rotation, np.eye(3), atol=2e-3)
            or abs(np.linalg.det(rotation) - 1) > 2e-3):
        raise ValueError("invalid rigid pose")
    return pose.copy()


def rotation_angle(rotation) -> float:
    return float(np.arccos(np.clip((np.trace(rotation) - 1) / 2, -1, 1)))


@dataclass
class Clutch:
    """Capture alignment at grip press; each press starts at the measured wrist pose."""
    controller: np.ndarray
    wrist: np.ndarray
    basis: np.ndarray
    previous: np.ndarray
    config: ArmConfig = ArmConfig()

    @classmethod
    def capture(cls, controller, wrist, head, config=None):
        controller, wrist, head = map(rigid_pose, (controller, wrist, head))
        head_rotation = XR_TO_ROBOT @ head[:3, :3] @ XR_TO_ROBOT.T
        forward = head_rotation[:2, 0]
        if np.linalg.norm(forward) < 0.3:
            raise ValueError("look approximately forward when pressing the grip")
        yaw = np.arctan2(forward[1], forward[0])
        c, s = np.cos(yaw), np.sin(yaw)
        yaw_rotation = np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])
        return cls(controller, wrist, yaw_rotation.T @ XR_TO_ROBOT, controller.copy(), config or ArmConfig())

    def target(self, controller) -> np.ndarray:
        controller = rigid_pose(controller)
        delta = controller[:3, 3] - self.controller[:3, 3]
        turn = controller[:3, :3] @ self.controller[:3, :3].T
        if (np.linalg.norm(controller[:3, 3] - self.previous[:3, 3]) > 0.15
                or rotation_angle(controller[:3, :3] @ self.previous[:3, :3].T) > 0.6):
            raise ValueError("controller tracking jumped; release grip and re-enable")
        if np.linalg.norm(delta) > self.config.clutch_translation_m or rotation_angle(turn) > self.config.clutch_rotation_rad:
            raise ValueError(f"clutch range exceeded: distance={np.linalg.norm(delta):.3f}m/"
                             f"{self.config.clutch_translation_m:.3f}m, rotation={rotation_angle(turn):.3f}rad/"
                             f"{self.config.clutch_rotation_rad:.3f}rad; release grip and re-enable")
        self.previous = controller.copy()
        target = self.wrist.copy()
        target[:3, 3] += self.config.position_scale * (self.basis @ delta)
        target[:3, :3] = self.basis @ turn @ self.basis.T @ self.wrist[:3, :3]
        return target
