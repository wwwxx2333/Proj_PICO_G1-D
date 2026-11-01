# 自动运行日志

三个启动入口 `run_pc2_base.sh`、`run_pc2_arms.sh`、`record_pc2.sh` 都会自动保存完整运行日志。一次启动对应一个文件，即使通过 L3 录了多条 episode，也只生成一份整场运行日志。

## PC2 启动方式

```bash
cd /home/unitree/Proj/Proj_PICO_G1-D

# 默认终端仅显示必要信息，文件保存全部日志。
bash deploy/run_pc2_arms.sh --with-hands

# 需要详细排查时，替代上一条命令。
bash deploy/run_pc2_arms.sh --with-hands --full-log
```

控制并录制时：

```bash
bash deploy/record_pc2.sh \
  --instruction "前往实验台拿起杯子" \
  --scene lab209 --objects "cup_01" \
  -- --with-hands
```

需要全部终端输出时，在 `record_pc2.sh` 后添加 `--full-log`，推荐放在分隔符 `--` 之前。日志配置固定读取项目的 `config/logging.jsonc`，无需传入配置路径。

L3、A/B、归位和退出流程保持原有行为：启动完成前置准备；`REC READY` 后按 L3 开始，松开后再次长按约 1 秒结束本条。全部任务结束按一次 Ctrl+C，等待收尾及终端提示符返回。

## 文件位置与保留规则

默认 PC2 路径：

```text
/home/unitree/Proj/Proj_PICO_G1-D/logs/runtime/
  run_20261031_153020_123456_a1b2c3d4.log
  .rotation.lock
```

每行附加本机接收时间，精确到毫秒并带时区，例如 `[2026-10-31T15:30:20.123+08:00]`。这不是相机曝光时间或机器人反馈源时间；源数据时间仍在遥测文件中。

启动终端显示 `LOG FILE` 和绝对路径。文件记录启动命令、启动检查、等待状态、Python/C++ 普通输出、异常堆栈、录制过程及退出状态。无需再用 `tee`；对默认终端做 `tee` 只会得到筛选后的内容。

- 默认最多 100 份；第 101 次启动前删除最旧的已关闭日志。
- 只管理本目录符合 `run_日期_时间_微秒_随机编号.log` 格式的普通文件，不删除符号链接、手动日志、episode 或其他文件。
- 使用文件锁保护正在写入的日志，并发启动时不会删掉活动日志。若所有名额都在使用，明确报错且不启动新的控制进程。
- 保留规则在新建时执行；修改目录后不会清理旧目录。数量上限不是磁盘容量上限，单次长期运行的文件会持续增长。

## 终端显示规则

| 内容 | 默认终端 | 完整文件 / `--full-log` |
|---|---|---|
| 录制准备、开始、长按结束、保存、失败 | 显示 | 保留 |
| A/B 启停、等待 PICO、等待按键 | 简化状态，变化立即提示，等待定期提醒 | 原始输出保留 |
| 双臂归位 | 等待、倒计时、约每 10% 进度、完成和取消 | 原始输出保留 |
| 摇杆速度、手爪扳机数值、连续 IK 耗时 | 隐藏 | 保留 |
| 俯仰不跟随、立柱限制、模式切换 | 状态变化和必要提醒 | 保留 |
| 异常、警告、未知 SDK 输出、多行堆栈 | 保留 | 保留 |

只筛选已识别的周期状态，不会把所有不认识的输出都隐藏。第三方库的独立警告可能仍较多。`--full-log` 不增加额外机器人采样，只显示原本已写入文件的详细输出。

## 日志与采集数据的关系

| 文件 | 范围 | 限制及传输 |
|---|---|---|
| `logs/runtime/run_*.log` | 整次启动，包括未录制时和保存阶段 | 按数量轮换，不占录制缓存配额；在 PC2 占磁盘，不由 fetch 自动传输 |
| `episode_*/console.log` | 本条录制期间的控制输出，终端筛选前的内容 | 属于 episode 缓存，随任务校验和传回主机 |
| `episode_*/telemetry.jsonl` | 机器人状态、目标、PICO 输入等结构化数据 | 沿用原采集策略 |

机器人桥的 STATE6/HANDSTATE2 是机器通信协议，继续按原方式解析和记录遥测，不额外逐条复制到普通运行日志。TeleImager 的独立系统服务日志不在本进程树内，仍需通过原服务日志查看。当前控制进程内的 ImageClient 输出会被收集。

需要将完整运行日志交给主机分析，结束程序后可在主机执行：

```bash
cd /home/lab209/Proj/Proj_PICO_G1-D
mkdir -p logs/pc2_runtime
rsync -av \
  --include='run_*.log' --exclude='*' \
  unitree@192.168.1.118:/home/unitree/Proj/Proj_PICO_G1-D/logs/runtime/ \
  logs/pc2_runtime/
```

这是手动复制，不删除 PC2 文件；主机归档目录不参与 PC2 的 100 份轮换。

## 配置及异常处理

`config/logging.jsonc` 每个字段已写明注释和范围：

| 字段 | 默认 | 允许范围 / 用途 |
|---|---|---|
| `directory` | `logs/runtime` | 项目相对路径或绝对路径 |
| `max_files` | 100 | 整数 1～100 |
| `flush_interval_s` | 1.0 | 0.1～10 秒，文件缓冲刷新间隔 |
| `wait_repeat_s` | 10.0 | 1～300 秒，相同等待状态的终端提醒间隔 |
| `queue_capacity` | 4096 | 整数 128～16384，文件与终端各自的有界队列 |

独立日志进程持续读取输出，文件与终端分别由后台线程处理，控制循环不直接等待日志写盘。它仍消耗少量 CPU、内存和磁盘，不能据此保证真机延迟完全不受影响。

正常运行完整保存日志；磁盘写入失败或输出超过缓冲能力时显示 `LOG_HEALTH` / `LOG_ERROR`，给出错误或丢失数量，控制不因日志队列已满而等待。终端慢导致的显示丢失与文件丢失分别计数。日志文件出现此类提示时不能视为完整记录。若控制本身正常退出而完整文件保存失败，日志入口返回非零状态。

配置缺失/非法、目录无权限或初始文件写入失败时，在启动控制前报错。Python 环境本身不可用时只能直接在终端报错，无法创建 Python 日志进程。正常退出会刷新并同步日志；最后的日志收尾最多等待约 10 秒，这发生在机器人和录制收尾结束之后，不限制 episode 的保存时长。强制结束或断电仍可能丢失尚未刷盘的尾部。

## 本次更新到 PC2

本次没有 C++ 改动，无需重新编译。在 PC2 退出控制/录制程序后，在主机执行：

```bash
cd /home/lab209/Proj/Proj_PICO_G1-D
rsync -av --relative --exclude='__pycache__/' --exclude='*.pyc' \
  g1d_pico/ \
  deploy/runtime_logging.sh deploy/run_pc2_base.sh deploy/run_pc2_arms.sh \
  deploy/record_pc2.sh deploy/RUNTIME_LOGGING.md deploy/RUN_OPTIONS.md \
  config/logging.jsonc \
  unitree@192.168.1.118:/home/unitree/Proj/Proj_PICO_G1-D/
```

此命令不覆盖身体、手臂或录制参数。新增日志配置须随代码一起同步；之后若 PC2 已定制这份配置，可从再次同步的清单中去掉 `config/logging.jsonc`。同步后按本文命令启动即可。
