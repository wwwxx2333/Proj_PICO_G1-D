# 录制收尾与恢复更新

恢复工具兼容旧 v1 与分目录 v2，保持源格式和原模型/标定快照，另存新目录。v2 恢复无法由分流文件重建总接收顺序，因此报告不重建跨流缺包计数；空计数不能解释为没有丢包。当前同步与编译以 [TRAINING_DATA.md](TRAINING_DATA.md) 为准。

当前启动入口已支持 L3 分段录制：启动完成准备后，短按开始、松开后长按约 1 秒结束一条；详细流程见 [PC2_RECORDING.md](PC2_RECORDING.md)。下文恢复与退出规则继续适用。

当前默认配置为 `.jsonc`。已有项目升级时先按 [JSONC 配置迁移说明](ARM_TUNING.md) 保留 PC2 参数并更新代码；仅本次扩展名迁移无需执行下文针对历史功能更新的 C++ 编译。

本次修复录制结束超时、增加离线恢复，并调整 Dex3 DDS 退出生命周期。20 FPS、原来的 JPEG 格式、缓存上限和遥操作按键不变。原始录制目录不自动修改或删除。

## 同步和编译

先退出 PC2 上的控制/录制程序。在主机执行：

```bash
cd /home/lab209/Proj/Proj_PICO_G1-D
rsync -av --relative --exclude='__pycache__/' --exclude='*.pyc' \
  g1d_pico/ \
  bridge/g1d_hand_bridge.cpp bridge/g1d_dds_channel.hpp \
  deploy/PC2_RECORDING.md deploy/PC2_RECORDING_RECOVERY.md \
  unitree@192.168.1.118:/home/unitree/Proj/Proj_PICO_G1-D/
```

同步不包含 `.local/`、配置、日志、第三方库或主机二进制。在 PC2 现有构建目录重编译手爪桥接：

```bash
conda activate pico_g1d
cd /home/unitree/Proj/Proj_PICO_G1-D
cmake --build bridge/build-arm64 --target g1d_hand_bridge -j2
```

该命令只编译手爪桥接，不替换 PC2 现有 DDS 动态库。需要同时部署新的 Python 包、C++ 文件和头文件。其余桥接无需为本次更新重编译。

## 新录制和退出行为

启动方式不变，例如在 PC2：

```bash
bash deploy/record_pc2.sh \
  --instruction '拿起实验台上的杯子并放入托盘' \
  --scene lab209 \
  --objects cup,tray \
  -- --with-hands
```

第一次 Ctrl+C 交给主控程序，让底盘、手爪、手臂、视频按顺序清理。手爪通过主线程显式关闭底层 DDS reader/writer 后再释放 SDK 对象；每侧打印退出码，并尽力记录为 `bridge_exit`。单个资源清理报错不会跳过其他资源。

主控结束后进入录制收尾，显示 `state: finalizing`、阶段、已处理/总文件数、当前文件和耗时。阶段包括 `enumerate_files`、`sync_metadata`、`sync_files`、`sync_directories`、`publish_manifest`。没有总计 30 秒强制终止。没有实际进度超过 60 秒会提示当前阶段并继续等待。

等待 `state: finished` 和终端提示符返回后，再运行原来的 fetch 命令。控制尚未退出时再次 Ctrl+C 会升级为进程组终止；在录制收尾时再次 Ctrl+C 则明确取消收尾并保留恢复数据。长时间未完成不等于程序已经退出，请不要在此时手动删除 `IN_PROGRESS`。

录制进程异常退出或收尾取消时，监督程序不把它报告为成功。控制正常但录制未完成时返回码为 2；控制本身失败则保留其错误码。`manifest.json` 新增 `control_exit_code` 和已收到的 `bridge_exits`；缺少退出记录意味着未知，不能当成正常。

## 恢复旧的未完成录制

恢复工具只在控制/录制结束后使用。默认仅审计原始数据；指定输出目录时创建一个新的 episode。恢复工具不启动 DDS、不连接机器人、不要求遥操作复现问题。

先在**主机**备份这次 PC2 缓存，保留机器人原文件：

```bash
mkdir -p /home/lab209/Datasets/G1D_Lab_recovery
rsync -av --partial --info=progress2 \
  unitree@192.168.1.118:/home/unitree/Proj/Proj_PICO_G1-D/.local/recording_spool/episode_20261027_194904_05ca9dae \
  /home/lab209/Datasets/G1D_Lab_recovery/
```

主机执行只读审计（不改 episode 内文件；会持有旁边的锁文件防止并发修改）：

```bash
conda activate pico_g1d
cd /home/lab209/Proj/Proj_PICO_G1-D
python -m g1d_pico.recording recover \
  /home/lab209/Datasets/G1D_Lab_recovery/episode_20261027_194904_05ca9dae
```

检查图片 SHA256/大小/JPEG边界、索引连续性、图像时间顺序、状态 JSONL 记录，统计各相机和状态流；输出被忽略的未索引文件、残缺尾部、是否观察到 `session_end`。这里的 JPEG 检查是字节完整性和边界检查，不代替实际图像解码/人工质量检查。

审计通过后另存恢复副本到主机正式数据目录：

```bash
python -m g1d_pico.recording recover \
  /home/lab209/Datasets/G1D_Lab_recovery/episode_20261027_194904_05ca9dae \
  --output-root /home/lab209/Datasets/G1D_Lab
```

工具打印 `recovered_episode`，里面是新的 episode ID。副本保留原始观测时间和时钟锚点，使用新目录 ID，并生成 `recovery.json` 和带恢复标记的 `manifest.json`；完成后自动执行校验。

若**仅最后一行 JSONL 残缺**，默认拒绝复制；检查审计报告后，可显式加 `--allow-truncated` 只在新副本中忽略该尾行。中间记录损坏、索引图片缺失或校验不一致仍拒绝恢复。未索引图片不会混入训练数据，但保留在原始备份中并列入报告。

恢复记录的 `recording_stop_reason` 为 `RECOVERED_UNFINALIZED`，`tail_completeness` 为 `unknown`，`task_success` 为 null；即使现存文件校验通过，也不能证明中断前最后一段数据全部到达。恢复副本禁止自动删除源端副本；先人工查看任务完整性，再自行决定如何管理原始缓存。

不要把 `--output-root` 设在原缓存/原备份目录内部。推荐始终在主机恢复，以免 PC2 同时放下原始数据和恢复副本、增加缓存占用。恢复并非自动回收 PC2 缓存；原未完成目录仍保留。

## 离线验证范围

Python 测试包含慢速收尾超过 30 秒、状态管道 EOF、明确取消、原文件保留、残缺尾行、损坏图片拒绝、恢复副本禁止自动删除、正常 Ctrl+C 和主控优先停止。额外规模检查覆盖三路合计 36,000 张合成 JPEG 边界数据（相当于每路 20 FPS、10 分钟的文件数量，不是实际场景录像）。

`tests/dds_shutdown_test.cpp` 在 `lo`、独立 domain 230 和虚构状态话题上反复关闭 DDS reader/writer，不发布任何机器人运动话题。运行时必须使用配套的 `libddsc.so.0` / `libddscxx.so.0`，避免混用系统 ROS 的 DDS 库。主机验证不能替代 PC2 ARM64 编译或证明 PC2 定制动态库的行为完全相同。
