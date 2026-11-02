#!/usr/bin/env bash
# Run manually on PC2: PICO -> TeleVuer -> local SDK2 -> internal eth0.
# Default remains base-only; arm mode adds waist yaw; --with-lift adds the column.
set -euo pipefail
project_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
source "$project_root/deploy/runtime_logging.sh"
g1d_start_runtime_log "$project_root/deploy/run_pc2_base.sh" "$@"

usage() {
    echo "Usage: bash deploy/run_pc2_base.sh [--camera-only | --with-arms | --with-arms-and-hands]"
    echo "       [--with-lift]"
    echo "       [--full-log]  Show all logs; full runtime file is always saved."
    echo "Settings: project config/body.jsonc and config/arms.jsonc (no CLI overrides)."
    echo "Default: live base control and camera; right A arms, right B disarms."
    echo "Height limits must be verified rt/hispeed_state.y coordinates, not overall robot height."
    echo "Optional environment: PICO_G1D_PYTHON=/absolute/path/to/pico_g1d/bin/python"
}
camera_only=false
with_arms=false
with_hands=false
with_lift=false
mode_selected=false
while [[ $# -gt 0 ]]; do
    case "$1" in
        --full-log) shift ;; # Consumed by the outer logging supervisor.
        --help) usage; exit 0 ;;
        --camera-only|--with-arms|--with-arms-and-hands)
            if $mode_selected; then echo "Select one camera/arm mode only." >&2; exit 2; fi
            mode_selected=true
            case "$1" in
                --camera-only) camera_only=true ;;
                --with-arms) with_arms=true ;;
                --with-arms-and-hands) with_arms=true; with_hands=true ;;
            esac
            shift ;;
        --with-lift) with_lift=true; shift ;;
        --config|--body-config|--arm-config|--max-forward-mps|--max-yaw-rps|--max-lift|--deadzone|--height-min-m|--height-max-m)
            echo "Removed option: $1. Edit project config/body.jsonc, config/arms.jsonc or config/recording.jsonc and restart." >&2
            exit 2 ;;
        *) usage >&2; exit 2 ;;
    esac
done
if $with_lift; then
    if $camera_only; then
        echo "--with-lift needs live control; limits come from config/body.jsonc." >&2
        exit 2
    fi
fi
if [[ $(uname -m) != aarch64 ]]; then
    echo "Run this launcher on the robot's ARM64 PC2, not the desktop." >&2
    exit 2
fi

runtime_python=${PICO_G1D_PYTHON:-"$HOME/miniconda3/envs/pico_g1d/bin/python"}
bridge_bin="$project_root/bridge/build-arm64/g1d_agv_bridge"
cert="$project_root/.local/https/cert.pem"
key="$project_root/.local/https/key.pem"

if [[ ! -x "$runtime_python" || ! -r "$cert" || ! -r "$key" ]]; then
    echo "Missing runtime or certificates. Follow PROJECT_GUIDE.md sections 2.1.4 and 2.3." >&2
    exit 2
fi
if ! $camera_only; then
    if [[ ! -x "$bridge_bin" ]]; then
        echo "Missing ARM64 executable: $bridge_bin; run deploy/build_on_pc2.sh." >&2
        exit 2
    fi
    ip link show dev eth0 >/dev/null
fi

if $with_arms; then
    if [[ ! -x "$project_root/bridge/build-arm64/g1d_arm_bridge" ]]; then
        echo "Missing ARM64 arm bridge. Follow PROJECT_GUIDE.md section 2.3 and run deploy/build_on_pc2.sh deps/unitree_sdk2." >&2
        exit 2
    fi
    "$runtime_python" -c 'import casadi; from pinocchio import casadi as cpin'
fi

if $with_hands && [[ ! -x "$project_root/bridge/build-arm64/g1d_hand_bridge" ]]; then
    echo "Missing Dex3-1 ARM64 bridge; run deploy/build_on_pc2.sh first." >&2
    exit 2
fi

# Check TLS and port availability before any SDK2 client can be created.
"$runtime_python" - "$cert" "$key" <<'PY'
import socket
import ssl
import sys
from teleimager.image_client import ImageClient
from televuer.televuer import TeleVuer
context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.load_cert_chain(sys.argv[1], sys.argv[2])
with socket.socket() as port_check:
    # Match the web server's restart behavior: TIME_WAIT is not a live listener.
    port_check.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        port_check.bind(("0.0.0.0", 8012))
    except OSError as exc:
        raise SystemExit(f"Cannot bind port 8012. Inspect with: ss -ltnp 'sport = :8012'. Error: {exc}")
PY

wifi_ip=$(ip -4 -o addr show dev wlan0 | awk '{split($4, address, "/"); print address[1]; exit}')
if [[ -z "$wifi_ip" ]]; then
    echo "PC2 wlan0 has no IPv4 address. Connect it to the PICO Wi-Fi first." >&2
    exit 2
fi
openssl verify -CAfile "$cert" -verify_ip "$wifi_ip" "$cert"

echo "PICO page: https://$wifi_ip:8012/?ws=wss://$wifi_ip:8012"
echo "Camera: PC2 localhost. DDS: internal eth0. Keep this terminal open."
cd "$project_root"
arguments=(--robot-ip 127.0.0.1 --cert "$cert" --key "$key")
if ! $camera_only; then
    arguments+=(--live --network-interface eth0 --bridge-bin "$bridge_bin")
    if ! $with_lift; then
        arguments+=(--base-only)
    fi
fi
if $with_arms; then
    arguments+=(--with-arms --arm-bridge-bin "$project_root/bridge/build-arm64/g1d_arm_bridge")
    if [[ ${PICO_G1D_SKIP_ARM_HOME:-0} == 1 ]]; then arguments+=(--skip-arm-home); fi
    if $with_hands; then
        arguments+=(--with-hands --hand-bridge-bin "$project_root/bridge/build-arm64/g1d_hand_bridge")
    fi
    export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1
fi
# Replace the shell so Ctrl+C reaches the program's normal shutdown path.
exec "$runtime_python" -u -m g1d_pico "${arguments[@]}"
