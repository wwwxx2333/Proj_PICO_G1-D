#!/usr/bin/env bash
# Run on PC2. All recorder options precede --; control options follow it.
set -euo pipefail
project_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
runtime_python=${PICO_G1D_PYTHON:-"$HOME/miniconda3/envs/pico_g1d/bin/python"}
if [[ ${1:-} == --help ]]; then
    echo 'Usage: bash deploy/record_pc2.sh [--instruction TEXT] [--scene LAB] [--objects TEXT]'
    echo '       -- [--with-hands] [other run_pc2_arms.sh options]'
    echo 'Default control: camera + base + arms + torso + column. Add --with-hands for Dex3.'
    echo 'Settings: project config/body.jsonc, config/arms.jsonc and config/recording.jsonc.'
    echo 'Starts in REC WAITING. Centre the left stick and hold its click (L3) for 1 second to start/stop an episode.'
    echo 'Release L3 between presses. A/B control motion only; wait for REC SAVED before the next episode.'
    exit 0
fi
if [[ $(uname -m) != aarch64 ]]; then
    echo 'Run record_pc2.sh on the robot PC2.' >&2
    exit 2
fi
record_options=()
while [[ $# -gt 0 && $1 != -- ]]; do
    case "$1" in
        --instruction|--scene|--objects)
            if [[ $# -lt 2 ]]; then echo "Missing value: $1" >&2; exit 2; fi
            record_options+=("$1" "$2"); shift 2 ;;
        --config|--body-config|--arm-config|--max-forward-mps|--max-yaw-rps|--max-lift|--deadzone|--height-min-m|--height-max-m)
            echo "Removed option: $1. Edit project config/body.jsonc, config/arms.jsonc or config/recording.jsonc and restart." >&2
            exit 2 ;;
        *) echo 'Recorder options precede --; robot control options follow it. Use --help.' >&2; exit 2 ;;
    esac
done
if [[ ${1:-} == -- ]]; then shift; fi
cd "$project_root"
exec "$runtime_python" -u -m g1d_pico.recording run "${record_options[@]}" -- \
    bash deploy/run_pc2_arms.sh "$@"
