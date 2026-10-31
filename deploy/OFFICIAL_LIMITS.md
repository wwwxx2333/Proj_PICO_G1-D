# G1-D 配置范围核验（2026-10-30）

检查范围：`config/body.jsonc` 的 16 项、`config/arms.jsonc` 的 9 项、`config/recording.jsonc` 的 6 项，共 31 项；同时检查 Python 映射、配置加载、旧远程入口、C++ 指令校验和目标限幅。

本次只调整合法范围，不提高当前文件中的运行速度。所有配置仍逐项注释。官方接口范围、官方模型参数、项目调参范围是三种不同依据；不能把项目建议值称为官方限制，也不能把模型电机最大速度直接当作遥操作推荐速度。

## 核验依据

1. [宇树 SDK2 AgvClient](https://github.com/unitreerobotics/unitree_sdk2/blob/main/include/unitree/robot/g1/agv/g1_agv_client.hpp)：`Move` 的 `vx` 为 [-1.5, 1.5] m/s，`vyaw` 为 [-0.6, 0.6] rad/s；不支持横向 `vy`；`HeightAdjust` 的 `vz` 为 [-1, 1] 归一化值。项目固定 SDK 提交 `63096d0ac0c5d2dec9d6e0c22cd5233410ca2f36` 的相应注释一致。
2. [G1-D 官网规格](https://www.unitree.com/cn/G1-D/)：旗舰版最高底盘速度 1.5 m/s；整机高度 1260～1680 mm，差值为 0.42 m；腰部 Z 轴 ±155°、Y 轴 -2.5°～135°。
3. [官方 G1-D URDF](https://github.com/unitreerobotics/unitree_ros/blob/master/robots/g1_d_description/g1_d.urdf)：本次角度校验使用模型中的弧度数值，下表说明具体关节。项目已有模型相关数值与本次核验一致，不替换模型文件。
4. [官方腰部控制例程](https://github.com/unitreerobotics/unitree_sdk2/blob/main/example/g1/g1d/g1d_waist_example.cpp)及[双臂例程](https://github.com/unitreerobotics/unitree_sdk2/blob/main/example/g1/g1d/g1d_arm_example.cpp)演示控制接口；其中演示角度、增益和周期不等于官方公布的全部合法范围。

**立柱速度的官方资料存在差异：**官网写约 60 mm/s，SDK 注释写 `velocity = vz × 76.5 mm/s`。本程序依照两者没有歧义的接口输入 [-1, 1] 校验，不把任一速度系数视为已实测标定。0.42 m 是行程，不能仅用整机高度差证明 `rt/hispeed_state.y` 的绝对零点；本机沿用的反馈窗口 0～0.42 m 应以实机反馈标定为准。

## 身体配置：16 项

下表中的范围是更新后代码实际接受的范围。标注“项目”的项没有对应的官方配置范围，保留现有工程约束并明确来源；未将其伪装成官方规格。

| 字段 | 实际接受范围 | 依据与生效方式 |
|---|---|---|
| `deadzone` | 0 ≤ 值 < 1 | 项目摇杆映射；超死区后重新缩放 |
| `max_forward_mps` | 0～1.5 m/s | 官方 SDK 的 vx 幅值；0 禁用此轴输出 |
| `max_yaw_rps` | 0～0.6 rad/s | 官方 SDK 的 vyaw 幅值；0 禁用此轴输出 |
| `max_lift_normalized` | 0～1 | 官方 SDK 的 vz 幅值；0 禁用升降输出 |
| `height_min_m` | 有限数且 ≥ 0，与 max 联合校验 | 项目反馈坐标窗口，非整机高度；官网未确定反馈零点 |
| `height_max_m` | 大于 min，且 max − min ≤ 0.42 m | 按官方整机高度差约束窗口跨度；移除旧 3 m 任意上限 |
| `waist_yaw_limit_rad` | 0～2.7053 rad | 官方 URDF 对称角度幅值，完整关节约 ±155°；0 表示窗口收至零位，不是关闭电机 |
| `waist_yaw_speed_rad_s` | 0.02～0.20 rad/s | 项目目标轨迹速度约束；不是官方模型速度上限 |
| `waist_yaw_accel_rad_s2` | 0.05～0.50 rad/s² | 项目目标轨迹加速度；官方未公布同名参数范围 |
| `waist_pitch_min_rad` | -0.04363 ≤ min < max | 官方 URDF 的俯仰下界，约 -2.5° |
| `waist_pitch_max_rad` | min < max ≤ 2.3562 rad | 官方 URDF 的俯仰上界，约 135°；窗口不必包含 0 |
| `waist_pitch_speed_rad_s` | 0.01～0.15 rad/s | 项目目标轨迹速度约束；不是官方模型速度上限 |
| `waist_pitch_accel_rad_s2` | 0.02～0.30 rad/s² | 项目目标轨迹加速度 |
| `waist_pitch_max_error_rad` | 0.05～0.15 rad | 项目目标与实测位置的允许领先量 |
| `waist_pitch_stall_timeout_s` | 0.5～5 s | 项目停滞判定时长，非 DDS 缺帧超时 |
| `waist_pitch_min_progress_rad` | 0.001～0.020 rad | 项目同向进展判据 |

Python、C++ 和旧远程入口的底盘/立柱速度边界一致。俯仰端点同时检查 min < max，防止单项合法但窗口倒置。数值须有限且不能是布尔值。高度跨度校验有 1e-7 m 的浮点舍入容差。

## 双臂配置：9 项

这九项是本项目手柄映射、轨迹生成、归位和滤波参数。官方未提供与这些字段对应的配置范围，因此保留项目允许值；它们并非官方关节速度或工作空间极限。

| 字段 | 项目允许范围 | 说明 |
|---|---|---|
| `joint_speed_rad_s` | 0.05～1.0 rad/s | 每侧双臂关节目标变化速率上限 |
| `home_speed_rad_s` | 0.05～0.20 rad/s | 归位目标速率；归位轨迹仍有至少 4 s 时长 |
| `control_hz` | 10～60 Hz | 启用双臂时的共享 Python 循环目标频率，不保证实际频率 |
| `position_scale` | 0.1～1.0 | 手柄相对平移缩放 |
| `clutch_translation_m` | 0.1～0.8 m | 单次侧握相对位移阈值，不是臂展 |
| `clutch_rotation_rad` | 0.2～2.0 rad | 单次侧握相对旋转阈值 |
| `cartesian_speed_m_s` | 0.05～0.8 m/s | IK 前末端目标平移速率上限 |
| `rotation_speed_rad_s` | 0.2～3.0 rad/s | IK 前末端目标旋转速率上限 |
| `filter_weights` | 1～8 个正有限数，和为 1 | 从新到旧；和使用 1e-6 绝对容差 |

C++ 双臂速率上限由 Python 按 `max(0.30, 1.2 × 跟随速度, 1.2 × 归位速度)` 传入，最高 1.2 rad/s；它为 Python 目标留出余量，不是另一处将 1.0 rad/s 配置无声压回 0.25 的固定限制。实际目标仍受末端限速、IK 可达性、关节限速和滤波共同影响。

官方 URDF 的位置与速度字段如下（速度列是模型字段，不是推荐运行参数）：

| 关节 | 官方模型位置范围（rad） | 模型 velocity（rad/s） |
|---|---|---:|
| 腰部左右转 `torso_Joint` | -2.7053～2.7053 | 5 |
| 腰部俯仰 `Yaw_Joint`（轴为 Y） | -0.04363～2.3562 | 5 |
| 左右肩 pitch | -3.0892～2.6704 | 37 |
| 左肩 roll / 右肩 roll | -1.5882～2.2515 / -2.2515～1.5882 | 37 |
| 左右肩 yaw | -2.618～2.618 | 37 |
| 左右肘 | -1.0472～2.0944 | 37 |
| 左右腕 roll | -1.972222054～1.972222054 | 37 |
| 左右腕 pitch / yaw | -1.614429558～1.614429558 | 22 |

双臂已有的 0.02 rad 关节端点内缩余量仍保留；上述表格是原始模型范围，不能把余量后的目标范围误报为官方机械范围。Dex3 当前没有独立 JSONC 配置项，本次不改手指姿态、力矩和抓握逻辑。

## 录制配置：6 项

这些是本项目数据保存设置，不存在“机器人官方允许帧率/缓存配额”这一对应范围。

| 字段 | 项目允许范围 | 生效条件 |
|---|---|---|
| `spool` | 非空路径字符串 | 相对项目根目录，或绝对路径、~；目录仍受文件权限限制 |
| `max_cache_gb` | 0.01～10000 GB | 缓存总配额；实际受可用磁盘限制 |
| `min_free_gb` | 0～10000 GB | 文件系统预留空间 |
| `image_fps` | 1～60 FPS | 每路图像保存目标采样率；不能高于实际源帧率且受写盘负载影响 |
| `cameras` | head、left_wrist、right_wrist 的不重复列表，也可 [] | 相机需开启 ZMQ 输出 |
| `camera_config_port` | 整数值 1～65535 | TCP 端口；需与 TeleImager 服务端一致，默认 60000 |

缓存参数只决定保存策略，不改变控制速度；帧率配置不修改相机源帧率。本次也修正了自定义端口失联时仍报“60000”的错误提示。

## 配置为何不一定等于实测运动

修改后的程序不会把底盘 0.5 m/s 配置偷偷限制在旧的 0.1 m/s；摇杆打满才生成所配置的幅值，死区内输出零。SDK 接收指令成功也不代表实际速度已达到目标，底层模式、加减速、负载和机械状态仍影响运动。

升降两端余量仍为 `min(0.01, 窗口宽度 × 0.2)` m，当前 0～0.42 窗口约在 0.01/0.41 m 方向停止；它是发出零速度的阈值，不是制动距离承诺。腰部目标仍受跟随偏差限制，俯仰停滞保护仍生效。以上限制有状态输出，不属于配置未读入。

程序固定读取运行端项目内的 `config/body.jsonc`、`config/arms.jsonc` 和 `config/recording.jsonc`（按所启用功能加载）。不再接受配置路径选项或速度、死区、高度的命令行覆盖；双臂/身体缺省字段仍使用代码默认值，缺少配置文件或非法值会报错。运行实际读取 PC2 上的文件，不支持热加载；默认 `body.jsonc` 启动时打印路径、速度、角度窗口，录制事件 `control_start` 保留最终生效值。当前预设不是官方推荐值。

## 同步与编译

本次需把 Python、C++ 和配置一起同步，并在 PC2 重新编译；以后只在这些合法范围内修改 JSONC 值，重启程序即可，无需再编译。完整同步命令见 [BODY_CONFIG.md](BODY_CONFIG.md)。

在 PC2：

```bash
cd /home/unitree/Proj/Proj_PICO_G1-D
bash deploy/build_on_pc2.sh deps/unitree_sdk2
bash deploy/run_pc2_arms.sh --with-hands
```

AGV 与上身桥启动前均执行离线范围核对，旧二进制或不一致二进制会报 `Bridge limit check failed ... rebuild`。检查只读取编译后的常量，不建立 DDS 连接、不发送控制指令。无需更换相机服务或重新安装 conda 依赖。
