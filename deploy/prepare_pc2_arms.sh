#!/usr/bin/env bash
# Extend the existing PC2 project environment; never start robot control.
set -euo pipefail
if [[ $# -ne 0 ]]; then
    echo "Usage: bash deploy/prepare_pc2_arms.sh" >&2
    exit 2
fi
if [[ $(uname -m) != aarch64 ]]; then
    echo "Run this script on ARM64 PC2." >&2
    exit 2
fi
project_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
conda_bin=${CONDA_EXE:-"$HOME/miniconda3/bin/conda"}
conda_root=$("$conda_bin" info --base)
runtime_env="$conda_root/envs/pico_g1d"
if [[ ! -x "$runtime_env/bin/python" || ! -f "$project_root/assets/g1d/g1_d.urdf" ||
      ! -f "$project_root/deps/unitree_sdk2/lib/aarch64/libunitree_sdk2.a" ]]; then
    echo "First sync the project assets and complete the existing PC2 camera/base preparation." >&2
    exit 2
fi
# Only the project environment changes; the camera server's tv environment stays separate.
"$conda_bin" install --yes --prefix "$runtime_env" -c conda-forge 'pinocchio=3.1.0' 'casadi=3.6.7' 'numpy<2'
export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1
cd "$project_root"
"$runtime_env/bin/python" - <<'PY'
from pathlib import Path
from g1d_pico.arm_ik import DualArmIK
ik = DualArmIK(Path("assets/g1d/g1_d.urdf"))
print("G1-D official-method IPOPT warm-up passed; no robot connection was made.")
PY
bash deploy/build_on_pc2.sh "$project_root/deps/unitree_sdk2"
echo "Arms preparation complete. Start manually with: bash deploy/run_pc2_arms.sh"
