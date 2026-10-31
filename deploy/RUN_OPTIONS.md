# 启动选项与固定配置

控制参数只在项目配置文件中修改。运行程序固定根据自身项目位置寻找配置，不根据终端当前目录寻找，也不接受另一份配置路径。

| 配置文件 | 使用场景与内容 |
|---|---|
| `config/body.jsonc` | 控制程序启动读取：底盘速度、死区、立柱高度/速度、腰部与俯仰 |
| `config/arms.jsonc` | 启用双臂时读取：双臂跟随、归位、IK 与手柄映射 |
| `config/recording.jsonc` | 录制时读取：相机、保存 FPS、缓存位置与配额；拉取时由 PC2 读取以定位缓存 |

PC2 运行时读取 `/home/unitree/Proj/Proj_PICO_G1-D/config/`。主机上的文件改好后要同步过去；修改后重启生效，不支持热更新。配置文件不存在或数值非法会报错；双臂/身体省略的字段仍使用代码默认值。

已移除 `--config`、`--body-config`、`--arm-config`，以及 `--max-forward-mps`、`--max-yaw-rps`、`--max-lift`、`--deadzone`、`--height-min-m`、`--height-max-m`。旧命令带这些参数会报错，请删掉它们并把数值写进配置文件。

功能开关仍决定本次启用哪些设备。例如 `--without-lift` 关闭升降，即使配置中升降速度大于零也不会启用；它不修改配置文件。

## 完整控制入口

在 PC2 项目目录执行：

```bash
bash deploy/run_pc2_arms.sh --with-hands
```

默认包含相机、底盘、立柱、腰部转动/俯仰和双臂，不录制。

| 选项 | 作用 |
|---|---|
| `--with-hands` | 加入左右 Dex3-1 控制；不加则不启动手部桥 |
| `--without-lift` | 关闭本次立柱升降，仍保留腰部与俯仰 |
| `--with-lift` | 启用升降，已是此入口的默认值，无需特意填写 |
| `--skip-home` | 跳过启动时的双臂自动归位；不改变运行中的 Y 归位功能 |
| `--help` | 显示帮助，不启动控制 |

## 底盘与相机入口

```bash
bash deploy/run_pc2_base.sh
```

默认相机加底盘，不控制手臂、手部或立柱。

| 选项 | 作用 |
|---|---|
| `--camera-only` | 只显示相机和接收 PICO 输入，不启动机器人运动桥 |
| `--with-lift` | 在运动模式中加入立柱升降，不能与 `--camera-only` 组合 |
| `--with-arms` | 加入双臂和腰部；此底层入口仍需 `--with-lift` 才启用立柱 |
| `--with-arms-and-hands` | 加入双臂、腰部与 Dex3；立柱规则同上 |
| `--help` | 显示帮助 |

`--camera-only`、`--with-arms`、`--with-arms-and-hands` 三种模式互斥。日常完整操作建议使用上一节的 `run_pc2_arms.sh`，避免混淆默认升降状态。

## 控制并录制

```bash
bash deploy/record_pc2.sh \
  --instruction "去实验台拿起杯子" --scene lab209 --objects "cup_01" \
  -- --with-hands
```

| 选项/位置 | 作用 |
|---|---|
| `--instruction TEXT` | 本次任务的自然语言描述，默认空字符串 |
| `--scene TEXT` | 场景标识，默认 `lab` |
| `--objects TEXT` | 本次任务涉及的物体信息，原样保存为文本，默认空字符串 |
| `--` | 分隔录制任务信息和控制开关，本身不代表配置文件 |
| `--` 后 | 可写上一节完整控制入口的开关，如 `--with-hands`、`--without-lift`、`--skip-home` |
| `--help` | 显示录制入口帮助 |

启动后先显示 `REC WAITING`；左摇杆回中、松开后长按 L3 约 1 秒，收到 `REC RECORDING` 后开始本条任务。再次长按显示 `REC SAVING`；等待 `REC SAVED` 和 `REC WAITING` 后可录下一条。每条共用本次启动的 instruction/scene/objects，修改任务文字需重新启动。A/B 仍只控制运动。全部完成后按一次 Ctrl+C，等当前录制收尾及终端提示符返回，再拉取数据。只运行 `run_pc2_arms.sh` / `run_pc2_base.sh` 不会录制。

## 主机拉取

```bash
bash deploy/fetch_pc2_recordings.sh \
  --host unitree@192.168.1.118 \
  --destination /home/lab209/Datasets/G1D_Lab \
  --delete-after-verify
```

| 选项 | 作用 |
|---|---|
| `--destination PATH` | 必填，主机长期保存目录 |
| `--host USER@IP` | PC2 SSH 地址，默认 `unitree@192.168.1.118` |
| `--delete-after-verify` | 主机完整校验成功后删除允许删除的 PC2 源任务；不加则保留 |
| `--remote-root PATH` | PC2 项目目录，默认 `/home/unitree/Proj/Proj_PICO_G1-D` |
| `--remote-python PATH` | PC2 Python，默认 `/home/unitree/miniconda3/envs/pico_g1d/bin/python` |
| `--help` | 显示帮助 |

缓存路径由 PC2 项目内的 `config/recording.jsonc` 决定，主机不再传配置路径。传输完成前不要修改 PC2 的 `spool`，否则新配置会指向别处，旧数据仍留在原目录。

## 本次代码同步

固定配置入口及 L3 分段录制只改 Python、启动脚本和说明，不需要重新编译 C++。此前涉及官方范围版本的 C++ 升级仍需已完成。

先退出 PC2 上的控制/录制程序。在主机执行以下增量同步（配置数值以 PC2 现有文件为准）：

```bash
cd /home/lab209/Proj/Proj_PICO_G1-D
rsync -av --relative --exclude='__pycache__/' --exclude='*.pyc' \
  g1d_pico/ \
  deploy/run_pc2_base.sh deploy/run_pc2_arms.sh deploy/record_pc2.sh \
  deploy/fetch_pc2_recordings.sh deploy/RUN_OPTIONS.md \
  unitree@192.168.1.118:/home/unitree/Proj/Proj_PICO_G1-D/
```

若需要用主机配置替换 PC2 配置，再明确同步所需文件，例如：

```bash
rsync -av --relative config/body.jsonc config/arms.jsonc config/recording.jsonc \
  unitree@192.168.1.118:/home/unitree/Proj/Proj_PICO_G1-D/
```

第二条会覆盖 PC2 同名配置，包括录制缓存路径和参数；只同步准备采用的文件即可。同步后使用本文启动命令。

## Python 底层入口

通常无需直接调用。`python -m g1d_pico --help` 列出连接与运行模式参数：相机地址 `--robot-ip`、证书 `--cert/--key`、无相机 `--no-camera`、真机 `--live`、互斥的 `--base-only/--lift-only`、双臂/手部开关、`--skip-arm-home`、网卡 `--network-interface`、各桥二进制路径、URDF 路径、旧方案的 `--remote-bridge-port`。这些不覆盖三份配置中的调参数值；PC2 启动脚本已填写所需连接路径。

离线 `verify`、`recover` 的参数和用法保持不变，见 [恢复说明](PC2_RECORDING_RECOVERY.md)。
