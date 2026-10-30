# PC2 直接运行：相机 + PICO 底盘控制

相机和底盘已跑通后，新增双臂请使用 [PC2_ARMS.md](PC2_ARMS.md)；本文的默认启动方式仍只控制底盘。

这是本阶段的部署方案。以下命令由操作者自行执行；编写这些文件不代表已经部署或通过真机验证。

控制链路：PICO → Wi-Fi → PC2 上的 TeleVuer / 摇杆映射 → 官方 SDK2 → 内部 eth0 → 运控服务。
图像链路：现有 PC2 teleimager-server → PC2 本机 ImageClient → TeleVuer → Wi-Fi → PICO。
主机通过 SSH 查看日志。此阶段尚未实现视频/状态数据集记录、机器人反馈面板或双臂控制。

当前地址：PC2 wlan0 `192.168.1.118`，内部 eth0 `192.168.123.164`。
PICO 与 PC2 同连 `1301_5G`，PICO 访问 PC2 的 8012。无需 remote_server、8765 端口隧道或 PC1 网络改动。

## 1. 统一目录，并从主机复制文件（一次性）

PC2 上本项目新增的代码、依赖源码、证书和编译产物统一位于：

```text
/home/unitree/Proj/Proj_PICO_G1-D/
├── README.md
├── g1d_pico/                  # 遥操作 Python 代码
├── deploy/                    # 准备、编译、启动脚本与说明
├── tests/                     # 本项目测试
├── deps/                      # 第三方源码
│   ├── unitree_sdk2/          # 固定提交的官方 SDK2
│   └── televuer/              # 与主机已用版本一致
├── bridge/                    # C++ 控制接口源码
│   └── build-arm64/           # 在 PC2 编译生成，包含 ARM64 运行库
└── .local/                    # PC2 本地产物，不从主机同步
    ├── https/                 # PC2 专用证书与私钥
    ├── sdk2-build-arm64/       # SDK2 编译目录
    └── sdk2-arm64/             # SDK2 安装目录
```

Conda 环境继续由现有 Miniconda 管理：`~/miniconda3/envs/pico_g1d`。
现有相机服务和它引用的源码保留原位置。本阶段主机文本日志保存在主机项目的 `logs/`，不属于 PC2 数据集记录。

下面命令全部在**主机**执行。使用 rsync 的尾部 `/` 将目录内容复制到固定目标，重复执行不会多套一层文件夹。
两端需有 `rsync`（缺少时在对应机器安装 `sudo apt install rsync`）。

```bash
ssh unitree@192.168.1.118 'mkdir -p ~/Proj/Proj_PICO_G1-D/deps/unitree_sdk2 ~/Proj/Proj_PICO_G1-D/deps/televuer'

rsync -av \
  --exclude='.git/' --exclude='__pycache__/' --exclude='*.pyc' \
  --exclude='/.local/' --exclude='/deps/' --exclude='/logs/' \
  --exclude='/bridge/build/' --exclude='/bridge/build-arm64/' --exclude='/bridge/lib/' \
  --exclude='*.pem' \
  /home/lab209/Proj/Proj_PICO_G1-D/ \
  unitree@192.168.1.118:~/Proj/Proj_PICO_G1-D/

rsync -av \
  --exclude='.git/' --exclude='/build/' --exclude='/install/' \
  /tmp/unitree_sdk2_g1d/ \
  unitree@192.168.1.118:~/Proj/Proj_PICO_G1-D/deps/unitree_sdk2/

rsync -av \
  --exclude='.git' --exclude='__pycache__/' --exclude='*.pyc' \
  --exclude='*.pem' --exclude='*.egg-info/' --exclude='/build/' --exclude='/dist/' \
  /home/lab209/Proj/Proj_Pico_G1/xr_teleoperate/teleop/televuer/ \
  unitree@192.168.1.118:~/Proj/Proj_PICO_G1-D/deps/televuer/

ssh unitree@192.168.1.118
```

项目同步排除主机编译产物、运行环境、日志和证书；SDK2 保留官方预编译库，以供 PC2 构建使用。
命令不带 `--delete`，不会删除 PC2 已有文件。后续仅更新项目代码时，只需执行第一条 rsync；先正常退出遥操作程序再更新。

SDK2 使用当前已验证接口的固定提交 `63096d0ac0c5d2dec9d6e0c22cd5233410ca2f36`。
如果主机 `/tmp/unitree_sdk2_g1d` 已丢失，可在 PC2 上获取相同源码：

```bash
git clone https://github.com/unitreerobotics/unitree_sdk2.git ~/Proj/Proj_PICO_G1-D/deps/unitree_sdk2
git -C ~/Proj/Proj_PICO_G1-D/deps/unitree_sdk2 checkout 63096d0ac0c5d2dec9d6e0c22cd5233410ca2f36
```

复制 TeleVuer 是为了复用主机已经工作的版本，避免直接拉取更新版本引入接口差异。
TeleImager 沿用 PC2 当前 `tv` 环境中的客户端和正在运行的服务，不重启相机服务、不重新绑定 USB 相机。

## 2. PC2 上准备环境（一次性）

如缺少 `cmake`、`c++` 或 `openssl`，先执行：

```bash
sudo apt update
sudo apt install -y cmake build-essential openssl
```

在 PC2 执行：

```bash
conda activate tv
cd ~/Proj/Proj_PICO_G1-D
bash deploy/prepare_pc2_direct.sh 192.168.1.118
```

准备脚本自动从项目 `deps/unitree_sdk2` 与 `deps/televuer` 查找源码，无需另传依赖路径。

