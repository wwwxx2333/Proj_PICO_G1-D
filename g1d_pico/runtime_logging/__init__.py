"""Automatic full-file logs with independently filtered terminal output."""
import os


def active():
    return bool(os.environ.get('G1D_RUNTIME_LOG_ACTIVE'))


def full_log_requested(command):
    # Values such as --instruction '--full-log' are data, not logger switches.
    takes_value = {'--instruction', '--scene', '--objects', '--robot-ip', '--cert', '--key',
                   '--bridge-bin', '--arm-bridge-bin', '--hand-bridge-bin', '--network-interface',
                   '--arm-urdf'}
    skip = False
    for argument in command:
        if skip:
            skip = False
        elif argument in takes_value:
            skip = True
        elif argument == '--full-log':
            return True
    return False
