#!/usr/bin/env bash
# Run manually on PC2. Prepare dependencies and certificates; never start control.
set -euo pipefail

usage() {
    echo "Usage: bash deploy/prepare_pc2_direct.sh PC2_WIFI_IP"
    echo "Example: bash deploy/prepare_pc2_direct.sh 192.168.1.118"
    echo "Sources must be under the project: deps/unitree_sdk2 and deps/televuer."
}
if [[ ${1:-} == --help ]]; then usage; exit 0; fi
if [[ $# -ne 1 ]]; then usage >&2; exit 2; fi
if [[ $(uname -m) != aarch64 ]]; then
    echo "Run this preparation script on the robot's ARM64 PC2." >&2
    exit 2
fi

project_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
sdk_source="$project_root/deps/unitree_sdk2"
televuer_source="$project_root/deps/televuer"
wifi_ip=$1
conda_bin=${CONDA_EXE:-"$HOME/miniconda3/bin/conda"}

for command in cmake c++ openssl; do
    if ! command -v "$command" >/dev/null 2>&1; then
        echo "Missing $command. Install the build tools before preparing PC2." >&2
        exit 2
    fi
done
if [[ ! -x "$conda_bin" ]]; then
    echo "Conda not found. Activate the existing tv environment and retry." >&2
    exit 2
fi
if [[ ! -f "$sdk_source/lib/aarch64/libunitree_sdk2.a" ||
      ! -f "$televuer_source/src/televuer/televuer.py" ]]; then
    echo "Missing dependency source. Copy SDK2 to $sdk_source and TeleVuer to $televuer_source." >&2
    exit 2
fi

# Clone the existing camera environment so pip cannot change its installed packages.
# Existing editable TeleImager source paths may still be shared: do not edit them.
conda_root=$("$conda_bin" info --base)
runtime_env="$conda_root/envs/pico_g1d"
camera_python="$conda_root/envs/tv/bin/python"
"$camera_python" - "$wifi_ip" <<'PY'
import ipaddress
import sys
from teleimager.image_client import ImageClient
ipaddress.IPv4Address(sys.argv[1])
PY
if [[ ! -d "$runtime_env" ]]; then
    "$conda_bin" create --yes --prefix "$runtime_env" --clone "$conda_root/envs/tv"
fi
runtime_python="$runtime_env/bin/python"
if [[ ! -x "$runtime_python" ]]; then
    echo "Environment exists but has no Python: $runtime_env" >&2
    exit 2
fi
"$runtime_python" -m pip install -e "$televuer_source"
"$runtime_python" - <<'PY'
import inspect
import sys
from teleimager.image_client import ImageClient
from televuer.televuer import TeleVuer
if sys.version_info < (3, 10):
    raise SystemExit("Python 3.10 or later is required.")
if not {"host", "request_bgr"} <= set(inspect.signature(ImageClient).parameters):
    raise SystemExit("Installed TeleImager client API is incompatible with this project.")
if not {"cert_file", "key_file"} <= set(inspect.signature(TeleVuer).parameters):
    raise SystemExit("Installed TeleVuer API is incompatible with this project.")
print("TeleVuer and TeleImager imports and constructor interfaces are available.")
PY

bash "$project_root/deploy/build_on_pc2.sh" "$sdk_source"

# Use project-specific certificates; keep the existing camera service certificates.
cert_dir="$project_root/.local/https"
mkdir -p "$cert_dir"
chmod 700 "$cert_dir"
if [[ -e "$cert_dir/cert.pem" || -e "$cert_dir/key.pem" ]]; then
    if [[ ! -r "$cert_dir/cert.pem" || ! -r "$cert_dir/key.pem" ]]; then
        echo "Incomplete certificate pair in $cert_dir; restore or move it before retrying." >&2
        exit 2
    fi
    # x509 -checkip may return success even on mismatch; verify has a failing exit code.
    openssl verify -CAfile "$cert_dir/cert.pem" -verify_ip "$wifi_ip" "$cert_dir/cert.pem"
else
    # Generate in a temporary directory so a failed command cannot leave half a pair.
    temporary_cert_dir=$(mktemp -d "$cert_dir/new.XXXXXX")
    (
        umask 077
        openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
            -keyout "$temporary_cert_dir/key.pem" \
            -out "$temporary_cert_dir/cert.pem" \
            -subj "/CN=g1d-pc2" \
            -addext "subjectAltName=IP:$wifi_ip,IP:127.0.0.1,DNS:localhost"
    )
    mv "$temporary_cert_dir/key.pem" "$cert_dir/key.pem"
    mv "$temporary_cert_dir/cert.pem" "$cert_dir/cert.pem"
    rmdir "$temporary_cert_dir"
fi

echo "Preparation finished. No robot control process has been started."
echo "Start camera + base: bash $project_root/deploy/run_pc2_base.sh"
echo "PICO page: https://$wifi_ip:8012/?ws=wss://$wifi_ip:8012"
