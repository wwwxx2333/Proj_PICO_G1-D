# PC2：右摇杆转腰、俯仰与立柱升降

2026-10-26。代码运行在 PC2，部署与真机启动由操作者执行。

本版增加可配置的俯仰跟随偏差与局部不跟随处理。俯仰继续使用已修正的真机接口：命令改为官方 `rt/waistpitchcmd`（`MotorCmd_`），反馈改为 `rt/waistpitchstate`（`MotorState_`）。双臂和 yaw 继续使用 `rt/lowcmd` / `rt/lowstate`，其第 14 号槽位不再作为俯仰的命令或反馈入口。

## 操作映射

| 输入 | 动作 |
|---|---|
| 左摇杆上下 / 左右 | 底盘前后 / 转向 |
| 右摇杆左右 | 腰部 yaw 左转 / 右转 |
| 右摇杆上下（默认 LIFT 模式） | 立柱上升 / 下降，双臂启动脚本默认启用 |
| 左 X | 切换右摇杆上下：LIFT（升降）↔ PITCH（俯仰）；切换后右摇杆须回中 |
| 右摇杆上下（PITCH 模式） | 前推向前俯身，后拉向直立方向返回；默认 0～30° |
| 右摇杆斜推 | 转腰与当前选择的升降或俯仰同时进行 |
| 左右侧握键 | 对应手臂跟随手柄 |
| 左右食指扳机 | 使用 `--with-hands` 时控制对应 Dex3 手 |
| 右 A | 两个摇杆回中、侧握键松开后使能；启用手部时食指扳机也需松开 |
| 右 B | 停止底盘和升降，双臂和腰部保持；不是断电急停 |
| 左 Y | 停止运动并请求双臂 14 关节归位；腰部和立柱不归零 |

转腰、俯仰、升降无需按住侧握键。摇杆回中后，转腰/俯仰按配置减速至保持，升降发送零速度。B 或输入失联时，停止两个腰部角度目标的积分并停止底盘/升降。

X 只在按下沿切换，长按不会反复切换；切换立即清零升降请求并停止俯仰目标积分，保持最后俯仰目标。新模式等待**之后的一帧有效输入中右摇杆左右、上下都回中**，再接受上下输入。回中等待期间右摇杆左右仍可转腰。不会把还没松开的升降摇杆直接当成俯仰请求，也不会同时下发升降和俯仰速度。

已按 A 使能时，切换只需 X 和回中，无需再次按 A。未使能时 X 可以预选模式，但不能让机器人运动。B、Y、归位和失联优先于 X；B/A 不改变已选模式，程序重启恢复 LIFT。`--without-lift` 只禁用升降，仍能用 X 切到 PITCH。实体左 X 对应 Vuer 左侧 `aButton`，也兼容 `xButton`，与右 A 使能分开。

双臂目标使用躯干坐标：松开侧握键时，转腰或俯仰不会改变双臂相对躯干的关节目标，手臂和手中物体会一起随躯干运动；升降时也会随上身升降。程序不会补偿腰部/立柱运动来让手爪保持世界坐标不动。仍可同时握住侧握键调整手臂。臂部重力补偿使用实际腰部 yaw/pitch 反馈重算。

## 同步与编译

先退出 PC2 上旧控制程序。在主机执行原来的全项目同步：

```bash
rsync -av \
  --exclude='.git/' --exclude='__pycache__/' --exclude='*.pyc' \
  --exclude='/.local/' --exclude='/deps/' --exclude='/logs/' \
  --exclude='/bridge/build/' --exclude='/bridge/build-arm64/' --exclude='/bridge/lib/' \
  --exclude='*.pem' \
  /home/lab209/Proj/Proj_PICO_G1-D/ \
  unitree@192.168.1.118:/home/unitree/Proj/Proj_PICO_G1-D/
```

**此次修改俯仰轨迹、局部恢复与状态协议，需要在 PC2 重新编译一次，即使上一版官方俯仰接口已编译过也不能省略。** 已有双臂环境无需重装 IK 依赖：

```bash
conda activate pico_g1d
cd /home/unitree/Proj/Proj_PICO_G1-D
bash deploy/build_on_pc2.sh /home/unitree/Proj/Proj_PICO_G1-D/deps/unitree_sdk2
```

新版 Python 与 C++ 使用 `TARGET6` / `STATE6`，一条目标消息同时携带双臂、重力补偿、yaw/pitch 速度和俯仰使能位；反馈保留独立的俯仰来源时间戳，并增加限位原因、局部恢复状态、摇杆请求速度、无进展计时和触发前偏差。旧版 `STATE5` 或更早版本会被拒绝并提示重编译；不能只同步 Python。同一个 C++ 进程内新增官方俯仰话题的发布/订阅，不启动第二个腰部控制程序。

