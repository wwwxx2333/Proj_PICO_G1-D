# PICO 4 Ultra 控制 G1-D 的 Dex3-1 双手

本模式沿用 PC2 上的相机、底盘和双臂程序，增加左右 Dex3-1 三指手。主机保存源代码；同步、ARM64 编译和实机启动由操作者执行。

左右食指扳机分别控制对应手的三指弯曲预设，左右侧握键仍控制双臂。归位完成且两个扳机、侧握键均松开后，按右 A 接管。第一次扣动扳机前，手保持接管时的实测姿态；之后扳机值 0～1 映射到开合目标。右 B 停止底盘并保持双臂与手部，左 Y 请求双臂归位。

这不是七个指关节的独立遥操作，也不按夹持力闭环控制。Dex3 指令沿用 SDK2 官方示例的 7 电机顺序、限位、模式位和低增益设置；C++ 桥以 100 Hz 发布 `rt/dex3/left|right/cmd`，并订阅 `rt/dex3/left|right/state` 与 `rt/lf/dex3/left|right/state`。

## 手部反馈暂停时的行为

手部反馈没有“超过 250 ms 就释放接管或退出”的门槛。某一侧暂时没有新状态时，桥不再接受该侧的新位置目标；如果间隔达到 180 ms，桥把目标冻结在最后的限速命令位置并继续发布保持指令。这个间隔允许低频反馈话题约 10 Hz 的正常帧间隔。另一侧继续正常工作。该侧收到新反馈后恢复反馈处理，无须重按 A。如果长间隔期间扣住了该侧食指扳机，须先松开该扳机，才能再次让它运动。状态行会分别显示 `left=FEEDBACK_HOLD` 或 `right=FEEDBACK_HOLD`；反馈恢复但仍需松扳机时显示 `WAIT_TRIGGER_RELEASE`。这项恢复规则仅针对反馈中断，不用于正常抓握受阻。

这不会修复底层 DDS/手部控制器的反馈断流，也不能保证断流期间的实际夹持力或物体不掉落。250 ms 的 **PICO 输入** 看门狗仍保留；输入失联时 C++ 停止目标跟随并保持最后命令位置。关节限位、无效目标、DDS 写失败、进程异常等检查仍保留；普通位置跟随误差不再触发全局停止。只有初次启动时要求收到至少一帧有效的双手状态；接管必须使用 A 事件之后到达的新状态。

## 抓住物体时的控制

Python 只把扳机开合意图映射成 7 个关节目标；C++ 在最终发布前统一执行受阻保持、偏差窗口和速度约束。去掉了 Python 的第二套目标积分，以及 `following error exceeds 0.45 rad` 导致整机停止的逻辑。普通位置不匹配不会要求重新按 A，也不会要求先松开扳机。

每个弯曲关节独立工作，左右手使用镜像闭合方向：

- `FOLLOW`：正常跟随目标；`LEAD_LIMIT` 表示目标受到相对于实测位置的偏差窗口限制。
- `BLOCKED[关节编号]`：连续新反馈显示闭合方向存在持续误差、关节移动很慢，保持一次捕获的闭合参考位置。其他关节可继续闭合。它表示“受阻候选”，不能证明物体已经稳定抓住。
- `OPENING[关节编号]`：回松扳机后，从受阻时的保持位置开始减小闭合目标，即使扳机的绝对映射目标仍大于保持位置也能张开。再次加深扳机可重新闭合；完全松开回到开手预设。无需松到底后重新按 A。

保持参考不会随每一帧实测角度反复增加，也不会反复改成实测角度而消除夹持偏差。反馈中断、PICO 输入失联和普通抓握受阻是三个不同状态。受阻保持不增加电机增益，仍为 `kp=0.5、kd=0.1`；这是有约束的位置控制，不是标定后的恒力抓取，不能保证任何物体的夹持力和防滑效果。

参数集中在 `bridge/g1d_hand_grasp.hpp` 的 `GraspSettings`，修改后需要重新编译 C++：

