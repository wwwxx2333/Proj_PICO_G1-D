# 主机单次认证传输与 MP4 任务回看

本功能只更新主机 Python 和脚本，不需要在 PC2 编译 C++，也不要求为此更新 PC2 的 transfer-lease。相机、控制、录制和 L3 操作保持原有流程。

## 1. 为什么以前不断输入密码

旧版 fetch 为管理连接和每条 episode 的 rsync 分别建立 SSH 连接，所以密码会重复询问。现在管理连接同时作为 OpenSSH ControlMaster，所有 rsync 共用该连接。

- 正确认证一次后，本次 fetch 的后续任务不再要求密码。已有免密登录则无需密码。
- 每次重新运行 fetch 是新会话，通常仍要认证一次；密码错误或账户额外认证不在此保证内。
- 不保存密码，不配置 sshpass，不写 SSH 配置或安装密钥。
- 使用权限受限的临时控制 socket；正常完成、失败或 Ctrl+C 时关闭连接。
- 已认证连接丢失后，不悄悄开新连接反复要密码。报告中断，保留未完成数据；重新运行 fetch 即可。
- 本次测试采用离线 SSH/rsync 模拟，没有连接机器人；实际无线链路仍以运行结果为准。

实现依据：[OpenSSH ControlMaster / ControlPath 文档](https://man.openbsd.org/ssh_config#ControlMaster)。

## 2. 拉取时顺便生成视频

先在 PC2 结束整个控制/录制会话。在 **主机**执行：

```bash
conda activate pico_g1d
cd /home/lab209/Proj/Proj_PICO_G1-D
bash deploy/fetch_pc2_recordings.sh \
  --host unitree@192.168.1.118 \
  --destination /home/lab209/Datasets/G1D_Lab/raw \
  --delete-after-verify \
  --with-video
```

`--with-video` 为本次成功拉取或核验为相同的任务生成视频。默认放在 destination 的上一级 `videos/` 中；上述命令即 `/home/lab209/Datasets/G1D_Lab/videos/`。需要另一位置时加：

```text
--video-output-root /home/lab209/Datasets/G1D_Lab/videos
```

执行顺序：认证 → 各条传输、校验、刷盘、按选项删除已验证 PC2 缓存 → 关闭 SSH/释放 PC2 活动锁 → 主机逐条转换视频。编码不占用 PC2 计算和存储，也不上传视频。

`--delete-after-verify` 仍以**原始数据**校验为条件，视频成功与否不改变这个条件。视频失败时主机 raw 保留，可能已经删除对应 PC2 缓存；用独立命令补做视频即可。失败返回非零状态并显示 `VIDEO ERROR`，仍会尝试其他已下载任务。若后续传输中断，之前已确认完成的任务仍会生成视频，然后报告传输错误。

去掉 `--with-video` 就只传输。去掉 `--delete-after-verify` 就保留 PC2 原始副本。不要把 `--with-video` 加到 `record_pc2.sh` 上，本功能在主机 fetch 时使用。

## 3. 单独转换已经拉取的数据

转换一条，不需要登录机器人：

```bash
conda activate pico_g1d
cd /home/lab209/Proj/Proj_PICO_G1-D
bash deploy/make_recording_video.sh \
  /home/lab209/Datasets/G1D_Lab/raw/episode_20261101_163742_3bb329d1 \
  --output-root /home/lab209/Datasets/G1D_Lab/videos
```

一次处理当前 raw 下所有任务（shell 将通配符展开为多个路径）：

```bash
bash deploy/make_recording_video.sh \
  /home/lab209/Datasets/G1D_Lab/raw/episode_* \
  --output-root /home/lab209/Datasets/G1D_Lab/videos
```

等价 Python 入口为 `python -m g1d_pico.recording video EPISODE... --output-root DIRECTORY`。支持原 v1 和新 v2。未完成任务拒绝转换，应先单独恢复。已从 PC2 删除的旧任务不会出现在下一轮 fetch 清单里，应使用本节命令转换。

| 独立转换参数 | 默认/作用 |
|---|---|
| 一个或多个 episode 路径 | 必填；可以用 shell 通配符 |
| `--output-root` | 必填；必须在原始 episode 之外 |
| `--fps` | 20；预览输出频率，1～60，按时间戳采样，不改变任务播放速度 |
| `--width` | 1280；拼接视频宽度，320～3840 且是 4 的倍数 |
| `--max-gap` | 0.25 秒；图像超过这个年龄后显示黑底 GAP，范围大于 0 且不超过 10 秒 |
| `--help` | 查看帮助 |

fetch 自动转换使用默认值。需要不同预览分辨率等参数时，用独立入口输出到另一个目录。这些参数只用于离线预览，不修改机器人配置、录制帧率或控制超时。

## 4. 输出目录与视频含义

```text
G1D_Lab/
├── raw/episode_.../                  原始数据，内容和清单不变
├── videos/episode_.../
│   ├── overview.mp4                  三路拼接任务回看
│   └── video.json                    来源、参数、时间、缺帧统计和视频哈希
├── annotations/episode_.../labels.json
└── training/episode_.../
```

视频上半部分为头部原图（双目左右拼接保留），下半部分为左腕和右腕。默认 1280×1000、20 FPS、无音频，使用 OpenCV 的 `mp4v` 编码写 MP4。是 MPEG-4 视频，不宣称为 H.264；可使用支持 MPEG-4 的本地播放器查看。

每帧显示本条经过时间和每路图像的接收年龄。按 PC2 单调接收时间取当前时刻之前最新图像，输出时长与任务区间一致，末尾最多向上补齐不足一个输出帧周期。

- `OK age=...`：使用不超过年龄阈值的图像；相邻输出时间步可能复用同张 JPEG。
- `NO FRAME`：该路尚未收到图像，或整条没有该相机；保留黑色面板。
- `GAP age=...`：最近图像过旧，显示黑底，不将长时间断流压缩掉，也不一直停留在旧画面掩盖缺帧。
- 三路按软件接收时间对齐，并非相机硬件曝光同步。

视频是有损、缩放后的回看副本，不能替代训练用原图和状态数据。`video.json` 记录各路源帧数、实际展示的不同源帧数、各状态所占输出帧数、源 manifest 哈希与视频 SHA-256。视频看起来流畅不代表任务成功，仍需人工检查和标注。

同一原始任务、相同参数且视频哈希正确时再次转换显示 `VIDEO EXISTS`，不会重复编码。参数不同、源清单不同或已有视频损坏时拒绝覆盖；可换一个 output-root。编码失败或 Ctrl+C 不发布半成品，原始数据保持不变。

## 5. 环境与排错

普通 fetch 仍只需 Python 标准库、ssh 和 rsync。视频功能额外使用**主机**环境中的 NumPy/OpenCV，不要求安装系统 ffmpeg 命令。代码会先试编码/解码一帧，确认本机 MP4 编码器可用。实现依据：[OpenCV VideoWriter 文档](https://docs.opencv.org/4.x/dd/d9e/classcv_1_1VideoWriter.html)。

当前主机 `pico_g1d` 已有可用的 OpenCV/NumPy。只有报缺少依赖时，才在主机该环境安装：

```bash
conda activate pico_g1d
python -m pip install opencv-python-headless numpy
```

不需要在 PC2 安装视频依赖。本项目的依赖提示不会自动安装包。

- `VIDEO START / PROGRESS / SAVED`：开始、转换进度、输出路径。
- `VIDEO ERROR`：查看具体原因，原始数据还在主机 raw，修复后用独立命令重试。
- `Transfer interrupted ... NOT deleted`：本条传输未走到源删除；保留 `.incoming`，再次执行 fetch。
- `No finalized episodes to fetch`：PC2 没有可拉取任务；不代表主机已有数据不能生成视频。
- `VIDEO EXISTS`：已有符合当前来源和参数的完整视频，直接复用。

编码占用主机 CPU 和额外磁盘；不会改变 PC2 缓存限额。整场运行日志仍需要另行归档。
