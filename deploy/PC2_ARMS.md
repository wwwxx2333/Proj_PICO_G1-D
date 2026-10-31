# PC2：官方方法双臂 IK 与启动归位

当前双臂参数在 `config/arms.jsonc`，底盘、立柱及腰部参数在 `config/body.jsonc`。本次配置拆分无需重编译；已有部署按 [BODY_CONFIG.md](BODY_CONFIG.md) 同步代码、启动脚本与两份配置。下方编译步骤用于此前控制功能首次安装。

当前默认配置为 `.jsonc`。已有项目升级时先按 [JSONC 配置迁移说明](ARM_TUNING.md) 保留 PC2 参数并更新代码；仅本次扩展名迁移无需执行下文针对历史功能更新的 C++ 编译。

2026-10-26 更新。可选 Dex3-1 双手控制见 [PC2_HANDS.md](PC2_HANDS.md)。

左 Y 运行中复位与集中调参见 [ARM_TUNING.md](ARM_TUNING.md)。右摇杆转腰与立柱升降见 [PC2_TORSO_LIFT.md](PC2_TORSO_LIFT.md)。

主机负责编写/保存代码与查看日志；部署及真机启动由操作者执行。
所有机器人项目文件仍位于 `/home/unitree/Proj/Proj_PICO_G1-D`。

## 本次变更

- 使用宇树公开 G1_29_ArmIK 的 Pinocchio + CasADi + IPOPT 方法，保留目标函数权重、关节历史滤波和模型重力补偿。使用官方 G1-D URDF，不直接运行普通 G1 的控制器。
- 官方 G1 与 G1-D 的 14 个臂关节 origin / axis / limit 一致；核对结果见 `third_party/unitree_xr/MODEL_COMPARISON.md`。上游提交、许可、保留项与适配项见同目录 `NOTICE.md`。
- IK 求解采用位置、旋转、关节正则与连续性之间的优化折中，不再因位置残差刚超过 15 mm 就锁死。求解器失败只保留上一条有效目标，显示 `IK_RETRY`，后续有效解可以恢复；不发送优化器失败的中间解。
- IK 之前限制每帧末端目标相对当前命令端点的增量；之后对关节限速。模型目标端点按官方实现设为腕部局部 X 方向 5 cm，仍不是已标定的三指手抓取 TCP。
- 默认启动归位：PICO 就绪后倒计时 3 秒，从实际关节位置平滑回到 14 个关节零角度，归位期间底盘、立柱和转腰锁止。这个零位来自官方 G1 `ctrl_dual_arm_go_home`；不是新增加的 G1-D 固件归位服务。
- 右摇杆左右控制腰部 yaw，双臂采用躯干相对目标，随躯干一起转动；左 X 切换右摇杆上下为立柱升降或上身俯仰，切换后需右摇杆回中。默认模式为升降，双臂启动脚本默认启用（0～0.42 m 反馈范围，内侧各留 1 cm）。
- 使用 `TARGET6` 一并传递双臂目标、重力补偿和腰部速度；`STATE6` 包含腰部反馈、目标角度、限幅状态、专用俯仰反馈时间戳及局部不跟随/恢复状态。必须重新编译 PC2 的控制程序，不能混用旧版二进制。

## 1. 主机同步

先退出 PC2 的旧遥操作程序，再在主机执行；不用停止相机服务：

```bash
rsync -av \
  --exclude='.git/' --exclude='__pycache__/' --exclude='*.pyc' \
  --exclude='/.local/' --exclude='/deps/' --exclude='/logs/' \
  --exclude='/bridge/build/' --exclude='/bridge/build-arm64/' --exclude='/bridge/lib/' \
  --exclude='*.pem' \
  /home/lab209/Proj/Proj_PICO_G1-D/ \
  unitree@192.168.1.118:/home/unitree/Proj/Proj_PICO_G1-D/
```

这次不能只复制 `arm_ik.py` / `arms.py`。需要一起更新 Python、C++、部署脚本和来源文件。

## 2. PC2 准备依赖并编译

首次安装依赖时，在 PC2 终端执行：

```bash
conda activate pico_g1d
cd /home/unitree/Proj/Proj_PICO_G1-D
bash deploy/prepare_pc2_arms.sh
```

脚本显式向 `pico_g1d` 安装 Pinocchio 3.1.0、CasADi 3.6.7、NumPy 1.x，离线构造模型并运行一次 IPOPT，然后编译 ARM64 控制程序。
需要 conda-forge 网络访问。不使用 sudo，不改相机服务器的 `tv` 环境，不启动机器人控制。

