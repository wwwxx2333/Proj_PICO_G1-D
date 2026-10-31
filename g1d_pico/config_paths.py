"""One project-local configuration location, independent of the working directory.

Runtime tuning is read from these files at startup. CLI options select features
and connection endpoints; they do not select or override tuning files.
"""
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BODY_CONFIG = PROJECT_ROOT / 'config/body.jsonc'
ARM_CONFIG = PROJECT_ROOT / 'config/arms.jsonc'
RECORDING_CONFIG = PROJECT_ROOT / 'config/recording.jsonc'
