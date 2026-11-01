# 训练数据 v2：内容、目录、标注与导出

本次补充只增加记录和主机离线处理：A/B、L3、手臂归位、底盘/腰部/立柱控制保持原有流程。新录制采用 `g1d-lab-recording-v2`；旧 v1 的读取、校验、恢复和传输继续支持，不自动改写旧任务。

## 1. PC2 每条任务的结构

```text
.local/recording_spool/episode_YYYYMMDD_HHMMSS_xxxxxxxx/
├── metadata.json                    入口：episode/session、任务、时间、软件/配置摘要、相机配置
├── manifest.json                    保存完成标记、退出原因、统计、SHA-256 清单
├── observations/
│   ├── images/
│   │   ├── index.jsonl               每帧路径、接收时间、序号、字节数、SHA-256
│   │   ├── head/00000000.jpg         头部原始拼接 JPEG
│   │   ├── left_wrist/00000000.jpg
│   │   └── right_wrist/00000000.jpg
│   ├── robot/
│   │   ├── arms.jsonl                双臂实测 q、腰部 yaw/pitch、反馈时间与状态
│   │   ├── hands.jsonl               左右手原始快照：实测 q、已提交目标及反馈时间
│   │   ├── body.jsonl                立柱反馈；底盘全局位姿/实测速度为 null
│   │   ├── arm_dynamics.jsonl        双臂 q/dq/tau_est；腰部各自 q/dq/tau_est/时间
│   │   └── hand_dynamics.jsonl       左右手 q/dq/tau_est、实际反馈话题与时间
│   └── operator/pico.jsonl           PICO 位姿/按钮/摇杆、使能、归位和控制状态
├── actions/
│   ├── arms.jsonl                    双臂请求、限速后实际提交目标、前馈力矩、腰部目标
│   ├── hands.jsonl                   左右手实际提交目标、请求、受阻/张开/限幅掩码
│   ├── body.jsonl                    底盘 vx/yaw、立柱归一化输入、SDK 返回值
│   └── bridge_commands.jsonl         Python 发给 C++ 的原始协议行
├── robot/
│   ├── model.urdf                    本次控制使用的模型内容，保持原始字节
│   ├── joint_order.json              各数组顺序及 SDK 索引
│   └── control_config.json           本次身体和双臂配置快照
├── calibration/calibration.json     相机/TCP/立柱映射说明；未提供则 null
├── task/task.json                    任务、场景、布局/路线等编号；成功与阶段待标注
├── schema/fields.json                路径、字段语义、单位、时间与坐标系说明
├── quality/summary.json              实际收到的流、过期/无效状态、可用性统计
├── events/
│   ├── control.jsonl                退出等其他事件，带时间
│   └── console.jsonl                原始输出片段，带时间
└── logs/console.log                  本条录制区间的可读日志
```

只有收到某路数据才创建对应 JSONL；缺文件表示没有收到数据，不表示“全为零”。未收尾有 `IN_PROGRESS`，不要手动删掉。`metadata.json`、静态快照在 L3 开始时写入；其余数据随录制持续写入；质量汇总和 manifest 在结束时生成。一次启动多条任务各自一个目录，共用一个 `session_id`。

`observations/robot/body.jsonl` 和 `actions/hands.jsonl` 是录制进程整理的视图，`derived_from` 指向原始 stream/producer/seq；它们不是机器人又发送了一个反馈包。原始 hand_state 与 agv_state_action 保留，以便追溯。不要把视图和原始事件混在一起统计丢包。

## 2. 哪些值能用于训练

| 内容 | 来源与单位 | 使用注意 |
|---|---|---|
| 双臂 q | HG LowState motor[15..28]，左 7 + 右 7，rad | 实际反馈，与目标位置分开 |
| 手部 q | HG HandState motor[0..6]，每侧 7，rad | 左右各自时间，不能按 JSONL 行号对应 |
| dq | 上述 MotorState.dq，rad/s | SDK 提供的速度；不是从指令差分伪造 |
| tau_est | MotorState.tau_est，N·m | 厂商估计力矩，不是触觉/抓取力；有限数值也不等于已验证传感器精度 |
| output_tau_ff | 双臂控制器前馈，N·m | 指令项，不能当成实测力矩 |
| 机器人动作 | 桥限速/受阻处理后提交的目标 | 手柄输入、期望目标、提交目标、实测结果分别保留 |
| 立柱状态/动作 | `rt/hispeed_state.y`，m / HeightAdjust 归一化输入 | 升降输入不是已测的 m/s；全机高度不是该反馈坐标 |
| 底盘动作 | Move 的 vx，m/s；yaw，rad/s | 当前没有底盘全局定位和实测平面速度 |
| 图像 | TeleImager 本机 JPEG | 没有深度；头部保持拼接，不自动拆左右目 |
| 任务结果 | 人工审核 | 程序正常退出、L3 结束或夹爪受阻都不代表任务成功 |

