"""Fixed project-local logging settings; no robot dependencies."""
from dataclasses import dataclass
import math
from pathlib import Path

from ..config_io import load_config
from ..config_paths import LOGGING_CONFIG, PROJECT_ROOT


@dataclass(frozen=True)
class Settings:
    directory: Path
    max_files: int = 100
    flush_interval_s: float = 1.0
    wait_repeat_s: float = 10.0
    queue_capacity: int = 4096

    @classmethod
    def load(cls, path=LOGGING_CONFIG):
        values = load_config(path)
        expected = {'directory', 'max_files', 'flush_interval_s', 'wait_repeat_s', 'queue_capacity'}
        if not isinstance(values, dict) or set(values) != expected:
            raise ValueError(f'logging.jsonc requires exactly {sorted(expected)}')
        for key, lower, upper, integer in (
                ('max_files', 1, 100, True), ('flush_interval_s', .1, 10, False),
                ('wait_repeat_s', 1, 300, False), ('queue_capacity', 128, 16384, True)):
            value = values[key]
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or not lower <= value <= upper
                    or (integer and not isinstance(value, int))):
                raise ValueError(f'Invalid logging {key}: expected {lower}..{upper}')
        directory = values['directory']
        if not isinstance(directory, str) or not directory.strip():
            raise ValueError('Logging directory must be a non-empty string')
        directory = Path(directory).expanduser()
        values['directory'] = ((PROJECT_ROOT / directory) if not directory.is_absolute() else directory).resolve()
        return cls(**values)
