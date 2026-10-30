# Dex3 左手反馈超时排查

这套方法先区分“当前手部桥没有处理到状态”与“独立 DDS 观察者也没有收到状态”。探针 `bridge/g1d_hand_state_probe.cpp` 仅订阅左右手的普通与低频 `HandState_`，不创建命令发布者、不接管机器人。它在 PC2 上使用 `eth0` 与 DDS domain 0，并使用独立进程和 SDK2 的直接回调（队列长度 0）；现有手部桥使用队列长度 1。

独立探针与手部桥都在 PC2 上，所以如果两者同时丢失左手状态，仍需继续区分机器人侧状态发布与 PC2 所在链路/DDS 接收问题；不能仅凭这一步判定硬件故障。

## 1. 主机同步

保持之前的项目目录。在**主机**执行：

```bash
rsync -av \
  --exclude='.git/' --exclude='__pycache__/' --exclude='*.pyc' \
  --exclude='/.local/' --exclude='/deps/' --exclude='/logs/' \
  --exclude='/bridge/build/' --exclude='/bridge/build-arm64/' --exclude='/bridge/lib/' \
  --exclude='*.pem' \
  /home/lab209/Proj/Proj_PICO_G1-D/ \
  unitree@192.168.1.118:/home/unitree/Proj/Proj_PICO_G1-D/
```

## 2. PC2 编译

在 **PC2** 执行；这一步只编译，不启动控制：

```bash
conda activate pico_g1d
cd /home/unitree/Proj/Proj_PICO_G1-D
bash deploy/build_on_pc2.sh /home/unitree/Proj/Proj_PICO_G1-D/deps/unitree_sdk2
```

编译完成后应有 `bridge/build-arm64/g1d_hand_state_probe`。

## 3. 两个 PC2 终端并行记录

**终端 A** 先启动探针，并保持运行：

```bash
cd /home/unitree/Proj/Proj_PICO_G1-D
mkdir -p logs
set -o pipefail
LD_LIBRARY_PATH="$PWD/bridge/build-arm64/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
  ./bridge/build-arm64/g1d_hand_state_probe eth0 2>&1 \
  | tee "logs/dex3_probe_$(date +%Y%m%d_%H%M%S).log"
```

先等约 3 秒，确认 `left.fast`、`left.lf`、`right.fast`、`right.lf` 的 `rx` 都在增长。探针每 0.5 秒输出两行，分别对应左、右手。

**终端 B** 启动已有遥操作：

```bash
conda activate pico_g1d
cd /home/unitree/Proj/Proj_PICO_G1-D
mkdir -p logs
set -o pipefail
bash deploy/run_pc2_arms.sh --with-hands 2>&1 \
  | tee "logs/dex3_takeover_$(date +%Y%m%d_%H%M%S).log"
```

按现有方法使用 PICO。若主程序再次退出，让终端 A 的探针继续运行至少 5 秒，观察左手反馈是否恢复，再按 Ctrl+C 结束探针。保留两个日志文件。

## 4. 读数和判定

- `rx`：探针回调收到的状态总数；`valid`：包含 7 个有限关节角的状态数；`bad`：电机数量错误或关节角非有限数的状态数。
- `age_ms`：该探针最近一次收到**任意状态**距今的时间；`max_gap_ms`：已结束的最长收包间隔；`gaps250`：已结束且至少 250 ms 的间隔数。若没有再收到新包，`age_ms` 会持续增长，而 `gaps250` 直到恢复收包后才增加。
- 探针 `age_ms` 与手部桥日志里的 `fast_age`/`lf_age` 来自**不同进程**。两者都只代表 PC2 观察到的状态，不直接代表机器人控制器是否发送。

| 主程序超时时探针的表现 | 下一步关注点 |
| --- | --- |
| 左手 `fast`、`lf` 的 `age_ms` 同时超过 250，右手继续更新 | 左手状态源、机器人内部传输或 PC2 对左手话题的 DDS 接收；若故障后恢复，看 `max_gap_ms` |
| 探针左手持续更新、`bad=0`，但手部桥报告左手超时 | 当前桥的队列长度 1 回调路径或进程内处理；对比直接回调与队列回调 |
| 探针 `rx` 增长而左手 `bad` 增长 | 有状态到达，但格式/数值异常；对比手部桥的丢弃计数 |
| 四条话题一起停止更新 | 优先查 PC2 `eth0`、DDS 与系统级停顿 |

若探针与手部桥同时丢失左手状态，还不能直接判断机器人内部哪一段故障。下一步才需要从状态发布端或更底层的链路观察。暂时保留 250 ms 保护，不通过增大超时门槛掩盖故障。
