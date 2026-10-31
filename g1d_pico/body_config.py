"""Base, column and torso tuning, validated before opening robot connections."""
from dataclasses import dataclass, fields
import math
from pathlib import Path

from .config_io import load_config
from .control import MappingConfig
from .limits import (WAIST_YAW_RAD, WAIST_PITCH_MIN_RAD, WAIST_PITCH_MAX_RAD,
                     valid_height_window)


@dataclass(frozen=True)
class BodyConfig(MappingConfig):
    # Column feedback coordinates (rt/hispeed_state.y), not overall robot height.
    height_min_m: float = 0.0
    height_max_m: float = 0.42
    waist_yaw_limit_rad: float = 0.5235987756
    waist_yaw_speed_rad_s: float = 0.10
    waist_yaw_accel_rad_s2: float = 0.20
    waist_pitch_min_rad: float = 0.0
    waist_pitch_max_rad: float = 0.5235987756
    waist_pitch_speed_rad_s: float = 0.08
    waist_pitch_accel_rad_s2: float = 0.16
    waist_pitch_max_error_rad: float = 0.10
    waist_pitch_stall_timeout_s: float = 1.5
    waist_pitch_min_progress_rad: float = 0.005

    def __post_init__(self):
        for field in fields(self):
            value = getattr(self, field.name)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value)):
                raise ValueError(f'{field.name} must be a finite number')
        # Retain the same base/lift command caps as the C++ bridge.
        super().__post_init__()
        if not valid_height_window(self.height_min_m, self.height_max_m):
            raise ValueError('height feedback window requires 0 <= min < max and span <= 0.42 m; '
                             'overall robot height is not a feedback coordinate')
        if not 0 <= self.waist_yaw_limit_rad <= WAIST_YAW_RAD:
            raise ValueError(f'waist_yaw_limit_rad must be within [0, {WAIST_YAW_RAD}] (official URDF)')
        if not WAIST_PITCH_MIN_RAD <= self.waist_pitch_min_rad < self.waist_pitch_max_rad <= WAIST_PITCH_MAX_RAD:
            raise ValueError(f'waist pitch requires {WAIST_PITCH_MIN_RAD} <= min < max <= '
                             f'{WAIST_PITCH_MAX_RAD} rad (official URDF)')
        # Project trajectory/protection settings: no equivalent official API range.
        ranges = {
            'waist_yaw_speed_rad_s': (0.02, 0.20),
            'waist_yaw_accel_rad_s2': (0.05, 0.50),
            'waist_pitch_speed_rad_s': (0.01, 0.15),
            'waist_pitch_accel_rad_s2': (0.02, 0.30),
            'waist_pitch_max_error_rad': (0.05, 0.15),
            'waist_pitch_stall_timeout_s': (0.5, 5.0),
            'waist_pitch_min_progress_rad': (0.001, 0.02),
        }
        for name, (lower, upper) in ranges.items():
            if not lower <= getattr(self, name) <= upper:
                raise ValueError(f'{name} must be within [{lower}, {upper}] (project tuning bound, not official)')

    @classmethod
    def load(cls, path: Path):
        values = load_config(path)
        if isinstance(values, dict):
            values.pop('_comments', None)  # Legacy documentation metadata only.
        if not isinstance(values, dict) or set(values) - {f.name for f in fields(cls)}:
            raise ValueError('body config must be a JSON object with known field names')
        return cls(**values)