原始反馈 `received_at` 与录像 `received_monotonic_s` 都是 PC2 软件时间。另有 producer 发送时间与 recorder 接收时间。旧反馈的重复快照仍保留原时间；质量标记阈值只影响数据标记，**不恢复 250 ms 自动断开手爪的逻辑**。dq/tau_est 非有限值保存为 JSON `null`，不会影响 q 的控制验收。

目前没有可靠来源的内容仍为 null/不可用：底盘实验室坐标轨迹、实测底盘速度、相机曝光时间、深度、触觉、标定后的抓取 TCP。未提供标定时，不生成假外参或末端全局位姿。模型不含 mesh 文件，足以保存关节树定义，但立柱的两段伸缩映射和 TCP 仍需实测确认。

`manifest.sequence_gaps` 仍是能观察到的录制消息缺口，不能覆盖采集前/最后一包的丢失。恢复 v2 时无法由分流文件还原原始总接收顺序，恢复报告明确不重建跨文件缺包计数，不把空计数当作“无丢包”。

## 3. 固定配置

- `config/recording.jsonc`：原有 FPS、相机、缓存限额，未改默认值。
- `config/dataset.jsonc`：采集者/机器人/布局/路线等 ID，质量阈值、主机导出频率/最大时间差/动作偏移。每项有注释；默认导出 20 Hz、最大间隔 0.10 s、动作偏移 0。
- `config/calibration.jsonc`：相机内外参和工具中心模板。没有测量结果就保持 null；不能直接用单位阵充数。填写只做记录，不改变控制坐标。

标识和标定在 PC2 录制启动时快照；主机导出参数取主机固定的 dataset.jsonc，并保存到导出 schema。它们都是固定路径，无新增 `--config` 参数。只修改这些 JSONC 不必编译。缺少文件或非法值会在录制初始化时报错。

建议给 `robot_id`、`collector_id`、`layout_id`、`task_id` 填稳定编号。家具位置变化更换 layout_id。后续按 episode/session/layout 划分训练验证集，避免相邻帧或同一条路线泄漏到两边。

## 4. 本次同步和编译

先结束 PC2 上运行的控制程序。在**主机**执行以下增量同步：

```bash
cd /home/lab209/Proj/Proj_PICO_G1-D
rsync -av --relative --exclude='__pycache__/' --exclude='*.pyc' \
  g1d_pico/ \
  bridge/g1d_recording.hpp bridge/g1d_upper_body_io.hpp \
  bridge/g1d_arm_bridge.cpp bridge/g1d_hand_bridge.cpp bridge/g1d_hand_guard.hpp \
  deploy/TRAINING_DATA.md deploy/PC2_RECORDING.md deploy/RUN_OPTIONS.md \
  unitree@192.168.1.118:/home/unitree/Proj/Proj_PICO_G1-D/

rsync -av --relative --ignore-existing \
  config/dataset.jsonc config/calibration.jsonc \
  unitree@192.168.1.118:/home/unitree/Proj/Proj_PICO_G1-D/
```

第二条只补齐新增配置，不覆盖 PC2 已有值。以后修改主机这两份配置、希望同步覆盖时，明确移除 `--ignore-existing`。现有 body/arms/recording/logging 配置、证书和缓存均不在这次同步清单中。

在**PC2**重新编译：

```bash
conda activate pico_g1d
cd /home/unitree/Proj/Proj_PICO_G1-D
bash deploy/build_on_pc2.sh deps/unitree_sdk2
```

本次加入 C++ 动态反馈记录，必须重编译双臂和手部桥。只同步 Python 仍可读取旧记录，但不会凭空出现 dq/tau_est；检查新任务 quality/summary.json 的 joint_dynamics_received。

启动命令沿用原来：

```bash
bash deploy/record_pc2.sh \
  --instruction '前往实验台拿起杯子' \
  --scene lab209 \
  --objects cup_01 \
  -- --with-hands
```

L3 开始/长按结束，等待 REC SAVED。全部结束后 Ctrl+C，等终端返回，再传输。没有新增训练处理进程在机器人上运行，也没有增加采集期间的 Wi-Fi 上传。

## 5. 主机长期目录与标注

建议从新任务开始使用以下主机结构；旧目录不用搬动，CLI 可直接接受旧路径：

