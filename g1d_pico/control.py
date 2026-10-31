"""Pure joystick mapping; this module never communicates with the robot."""

from dataclasses import dataclass
import math
from .limits import AGV_FORWARD_MPS, AGV_YAW_RPS, LIFT_NORMALIZED


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
        for name in ('deadzone', 'max_forward_mps', 'max_yaw_rps', 'max_lift_normalized'):
            value = getattr(self, name)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value)):
                raise ValueError(f'{name} must be a finite number')
        if not 0.0 <= self.deadzone < 1.0:
            raise ValueError("deadzone must be in [0, 1)")
        for name, maximum in (('max_forward_mps', AGV_FORWARD_MPS),
                              ('max_yaw_rps', AGV_YAW_RPS),
                              ('max_lift_normalized', LIFT_NORMALIZED)):
            if not 0 <= getattr(self, name) <= maximum:
                raise ValueError(f'{name} must be within [0, {maximum}] (official SDK2 bound)')


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
