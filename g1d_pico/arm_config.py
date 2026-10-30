"""Validated operator tuning, loaded once before any robot process is started."""
from dataclasses import dataclass, fields
import json
import math
from pathlib import Path


@dataclass(frozen=True)
class ArmConfig:
    joint_speed_rad_s: float = 0.25
    home_speed_rad_s: float = 0.15
    control_hz: float = 20.0
    position_scale: float = 0.5
    clutch_translation_m: float = 0.4
    clutch_rotation_rad: float = 0.8
    cartesian_speed_m_s: float = 0.4
    rotation_speed_rad_s: float = 2.0
    waist_yaw_limit_rad: float = 0.5235987756  # +/-30 degrees relative to joint zero.
    waist_yaw_speed_rad_s: float = 0.10
    waist_yaw_accel_rad_s2: float = 0.20
    waist_pitch_min_rad: float = 0.0  # Upright; no backward hyperextension by default.
    waist_pitch_max_rad: float = 0.5235987756  # Forward lean limited to 30 degrees.
    waist_pitch_speed_rad_s: float = 0.08
    waist_pitch_accel_rad_s2: float = 0.16
    waist_pitch_max_error_rad: float = 0.10
    waist_pitch_stall_timeout_s: float = 1.5
    waist_pitch_min_progress_rad: float = 0.005
    filter_weights: tuple = (0.4, 0.3, 0.2, 0.1)  # Newest first.

    def __post_init__(self):
        ranges = {
            'joint_speed_rad_s': (0.05, 1.0), 'home_speed_rad_s': (0.05, 0.2),
            'control_hz': (10., 60.), 'position_scale': (0.1, 1.),
            'clutch_translation_m': (0.1, 0.8), 'clutch_rotation_rad': (0.2, 2.),
            'cartesian_speed_m_s': (0.05, 0.8), 'rotation_speed_rad_s': (0.2, 3.),
            'waist_yaw_limit_rad': (0.087, 1.0472),
            'waist_yaw_speed_rad_s': (0.02, 0.20),
            'waist_yaw_accel_rad_s2': (0.05, 0.50),
            'waist_pitch_min_rad': (-0.02, 0.0),
            'waist_pitch_max_rad': (0.087, 1.0472),
            'waist_pitch_speed_rad_s': (0.01, 0.15),
            'waist_pitch_accel_rad_s2': (0.02, 0.30),
            'waist_pitch_max_error_rad': (0.05, 0.15),
            'waist_pitch_stall_timeout_s': (0.5, 5.0),
            'waist_pitch_min_progress_rad': (0.001, 0.02),
        }
        for name, (lo, hi) in ranges.items():
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not lo <= value <= hi:
                raise ValueError(f'{name} must be finite and within [{lo}, {hi}]')
        weights = self.filter_weights
        if not isinstance(weights, (tuple, list)) or not 1 <= len(weights) <= 8:
            raise ValueError('filter_weights must have 1..8 values, newest first')
        if any(isinstance(w, bool) or not isinstance(w, (float, int)) or not math.isfinite(w) or w <= 0 for w in weights):
            raise ValueError('filter_weights must be positive finite numbers')
        if not math.isclose(sum(weights), 1., abs_tol=1e-6):
            raise ValueError('filter_weights must sum to 1')
        object.__setattr__(self, 'filter_weights', tuple(weights))

    @property
    def bridge_speed_rad_s(self):
        return max(0.30, 1.2 * self.joint_speed_rad_s, 1.2 * self.home_speed_rad_s)

    @classmethod
    def load(cls, path: Path):
        values = json.loads(path.read_text())
        if not isinstance(values, dict) or set(values) - {f.name for f in fields(cls)}:
            raise ValueError('arm config must be a JSON object with known field names')
        return cls(**values)
