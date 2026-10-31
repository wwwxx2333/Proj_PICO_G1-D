# G1-D 无线真机部署：主机负责 PICO，PC2 负责 SDK2

本方案保留实验电脑上的 TeleVuer、手柄映射和图像接收；PC2 只运行一个轻量控制桥。数据走向是：PICO → 实验电脑 `:8012` → SSH 隧道 → PC2 `127.0.0.1:8765` → PC2 `eth0` → G1-D 运控单元。相机从 PC2 `wlan0=192.168.1.118` 发往实验电脑。PC2 控制服务只监听回环地址，不能从局域网直接连接。

目前代码只实现**底盘与升降**，尚未实现双臂或 Dex3-1。已有有线真机模式保持不变；无线模式新增 `--remote-bridge-port`。下面的网络地址来自当前设备配置：PC2 `wlan0=192.168.1.118/24`、`eth0=192.168.123.164/24`；实验电脑无线地址此前为 `192.168.1.150`。

## 1. 从实验电脑传输项目和 SDK2 源码

在**实验电脑**运行以下命令，`scp` 会提示输入 PC2 登录密码。`/tmp/unitree_sdk2_g1d` 是本机先前下载的官方 SDK2，固定提交为 `63096d0ac0c5d2dec9d6e0c22cd5233410ca2f36`。

```bash
ls /tmp/unitree_sdk2_g1d/lib/aarch64/libunitree_sdk2.a
scp -r /home/lab209/Proj/Proj_PICO_G1-D unitree@192.168.1.118:~/
scp -r /tmp/unitree_sdk2_g1d unitree@192.168.1.118:~/
```

如果 `/tmp` 源码已消失，可以在 PC2 可访问互联网时从[宇树官方 SDK2](https://github.com/unitreerobotics/unitree_sdk2) 获取相同提交：

```bash
git clone https://github.com/unitreerobotics/unitree_sdk2.git ~/unitree_sdk2_g1d
git -C ~/unitree_sdk2_g1d checkout 63096d0ac0c5d2dec9d6e0c22cd5233410ca2f36
```

## 2. PC2 上编译 ARM64 控制桥

从实验电脑 `ssh unitree@192.168.1.118` 登录后，在 **PC2** 执行。`uname -m` 应输出 `aarch64`。若缺少构建工具，先在 PC2 安装 `cmake` 和 `build-essential`。

```bash
uname -m
cd ~/Proj_PICO_G1-D
bash deploy/build_on_pc2.sh ~/unitree_sdk2_g1d
file bridge/build-arm64/g1d_agv_bridge
LD_LIBRARY_PATH="$PWD/bridge/build-arm64/lib" ./bridge/build-arm64/g1d_height_probe eth0
```

最后一条只读取立柱反馈，不发送运动命令。若无反馈，先检查 PC2 的 `eth0` / DDS。构建脚本不覆盖实验电脑原有的 x86-64 二进制文件；ARM64 库随 PC2 二进制文件放在 `bridge/build-arm64/lib`。

## 3. 在 PC2 启动控制桥

在 **PC2** 新开一个 SSH 终端执行：

```bash
cd ~/Proj_PICO_G1-D
/home/unitree/miniconda3/envs/tv/bin/python3.10 -m g1d_pico.remote_server \
  --network-interface eth0 --port 8765
```

终端应显示 `PC2 bridge listening on 127.0.0.1:8765; DDS on eth0`。此时尚未连接 PICO，也不会使能运动；保持该终端运行。

## 4. 在实验电脑建立 SSH 隧道

在**实验电脑**另开一个终端执行：

```bash
ssh -N \
  -L 127.0.0.1:8765:127.0.0.1:8765 \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=2 \
  -o ServerAliveCountMax=2 \
  unitree@192.168.1.118
```

输入 SSH 密码后，该终端停在前台且没有输出是正常现象；保持它运行。隧道断开或主机不再发命令时，PC2 桥的现有 300 ms 命令超时会下发零速度。此处只说明代码行为，最终仍须以现场真机结果确认。

## 5. 在实验电脑运行原有 PICO 程序

在**实验电脑**第三个终端运行，先用已验证的底盘单独模式：

```bash
conda activate pico_g1d
cd /home/lab209/Proj/Proj_PICO_G1-D
python -m g1d_pico --live --base-only \
  --robot-ip 192.168.1.118 \
  --remote-bridge-port 8765
```

若有线真机模式中的升降软件边界已经核实，先将**同一组** `rt/hispeed_state.y` 上下限写入运行端 `config/body.jsonc` 的 `height_min_m` / `height_max_m`，再启用双摇杆；整机高度 `1.26–1.68 m` 不是这两个参数的坐标：

```bash
python -m g1d_pico --live \
  --robot-ip 192.168.1.118 \
  --remote-bridge-port 8765
```

PICO 继续访问**实验电脑**上的 TeleVuer 地址，例如 `https://vuer.ai?ws=wss://192.168.1.150:8012`；不用连接 PC2 的 `8012`。每次启动两根摇杆先回中，右手 A 使能，右手 B 解除使能。机器人周围保持空旷、原装遥控器在手边；不要同时运行旧的有线真机进程或其他底盘/升降控制程序。

断开时先在 PICO 按 B，再在实验电脑的控制程序按 `Ctrl+C`；控制程序结束后可关闭 SSH 隧道和 PC2 桥服务。
