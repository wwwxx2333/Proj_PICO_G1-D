"""An isolated project with its own fixed configs; never edits operator settings."""
from pathlib import Path
import shutil


def copy_runtime_project(destination):
    source = Path(__file__).resolve().parents[1]
    for name in ('g1d_pico', 'config'):
        shutil.copytree(source / name, destination / name,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    return destination
