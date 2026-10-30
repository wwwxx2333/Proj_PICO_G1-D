**Dex3-1 可选双手控制：** [PC2_HANDS.md](deploy/PC2_HANDS.md)。使用 `bash deploy/run_pc2_arms.sh --with-hands`，左右食指扳机分别控制左右三指手；首次使用需在 PC2 重新编译。

# PICO → G1-D：第一阶段，双摇杆控制

**双臂更新：官方方法 IK + 启动归位，见 [PC2 双臂部署](deploy/PC2_ARMS.md)。** 使用 G1-D 模型和接口，移植宇树公开 G1 的优化、滤波与模型重力补偿。默认在 PICO 就绪后倒计时并缓慢回到关节零位，完成后按 A 开始遥操作。此次须重新执行 `prepare_pc2_arms.sh` 安装 CasADi 并编译 V2 控制桥；B 可中止归位，底盘归位期间锁止。仅完成本地离线验证；归位没有碰撞规划。

**当前推荐部署：[PC2 直接运行相机与底盘控制](deploy/PC2_DIRECT.md)。** PICO 连接 PC2 的 `8012`，PC2 使用内部 `eth0` 调用官方 SDK2；主机通过 SSH 查看并可保存运行日志。此部署阶段只启用底盘，升降、双臂和 Dex3-1 暂不接管。先执行 `deploy/prepare_pc2_direct.sh` 准备环境，再用 `deploy/run_pc2_base.sh` 启动。准备脚本不启动机器人控制。

PC2 的统一项目目录为 `~/Proj/Proj_PICO_G1-D`，依赖源码放在其 `deps/` 下，证书及 SDK2 安装产物放在 `.local/` 下。Conda 环境由已有 Miniconda 的 `envs/pico_g1d` 统一管理。复制命令与目录结构见上述新部署说明。

以下为已有有线模式与主机转发模式的历史说明，不作为 PC2 新部署步骤；新方案不需要 8765 隧道或 `remote_server`。

本工程使用 PICO 4 Ultra 的左摇杆控制 G1-D 差速底盘，右摇杆纵轴控制升降立柱；继续转发 G1-D 头部相机画面。双臂和 Dex3-1 暂不下发任何命令。支持原有电脑有线直连，也支持[主机 + PC2 的无线部署](deploy/PC2_WIRELESS.md)：主机继续运行 TeleVuer、手柄映射和图像接收，PC2 通过机器人内部 `eth0` 运行 SDK2 控制桥。

| 输入 | 输出 |
| --- | --- |
| 左摇杆上/下 | 前进/后退，最大 0.10 m/s |
| 左摇杆左/右 | 左转/右转，最大 0.15 rad/s |
| 右摇杆上/下 | 上升/下降，最大 `HeightAdjust(±0.15)` |
| 右摇杆左右 | 不使用 |
| 右手 A | 两根摇杆回中时启用运动 |
| 右手 B | 解除运动使能 |

摇杆有 0.15 死区。PICO 手柄事件超过 250 ms 未更新，Python 立即下发零速度并解除使能；SDK2 桥超过 300 ms 未收到新指令也会下发零速度。升降桥还要求收到新鲜的 `rt/hispeed_state` 高度反馈，并在用户指定的软件高度范围内运行。**干运行是默认模式，不向机器人发送任何运动指令。**

## 1. 在电脑上检查映射

激活之前已用于 PICO/TeleVuer 的 Conda 环境：

```bash
conda activate pico_g1d
cd /home/lab209/Proj/Proj_PICO_G1-D
python -m unittest discover -s tests -v
python -m g1d_pico --robot-ip 192.168.123.164
```

电脑需能导入现有 `televuer` 与 `teleimager` 包，并使用已有 HTTPS 证书。若证书不在 TeleVuer 的默认路径，附加 `--cert /绝对路径/cert.pem --key /绝对路径/key.pem`。电脑和 PICO 的连接、相机服务器配置沿用已打通的视频链路。**先退出旧的 8012 端口视频转发脚本**，否则新程序无法占用同一端口。

在 PICO 浏览器打开此前可用的 TeleVuer 地址，例如 `https://192.168.1.150:8012` 或 `https://vuer.ai?ws=wss://192.168.1.150:8012`。用电脑当前的 Wi-Fi IP 替换示例地址。终端每 0.5 秒打印两根摇杆及映射结果。先让两根摇杆居中，按右手 A，再分别轻推左、右摇杆，确认正负方向；按 B 后结果应恢复为零。`Ctrl+C` 结束。