脚本会：

- 首次创建 PC2 的 `pico_g1d` 环境，克隆现有 `tv`；不在相机服务的 `tv` 环境中安装新包。
- 在 `pico_g1d` 中安装上述 TeleVuer。缺少的 Python 依赖通过 pip 下载，需要联网。
- 检查 TeleVuer / TeleImager 导入和构造接口，再编译 `bridge/build-arm64/g1d_agv_bridge`。
- 在项目 `.local/https/` 下生成带 PC2 IP 的专用自签名证书，不覆盖图像服务证书。
- 不启动 TeleVuer 或 SDK2 控制程序，也不修改 PC1、网卡和系统服务。

Conda 克隆后的 editable 包可能仍引用原来的源码目录，因此不要修改共享的 TeleImager 源码。
脚本重复执行会复用环境和证书。证书过期或 Wi-Fi IP 变化时会报错；优先在路由器为 PC2 保留当前地址。
若确需换 IP，将项目 `.local/https/` 目录移到备份位置后用新 IP 重跑准备脚本，并在 PICO 重新信任证书。
此项目仅针对已知 `/home/unitree/miniconda3` 布局提供默认启动路径；若 Conda 位于其他目录，设置 `PICO_G1D_PYTHON`。

## 3. PC2 上启动相机与底盘（每次使用）

停止旧的底盘遥操作进程，PC2 上的 8012 也只能有一个 TeleVuer 实例；保留现有 teleimager-server。
机器人保持此前有线底盘控制已经可用的运控状态，原装遥控器在操作者手边。

```bash
cd ~/Proj/Proj_PICO_G1-D
bash deploy/run_pc2_base.sh
```

启动器明确选择 ARM64 二进制、内部 `eth0`、本机相机 `127.0.0.1` 和 `--live --base-only`。
无需在主机运行 `python -m g1d_pico`，也无需启动 `g1d_pico.remote_server`。
启动器不需要 `sudo`，不依赖图形桌面；保持 SSH 终端打开以查看日志。

进入真机模式会开始调用底盘接口；尚未使能时下发零速度。只有新鲜 PICO 输入、摇杆回中并按 A 后才允许非零指令。
默认速度上限：前后 `0.10 m/s`、转向 `0.15 rad/s`。

## 4. PICO 打开 PC2 页面

在 PICO 浏览器先访问并按浏览器提示信任此证书：

```text
https://192.168.1.118:8012
```

然后使用：

```text
https://192.168.1.118:8012/?ws=wss://192.168.1.118:8012
```

如果原有 PICO 浏览器需要外部页面作为前端，改用：

```text
https://vuer.ai?ws=wss://192.168.1.118:8012
```

进入 Virtual Reality，确认看到机器人画面后：

- 两根摇杆回中，按右手 A 使能。
- 左摇杆上下前后移动，左右转向；右摇杆不控制升降。
- 右手 B 解除使能，退出时先按 B，再在 SSH 终端按 Ctrl+C。

本阶段不控制升降、双臂或 Dex3-1。输入超时保护和 SDK2 子进程命令超时保护沿用原有代码；它们不能替代现场停机手段。
不要用拔网线、关闭 SSH 窗口或结束浏览器来代替正常停机流程。

## 5. 主机保留运行日志（可选启动方式）

下面命令在**主机**执行，是第 3 步启动方式的替代，不能两处同时启动：

```bash
mkdir -p /home/lab209/Proj/Proj_PICO_G1-D/logs
set -o pipefail
ssh -tt unitree@192.168.1.118 \
  'cd ~/Proj/Proj_PICO_G1-D && bash deploy/run_pc2_base.sh' \
  2>&1 | tee "/home/lab209/Proj/Proj_PICO_G1-D/logs/pc2-base-$(date +%Y%m%d-%H%M%S).log"
```

主机可实时看到 ARMED / DISARMED、摇杆映射指令和 SDK2 错误，并保留终端文本。
这些日志是调试信息，不是机器人实际位姿反馈或完整训练数据集。

## 常见启动错误

- `Run this launcher on ... ARM64 PC2`：在主机误运行了 PC2 启动器。
- `Missing runtime or certificates`：先执行环境准备脚本，或核对 `PICO_G1D_PYTHON`。
- `Cannot use port 8012`：PC2 已有 TeleVuer 实例；在对应终端正常退出它，不要停止相机服务器。
- `IP address mismatch`：PC2 的 Wi-Fi IP 与证书不匹配，按第 2 步处理。
- 相机连接失败：保留现有图像服务，确认它的配置服务和 head_camera 的 ZMQ 流仍开启。
- `SDK2 call failed`：先停下程序，检查内部 eth0、运控状态以及是否有另一控制程序占用。无需改 PC1 的网络配置。

仅需排查图像时，可运行 `bash deploy/run_pc2_base.sh --camera-only`，该选项不会创建 SDK2 控制进程。
双臂、Dex3-1 和主机正式数据记录属于后续独立扩展。

## 若已经按旧说明复制过文件

新方案不再使用 `~/Proj_PICO_G1-D`、`~/unitree_sdk2_g1d` 和 `~/televuer` 这三个分散目录。
按新路径重新同步后，在新目录重跑准备脚本：它会重新建立编译目录，并将本项目环境中的 TeleVuer editable 安装指向新的 `deps/televuer`。
不要直接搬动 Conda 环境或复用旧路径的 CMake 编译缓存。
旧目录由你确认无进程或 editable 包引用后再归档；本项目脚本不会自动搬移或删除它们，也不会改动现有相机服务的目录。
