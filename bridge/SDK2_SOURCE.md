# SDK2 控制桥的来源与重编译

`g1d_agv_bridge` 和只读的 `g1d_height_probe` 在本机 Ubuntu x86_64 上编译。源代码来自 [宇树官方 unitree_sdk2](https://github.com/unitreerobotics/unitree_sdk2)，提交 `63096d0ac0c5d2dec9d6e0c22cd5233410ca2f36`。`bridge/lib/` 的 `libddsc.so`、`libddscxx.so` 来自同一次 SDK2 安装，运行时由 Python 启动器优先加载，以免与 ROS 环境中的同名库混用。

若换电脑、系统或 SDK2 版本，应在对应系统上重新编译。先按官方 README 将 SDK2 安装到个人可写目录，再执行：

```bash
cmake -S bridge -B bridge/build -DCMAKE_PREFIX_PATH=/SDK2/安装前缀 -DCMAKE_BUILD_TYPE=Release
cmake --build bridge/build -j2
```

将同一安装前缀下的 `libddsc.so`、`libddsc.so.0`、`libddscxx.so`、`libddscxx.so.0` 放入 `bridge/lib/`。本工程的控制桥只支持 G1-D；不能用旧版 SDK2 中 G1 双足 `LocoClient` 替代。