| 参数 | 初始值 | 含义 |
| --- | --- | --- |
| `max_lead` | 0.20 rad | 有效运动目标相对实测位置的偏差窗口 |
| `blocked_error` | 0.10 rad | 实际发送位置在闭合方向的误差门槛 |
| `blocked_speed` | 0.05 rad/s | 位置差分、滤波后的低速判据 |
| `blocked_duration` | 0.15 s | 需由连续新反馈确认的持续时间 |
| `intent_deadband` | 0.02 rad | 单关节对应的回松/再闭合意图滞回；不是扳机百分比 |
| `command_speed` | 0.60 rad/s | 最终命令限速，保留此前实际有效的速度上限 |
| `feedback_gap` | 0.18 s | 反馈暂停时冻结目标的时间 |

这些是软件初始参数，并未针对具体物体标定。偏差窗口限制的是下一运动目标；如果外力突然改变实测位置，已经发出的命令可能暂时超出窗口，程序以限速返回，不瞬间跳变命令。接管时仍允许沿用实测位置的既有 0.05 rad 限位容差；后续主动运动目标严格使用名义限位。

`HANDSTATE2` 同时返回原始关节反馈时间、最后一次成功 DDS Write 的时间/位置、扳机请求位置和受阻/张开/限幅标记。DDS Write 成功不等于电机执行确认；新命令时间不会刷新关节反馈时间。Python 检测到旧版 `HANDSTATE` 会明确要求重新编译，避免新旧协议混用。

日志中 `Dex3 grasp` 记录每个关节的状态转换，包括 `requested`（扳机请求）、`sent`（实际发送）、`measured`（实测）、误差、速度和反馈年龄；`Dex3 mode` 记录每侧手部的状态变化。可用下面的命令筛选保存的日志：

```bash
rg 'Dex3 grasp|Dex3 mode|Dex3 HOLD|Control fault|NO FRESH PICO' logs/你的日志文件.log
```

## 主机同步

先退出 PC2 上旧的遥操作程序，在主机执行：

```bash
rsync -av \
  --exclude='.git/' --exclude='__pycache__/' --exclude='*.pyc' \
  --exclude='/.local/' --exclude='/deps/' --exclude='/logs/' \
  --exclude='/bridge/build/' --exclude='/bridge/build-arm64/' --exclude='/bridge/lib/' \
  --exclude='*.pem' \
  /home/lab209/Proj/Proj_PICO_G1-D/ \
  unitree@192.168.1.118:/home/unitree/Proj/Proj_PICO_G1-D/
```

本次同时修改了 Python 与 C++ 桥，需要两者一起同步。PC2 上保留既有的 `deps/unitree_sdk2`、相机服务、证书和日志。

## PC2 编译与启动

```bash
conda activate pico_g1d
cd /home/unitree/Proj/Proj_PICO_G1-D
bash deploy/build_on_pc2.sh /home/unitree/Proj/Proj_PICO_G1-D/deps/unitree_sdk2
bash deploy/run_pc2_arms.sh --with-hands
```

启动日志中的 `protocol=HANDSTATE2, bounded grasp enabled` 表示已运行本次更新后的手部二进制。不要仅同步 Python 后继续使用旧的 ARM64 桥。相机和 TeleVuer 仍使用 PC2 上已有配置。不加 `--with-hands` 时只运行相机、底盘和双臂。

若要保存日志，可以在项目目录运行：

```bash
mkdir -p logs
set -o pipefail
PYTHONUNBUFFERED=1 bash deploy/run_pc2_arms.sh --with-hands 2>&1 | tee "logs/dex3_takeover_$(date +%Y%m%d_%H%M%S).log"
```

首次实机操作时让手中无物体，保持手指周围无障碍物，并从小幅度扳机行程开始。项目未在当前主机上连接 G1-D 验证该修改；实机行为需在 PC2 编译后确认。
