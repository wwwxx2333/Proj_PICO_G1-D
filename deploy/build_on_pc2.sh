#!/usr/bin/env bash
# Build the G1-D SDK2 bridge on the robot's ARM64 development PC (PC2).
# This script only compiles files; it never connects to the motion controller.
set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "Usage: bash deploy/build_on_pc2.sh /path/to/unitree_sdk2_source" >&2
    exit 2
fi

sdk_source=$(realpath "$1")
project_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
arch=$(uname -m)
if [[ "$arch" != "aarch64" ]]; then
    echo "PC2 ARM64 build expected; found $arch" >&2
    exit 2
fi
if [[ ! -f "$sdk_source/lib/aarch64/libunitree_sdk2.a" ||
      ! -f "$sdk_source/thirdparty/lib/aarch64/libddsc.so" ||
      ! -f "$sdk_source/thirdparty/lib/aarch64/libddscxx.so" ]]; then
    echo "SDK2 source is missing its aarch64 libraries: $sdk_source" >&2
    exit 2
fi
for command in cmake c++; do
    if ! command -v "$command" >/dev/null 2>&1; then
        echo "Missing build tool: $command" >&2
        exit 2
    fi
done

sdk_build="$project_root/.local/sdk2-build-arm64"
sdk_install="$project_root/.local/sdk2-arm64"
bridge_build="$project_root/bridge/build-arm64"

cmake -S "$sdk_source" -B "$sdk_build" \
    -DBUILD_EXAMPLES=OFF \
    -DCMAKE_INSTALL_LIBDIR=lib \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX="$sdk_install"
cmake --build "$sdk_build" -j2
cmake --install "$sdk_build"

cmake -S "$project_root/bridge" -B "$bridge_build" \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_PREFIX_PATH="$sdk_install"
cmake --build "$bridge_build" -j2

# Keep the matching ARM64 CycloneDDS libraries beside the executables.
mkdir -p "$bridge_build/lib"
cp "$sdk_source/thirdparty/lib/aarch64/libddsc.so" "$bridge_build/lib/"
cp "$sdk_source/thirdparty/lib/aarch64/libddscxx.so" "$bridge_build/lib/"
ln -sfn libddsc.so "$bridge_build/lib/libddsc.so.0"
ln -sfn libddscxx.so "$bridge_build/lib/libddscxx.so.0"
cp "$project_root/bridge/CYCLONEDDS_LICENSE" "$project_root/bridge/CYCLONEDDS_CXX_LICENSE" "$bridge_build/lib/"

echo "ARM64 bridge: $bridge_build/g1d_agv_bridge"
echo "Read-only height probe: $bridge_build/g1d_height_probe"
echo "G1-D arm bridge: $bridge_build/g1d_arm_bridge"
echo "Dex3-1 hand bridge: $bridge_build/g1d_hand_bridge"
echo "Read-only Dex3 state probe: $bridge_build/g1d_hand_state_probe"
