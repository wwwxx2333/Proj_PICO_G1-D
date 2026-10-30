#!/usr/bin/env bash
# PC2 camera+base+arms+waist+column lift. Dex3-1 hands remain optional.
set -euo pipefail
project_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
with_hands=false
with_lift=true
# User-confirmed column travel. The C++ guard applies a further 10 mm
# directional stop margin, allowing inward movement from either end zone.
height_min=0.0
height_max=0.42
height_options=false
extra=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --with-hands) with_hands=true; shift ;;
        --skip-home) export PICO_G1D_SKIP_ARM_HOME=1; shift ;;
        --with-lift) with_lift=true; shift ;;
        --without-lift) with_lift=false; shift ;;
        --height-min-m|--height-max-m|--max-lift)
            if [[ $# -lt 2 || $2 == --* ]]; then echo "Missing value for $1" >&2; exit 2; fi
            height_options=true
            case "$1" in
                --height-min-m) height_min=$2 ;;
                --height-max-m) height_max=$2 ;;
                --max-lift) extra+=("$1" "$2") ;;
            esac
            shift 2 ;;
        --help)
            echo "Usage: bash deploy/run_pc2_arms.sh [--with-hands] [--skip-home]"
            echo "       [--without-lift | --with-lift] [--max-lift SPEED]"
            echo "       [--height-min-m MIN --height-max-m MAX]"
            echo "Right stick horizontal: waist yaw. Vertical: lift (default) or torso pitch."
            echo "Left controller X toggles LIFT/PITCH; centre the right stick after switching."
            echo "Default feedback bounds: 0.0..0.42 m; directional stops: 0.01..0.41 m."
            echo "MIN/MAX use rt/hispeed_state.y, not the overall robot height."
            exit 0 ;;
        *) echo "Unknown option: $1; use --help" >&2; exit 2 ;;
    esac
done
if $with_lift; then
    extra+=(--with-lift --height-min-m "$height_min" --height-max-m "$height_max")
elif $height_options; then
    echo "Height/speed options cannot be combined with --without-lift." >&2
    exit 2
fi
mode=--with-arms
if $with_hands; then mode=--with-arms-and-hands; fi
exec bash "$project_root/deploy/run_pc2_base.sh" "$mode" "${extra[@]}"