```text
/home/lab209/Datasets/G1D_Lab/
├── raw/episode_.../                   校验后的原始数据，只读使用
├── annotations/episode_.../labels.json 人工标注，可以修改
└── training/episode_.../               离线对齐导出，可重建
    ├── data/samples.jsonl             每行一个时间步，观测/动作/有效性/任务标签
    ├── data/images.jsonl              本次使用的图像索引和校验值
    ├── images/{camera}/...jpg         已使用图像的副本，可独立搬走
    ├── source/                       原始 metadata、模型、配置、标定、字段说明
    ├── labels.json                    本次使用的标注快照
    ├── schema.json                    对齐方式、单位、导出配置和源 manifest 摘要
    ├── quality.json                   有效样本、无效通道和图像统计
    └── manifest.json                  导出完成与文件校验；未完成有 IN_PROGRESS
```

**主机**执行拉取，配置与原始数据都保留在自己的目录中：

```bash
conda activate pico_g1d
cd /home/lab209/Proj/Proj_PICO_G1-D
bash deploy/fetch_pc2_recordings.sh \
  --host unitree@192.168.1.118 \
  --destination /home/lab209/Datasets/G1D_Lab/raw \
  --delete-after-verify
```

将下面的 episode 名替换成实际名称，仅定义一次：

```bash
episode=/home/lab209/Datasets/G1D_Lab/raw/episode_YYYYMMDD_HHMMSS_xxxxxxxx
python -m g1d_pico.recording annotate "$episode" \
  --output-root /home/lab209/Datasets/G1D_Lab/annotations
```

此命令先校验原始数据，再创建独立 labels.json，不覆盖已有标注。打开该文件填写：

- `review_status`：unreviewed / accepted / rejected；accepted 表示人工确认质量可用于所定任务。
- `task_success`、`navigation_success`、`grasp_success`、`placement_success`：true / false / null。
- `instruction`、`object_ids`（字符串数组）、`task_id`、`route_id`：可按本条实际任务修正。
- `stages`、`interventions`、`exclude_intervals`：每项包含 start_s、end_s、label；单位是从本条录制开始的秒，左闭右开。stages 按时间排列且不重叠。
- `notes`：失败原因、异常情况等自由文本。

例如任务阶段为 `{"start_s": 0, "end_s": 12.3, "label": "navigate"}`，干预或排除区间使用同一结构。时间必须在自动生成的 duration_s 内；不要修改 episode_id、source_manifest_sha256、duration_s、time_origin、schema。这些字段将标注绑定到特定原始数据。

不要向 raw/episode 内添加标注文件或修改 task/task.json；这会破坏原始校验。标注和训练结果在外部目录，不影响再次 fetch 或验证原始任务。

## 6. 主机离线导出

```bash
python -m g1d_pico.recording export "$episode" \
  --annotations "/home/lab209/Datasets/G1D_Lab/annotations/${episode##*/}/labels.json" \
  --output-root /home/lab209/Datasets/G1D_Lab/training
```

未标注也可以省略 --annotations 做质量预览，但成功默认为未知，eligible_for_imitation 为 false。已有导出目录不会被覆盖；改标注后要重导，可指定新的 output-root，例如 G1D_Lab/training_v2。

对齐方式：观测只取时间 t 之前、且在 t 时已到达录制进程的最近值；动作取 t + offset 之后最近一次实际提交的目标。全部受最大时间差约束，无插值、无补零。每个通道都有 source_monotonic_s、time_delta_s、valid；dq/tau_est 另有各字段有效性。状态、动作、图像都不能直接按原始文件行号配对。

只有审核 accepted、任务成功 true、必须通道完整新鲜、PICO 已使能且不在归位、没有人工排除/干预区间的样本，才标 eligible_for_imitation=true。恢复出来的任务保留单独标记，不自动投入成功示范集合。失败任务与无效样本仍保留，可供错误分析或其他训练目标使用。

该输出是本项目明确字段的训练中间格式，**不是已经转换好的 LeRobot/RLDS 数据集**；接入具体模型时再做特征打包、归一化和 action horizon 切片。深度、定位、TCP 未接入的限制不会因导出消失。

导出只在主机运行，SQLite 临时索引限制内存增长；使用到的 JPEG 会复制一份，主机需要额外空间。原始图像不重编码；输入全部校验通过后才开始导出。新增状态/质量字段增加 PC2 本地小消息与写盘量，仍以图像为主要体积。未声称真机延迟零影响；控制通道保持非阻塞、无重试、无录制导致的自动断开。
