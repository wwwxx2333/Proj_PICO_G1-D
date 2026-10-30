# Official IK attribution and G1-D adaptation

Upstream: Unitree Robotics `xr_teleoperate`, local checkout commit
`817fb00c63cde15e5f24a0f8fa08e1e33ed89d3b`.

- https://github.com/unitreerobotics/xr_teleoperate/blob/817fb00c63cde15e5f24a0f8fa08e1e33ed89d3b/teleop/robot_control/robot_arm_ik.py
- https://github.com/unitreerobotics/xr_teleoperate/blob/817fb00c63cde15e5f24a0f8fa08e1e33ed89d3b/teleop/utils/weighted_moving_filter.py
- https://github.com/unitreerobotics/xr_teleoperate/blob/817fb00c63cde15e5f24a0f8fa08e1e33ed89d3b/teleop/robot_control/robot_arm.py

Copyright 2025 HangZhou YuShu TECHNOLOGY CO., LTD. Apache-2.0; see LICENSE.
Adapted on 2026-10-16 in `g1d_pico/arm_ik.py`.

Retained: Pinocchio/CasADi forward kinematics; IPOPT; objective weights
50(position), 1(rotation), .02(joint regularization), .1(previous-solution
regularization); 30 iterations; weighted history .4/.3/.2/.1; a local X-axis
5 cm operational frame at each wrist; static RNEA model gravity compensation.
Home joint target zero follows upstream `ctrl_dual_arm_go_home`, not a claimed
G1-D factory collision-free recovery service.

Changes: use official G1-D URDF and DDS interface; inactive arms constrained to
last target; .02 rad limit margins; reset filter on clutch/takeover; bound
Cartesian increments and joint speed; reject failed solver iterates (no debug
iterate commands); 80 ms IPOPT CPU budget (not a hard wall-clock deadline);
model torque calculated using measured arm and waist positions with zero
velocity/acceleration; checked and ramped in C++; no Meshcat or model pickle.

The G1-D URDF naming is misleading: `torso_Joint` has Z axis (SDK waist yaw,
12); `Yaw_Joint` has Y axis (dedicated `rt/waistpitchstate.q`, not lowstate[14]).
Gravity mapping uses these actual yaw/pitch feedback angles.
Gravity assumes an upright mobile base, empty hands, and neutral finger joints;
it is not payload or contact-force compensation. Joint trajectory homing adds
quintic interpolation, PICO readiness/countdown, cancel, timeout and measured
settling. There is no collision planner.
