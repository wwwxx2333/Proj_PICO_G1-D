# PC2 本地限额录制，结束后传回主机

本功能只增加数据记录。A/B、X/Y、摇杆、手臂和 Dex3 控制方式沿用原程序。
一个录制启动对应一个 episode，包含启动归位、等待 A、遥操作、暂停和退出。A 仍是控制使能，不负责开始/结束录制。

## 数据流与边界

采集时：TeleImager 的三路本机 JPEG + 现有控制进程的观测 → PC2 独立录制进程 → PC2 缓存。
采集结束后：主机手动执行 fetch → SSH/rsync 拉取 → 主机逐文件 SHA-256 校验并刷盘 → 可选删除已验证的 PC2 缓存。

录制期间没有上传线程，没有主机接收服务，没有新增跨 Wi-Fi 的图像流。录制只订阅 PC2 的 `127.0.0.1`，不重新打开 USB 相机。
原有 PICO 输入/视频预览仍使用 Wi-Fi。采集本身仍消耗 PC2 CPU、内存和磁盘，不能保证对延迟完全没有影响。

控制侧只通过非阻塞 Unix 数据报发送小型观测，既不写数据文件，也不等待录制进程。录制慢时允许丢观测，并在清单中记录可检测到的序号缺口；不补造数据、不刷新机器人反馈时间、不因此重新要求按 A。
C++ 中新增的输出只是观测接口，不改变运动指令或保护判断。原有 STATE6/HANDSTATE2 协议不变。

本次收尾/恢复修复的增量同步、手爪重编译和旧录制恢复步骤见 [PC2_RECORDING_RECOVERY.md](PC2_RECORDING_RECOVERY.md)。

## 第一次部署

按原来的方式把项目代码同步至 PC2。需要同步 `g1d_pico/recording/`、修改后的 Python 主程序及桥接文件、`bridge/g1d_recording.hpp`、两个 C++ 文件、`config/recording.json` 和新启动脚本。
不要将主机的 `.local`、`logs`、x86 编译产物覆盖到 PC2。

可以在主机执行下面的增量同步，不覆盖 PC2 的手臂参数、证书、缓存或二进制：

```bash
cd /home/lab209/Proj/Proj_PICO_G1-D
rsync -av --relative --exclude='__pycache__/' --exclude='*.pyc' \
  g1d_pico/ \
  bridge/g1d_agv_bridge.cpp bridge/g1d_arm_bridge.cpp bridge/g1d_recording.hpp \
  bridge/g1d_hand_bridge.cpp bridge/g1d_dds_channel.hpp \
  deploy/record_pc2.sh deploy/fetch_pc2_recordings.sh deploy/PC2_RECORDING.md deploy/PC2_RECORDING_RECOVERY.md \
  config/recording.json \
  unitree@192.168.1.118:/home/unitree/Proj/Proj_PICO_G1-D/
```

同步时先退出 PC2 上的控制程序。以后若已在 PC2 自定义 `recording.json`，再次同步可去掉这一项。

PC2 执行：

```bash
conda activate pico_g1d
cd /home/unitree/Proj/Proj_PICO_G1-D
bash deploy/build_on_pc2.sh /home/unitree/Proj/Proj_PICO_G1-D/deps/unitree_sdk2
python -c 'import zmq; print("pyzmq ready")'
```

现有 TeleImager 已依赖 pyzmq，通常不必额外安装。只有缺失时，在这个环境运行 `python -m pip install pyzmq`。
不用修改 TeleImager 服务、安装 ROS bag、重建 conda 环境或给主机配置 DDS。

首次安装录制功能必须重编译 AGV 和手臂桥，否则缺少立柱反馈/最终底盘指令/最终双臂输出记录。安装本次退出修复还需重编译手爪桥。仅修改录制配置则不需要重编译。

## PC2 开始采集

```bash
conda activate pico_g1d
cd /home/unitree/Proj/Proj_PICO_G1-D
bash deploy/record_pc2.sh \
  --instruction '从实验台前出发，拿起红色杯子并放到右侧托盘' \
  --scene 'lab209' \
  --objects 'red_cup,tray' \
  -- --with-hands
```