已有可用双臂环境时，无需重装依赖，同步后只需重新编译：

```bash
conda activate pico_g1d
cd /home/unitree/Proj/Proj_PICO_G1-D
bash deploy/build_on_pc2.sh /home/unitree/Proj/Proj_PICO_G1-D/deps/unitree_sdk2
```

若提示 `old arm bridge protocol`，表示 Python 已更新但 C++ 仍是旧版本，重新执行上述编译命令。

## 3. 启动及自动归位

**启动前手中不夹物体，双臂运动路径要空旷。关节零位归位是插值轨迹，没有碰撞规划；手臂若交叉卡住、贴住设备或缠绕线缆，不能指望本程序自动解困。**

机器人须处于随机 G1-D 开发资料规定的可接管状态，不同时运行其他 `rt/lowcmd` 发布者。

在 PC2 执行：

```bash
cd /home/unitree/Proj/Proj_PICO_G1-D
bash deploy/run_pc2_arms.sh
```

PICO 进入：

```text
https://192.168.1.118:8012/?ws=wss://192.168.1.118:8012
```

进入 Virtual Reality，两个摇杆回中，两个侧握键松开。默认流程为：

```text
HOME_WAITING_FOR_PICO
    → HOME_COUNTDOWN（3 秒，B 可取消）
    → HOME_TAKING_OVER
    → HOMING（双臂缓慢回零，底盘锁止）
    → HOME_DONE / WAITING_FOR_A
```

- 无有效 PICO 输入时不会开始归位；归位期间数据超时会中止。
- 归位使用五次平滑插值，最大关节目标速度 0.15 rad/s，时长最少 4 秒，实际根据起点离零位的距离计算。
- 以实际反馈确认全部关节误差小于 0.05 rad 并保持 0.5 秒才算归位完成。超时和跟随误差均中止。
- 按 B、移动摇杆、按侧握键或输入异常会取消归位；不会自动继续。排除原因后按一下 A，重新进入就绪检查及倒计时。
- 归位完成后不自动开始遥操作：按一下 A 再进入正常跟随。归位完成和取消均不释放电机为无力状态。
- 若明确需要保留当前姿态，可启动 `bash deploy/run_pc2_arms.sh --skip-home`；此时仍需 A 使能，默认命令不跳过归位。

## 4. 归位后的操作

| 操作 | 效果 |
|---|---|
| 摇杆回中、两侧握键松开，按一下右 A | 使能底盘、双臂、转腰，以及已配置的立柱；捕获实际位置，不再重复启动归位 |
| 按住左/右侧握键并移动对应手柄 | 对应手臂相对位置/旋转跟随 |
| 松开侧握键 | 该臂保持最后有效目标 |
| 左摇杆 | 底盘前后和转向 |
| 右摇杆左右 | 腰部向左/右转，双臂随躯干一起移动；无需按侧握键 |
| 右摇杆上下 | LIFT 模式为立柱升/降；PITCH 模式为前倾/返回直立，可与转腰同时操作 |
| 左 X | 切换 LIFT/PITCH，随后右摇杆回中；长按只切换一次，无需再按 A |
| 左 Y | 停止底盘、升降及转腰，请求双臂归位；不把腰部角度或立柱高度归零 |
| 右 B | 底盘和升降停止，双臂和腰部保持；不是断电急停 |

位置增量为手柄的 0.5 倍，旋转为 1 倍；每次握键重新对齐，朝前看时握键。
头显仅在 A 使能/新握键对齐时要求新鲜；建立对齐后继续转头或停止头显位姿事件不会改变腕部目标。
手柄位姿仍须每 250 ms 内有有效更新，机器人关节反馈也须新鲜。俯仰的 250 ms 时效独立按 `waistpitchstate` 到达时间检查，缺少该反馈不能接管，不能用 lowstate[14] 或零值顶替；这属于本项目保护逻辑。左右位姿当前作为一个有效快照；任一侧无效会停止刷新该快照。

正常终端显示如 `ARMS FOLLOW=HOLD/ON IK=2.1ms WAIST=+5.0deg target=+5.1deg rate=+0.050rad/s`；后者为最近一次求解的本机测量耗时，不是保证的频率。
`IK_RETRY` 表示该帧优化失败并保持上一有效目标。`controller poses stale/invalid` 与 `head pose stale/invalid` 会带数据年龄，区分输入问题；追踪跳变、超范围、关节跟随误差仍进入 HOLD，需要松开侧握键再按 A。

