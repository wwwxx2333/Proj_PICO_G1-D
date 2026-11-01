"""Recording supervisor: local subprocesses only; no SSH, rsync or upload."""
import json
import codecs
import multiprocessing as mp
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from ..config_io import load_config
from ..config_paths import PROJECT_ROOT, ARM_CONFIG, BODY_CONFIG
from .dataset import snapshot_artifacts
from .button import RecordingRequests
from .session import EpisodeSession
from .storage import FileLock, sha256
from .telemetry import Telemetry
from ..runtime_logging.runner import ConsoleForwarder

PROJECT = PROJECT_ROOT
CONTROL_LOCK = PROJECT / '.local/control_session.lock'


def load_settings(path):
    settings = load_config(path)
    if not isinstance(settings, dict):
        raise ValueError('Recording config must be a JSON object')
    # Ignore only documentation; misspelled or missing settings remain errors.
    settings.pop('_comments', None)
    expected = {'spool', 'max_cache_gb', 'min_free_gb', 'image_fps', 'cameras', 'camera_config_port'}
    if set(settings) != expected:
        raise ValueError(f'Recording config requires exactly {sorted(expected)}')
    for key, lower, upper in (('max_cache_gb', 0.01, 10000), ('min_free_gb', 0, 10000),
                              ('image_fps', 1, 60), ('camera_config_port', 1, 65535)):
        value = settings[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not lower <= value <= upper:
            raise ValueError(f'Invalid {key}')
    if settings['camera_config_port'] != int(settings['camera_config_port']):
        raise ValueError('camera_config_port must be an integer')
    cameras = settings['cameras']
    if (not isinstance(cameras, list) or not all(isinstance(c, str) for c in cameras)
            or len(set(cameras)) != len(cameras)
            or any(c not in ('head', 'left_wrist', 'right_wrist') for c in cameras)):
        raise ValueError('Invalid cameras list')
    if not isinstance(settings['spool'], str) or not settings['spool'].strip():
        raise ValueError('spool must be a non-empty path string')
    spool = Path(settings['spool']).expanduser()
    settings['spool'] = str((PROJECT / spool).resolve() if not spool.is_absolute() else spool.resolve())
    return settings


def code_snapshot():
    files = {}
    for folder, patterns in (('g1d_pico', ('*.py',)), ('bridge', ('*.cpp', '*.hpp')),
                             ('config', ('*.jsonc', '*.json')), ('deploy', ('*.sh',)),
                             ('assets/g1d', ('*.urdf',))):
        for pattern in patterns:
            for path in (PROJECT / folder).rglob(pattern):
                if 'build' not in str(path.relative_to(PROJECT)):
                    files[str(path.relative_to(PROJECT))] = sha256(path)
    arm_config = load_config(ARM_CONFIG)
    body_config = load_config(BODY_CONFIG)
    binaries = {str(p.relative_to(PROJECT)): sha256(p)
                for p in (PROJECT / 'bridge/build-arm64').glob('g1d_*bridge') if p.is_file()}
    return {'source_sha256': files, 'binary_sha256': binaries, 'arm_config': arm_config,
            'body_config': body_config,
            'calibration_status': 'camera intrinsics/extrinsics and gripper TCP not supplied'}


def drain_console(pipe, sink, display):
    """Always drain the control pipe even if disk, terminal or SSH is slow."""
    decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
    try:
        while True:
            chunk = os.read(pipe.fileno(), 4096)
            if not chunk:
                break
            text = decoder.decode(chunk)
            sink.emit('console', {'text': text})
            display.write(text)
    finally:
        tail = decoder.decode(b'', final=True)
        if tail:
            sink.emit('console', {'text': tail})
            display.write(tail)
        pipe.close()


def run(settings, command, instruction='', scene='lab', object_ids=''):
    if not command:
        raise ValueError('A control command after -- is required')
    # The shared activity lock also excludes host transfer when launched without recording.
    with FileLock(CONTROL_LOCK, shared=True), FileLock(Path(settings['spool']) / '.lock'):
        return _run_locked(settings, command, instruction, scene, object_ids)


def _run_locked(settings, command, instruction, scene, object_ids):
    context = mp.get_context('spawn')
    display = ConsoleForwarder()
    def notice(text):
        display.write(text + '\n')
    metadata = dict(session_id='session_' + uuid.uuid4().hex, instruction=instruction, scene=scene, object_ids=object_ids,
                    control_command=command, **code_snapshot(),
                    units={'joint_q': 'rad', 'height': 'm', 'linear_velocity': 'm/s', 'yaw_rate': 'rad/s'},
                    arm_joint_order=[f'{side}_{joint}' for side in ('left', 'right')
                                     for joint in ('shoulder_pitch', 'shoulder_roll', 'shoulder_yaw',
                                                   'elbow', 'wrist_roll', 'wrist_pitch', 'wrist_yaw')],
                    limitations=['No global base localization or measured planar velocity source',
                                 'Joint dq/tau_est use SDK feedback; tau_est is not a contact-force measurement',
                                 'PICO matrices are XR tracking-frame poses, not robot world poses',
                                 'Camera time is PC2 receive time; transport may skip camera frames',
                                 'Command submission / DDS Write is not proof of physical execution'])
    metadata = snapshot_artifacts(metadata, command)
    with tempfile.TemporaryDirectory(prefix='g1d-record-') as temporary:
        address = str(Path(temporary) / 'telemetry.sock')
        requests = RecordingRequests(Path(temporary) / 'control.sock')
        session = EpisodeSession(context, settings, metadata, address, notice)
        child = sink = reader = None
        previous = {}
        result = 1
        finalizing = abort_finalization = False
        control_interrupts = 0
        recording_finished = False
        try:
            session.prepare()
            environment = os.environ.copy()
            environment['G1D_RECORD_SOCKET'] = address
            environment['G1D_RECORD_CONTROL_SOCKET'] = str(requests.path)
            environment['PYTHONUNBUFFERED'] = '1'
            sink = Telemetry(address)
            child = subprocess.Popen(command, cwd=PROJECT, env=environment,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     start_new_session=True, bufsize=0)
            def forward(signum, _frame):
                nonlocal abort_finalization, control_interrupts
                if finalizing:
                    abort_finalization = True
                    return
                if child.poll() is None:
                    try:
                        control_interrupts += 1
                        if control_interrupts == 1:
                            child.send_signal(signal.SIGINT)
                        else:
                            notice('Repeated interrupt: escalating control shutdown to the process group.')
                            os.killpg(child.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous[signum] = signal.signal(signum, forward)
            reader = threading.Thread(target=drain_console, args=(child.stdout, sink, display), daemon=True)
            reader.start()
            while child.poll() is None:
                # Poll requests BEFORE reaping: a press arriving during saving is
                # rejected as busy, never turned into a delayed start afterward.
                requests.poll(session.request, session.set_control_ready, session.snapshot)
                session.poll()
                time.sleep(.05)
            result = child.wait()
            reader.join(timeout=3)
            sink.emit('session_end', {'control_exit_code': result})
        finally:
            if child is not None and child.poll() is None:
                child.send_signal(signal.SIGINT)
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGTERM)
                    try:
                        child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(child.pid, signal.SIGKILL)
                        child.wait()
            if reader is not None:
                reader.join(timeout=3)
            finalizing = True
            notice('Control ended. 正在等待当前录制收尾；此时再次 Ctrl+C 会中断保存并保留恢复数据。')
            recording_finished = session.finish_control(lambda: abort_finalization)
            if sink:
                sink.close()
            requests.close()
            for signum, handler in previous.items():
                signal.signal(signum, handler)
            notice('REC SESSION_END 录制会话结束；可在主机拉取已保存的任务。')
            time.sleep(.1)
        return result if result else (0 if recording_finished else 2)
