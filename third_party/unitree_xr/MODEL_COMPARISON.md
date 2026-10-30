# 双臂模型核对（2026-10-16）

与来源提交的 `assets/g1/g1_body29_hand14.urdf` 对比，G1-D 的 14 个手臂关节
origin（xyz/rpy）、axis、limit（位置、速度、力矩）逐项一致。
这不代表底盘、腰部、DDS 接口和所有惯量一致；运行始终加载 G1-D 模型。

| 关节 | origin / axis / limit |
|---|---|
| left_shoulder_pitch_joint | 相同 |
| left_shoulder_roll_joint | 相同 |
| left_shoulder_yaw_joint | 相同 |
| left_elbow_joint | 相同 |
| left_wrist_roll_joint | 相同 |
| left_wrist_pitch_joint | 相同 |
| left_wrist_yaw_joint | 相同 |
| right_shoulder_pitch_joint | 相同 |
| right_shoulder_roll_joint | 相同 |
| right_shoulder_yaw_joint | 相同 |
| right_elbow_joint | 相同 |
| right_wrist_roll_joint | 相同 |
| right_wrist_pitch_joint | 相同 |
| right_wrist_yaw_joint | 相同 |

G1 URDF SHA256: `8bbf006633fc50b616f665c7a970780cc296577a0adfd7d28b049e751c238735`
G1-D URDF SHA256: `46527573a7140c7129c3587ba5591f269974deb893a0b095ebca500028b8bf40`
