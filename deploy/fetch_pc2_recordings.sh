#!/usr/bin/env bash
# Run on the host AFTER stopping PC2 control. No robot motion is requested.
set -euo pipefail
project_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$project_root"
exec "${PICO_G1D_HOST_PYTHON:-python3}" -u -m g1d_pico.recording fetch "$@"
