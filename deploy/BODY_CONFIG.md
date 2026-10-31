# 双臂与身体配置分离

配置目录现在分为三份，每项参数上方保留含义、单位、建议范围和程序允许范围的 `//` 注释：

| 文件 | 调整内容 |
|---|---|
| `config/body.jsonc` | 底盘前后/转向、摇杆死区、立柱升降、腰部左右转动、上身俯仰与俯仰跟随保护 |
| `config/arms.jsonc` | 双臂关节跟随/归位速度、手柄位移比例、侧握范围、末端限速与 IK 平滑 |
| `config/recording.jsonc` | 图像保存帧率、相机列表、PC2 缓存目录与容量 |

这是配置组织调整，不改变按键映射、接管方式或 C++ 控制协议。双臂和腰部仍由现有上身桥共同执行；`--with-arms` 仍启用双臂与腰部，未增加单独的腰部运行模式。相机与 Dex3 参数不在 `body.jsonc` 内。

## 常用身体参数

| 参数 | 随本次拆分保存的值 | 含义 |
|---|---:|---|
| `deadzone` | 0.15 | 左右摇杆共用死区，包括按 A 和模式切换的回中判断 |
| `max_forward_mps` | 0.10 | 底盘前后最大速度，m/s |
| `max_yaw_rps` | 0.15 | 底盘转向最大角速度，rad/s |
| `max_lift_normalized` | 0.15 | 立柱升降归一化速度，不是 m/s |
| `height_min_m` / `height_max_m` | 0.0 / 0.42 | `rt/hispeed_state.y` 立柱反馈窗口，m |
| `waist_yaw_limit_rad` | 0.5235987756 | 腰部相对零位的左右转动范围 ±30° |
| `waist_yaw_speed_rad_s` | 0.10 | 腰部左右转动最大目标速度，rad/s |
| `waist_pitch_min_rad` / `waist_pitch_max_rad` | 0.0 / 0.5235987756 | 俯仰目标角度窗口，rad |
| `waist_pitch_speed_rad_s` | 0.08 | 俯仰最大目标速度，rad/s |

原 `arms.jsonc` 中所有 `waist_*` 参数原值迁到 `body.jsonc`，包括转腰/俯仰加速度和俯仰跟随保护。双臂当前已调整的 `position_scale=0.6`、`clutch_rotation_rad=1.2` 保留。

当前主机文件选定值为前后 0.30 m/s、转向 0.15 rad/s、升降 0.15；合法幅值已分别对齐官方 1.5、0.6、1.0。Python、远程入口和 C++ 一致校验，超范围报错而非静默裁剪。全部配置核验与来源见 [OFFICIAL_LIMITS.md](OFFICIAL_LIMITS.md)。立柱不是整机高度，不能填 1.26/1.68 m；当前窗口内的停止余量仍由 C++ 自动保留，约为 0.01～0.41 m。

`arms.jsonc` 的 `control_hz` 仍表示启用双臂时的共享 Python 主循环频率，因此也影响这个循环内底盘等指令的更新节奏。纯底盘模式仍为 20 Hz。

## 本次同步到 PC2

2026-10-30 官方范围对齐修改了 Python 和 C++，必须同步并重新编译。启动时离线检查二进制范围，未更新会明确提示。以下命令已包含桥源码；仅完成先前的配置拆分同步不够。

先正常退出 PC2 上的控制/录制程序，等待录制收尾。若 PC2 的 `arms.jsonc` 有主机没有的自定义值，先保留备份并将其双臂值合并到主机 `arms.jsonc`、`waist_*` 值合并到主机 `body.jsonc`，再执行同步。旧的混合 `arms.jsonc` 不会静默忽略腰部参数，而是报错提示移入 `body.jsonc`。

可在 **PC2** 备份现有配置：

```bash
cd /home/unitree/Proj/Proj_PICO_G1-D
config_backup=".local/config_backups/$(date +%Y%m%d_%H%M%S)"
mkdir -p "$config_backup"
cp -a config/arms.jsonc "$config_backup/"
if [ -f config/body.jsonc ]; then cp -a config/body.jsonc "$config_backup/"; fi
```

然后在 **主机** 同步。本命令会覆盖 PC2 的同名双臂/身体配置，使用主机当前值：

```bash
cd /home/lab209/Proj/Proj_PICO_G1-D
rsync -av --relative --exclude='__pycache__/' --exclude='*.pyc' \
  g1d_pico/ config/arms.jsonc config/body.jsonc \
  bridge/*.cpp bridge/*.hpp bridge/CMakeLists.txt \
  deploy/run_pc2_base.sh deploy/run_pc2_arms.sh \
  deploy/BODY_CONFIG.md deploy/ARM_TUNING.md deploy/OFFICIAL_LIMITS.md \
  unitree@192.168.1.118:/home/unitree/Proj/Proj_PICO_G1-D/
```

**随后在 PC2 编译：**

```bash
cd /home/unitree/Proj/Proj_PICO_G1-D
bash deploy/build_on_pc2.sh deps/unitree_sdk2
```

该增量同步不包含 PC2 的相机服务、证书、录制缓存、二进制或 `recording.jsonc`。旧 `.json` 格式用户也应同步新的 Python 读取器和 `.jsonc` 文件，默认路径不回退到 `.json`；仅重命名旧混合配置不能完成身体参数拆分。

## 启动与修改生效

实际运行读取 PC2 项目中的配置。编辑主机配置后需同步；编辑 PC2 配置后需退出并重新启动程序，不支持热更新。

在 **PC2** 运行完整控制（相机、底盘、立柱、腰部、双臂、Dex3）：

```bash
cd /home/unitree/Proj/Proj_PICO_G1-D
bash deploy/run_pc2_arms.sh --with-hands
```

只开相机和底盘仍用 `bash deploy/run_pc2_base.sh`，同样读取 `body.jsonc` 的底盘速度和死区。若这个启动方式需要立柱，再加 `--with-lift`，高度从配置读取。完整控制加 `--without-lift` 会关闭升降，保留转腰/俯仰。

配置位置固定为运行端项目的 `config/`，与当前工作目录无关。不再提供指定配置路径或临时覆盖数值的参数；速度、死区、高度和腰部参数直接编辑 `body.jsonc`，双臂参数编辑 `arms.jsonc`。缺少文件或参数非法会报错，不会悄悄使用另一份文件。

录制时同样自动读取三份配置，例如：

```bash
bash deploy/record_pc2.sh --instruction "实验室取物" --scene lab209 -- --with-hands
```

配置快照及文件摘要会包含 `body.jsonc`。控制启动事件 `control_start` 另外记录实际路径和生效的双臂/身体参数，便于追溯。

本次固定配置入口仅涉及 Python 和启动脚本，不需要重新编译 C++；上文编译步骤属于此前的官方范围对齐升级。完整选项和本次同步命令见 [RUN_OPTIONS.md](RUN_OPTIONS.md)。