本页默认双臂启动模式已启用右摇杆左右转腰及上下升降。按用户确认的 0.42 m 行程，采用 0～0.42 m 反馈窗口及两端各 1 cm 方向限位；无需再填写高度参数。添加 `--without-lift` 可关闭升降。启动命令及本次是否需要编译见 [PC2_TORSO_LIFT.md](PC2_TORSO_LIFT.md)。加 `--with-hands` 后食指扳机控制双手，详见手部说明。相机+底盘单独运行仍使用 `bash deploy/run_pc2_base.sh`，不会归位或接管双臂和腰部。

## 控制边界与退出

G1-D `rt/lowstate` / `rt/lowcmd` 仅用于臂索引 15..28 和腰部 yaw(12)。pitch 通过专用 `rt/waistpitchcmd` / `rt/waistpitchstate` 控制与反馈：PITCH 模式接收右摇杆上下速度，LIFT 模式保持目标角度。两组接口由同一 C++ 进程管理，旧的 lowcmd/lowstate[14] 不作为俯仰入口。不使用普通 G1 的权重切换接口。
专用俯仰 PD 使用官方腰部例程的 kp=500、kd=12；臂部 PD 保持 G1-D 双臂示例的 kp=40、kd=1；静态重力补偿基于模型和实际臂/腰角度，无负载辨识、接触控制或动态力矩补偿。
URDF 的 `torso_Joint` 对应 SDK 腰部 yaw(12)，`Yaw_Joint` 对应专用 `waistpitchstate.q`，按轴向核对而不是按名称猜测。
C++ 对模型前馈力矩做限幅验证（每关节不超过 URDF 限制且不超过 20 Nm，两个末端腕关节各 5 Nm），并限制变化率；HOLD 保留最后已输出的有界支撑力矩，不能替代实时重力重算或固件安全控制。

程序没有全身、双臂相互和环境碰撞检测。不得将这版描述为宇树原厂完整验证的 G1-D 遥操作系统。

退出前按 B，确认底盘停止，再 Ctrl+C。退出前短暂保持测量姿态并保留有界支撑力矩，随后停止发布；不在退出时再自动归位，不自动切换运控服务，后续接管按 G1-D 随机资料执行。

## 主机保存运行日志

以下主机命令可替代 PC2 启动命令，不能同时启动两份：

```bash
mkdir -p /home/lab209/Proj/Proj_PICO_G1-D/logs
set -o pipefail
ssh -tt unitree@192.168.1.118 \
  'cd /home/unitree/Proj/Proj_PICO_G1-D && bash deploy/run_pc2_arms.sh' \
  2>&1 | tee "/home/lab209/Proj/Proj_PICO_G1-D/logs/pc2-arms-$(date +%Y%m%d-%H%M%S).log"
```

这只是运行日志，尚未实现同步视频/动作/状态数据集。

## 文件与验证

- `g1d_pico/arm_ik.py`：官方方法的 G1-D 优化求解、平滑和模型重力补偿。
- `g1d_pico/arm_home.py`：独立的启动关节归位轨迹。
- `g1d_pico/arms.py`：归位与跟随状态机，输入验证和命令生成。
- `g1d_pico/input.py`：手柄/头显有效位姿与分项超时诊断。
- `g1d_pico/arm_bridge.py`、`bridge/g1d_arm_bridge.cpp`：含双臂、转腰与俯仰命令及反馈的 V5 协议。
- `bridge/g1d_arm_guard.hpp`：独立关节限位、关节/力矩变化速率及超时保护。
- `bridge/g1d_waist_guard.hpp`：腰部角度、速度、加速度及跟随目标距离限制。
- `bridge/g1d_lift_guard.hpp`：立柱反馈窗口与方向限位；高度反馈异常只停止升降。

本次通过 84 项无网络 Python 测试、C++ 双臂、腰部/升降及俯仰不跟随保护测试、SDK 消息构造/反馈来源测试、C++→Python 状态消息检查及控制桥 SDK2 x86_64 编译。
覆盖 X 切换/回中、升降与俯仰互斥、右摇杆双轴同时操作、B 停止、A 中位要求、V5 协议、腰部限角/限速/失联保持、立柱边界以及躯干相对运动学。
本次没有连接机器人测试；ARM64 编译、实际方向和转腰/升降与双臂的真机联动仍由用户部署后确认。

俯仰目标偏差默认允许 0.10 rad；达到偏差上限后持续 1.5 s 沿请求方向不足 0.005 rad 的进展，只暂停俯仰并捕获实际角度保持。恢复需在 PITCH 模式将右摇杆两轴回中，再重新推动，无需因这一局部事件再按 A。参数、状态日志与反馈失联的区别见 [PC2_TORSO_LIFT.md](PC2_TORSO_LIFT.md)。
