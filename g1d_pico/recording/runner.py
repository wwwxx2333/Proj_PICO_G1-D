"""Recording supervisor: local subprocesses only; no SSH, rsync or upload."""
import json
import codecs
import multiprocessing as mp
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import time

from .recorder import run_worker
from .storage import FileLock, sha256
from .telemetry import Telemetry
from .shutdown import wait_for_recorder

PROJECT = Path(__file__).resolve().parents[2]
CONTROL_LOCK = PROJECT / '.local/control_session.lock'


def load_settings(path):
    settings = json.loads(Path(path).read_text())
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
    spool = Path(settings['spool']).expanduser()
    settings['spool'] = str((PROJECT / spool).resolve() if not spool.is_absolute() else spool.resolve())
    return settings


def code_snapshot():
    files = {}
    for folder, patterns in (('g1d_pico', ('*.py',)), ('bridge', ('*.cpp', '*.hpp')),
                             ('config', ('*.json',)), ('deploy', ('*.sh',)),
                             ('assets/g1d', ('*.urdf',))):
        for pattern in patterns:
            for path in (PROJECT / folder).rglob(pattern):
                if 'build' not in str(path.relative_to(PROJECT)):
                    files[str(path.relative_to(PROJECT))] = sha256(path)
    arm_config = json.loads((PROJECT / 'config/arms.json').read_text())
    binaries = {str(p.relative_to(PROJECT)): sha256(p)
                for p in (PROJECT / 'bridge/build-arm64').glob('g1d_*bridge') if p.is_file()}
    return {'source_sha256': files, 'binary_sha256': binaries, 'arm_config': arm_config,
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
            try:
                display.put_nowait(text)
            except queue.Full:
                pass  # Only terminal display is dropped; recording has its own path.
    finally:
        tail = decoder.decode(b'', final=True)
        if tail:
            sink.emit('console', {'text': tail})
        pipe.close()


def display_console(display):
    while True:
        text = display.get()
        if text is None:
            return
        try:
            sys.stdout.write(text)
            sys.stdout.flush()
        except (OSError, ValueError):
            return


def run(settings, command, instruction='', scene='lab', object_ids=''):
    if not command:
        raise ValueError('A control command after -- is required')
    # The shared activity lock also excludes host transfer when launched without recording.
    with FileLock(CONTROL_LOCK, shared=True), FileLock(Path(settings['spool']) / '.lock'):
        return _run_locked(settings, command, instruction, scene, object_ids)


def _run_locked(settings, command, instruction, scene, object_ids):
    context = mp.get_context('spawn')
    stop = context.Event()
    receive, send = context.Pipe(duplex=False)
    display = queue.Queue(maxsize=256)
    threading.Thread(target=display_console, args=(display,), daemon=True).start()
    def notice(text):
        try:
            display.put_nowait(text + '\n')
        except queue.Full:
            pass
    metadata = dict(instruction=instruction, scene=scene, object_ids=object_ids,
                    control_command=command, **code_snapshot(),
                    units={'joint_q': 'rad', 'height': 'm', 'linear_velocity': 'm/s', 'yaw_rate': 'rad/s'},
                    arm_joint_order=[f'{side}_{joint}' for side in ('left', 'right')
                                     for joint in ('shoulder_pitch', 'shoulder_roll', 'shoulder_yaw',
                                                   'elbow', 'wrist_roll', 'wrist_pitch', 'wrist_yaw')],
                    limitations=['No global base localization or measured planar velocity source',
                                 'Joint q and targets only; dq/torque/force feedback not exported',
                                 'PICO matrices are XR tracking-frame poses, not robot world poses',
                                 'Camera time is PC2 receive time; transport may skip camera frames',
                                 'Command submission / DDS Write is not proof of physical execution'])
    with tempfile.TemporaryDirectory(prefix='g1d-record-') as temporary:
        address = str(Path(temporary) / 'telemetry.sock')
        worker = context.Process(target=run_worker, args=(settings, metadata, address, send, stop),
                                 name='g1d-recorder')
        worker.start()
        send.close()
        child = None
        sink = None
        reader = None
        previous = {}
        result = 1
        first = None
        finalizing = False
        abort_finalization = False
        control_interrupts = 0
        recording_finished = False
        try:
            if not receive.poll(15):
                raise RuntimeError('Recorder startup timed out; control was not started')
            first = receive.recv()
            if first.get('state') != 'ready':
                raise RuntimeError(f'Recorder startup failed: {first}')
            notice(f"RECORDING: {first['episode']}\nLocal cache only; no upload during control.")
            environment = os.environ.copy()
            environment['G1D_RECORD_SOCKET'] = address
            environment['PYTHONUNBUFFERED'] = '1'
            sink = Telemetry(address)
            child = subprocess.Popen(command, cwd=PROJECT, env=environment,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     start_new_session=True, bufsize=0)
            # First interrupt follows the normal Ctrl+C path. A second is allowed
            # to escalate only at the operator's explicit request.
            def forward(signum, _frame):
                nonlocal abort_finalization, control_interrupts
                if finalizing:
                    abort_finalization = True
                    return
                if child.poll() is None:
                    try:
                        control_interrupts += 1
                        if control_interrupts == 1:
                            # Launch scripts exec Python. Let the parent stop its
                            # bridges in order before escalating to the group.
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
            worker_failed_reported = False
            while child.poll() is None:
                if receive.poll(0.1):
                    try:
                        message = receive.recv()
                    except EOFError:
                        message = None
                    if message:
                        if message['state'] in ('stopped', 'error'):
                            notice(f'RECORDING STOPPED: {message}. Robot control continues; no automatic restart.')
                        elif message['state'] == 'progress':
                            notice('RECORDER ' + json.dumps(message, ensure_ascii=False))
                if not worker.is_alive() and not worker_failed_reported:
                    notice('RECORDING STOPPED: worker exited. Robot control continues.')
                    worker_failed_reported = True
                    time.sleep(0.05)
            result = child.wait()
            reader.join(timeout=3)
            sink.emit('session_end', {'control_exit_code': result})
        finally:
            # Only this cleanup, after the controlled process exits, waits for disk flush.
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
            stop.set()
            notice('Control ended. Finalizing local recording; wait for state=finished. '
                   'Ctrl+C now cancels finalization and preserves recovery data.')
            recording_finished = wait_for_recorder(worker, receive, notice,
                                                   lambda: abort_finalization)
            if sink:
                sink.close()
            receive.close()
            for signum, handler in previous.items():
                signal.signal(signum, handler)
            if first and first.get('episode'):
                notice(f"Recording directory: {first['episode']}\nRun host fetch only after control has ended.")
            time.sleep(0.1)  # Allow final terminal notices; never in control loop.
        return result if result else (0 if recording_finished else 2)
