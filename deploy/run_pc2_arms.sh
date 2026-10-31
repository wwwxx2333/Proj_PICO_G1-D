#!/usr/bin/env bash
# PC2 camera+base+arms+waist+column lift. Dex3-1 hands remain optional.
set -euo pipefail
project_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
with_hands=false
with_lift=true
# All tuning values come from the project config files.
extra=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --with-hands) with_hands=true; shift ;;
        --skip-home) export PICO_G1D_SKIP_ARM_HOME=1; shift ;;
        --with-lift) with_lift=true; shift ;;
        --without-lift) with_lift=false; shift ;;
        --config|--body-config|--arm-config|--max-forward-mps|--max-yaw-rps|--max-lift|--deadzone|--height-min-m|--height-max-m)
            echo "Removed option: $1. Edit project config/body.jsonc, config/arms.jsonc or config/recording.jsonc and restart." >&2
            exit 2 ;;
        --help)
            echo "Usage: bash deploy/run_pc2_arms.sh [--with-hands] [--skip-home]"
            echo "       [--without-lift | --with-lift]"
            echo "Settings: project config/body.jsonc and config/arms.jsonc (no CLI overrides)."
            echo "Right stick horizontal: waist yaw. Vertical: lift (default) or torso pitch."
            echo "Left controller X toggles LIFT/PITCH; centre the right stick after switching."
            echo "Default settings: config/body.jsonc (including feedback bounds and speeds)."
            echo "MIN/MAX use rt/hispeed_state.y, not the overall robot height."
            exit 0 ;;
        *) echo "Unknown option: $1; use --help" >&2; exit 2 ;;
    esac
done
if $with_lift; then
    extra+=(--with-lift)
fi
mode=--with-arms
if $with_hands; then mode=--with-arms-and-hands; fi
exec bash "$project_root/deploy/run_pc2_base.sh" "$mode" "${extra[@]}"