## 启动

按用户确认的 0.42 m 立柱行程，项目采用 `rt/hispeed_state.y = 0～0.42 m` 的反馈范围配置。双臂启动脚本现在默认启用升降，两端由已有 C++ 保护各留 1 cm，正常停止边界约为 **0.01～0.41 m**。这属于本项目配置依据，不表示新增了官方接口零点声明。

直接使用原命令即可，不再要求手动输入上下限或添加 `--with-lift`：

```bash
bash deploy/run_pc2_arms.sh --with-hands
```

启动时应显示 `Column lift ENABLED` 和 `Lift directional stops: [0.010, 0.410] m`。
保存日志可使用：

```bash
cd /home/unitree/Proj/Proj_PICO_G1-D
mkdir -p logs
set -o pipefail
bash deploy/run_pc2_arms.sh --with-hands \
  2>&1 | tee "logs/pico_g1d_$(date +%Y%m%d_%H%M%S).log"
```

只想关闭升降时使用：

```bash
bash deploy/run_pc2_arms.sh --with-hands --without-lift
```

`--with-lift` 仍兼容旧命令。`--height-min-m` / `--height-max-m` 可覆盖默认反馈窗口；注意 C++ 还会在窗口内各保留最多 1 cm，不要为了得到 0.01～0.41 m 停止边界再把反馈窗口也填成这两个值。`--without-lift` 不能同时携带高度/升降速度选项。独立的 `run_pc2_base.sh` 仍默认只控制底盘。

不控制手指时去掉 `--with-hands`。需要保留启动时的双臂姿态时添加已有的 `--skip-home`；否则维持原来的启动归位流程。归位期间底盘、升降、转腰和俯仰均停止，完成后按 A 使能。

PICO 仍使用 PC2 的 HTTPS 页面：

```text
https://192.168.1.118:8012/?ws=wss://192.168.1.118:8012
```

## 参数与边界

腰部参数位于 `config/arms.json`：

```json
{
  "waist_yaw_limit_rad": 0.5235987756,
  "waist_yaw_speed_rad_s": 0.10,
  "waist_yaw_accel_rad_s2": 0.20,
  "waist_pitch_min_rad": 0.0,
  "waist_pitch_max_rad": 0.5235987756,
  "waist_pitch_speed_rad_s": 0.08,
  "waist_pitch_accel_rad_s2": 0.16,
  "waist_pitch_max_error_rad": 0.10,
  "waist_pitch_stall_timeout_s": 1.5,
  "waist_pitch_min_progress_rad": 0.005
}
```

上面只展示腰部字段，不要用它覆盖整份配置。默认 yaw 转角 ±30°、最大目标速度约 5.7°/s、加/减速度约 11.5°/s²。pitch 范围 0～30°、最大目标速度约 4.6°/s、加/减速度约 9.2°/s²。新增俯仰版本首次需要编译；以后只改 JSON，退出再启动即可，无需重新编译。

yaw 窗口以机器人腰部零位为中心；pitch 的 0 表示模型关节零位（直立），正方向为前倾。俯仰上下限不采用对称 ±30°：官方模型 pitch 下限约 -2.5°，本项目默认只允许直立至前倾 30°。这些是本项目的软件目标限制，不是额定能力或已验证负载包络。程序没有全身碰撞规划。

接管从实际角度开始，不自动回零；若起始角度在软件窗口外，仅允许向窗口内移动，不会跳到边界。在 LIFT 模式或 X 切换等待期间，pitch 保持目标角度；若在 20° 前倾时切回升降，上身会维持该姿态，不自动立直。Y 仅归位双臂，不归位腰部。

pitch 参数接受范围：min 为 -0.02～0 rad，max 为 0.087～1.0472 rad，速度 0.01～0.15 rad/s，加速度 0.02～0.30 rad/s²，Python 和 C++ 都会检查。俯仰目标偏差窗口由 `waist_pitch_max_error_rad` 配置，默认从 0.05 改为 **0.10 rad（约 5.7°）**，接受范围 0.05～0.15 rad。目标以原有速度/加速度生成，偏差达到窗口后不再向外累积；yaw 保持原有 0.10 rad 限制。外力使实际位置突变时，已有目标可能暂时位于窗口外，不会仅因收包把目标瞬间拉到窗口边界。

### 俯仰持续不跟随与恢复

