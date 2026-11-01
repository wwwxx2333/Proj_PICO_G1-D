#!/usr/bin/env bash
# Sourced before startup checks. Nested launchers share the outer runtime log.
# A runtime unavailable here cannot run the logger; report that bootstrap error directly.
g1d_start_runtime_log() {
    local entry_script=$1
    shift
    if [[ -n ${G1D_RUNTIME_LOG_ACTIVE:-} || ${1:-} == --help ]]; then
        return
    fi
    local log_python=${PICO_G1D_PYTHON:-"$HOME/miniconda3/envs/pico_g1d/bin/python"}
    if [[ ! -x "$log_python" ]]; then
        echo "LOG STARTUP_ERROR Python runtime not executable: $log_python" >&2
        exit 2
    fi
    cd "$project_root"
    exec "$log_python" -u -m g1d_pico.runtime_logging -- bash "$entry_script" "$@"
}
