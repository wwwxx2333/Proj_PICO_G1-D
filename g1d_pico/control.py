"""Pure joystick mapping; this module never communicates with the robot."""

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class MotionCommand:
    forward_mps: float = 0.0
    yaw_rps: float = 0.0
    lift_normalized: float = 0.0


@dataclass(frozen=True)
class MappingConfig:
    deadzone: float = 0.15
    max_forward_mps: float = 0.10
    max_yaw_rps: float = 0.15
    max_lift_normalized: float = 0.15

    def __post_init__(self) -> None:
        if not 0.0 <= self.deadzone < 1.0:
            raise ValueError("deadzone must be in [0, 1)")
        if not 0.0 < self.max_forward_mps <= 0.10:
            raise ValueError("max_forward_mps must be in (0, 0.10]")
        if not 0.0 < self.max_yaw_rps <= 0.15:
            raise ValueError("max_yaw_rps must be in (0, 0.15]")
        if not 0.0 < self.max_lift_normalized <= 0.15:
            raise ValueError("max_lift_normalized must be in (0, 0.15]")


def _axis(value: float, deadzone: float) -> float:
    """Apply a rescaled deadzone while retaining full travel at +/-1."""
    value = float(value)
    if not math.isfinite(value) or abs(value) > 1.2:
        raise ValueError(f"invalid joystick axis: {value}")
    value = max(-1.0, min(1.0, value))
    magnitude = abs(value)
    if magnitude <= deadzone:
        return 0.0
    return math.copysign((magnitude - deadzone) / (1.0 - deadzone), value)


def map_sticks(
    left_x: float, left_y: float, right_x: float, right_y: float,
    config: MappingConfig = MappingConfig(),
) -> MotionCommand:
    """TeleVuer uses y=-1 for up and x=+1 for right.

    Left y is forward/back, left x is turning, right y is lift.
    Right x is validated here; the arm bridge handles its waist-yaw mapping.
    """
    lx = _axis(left_x, config.deadzone)
    ly = _axis(left_y, config.deadzone)
    _axis(right_x, config.deadzone)
    ry = _axis(right_y, config.deadzone)
    return MotionCommand(
        forward_mps=-ly * config.max_forward_mps,
        yaw_rps=-lx * config.max_yaw_rps,
        lift_normalized=-ry * config.max_lift_normalized,
    )


def waist_yaw_rate(right_x: float, max_rate: float, deadzone: float = 0.15) -> float:
    """Stick right -> negative yaw, stick left -> positive yaw."""
    if not math.isfinite(max_rate) or not 0 < max_rate <= 0.20:
        raise ValueError('waist yaw speed must be within (0, 0.20] rad/s')
    return -_axis(right_x, deadzone) * max_rate


def waist_pitch_rate(right_y: float, max_rate: float, deadzone: float = 0.15) -> float:
    """Stick forward/up -> positive pitch (torso leans forward about +Y)."""
    if not math.isfinite(max_rate) or not 0 < max_rate <= 0.15:
        raise ValueError('waist pitch speed must be within (0, 0.15] rad/s')
    return -_axis(right_y, deadzone) * max_rate


def sticks_neutral(left_x: float, left_y: float, right_y: float, deadzone: float,
                   right_x: float = 0.0) -> bool:
    return all(math.isfinite(value) and abs(value) <= deadzone
               for value in (left_x, left_y, right_x, right_y))
