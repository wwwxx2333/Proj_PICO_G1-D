"""TeleVuer adapter with an actual controller-event timestamp."""

from dataclasses import dataclass
import math
from multiprocessing import Array
import time
import numpy as np

from televuer.televuer import TeleVuer
from .arm_math import rigid_pose


@dataclass(frozen=True)
class StickSample:
    left_x: float
    left_y: float
    right_x: float
    right_y: float
    start_button: bool
    stop_button: bool
    received_at: float
    home_button: bool = False
    mode_button: bool = False
    record_button: bool = False  # Physical left thumbstick press (L3), not touch.

    def is_fresh(self, now: float, timeout_s: float) -> bool:
        return self.received_at > 0.0 and 0.0 <= now - self.received_at <= timeout_s


@dataclass(frozen=True)
class PoseSample:
    head: np.ndarray
    left: np.ndarray
    right: np.ndarray
    grips: tuple[bool, bool]
    received_at: float
    head_received_at: float
    triggers: tuple[float, float] = (0.0, 0.0)

    def freshness_error(self, now, require_head=True):
        controller_age = now - self.received_at
        head_age = now - self.head_received_at
        if not (0 < self.received_at <= now and controller_age <= 0.25):
            return (f"controller poses stale/invalid: age={controller_age:.3f}s "
                    f"(limit 0.250s); head age={head_age:.3f}s")
        if require_head and not (0 < self.head_received_at <= now and head_age <= 0.5):
            return f"head pose stale/invalid: age={head_age:.3f}s (limit 0.500s)"
        return None

    def is_fresh(self, now, require_head=True):
        return self.freshness_error(now, require_head) is None


class FreshTeleVuer(TeleVuer):
    """Expose a coherent stick snapshot from the TeleVuer child process.

    Upstream motion_data_ready stays true after disconnection, so it is not a
    suitable watchdog. This shared snapshot changes only on CONTROLLER_MOVE.
    """

    def __init__(self, **kwargs):
        # Must exist before TeleVuer starts its child process.
        self._stick_snapshot = Array("d", 10, lock=True)
        self._pose_snapshot = Array("d", 37, lock=True)
        self._head_snapshot = Array("d", 17, lock=True)
        super().__init__(use_hand_tracking=False, **kwargs)

    async def on_cam_move(self, event, session, fps=60):
        await super().on_cam_move(event, session, fps=fps)
        try:
            head = rigid_pose(np.asarray(event.value["camera"]["matrix"]).reshape(4, 4, order="F"))
        except (KeyError, TypeError, ValueError):
            return
        with self._head_snapshot.get_lock():
            self._head_snapshot[:] = [*head.ravel(), time.monotonic()]

    async def on_controller_move(self, event, session, fps=60):
        try:
            left = event.value["leftState"]["thumbstickValue"]
            right = event.value["rightState"]["thumbstickValue"]
            axes = [float(left[0]), float(left[1]), float(right[0]), float(right[1])]
            if len(left) != 2 or len(right) != 2 or not all(math.isfinite(x) for x in axes):
                return
            start = bool(event.value["rightState"].get("aButton", False))
            stop = bool(event.value["rightState"].get("bButton", False))
            # Vuer uses aButton/bButton for both hands: left bButton is physical Y.
            left_state = event.value["leftState"]
            home = bool(left_state.get("yButton", left_state.get("bButton", False)))
            mode = bool(left_state.get("xButton", left_state.get("aButton", False)))
            record = bool(left_state.get("thumbstick", False))
        except (KeyError, TypeError, ValueError, IndexError):
            return
        await super().on_controller_move(event, session, fps=fps)
        with self._stick_snapshot.get_lock():
            self._stick_snapshot[:] = [*axes, float(start), float(stop), time.monotonic(), float(home), float(mode), float(record)]
        try:
            left_pose = rigid_pose(np.asarray(event.value["left"]).reshape(4, 4, order="F"))
            right_pose = rigid_pose(np.asarray(event.value["right"]).reshape(4, 4, order="F"))
            grips = [bool(event.value[f"{side}State"].get("squeeze", False)) for side in ("left", "right")]
            triggers = [float(event.value[f"{side}State"].get("triggerValue", 0.0))
                        for side in ("left", "right")]
            if not all(math.isfinite(v) and 0.0 <= v <= 1.0 for v in triggers):
                return
        except (KeyError, TypeError, ValueError):
            return
        with self._pose_snapshot.get_lock():
            self._pose_snapshot[:] = [*left_pose.ravel(), *right_pose.ravel(), *map(float, grips), time.monotonic(), *triggers]

    def read_sticks(self) -> StickSample:
        with self._stick_snapshot.get_lock():
            lx, ly, rx, ry, start, stop, timestamp, home, mode, record = self._stick_snapshot[:]
        return StickSample(lx, ly, rx, ry, bool(start), bool(stop), timestamp, bool(home), bool(mode), bool(record))

    def read_poses(self) -> PoseSample:
        with self._pose_snapshot.get_lock():
            values = np.array(self._pose_snapshot[:])
        with self._head_snapshot.get_lock():
            head = np.array(self._head_snapshot[:])
        return PoseSample(head[:16].reshape(4, 4), values[:16].reshape(4, 4),
                          values[16:32].reshape(4, 4), (bool(values[32]), bool(values[33])),
                          float(values[34]), float(head[16]), (float(values[35]), float(values[36])))
