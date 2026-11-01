#!/usr/bin/env bash
# Host only: make review videos from already fetched episodes; no SSH or robot control.
set -euo pipefail
project_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$project_root"
exec "${PICO_G1D_HOST_PYTHON:-python3}" -u -m g1d_pico.recording video "$@"
