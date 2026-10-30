# G1-D 模型与接口来源

- 模型：宇树官方 `unitree_ros/robots/g1_d_description/g1_d.urdf`。
- 下载地址：https://raw.githubusercontent.com/unitreerobotics/unitree_ros/master/robots/g1_d_description/g1_d.urdf
- 获取日期：2026-10-16。
- 本地文件 SHA256：`46527573a7140c7129c3587ba5591f269974deb893a0b095ebca500028b8bf40`。
- 仓库许可：同目录 `LICENSE`（BSD 3-Clause）。URDF 原样保留，未修改；加载运动学及惯性参数用于模型重力补偿，不需要 STL 网格。
- DDS 控制依据：官方 SDK2 提交 `63096d0ac0c5d2dec9d6e0c22cd5233410ca2f36` 的 `example/g1/g1d/g1d_arm_example.cpp`。
- 官方例程地址：https://github.com/unitreerobotics/unitree_sdk2/blob/63096d0ac0c5d2dec9d6e0c22cd5233410ca2f36/example/g1/g1d/g1d_arm_example.cpp
- 专用俯仰例程（同一 SDK2 提交）：https://github.com/unitreerobotics/unitree_sdk2/blob/63096d0ac0c5d2dec9d6e0c22cd5233410ca2f36/example/g1/g1d/g1d_waist_example.cpp
- SDK2 许可见项目 `bridge/UNITREE_SDK2_LICENSE`。

双臂索引 15..21 / 22..28、HG CRC、mode_pr=0、mode_machine 来自状态反馈以及 PD 参数遵循 G1-D 专用示例。
yaw 使用 lowcmd/lowstate[12]，pitch 使用专用 `rt/waistpitchcmd` (`MotorCmd_`) / `rt/waistpitchstate` (`MotorState_`)；lowcmd/lowstate[14] 不再作为俯仰入口。同一 C++ 进程管理两组接口，从实际角度接管，不使用普通 G1 的 rt/arm_sdk 权重接管方式。
俯仰消息参数遵循专用例程的 mode=1、kp=500、kd=12、dq=0、tau=0；目标轨迹、A/B、独立反馈时效检查为本项目适配，不执行例程的自动回零/往返动作。
模型的名称与索引需与实际 G1-D 七自由度双臂对应；不能用于其他机器人或不同关节布局。

官方 G1 IK 方法来源、适配项和双臂模型核对见 `../../third_party/unitree_xr/NOTICE.md` 与 `MODEL_COMPARISON.md`。
