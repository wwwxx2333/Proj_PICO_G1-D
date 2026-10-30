#!/usr/bin/env bash
# Run on PC2. All recorder options precede --; control options follow it.
set -euo pipefail
project_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
runtime_python=${PICO_G1D_PYTHON:-"$HOME/miniconda3/envs/pico_g1d/bin/python"}
if [[ ${1:-} == --help ]]; then
    echo 'Usage: bash deploy/record_pc2.sh [--instruction TEXT] [--scene LAB] [--objects TEXT]'
    echo '       [--config config/recording.json] -- [--with-hands] [other run_pc2_arms.sh options]'
    echo 'Default control: camera + base + arms + torso + column. Add --with-hands for Dex3.'
    echo 'One launch = one recording session (including startup homing and disarmed time).'
    exit 0
fi
if [[ $(uname -m) != aarch64 ]]; then
    echo 'Run record_pc2.sh on the robot PC2.' >&2
    exit 2
fi
record_options=()
while [[ $# -gt 0 && $1 != -- ]]; do
    case "$1" in
        --instruction|--scene|--objects|--config)
            if [[ $# -lt 2 ]]; then echo "Missing value: $1" >&2; exit 2; fi
            record_options+=("$1" "$2"); shift 2 ;;
        *) echo 'Recorder options precede --; robot control options follow it. Use --help.' >&2; exit 2 ;;
    esac
done
if [[ ${1:-} == -- ]]; then shift; fi
cd "$project_root"
exec "$runtime_python" -u -m g1d_pico.recording run "${record_options[@]}" -- \
    bash deploy/run_pc2_arms.sh "$@"
