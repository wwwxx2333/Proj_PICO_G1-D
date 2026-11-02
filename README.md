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

## 运行流程

已完成部署后，在 **PC2** 执行：

```bash
cd /home/unitree/Proj/Proj_PICO_G1-D
conda activate pico_g1d

# 相机、底盘、立柱、腰部、双臂、左右 Dex3；不录制
bash deploy/run_pc2_arms.sh --with-hands
```

需要采集时，用下面的命令替代上面的控制命令，不要同时启动两份：

```bash
bash deploy/record_pc2.sh \
  --instruction "实验室导航与取放物体" --scene lab209 \
  -- --with-hands
```

启动时先初始化控制、等待 PICO 就绪并完成双臂归位，同时预热录制进程和相机连接；不保存任务数据。看到 `REC READY` 后按一下左摇杆按压键 L3 开始录制，松开后再次长按约 1 秒结束本条；无需摇杆回中。看到 `REC SAVED` 后会预热下一条，等 `REC READY` 即可继续，无需重启。L3 不触发归位或运动接管；同一次按住不会先开始又结束。A/B 不切换录制；等待和保存期间不录制新的任务数据。右 A 接管，右 B 停止操作；左 X 切换右摇杆纵轴的升降/俯仰；左 Y 请求双臂归位。左右侧握控制对应手臂，左右食指扳机控制手部。完整按键和恢复条件见 [项目说明](PROJECT_GUIDE.md)。

单条任务结束后按 B 停止操作，再长按 L3 并等待 `REC SAVED`。全部任务完成后在 PC2 终端按一次 Ctrl+C，等待收尾及终端提示符返回，再到 **主机** 拉取：

```bash
cd /home/lab209/Proj/Proj_PICO_G1-D
bash deploy/fetch_pc2_recordings.sh \
  --host unitree@192.168.1.118 \
  --destination /home/lab209/Datasets/G1D_Lab/raw \
  --with-video
```

只有内容校验通过的完整任务才会按该选项删除 PC2 源缓存。 添加`--delete-after-verify` 可删除源数据，方便节约空间；未完成目录按 [恢复说明](PROJECT_GUIDE.md#recovery) 处理。

fetch 复用单条 SSH 连接：正确认证一次后，本轮各条传输不再重复询问密码。加 `--with-video` 可在拉取结束后由主机生成三路拼接 MP4，默认放入 `G1D_Lab/videos/<episode_id>/overview.mp4`，原始数据不变。已经拉取的任务可单独转换：

```bash
bash deploy/make_recording_video.sh \
  /home/lab209/Datasets/G1D_Lab/raw/episode_* \
  --output-root /home/lab209/Datasets/G1D_Lab/videos
```

命令、视频时间对齐和缺帧提示见 [主机传输与 MP4 回看](PROJECT_GUIDE.md#host-video)。视频在主机生成。

## 自动运行日志

以上三个 PC2 启动入口都会自动保存完整运行日志，无需手动 `tee`：
`logs/runtime/run_YYYYMMDD_HHMMSS_微秒_随机编号.log`，最多保留 100 份，启动时淘汰最旧的已关闭日志。
一份日志包含本次启动检查、等待、控制、各条录制与退出保存；终端启动时会打印完整路径。
默认终端只显示必要状态和错误；加 `--full-log` 显示全部：

```bash
bash deploy/run_pc2_arms.sh --with-hands --full-log
# 或控制并录制，--full-log 放在分隔符 -- 前
bash deploy/record_pc2.sh --full-log \
  --instruction "实验室导航与取放物体" --scene lab209 -- --with-hands
```

运行日志与 episode 的 `logs/console.log`（旧 v1 为根目录 console.log） 分开，完整文件不受终端显示模式影响。运行日志不计入录制缓存额度，也不会由 fetch 自动传回主机。配置和故障提示见 [运行日志说明](PROJECT_GUIDE.md#logging)。

## 配置与编译

| 文件 | 用途 |
|---|---|
| [body.jsonc](config/body.jsonc) | 底盘、立柱、腰部转动、俯仰和保护参数 |
| [arms.jsonc](config/arms.jsonc) | 双臂跟随、归位、手柄映射和 IK 滤波 |
| [recording.jsonc](config/recording.jsonc) | PC2 缓存配额、相机列表和保存帧率 |
| [dataset.jsonc](config/dataset.jsonc) | 采集标识、数据质量阈值、主机离线导出参数 |
| [calibration.jsonc](config/calibration.jsonc) | 相机/TCP 标定模板，未测数据保持 null |
| [logging.jsonc](config/logging.jsonc) | 自动运行日志路径、数量上限、刷新与提示间隔 |

配置支持 `//` 行注释。录制另固定读取 `config/dataset.jsonc` 与 `config/calibration.jsonc`，详见 [训练数据结构与使用](PROJECT_GUIDE.md#training-data)。程序固定读取运行端项目内的 `config/body.jsonc`、`config/arms.jsonc`、`config/recording.jsonc` 和 `config/logging.jsonc`（按所启用功能加载）。不再接受配置路径选项或速度、死区、高度的命令行覆盖；双臂/身体缺省字段仍使用代码默认值，缺少配置文件或非法值会报错。实际读取 PC2 文件，修改后重启生效。只改合法配置值不需要编译。

功能开关和任务参数见 [启动选项](PROJECT_GUIDE.md#run-options)。

**首次部署或修改 C++ / SDK 后，在 PC2 编译：**

```bash
cd /home/unitree/Proj/Proj_PICO_G1-D
bash deploy/build_on_pc2.sh deps/unitree_sdk2
```

启动会检查 AGV / 上身桥的范围版本；若 Python 和二进制不一致，会明确报错。控制与录制参数的允许范围、来源及区别见 [参数范围说明](PROJECT_GUIDE.md#parameter-limits)。

## 文档导航

| 内容 | 位置 |
|---|---|
| 首次连接、证书、环境和部署编译 | [项目指南第 2 节](PROJECT_GUIDE.md#installation) |
| 启动选项与手柄操作 | [启动选项](PROJECT_GUIDE.md#run-options)、[按键与恢复](PROJECT_GUIDE.md#controls) |
| 配置与参数范围 | [配置说明](PROJECT_GUIDE.md#configuration)、[范围来源](PROJECT_GUIDE.md#parameter-limits) |
| 录制、日志、回看和训练导出 | [录制](PROJECT_GUIDE.md#recording)、[日志](PROJECT_GUIDE.md#logging)、[视频](PROJECT_GUIDE.md#host-video)、[导出](PROJECT_GUIDE.md#training-data) |
| 未完成任务恢复 | [恢复流程](PROJECT_GUIDE.md#recovery) |

控制直接运行在 PC2；主机负责部署和任务结束后的数据传输。所有当前运行、录制和数据处理脚本保留在 deploy/。第三方许可证及来源声明随代码保留。
