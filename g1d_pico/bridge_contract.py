"""Check compiled limits offline so an old binary cannot silently cap tuning."""
import json
import subprocess

from .limits import BRIDGE_LIMITS


def require_current_limits(binary, environment):
    try:
        result = subprocess.run(
            [str(binary), '--print-limits', '--offline', '1'], env=environment,
            capture_output=True, text=True, timeout=5, check=True,
        )
        if json.loads(result.stdout) != BRIDGE_LIMITS:
            raise ValueError('compiled limit contract differs from Python')
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise RuntimeError(
            f'Bridge limit check failed for {binary}. Sync bridge sources and rebuild '
            'with: bash deploy/build_on_pc2.sh deps/unitree_sdk2. '
            'No control process was started by this check.'
        ) from exc