`waist_pitch_stall_timeout_s` 默认 1.5 s，接受范围 0.5～5 s；`waist_pitch_min_progress_rad` 默认 0.005 rad（约 0.3°），接受范围 0.001～0.02 rad。三项新参数是本项目的初始工程配置，不是宇树官方推荐值，尚未验证具体负载下的物理响应。只改 JSON 后重新启动即可生效；首次升级仍须编译 C++。

仅在 PITCH 模式、输入与反馈有效、持续向同一方向推杆且已经到达目标偏差上限时开始检查。实际角度沿请求方向累计前进达到最小进展量，才重新开始计时；收到新消息本身不算进展。小幅抖动及反向运动不能清除计时。偏差附近使用一个最小进展量的滞回区，避免噪声让计时反复归零。松杆、反向请求、切换模式、正常角度限位会清除尚未触发的计时。

达到超时且没有足够进展时，仅俯仰进入 `PITCH_NOT_FOLLOWING/WAIT_CENTER`：取消积累的目标，捕获当前有效实测角度作为固定保持目标，清零俯仰目标速度。继续发送位置保持指令，而非关闭电机。双臂、转腰、底盘、升降、Dex3 的总使能不因此重置，不要求重新按 A。其他原有故障（例如 PICO/上身反馈过期）仍按原来的策略处理。

恢复步骤：保持或切回 PITCH 模式，把右摇杆**左右、上下都回中**，看到 `WAIT_PUSH` 后重新推动上下轴。C++ 要求回中和新推动分别来自更晚的有效输入，旧帧不能恢复。持续推住、仅左右转腰、在 LIFT 模式收到零俯仰速度、切换 X、重新接管或归位，都不会自行解除俯仰故障。恢复时重新从实测角度生成目标，不追赶故障前的目标。正常跟随时松杆仍使用原来的减速保持。

如果再次遇到持续不跟随，程序按同一规则局部保持，不会自动提高偏差上限或电机增益。放宽到 0.10 rad 解决的是已经确认的软件目标限制，不能保证机器人底层一定恢复回抬；若仍不动，应检查底层执行状态。

专用俯仰消息使用官方 `g1d_waist_example.cpp` 的 `mode=1`、`kp=500`、`kd=12`、`dq=0`、`tau=0`。实际位置目标仍由现有摇杆限速轨迹生成：从专用反馈的当前角度接管，不执行官方演示中的自动回零或 0/0.8 rad 往返动作。双臂和转腰的 PD 参数保持原值。`MotorCmd_` 与双臂 `LowCmd_` 是不同消息类型，专用俯仰消息不附加 LowCmd 的 CRC。

俯仰反馈有独立时间戳，不能被不断到达的 `lowstate` 刷新。启动时最多等待 5 秒，必须收到双臂和专用俯仰的有效新鲜反馈后才完成初始化；缺少专用反馈会明确报 `rt/waistpitchstate`，不会用旧槽位或零值顶替。现有上身反馈的新鲜度要求为 250 ms，本版对专用俯仰单独执行该检查；超时时停止控制并给出话题和数据年龄。这个时效保护属于本项目，不是官方演示的退出策略。没有改变 Dex3 缺帧处理逻辑。

立柱默认归一化速度上限为 0.15，可用 `--max-lift 0.10` 降低。这个数字不是 m/s。下行在约 0.01 m 停止，上行在约 0.41 m 停止。若启动时位于 0～0.01 m，允许向上；若位于 0.41～0.42 m，允许向下，避免余量区导致无法回到工作范围。若反馈缺失或已超出整个 0～0.42 m 窗口，仅停止升降，底盘和转腰仍可操作。停止边界是软件下发零速度的阈值，不是实测机械制动距离。

## 日志与实现位置

双臂日志新增：

```text
G1-D upper-body bridge: protocol=STATE6, ...
Pitch interface: cmd=rt/waistpitchcmd, state=rt/waistpitchstate, kp=500, kd=12; lowcmd[14] unused. ...
Pitch feedback READY: source=rt/waistpitchstate, q=... rad, age_ms=...
Left X: RIGHT_Y=PITCH / WAIT_CENTER; centre right stick to continue
RIGHT_Y=PITCH
ARMS FOLLOW=HOLD/HOLD WAIST=+5.0deg target=+5.1deg rate=+0.050rad/s PITCH=+10.0deg target=+10.1deg rate=+0.040rad/s request=+0.080rad/s error=+0.10deg feedback_age=4.0ms stall_elapsed=0.00s PITCH_LIMIT=NONE
```