`--` 之前是录制参数；之后原样传给 `run_pc2_arms.sh`。例如不加 `--with-hands`，仍是相机、底盘、升降、腰部和双臂，但不控制或记录 Dex3。
未使用录制启动器时，原来的 `bash deploy/run_pc2_arms.sh --with-hands` 仍只做控制。

看到 `RECORDING: .../episode_...` 表示录制目录已建立。正常使用 PICO 操作。每 5 秒输出缓存占用、三路已存帧数和相机接收年龄。
相机不出帧时年龄为 `null` 或持续增大；已启动的遥操作不会因此被录制模块中断。结束清单会列出没有画面的相机。

任务结束按一次 **Ctrl+C**。等待机器人程序完成原来的停止流程，再等待 `finished` 和终端提示符返回；此时录制文件已关闭。
不需要再套 `tee`，主程序及其子进程输出已记录到本 episode 的 `console.log`。如果另行使用 `tee`，它产生的文件不属于本模块缓存限额。

## 缓存配置

编辑 PC2 项目中的 `config/recording.json`：

```json
{
  "spool": ".local/recording_spool",
  "max_cache_gb": 20.0,
  "min_free_gb": 2.0,
  "image_fps": 20.0,
  "cameras": ["head", "left_wrist", "right_wrist"],
  "camera_config_port": 60000
}
```

- `max_cache_gb` 是整个缓存目录的总额度，包含之前保留的任务；GB 使用十进制，1 GB = 10^9 字节。文件按磁盘块保守计费，清单和目录预留空间包含在额度内，因此实际可录内容略少。
- `min_free_gb` 是 PC2 文件系统额外保留的可用空间，避免用尽机器人磁盘。目录/文件系统元数据也会占用少量实际空间。
- `image_fps` 为每路保存的目标上限。相机源帧率、网络和负载可能使实际保存率更低。只保存收到的独立 JPEG 消息，不把反复读取的旧画面算作新帧。
- `spool` 相对路径基于项目根目录。只保存在 PC2，不写入 PC1。

达到上限显示 `RECORDING STOPPED ... CACHE_LIMIT`，低可用空间显示 `LOW_DISK_SPACE`。之后图像、状态和日志都停止新增保存，机器人控制和终端显示继续。清单用预留空间收尾；不会自动清理旧任务、不会自动上传、不会自动恢复录制。
已经写下的数据保留。结束清单说明截止原因，不能把截止后的操作当作已采集。
启动时缓存已满会在启动机器人控制之前报错；可先传输清理，或者使用原来的纯控制启动命令。

## 在主机拉取

先退出 PC2 的控制/录制程序，然后在 **lab209 主机**执行：

```bash
cd /home/lab209/Proj/Proj_PICO_G1-D
bash deploy/fetch_pc2_recordings.sh \
  --host unitree@192.168.1.118 \
  --destination /home/lab209/Datasets/G1D_Lab \
  --delete-after-verify
```

该命令将所有已完成的 episode 拉回主机；只有主机内容校验、刷盘成功且 PC2 源数据再次验证成功后，才删除对应 PC2 episode 释放额度。
希望 PC2 也保留一份时，去掉 `--delete-after-verify`。未传输/未验证的数据从不自动删除。

需要主机与 PC2 均可运行 `rsync`，主机有 `ssh`；缺少时在对应 Ubuntu 机器安装 `sudo apt-get install rsync`。主机工具只需 Python 3.10+ 标准库，不需要 IK/TeleVuer/pyzmq。
工具沿用 SSH 登录，不保存密码。地址变化后修改 `--host`；可用 `--remote-root` 和 `--remote-python` 指定 PC2 路径。

传输持有排他活动锁：本项目控制仍运行时拒绝传输；传输进行时也拒绝本项目控制启动，避免用户误操作导致两者竞争无线链路。该锁不管理其他独立程序或其他项目副本。

中断后主机 `.incoming/` 保留未完成数据，PC2 原文件保留。再次执行相同命令可利用 rsync 的部分文件；校验成功后才将目录移到正式数据集目录。已有相同且验证通过的任务跳过重复复制。
校验失败不会自动删除任何源数据；保留报错供检查。传输不是任务成功判定，`task_success` 默认未知，需要后续人工标注。

## 文件结构

