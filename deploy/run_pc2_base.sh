#!/usr/bin/env bash
# Run manually on PC2: PICO -> TeleVuer -> local SDK2 -> internal eth0.
# Default remains base-only; arm mode adds waist yaw; --with-lift adds the column.
set -euo pipefail

usage() {
    echo "Usage: bash deploy/run_pc2_base.sh [--camera-only | --with-arms | --with-arms-and-hands]"
    echo "       [--with-lift --height-min-m MIN --height-max-m MAX [--max-lift SPEED]]"
    echo "Default: live base control and camera; right A arms, right B disarms."
    echo "Height limits must be verified rt/hispeed_state.y coordinates, not overall robot height."
    echo "Optional environment: PICO_G1D_PYTHON=/absolute/path/to/pico_g1d/bin/python"
}
camera_only=false
with_arms=false
with_hands=false
with_lift=false
mode_selected=false
height_min=""
height_max=""
max_lift=""
while [[ $# -gt 0 ]]; do
    case "$1" in
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
        --height-min-m|--height-max-m|--max-lift)
            if [[ $# -lt 2 || $2 == --* ]]; then echo "Missing value for $1" >&2; exit 2; fi
            case "$1" in
                --height-min-m) height_min=$2 ;;
                --height-max-m) height_max=$2 ;;
                --max-lift) max_lift=$2 ;;
            esac
            shift 2 ;;
        *) usage >&2; exit 2 ;;
    esac
done
if $with_lift; then
    if $camera_only || [[ -z "$height_min" || -z "$height_max" ]]; then
        echo "--with-lift needs live control and both --height-min-m/--height-max-m (rt/hispeed_state.y)." >&2
        exit 2
    fi
elif [[ -n "$height_min" || -n "$height_max" || -n "$max_lift" ]]; then
    echo "Height/speed options require --with-lift." >&2
    exit 2
fi
if [[ $(uname -m) != aarch64 ]]; then
    echo "Run this launcher on the robot's ARM64 PC2, not the desktop." >&2
    exit 2
fi

project_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
runtime_python=${PICO_G1D_PYTHON:-"$HOME/miniconda3/envs/pico_g1d/bin/python"}
bridge_bin="$project_root/bridge/build-arm64/g1d_agv_bridge"
cert="$project_root/.local/https/cert.pem"
key="$project_root/.local/https/key.pem"

if [[ ! -x "$runtime_python" || ! -r "$cert" || ! -r "$key" ]]; then
    echo "Missing runtime or certificates. Run deploy/prepare_pc2_direct.sh first." >&2
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
        echo "Run deploy/prepare_pc2_arms.sh before enabling arms." >&2
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
    if $with_lift; then
        arguments+=(--height-min-m "$height_min" --height-max-m "$height_max")
        if [[ -n "$max_lift" ]]; then arguments+=(--max-lift "$max_lift"); fi
    else
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