`RIGHT_Y` 为上下轴当前模式；`WAIT_CENTER` 表示需要右摇杆回中。`WAIST` 为 yaw 反馈，`PITCH` 明确来自 `waistpitchstate`，各自的 `target` 是下发角度，`rate` 是平滑后的目标速度，yaw 的 `LIMITED` 保持原含义；俯仰改为 `PITCH_LIMIT=ANGLE_MIN`、`ANGLE_MAX`、`FOLLOW_ERROR`，原因同时成立时以 `+` 连接。`request` 是经过死区处理的摇杆请求速度，`error=target-实际角度`，`stall_elapsed` 是无足够进展的计时。`feedback_age` 是专用俯仰反馈年龄；重力补偿也使用这个反馈角度。

俯仰不跟随时立即输出一次事件（数字仅为示意）：

```text
PITCH_NOT_FOLLOWING: request=-0.08 rad/s, measured=0.4 rad, previous_target=0.3 rad, error=-0.1 rad, elapsed=1.5 s; cancelled catch-up target, holding measured pitch. ...
PITCH_RECOVERY: centred; waiting for a new pitch stick push.
PITCH_RECOVERY: resumed from measured angle ... rad.
```

周期状态还会显示 `PITCH_NOT_FOLLOWING/WAIT_CENTER` 或 `WAIT_PUSH`。故障时目标已重置为实测值，周期 `error` 会接近零；导致故障的原始偏差保存在即时事件与状态消息的 `pitch_stall_error_rad` 中，不能把后来的零偏差当成恢复。

立柱日志在状态变化时输出：

```text
LIFT READY height=... m feedback_age_ms=... window=[...] requested=... sent=...
```

- `NO_FEEDBACK`：高度反馈缺失或过期，升降指令置零。
- `OUTSIDE_WINDOW`：反馈不在配置窗口内，升降指令置零。
- `UPPER_LIMIT` / `LOWER_LIMIT`：到达方向限位，阻止继续向外移动。
- `READY`：反馈满足窗口条件；只有实际推动右摇杆上下才有非零升降速度。

主循环打印的 `lift=` 是摇杆请求，最终受保护后的值看立柱日志的 `sent=` 或已有的底盘 trace。不要把收到摇杆输入当成已经移动的证据。

- `g1d_pico/control.py`：右摇杆两轴映射。
- `g1d_pico/right_stick.py`：X 按下沿、升降/俯仰模式及回中确认。
- `g1d_pico/arms.py`、`arm_bridge.py`：双臂与腰部统一命令及反馈。
- `bridge/g1d_waist_guard.hpp`：独立腰部轨迹限制。
- `bridge/g1d_arm_bridge.cpp`：同一进程管理 `rt/lowcmd` 的双臂/yaw 与独立 `rt/waistpitchcmd` 的 pitch，统一 A/B 接管与保持。
- `bridge/g1d_upper_body_io.hpp`：官方消息构造、两路反馈解码与独立时间戳，便于离线检查数据来源及实际下发字段。
- `bridge/g1d_lift_guard.hpp`、`g1d_agv_bridge.cpp`：高度过滤与官方 `AgvClient.HeightAdjust` 调用。

本次通过 84 项不访问网络的 Python 测试、C++ 双臂、腰部/升降、俯仰不跟随保护及 SDK 消息构造/解码四组测试、C++→Python 状态消息往返检查及主机 SDK2 编译。未改动的两项回环网络测试本次未重复执行。覆盖原有 X/A/B/归位操作，以及旧槽位为 0/无效值时仍采用专用俯仰反馈、两路时间戳互不刷新、接管不跳零、lowcmd[14] 不激活、缺少专用反馈不接管。新增覆盖日志中四个角度的旧限制突破、到达新偏差上限后的局部保持、正反方向进展、抖动、回中后新推动、模式/接管不绕过恢复条件、真实 elapsed time 及正常回直/角度限位。没有部署或运行真机程序，PC2 ARM64 编译与实际效果由操作者执行确认。

接口来源：[宇树 G1-D 俯仰例程](https://github.com/unitreerobotics/unitree_sdk2/blob/63096d0ac0c5d2dec9d6e0c22cd5233410ca2f36/example/g1/g1d/g1d_waist_example.cpp)、[双臂例程](https://github.com/unitreerobotics/unitree_sdk2/blob/63096d0ac0c5d2dec9d6e0c22cd5233410ca2f36/example/g1/g1d/g1d_arm_example.cpp)、[G1-D URDF](https://github.com/unitreerobotics/unitree_ros/blob/master/robots/g1_d_description/g1_d.urdf)。yaw 使用 lowstate[12]，pitch 使用专用 MotorState.q；URDF 对应 `torso_Joint`（Z 轴）及 `Yaw_Joint`（Y 轴）。双臂例程中的 `WaistPitch=14` 枚举不能替代专用俯仰接口。