```text
.local/recording_spool/episode_YYYYMMDD_HHMMSS_xxxxxxxx/
  metadata.json       # 任务文字、场景、相机/手臂配置、代码摘要、时间基准、限制说明
  telemetry.jsonl     # 每行独立记录，各路数据保持自己的时间戳
  frames.jsonl        # 图像路径、接收时间、序号、大小、SHA-256
  console.log         # Python 与控制子进程的终端输出
  images/
    head/00000000.jpg
    left_wrist/00000000.jpg
    right_wrist/00000000.jpg
  manifest.json       # 结束原因、帧数、流统计、缺包统计、文件校验
```

`metadata.json` 在启动即写入，`manifest.json` 在正常收尾时写入。进程被强杀、断电或磁盘故障可能留下 `IN_PROGRESS`，工具不将这种目录自动当作完整任务传走或删除；原始数据保留，可按 [恢复说明](PC2_RECORDING_RECOVERY.md) 在主机检查并另存恢复副本。

`telemetry.jsonl` 的 `stream`：

| stream | 内容与语义 |
|---|---|
| `control_cycle` | PICO 头显/左右手柄 4×4 位姿、摇杆、按钮、抓握、扳机；A 状态、升降/俯仰模式、状态文本；Python 底盘期望命令 |
| `agv_state_action` | 立柱高度和原始反馈接收时间；输入/高度新鲜度；桥接后的底盘/升降请求、SDK 返回码。数组顺序为 vx、yaw、lift |
| `arm_state` | 14 关节反馈、腰部 yaw/pitch、各自反馈时间、俯仰跟随状态等 STATE6 字段 |
| `arm_output` | 14 关节目标及桥接限速后的输出、前馈力矩、腰部目标；owned 和最近 DDS 写入时间。是抽样记录，不是每个 200 Hz 指令 |
| `hand_state` | 每侧 7 关节反馈、请求值、实际交给 DDS 的目标、堵转/释放掩码和各自时间 |
| `bridge_command` | Python 完整提交给 C++ 管道的 ARM/TARGET/HOLD 等原始协议行 |
| `console` | 对应终端输出片段，含软件时间戳 |
| `bridge_exit` | 每侧 Dex3 退出码、退出阶段、是否升级终止；缺失记录表示未知 |
| `control_start` / `session_end` | 控制配置和结束返回码 |

手爪数组顺序与官方 HandState/HandCmd 的 motor[0..6] 一致。双臂为左臂 7 关节再右臂 7 关节，对应 lowstate[15..28]，具体名称保存在 metadata。

不要混淆以下含义：

- 指令不等于真实运动结果；SDK 成功或 DDS Write 成功不等于电机到位。
- `received_at` 是 PC2 原始反馈接收时间。重复快照保留旧时间，不能当作新测量。
- JPEG 的 `received_monotonic_s` 是 PC2 本机 ZMQ 收到消息的时间，不是相机曝光时间。官方 JPEG 流没有硬件时间和帧号，本版明确记录为 null；三路不是硬件同步，源端丢帧也不能全部检测。
- PICO 位姿在 XR 跟踪坐标系中。未记录机器人在实验室的全局定位轨迹，也没有把命令积分冒充真实位置。
- 本版记录关节位置/目标和命令前馈力矩；没有接入关节实际速度/实际力矩/触觉，也没有相机标定参数。后续可从模型离线算末端位姿，但那不是经过标定的抓取 TCP。
- `sequence_gaps`/`producer_reported_drops` 是可检测到的录制消息缺口；进程崩溃时最后一段未送达消息未必能计数。

## 校验和查看

主机执行（替换真实 episode 路径）：

```bash
python3 -m g1d_pico.recording verify \
  /home/lab209/Datasets/G1D_Lab/episode_YYYYMMDD_HHMMSS_xxxxxxxx
```

查看 `manifest.json` 的 `recording_stop_reason`、`frame_counts`、`camera_frames_missing`、`stream_counts`、`sequence_gaps`，再打开 JPEG 和 `console.log`。`CONTROL_ENDED` 只表示采集随程序正常收尾，不代表任务完成或机器人无故障。
三路帧数为零、缺少 `agv_state_action` / `arm_output` 等预期流时，不应把任务直接投入训练；优先检查相机服务或是否部署了新的 C++ 二进制。

本地验证使用模拟 JPEG/控制进程和传输替身，不启动 DDS，也不操作机器人。PC2 实际性能和相机质量需要部署后从清单与控制日志确认。
