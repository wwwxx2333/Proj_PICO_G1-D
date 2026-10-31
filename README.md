# G1-D PICO 遥操作与数据采集

使用 PICO 4 Ultra 控制宇树 G1-D 旗舰版的底盘、立柱、腰部、双臂及 Dex3-1 三指手，并采集实验室导航和操作数据。当前控制程序运行在机器人 PC2；主机负责开发、SSH 终端和任务结束后的数据保存。

**项目结构、Python/C++ 分工、数据流、文件职责和操作流程，见 [项目说明](PROJECT_GUIDE.md)。**

## 当前部署

```text
PICO ──Wi-Fi──> PC2 Python / TeleVuer ──本机管道──> C++ SDK2 桥
                                                      │
                                                 内部 eth0
                                                      ↓
                                             机器人内部控制服务

PC2 相机及控制观测 ──> PC2 限额缓存 ──任务结束后──> 主机数据目录
```

Python 负责 PICO 输入、映射、双臂 IK 和流程协调，C++ 负责机器人消息和持续指令输出；独立录制进程保存 JPEG、状态、指令与日志。手部为扳机控制的开合预设。当前未接入底盘在实验室中的全局定位轨迹，也没有自主导航策略。

## 常用启动命令

已完成部署后，在 **PC2** 执行：

```bash
cd /home/unitree/Proj/Proj_PICO_G1-D

# 相机、底盘、立柱、腰部、双臂、左右 Dex3；不录制
bash deploy/run_pc2_arms.sh --with-hands
```

需要采集时，用下面的命令替代上面的控制命令，不要同时启动两份：

```bash
bash deploy/record_pc2.sh \
  --instruction "实验室导航与取放物体" --scene lab209 \
  -- --with-hands
```

启动后为 `REC WAITING`，尚未保存任务。左摇杆回中并长按按压键 L3 约 1 秒开始录制，再次长按结束本条；看到 `REC SAVED` 后可继续采集下一条，无需重启。每次触发后需松开 L3。A/B 不切换录制；等待和保存期间不录制新的任务数据。右 A 接管，右 B 停止操作；左 X 切换右摇杆纵轴的升降/俯仰；左 Y 请求双臂归位。左右侧握控制对应手臂，左右食指扳机控制手部。完整按键和恢复条件见 [项目说明](PROJECT_GUIDE.md)。

单条任务结束后按 B 停止操作，再长按 L3 并等待 `REC SAVED`。全部任务完成后在 PC2 终端按一次 Ctrl+C，等待收尾及终端提示符返回，再到 **主机** 拉取：

```bash
cd /home/lab209/Proj/Proj_PICO_G1-D
bash deploy/fetch_pc2_recordings.sh \
  --host unitree@192.168.1.118 \
  --destination /home/lab209/Datasets/G1D_Lab \
  --delete-after-verify
```

只有内容校验通过的完整任务才会按该选项删除 PC2 源缓存。去掉 `--delete-after-verify` 可保留源数据；未完成目录按 [恢复说明](deploy/PC2_RECORDING_RECOVERY.md) 处理。

## 配置与编译

| 文件 | 用途 |
|---|---|
| [body.jsonc](config/body.jsonc) | 底盘、立柱、腰部转动、俯仰和保护参数 |
| [arms.jsonc](config/arms.jsonc) | 双臂跟随、归位、手柄映射和 IK 滤波 |
| [recording.jsonc](config/recording.jsonc) | PC2 缓存配额、相机列表和保存帧率 |

配置支持 `//` 行注释。程序固定读取运行端项目内的 `config/body.jsonc`、`config/arms.jsonc` 和 `config/recording.jsonc`（按所启用功能加载）。不再接受配置路径选项或速度、死区、高度的命令行覆盖；双臂/身体缺省字段仍使用代码默认值，缺少配置文件或非法值会报错。实际读取 PC2 文件，修改后重启生效。只改合法配置值不需要编译。

保留的功能开关、任务参数与本次同步步骤见 [启动选项](deploy/RUN_OPTIONS.md)。

**2026-10-30 的官方范围对齐更新涉及 C++，同步后需重新编译一次：**

```bash
cd /home/unitree/Proj/Proj_PICO_G1-D
bash deploy/build_on_pc2.sh deps/unitree_sdk2
```

启动会检查 AGV / 上身桥的范围版本；若 Python 和二进制不一致，会明确报错。全部 31 项参数的允许范围、来源及区别见 [官方范围说明](deploy/OFFICIAL_LIMITS.md)。

## 文档导航

| 内容 | 文档 |
|---|---|
| 启动选项与固定配置 | [RUN_OPTIONS.md](deploy/RUN_OPTIONS.md) |
| 项目架构与代码导读 | [PROJECT_GUIDE.md](PROJECT_GUIDE.md) |
| 初始 PC2 环境 | [PC2_DIRECT.md](deploy/PC2_DIRECT.md) |
| 双臂依赖与启动 | [PC2_ARMS.md](deploy/PC2_ARMS.md) |
| 配置同步与使用 | [BODY_CONFIG.md](deploy/BODY_CONFIG.md) |
| 速度和映射调参 | [ARM_TUNING.md](deploy/ARM_TUNING.md) |
| 转腰、俯仰与升降 | [PC2_TORSO_LIFT.md](deploy/PC2_TORSO_LIFT.md) |
| Dex3 控制与抓握状态 | [PC2_HANDS.md](deploy/PC2_HANDS.md) |
| 采集、数据格式与传输 | [PC2_RECORDING.md](deploy/PC2_RECORDING.md) |
| 未完成任务恢复 | [PC2_RECORDING_RECOVERY.md](deploy/PC2_RECORDING_RECOVERY.md) |
| 早期有线及转发部署记录 | [历史 README](deploy/LEGACY_README.md) |

早期主机转发方案仍有代码保留，当前 PC2 直接运行方案不需要 `remote_server.py` 或 8765 SSH 隧道。历史记录中的阶段性功能描述不作为当前功能清单。
