"""Left-X mode switch for right-stick Y; no robot I/O in this state machine."""
from dataclasses import dataclass
from enum import Enum
import math


class VerticalMode(Enum):
    LIFT = "LIFT"
    PITCH = "PITCH"


@dataclass
class RightStickMode:
    mode: VerticalMode = VerticalMode.LIFT
    waiting_for_center: bool = False
    previous_x: bool = False

    def update(self, pressed: bool, right_x: float, right_y: float, *,
               fresh: bool, allow_switch: bool, deadzone: float) -> bool:
        """Return True on an accepted X edge. B/homing/stale input consume edges.

        A later fresh, centred sample unlocks the new vertical axis. Holding X
        cannot toggle repeatedly, and changing modes never enables the robot.
        """
        edge = pressed and not self.previous_x
        self.previous_x = bool(pressed)
        if not fresh or not allow_switch:
            return False
        if edge:
            self.mode = (VerticalMode.PITCH if self.mode is VerticalMode.LIFT
                         else VerticalMode.LIFT)
            self.waiting_for_center = True
            return True
        centered = all(math.isfinite(v) and abs(v) <= deadzone for v in (right_x, right_y))
        if self.waiting_for_center and centered:
            self.waiting_for_center = False
        return False

    @property
    def lift_selected(self) -> bool:
        return self.mode is VerticalMode.LIFT and not self.waiting_for_center

    @property
    def pitch_selected(self) -> bool:
        return self.mode is VerticalMode.PITCH and not self.waiting_for_center

    @property
    def status(self) -> str:
        return self.mode.value + (" / WAIT_CENTER" if self.waiting_for_center else "")
