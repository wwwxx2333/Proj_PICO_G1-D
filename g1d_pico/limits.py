"""Verified SDK/model bounds, not recommended operating settings.

Sources and the distinction from project trajectory limits are documented in
deploy/OFFICIAL_LIMITS.md. Keep bridge/g1d_limits.hpp in sync; boundary tests and
the offline bridge handshake check this before any DDS connection is opened.
"""
import math

AGV_FORWARD_MPS = 1.5
AGV_YAW_RPS = 0.6
LIFT_NORMALIZED = 1.0
COLUMN_TRAVEL_M = 0.42
WAIST_YAW_RAD = 2.7053
WAIST_PITCH_MIN_RAD = -0.04363
WAIST_PITCH_MAX_RAD = 2.3562

BRIDGE_LIMITS = dict(
    revision=1, agv_forward_mps=AGV_FORWARD_MPS, agv_yaw_rps=AGV_YAW_RPS,
    lift_normalized=LIFT_NORMALIZED, column_travel_m=COLUMN_TRAVEL_M,
    waist_yaw_rad=WAIST_YAW_RAD, waist_pitch_min_rad=WAIST_PITCH_MIN_RAD,
    waist_pitch_max_rad=WAIST_PITCH_MAX_RAD,
)


def valid_height_window(lower, upper):
    """Project feedback window; official travel does not establish its origin."""
    return (all(not isinstance(v, bool) and isinstance(v, (int, float))
                and math.isfinite(v) for v in (lower, upper))
            and 0 <= lower < upper and upper - lower <= COLUMN_TRAVEL_M + 1e-7)