只调试输入、暂不取机器人相机时，可添加 `--no-camera`，PICO 使用透视模式。

## 2. 真机控制桥与高度反馈

真机模式使用宇树官方 [unitree_sdk2](https://github.com/unitreerobotics/unitree_sdk2) 的 `AgvClient::Move()` 与 `HeightAdjust()`。`bridge/build/` 中的程序已在这台 x86_64 电脑上编译，`bridge/lib/` 包含配套的 CycloneDDS 运行库；SDK2 来源和重新编译步骤见 [bridge/SDK2_SOURCE.md](bridge/SDK2_SOURCE.md)。

先运行**只读**高度探针，确认 `rt/hispeed_state` 可订阅：

```bash
cd /home/lab209/Proj/Proj_PICO_G1-D
LD_LIBRARY_PATH="$PWD/bridge/lib" ./bridge/build/g1d_height_probe eno1
```

该值是**当前立柱位置**，不是机械行程上下限。若 5 秒内没有高度反馈，不要启动真机模式，先排查有线网卡、DDS 网络和机器人服务。

## 3. 真机低速验证

将机械臂收回、底盘放在空旷地面并留人在旁边观察，确认原装遥控器的停机方式可用，停止其他底盘/升降控制程序后，再进行真机试验。每次启动先在 PICO 页面内两根摇杆回中、按右手 A；按 B 可解除使能。

**先只测试底盘**，右摇杆在此模式下完全不起作用，也不调用升降控制接口：

```bash
python -m g1d_pico --live --base-only \
  --robot-ip 192.168.123.164 \
  --network-interface eno1
```

确认左摇杆前后和转向方向，放开摇杆、按 B、退出 PICO 页面时都应停止。**再单独测试升降**，此模式完全不调用底盘移动接口。`--height-min-m/--height-max-m` 接受的是 `rt/hispeed_state.y` 反馈坐标，不能填整机的 1.26/1.68 m。如果只读探针在立柱当前姿态仍显示约 `0.042 m`，可先使用较窄的 `0.02–0.08 m` 软件测试窗口；它只是首次试验窗口，不代表机械极限。先用原装遥控器小幅升降并再次读取探针，确认反馈值随上升增加、随下降减小，再运行：

```bash
python -m g1d_pico --live --lift-only \
  --robot-ip 192.168.123.164 \
  --network-interface eno1 \
  --height-min-m 0.02 \
  --height-max-m 0.08
```

若当前反馈不在上述窗口内，或方向/机械空隙没有核实，不要照抄数值。两部分分别通过后，把 `--base-only` / `--lift-only` 去掉，保留已核实的软件高度窗口，才会同时启用两个摇杆。如果方向与预期不一致，先按 B 并退出程序，再修改 `g1d_pico/control.py` 的对应符号。

## 代码结构

- `g1d_pico/control.py`：纯摇杆映射、死区与速度上限。
- `g1d_pico/input.py`：读取 TeleVuer 原始手柄事件并记录更新时间。
- `g1d_pico/__main__.py`：相机转发、启停状态与运行循环。
- `g1d_pico/bridge.py`：Python 与 SDK2 桥进程之间的本地连接。
- `bridge/g1d_agv_bridge.cpp`：调用官方 G1-D 控制接口并实现独立超时停车、升降高度范围约束。

本阶段没有接管机械臂或 Dex3-1，也没有尝试运行官方 G1 双足 `LocoClient`。

无线部署新增 `g1d_pico/remote_server.py`：PC2 只监听回环地址，主机通过 SSH 端口转发传送控制命令。完整传输、ARM64 编译和启动命令见[无线部署说明](deploy/PC2_WIRELESS.md)。这个新增的无线控制链路已用假桥接程序验证命令传输和断连清理，尚未完成 G1-D 真机端到端运动验证。


## 采集数据：PC2 限额缓存，结束后传到主机

见 [部署与数据说明](deploy/PC2_RECORDING.md)。PC2 使用 `bash deploy/record_pc2.sh -- --with-hands` 启动完整控制与录制；默认三路相机每路 20 FPS、缓存总额度 20 GB、磁盘保留 2 GB。达到额度只停止保存，控制继续。

退出后在主机执行 `bash deploy/fetch_pc2_recordings.sh --host unitree@192.168.1.118 --destination /home/lab209/Datasets/G1D_Lab --delete-after-verify`，通过校验后保存并释放 PC2 缓存。录制期间不进行上传。首次部署需同步代码并重新编译桥接程序。
