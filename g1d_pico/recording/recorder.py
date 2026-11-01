"""Recorder worker and local-only camera subscriber; never commands a robot."""
from collections import Counter
import json
import math
import os
from pathlib import Path
import signal
import socket
import time

from .storage import EpisodeStore, RecordingStopped

CAMERAS = {'head': 'head_camera', 'left_wrist': 'left_wrist_camera',
           'right_wrist': 'right_wrist_camera'}


class FinalizationProgress:
    """Bound status traffic while reporting real work, not synthetic heartbeats."""
    def __init__(self, status):
        self.status = status
        self.started = time.monotonic()
        self.last_sent = 0.0
        self.stage = None

    def __call__(self, **progress):
        now = time.monotonic()
        complete = progress['total'] is not None and progress['completed'] == progress['total']
        if progress['stage'] != self.stage or complete or now-self.last_sent >= 1.0:
            self.status.send(dict(state='finalizing', elapsed_s=round(now-self.started, 1), **progress))
            self.stage = progress['stage']
            self.last_sent = now


def camera_config(context, port):
    import zmq
    request = context.socket(zmq.REQ)
    request.setsockopt(zmq.LINGER, 0)
    request.connect(f'tcp://127.0.0.1:{port}')
    try:
        request.send(b'GET_DATA')
        if not request.poll(3000):
            raise RuntimeError(f'Local TeleImager port {port} did not answer; recorder not started')
        return request.recv_json()
    finally:
        request.close()


def run_worker(settings, metadata, socket_path, status, stop, cutoff=None, reason_code=None, begin=None):
    """Executed in a spawned process. IPC and ZMQ queues are bounded."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    import zmq
    context = zmq.Context()
    receiver = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sockets = {}
    store = None
    stopped_notice = False
    try:
        try:
            os.nice(5)
        except OSError:
            pass
        config = camera_config(context, settings['camera_config_port']) if settings['cameras'] else {}
        for camera in settings['cameras']:
            camera_settings = config.get(CAMERAS[camera], {})
            if not camera_settings.get('enable_zmq'):
                raise RuntimeError(f'{camera} ZMQ is disabled; no recording started')
            port = int(camera_settings['zmq_port'])
            if not 1 <= port <= 65535:
                raise ValueError('invalid camera port')
            subscriber = context.socket(zmq.SUB)
            subscriber.setsockopt(zmq.LINGER, 0)
            subscriber.setsockopt(zmq.CONFLATE, 1)
            subscriber.setsockopt(zmq.RCVHWM, 1)
            subscriber.setsockopt(zmq.MAXMSGSIZE, 16 * 1024 * 1024)
            subscriber.setsockopt(zmq.SUBSCRIBE, b'')
            subscriber.connect(f'tcp://127.0.0.1:{port}')
            sockets[subscriber] = camera
        if begin is not None:
            # Spawn/import/configure subscriptions during startup, without creating
            # an episode or writing idle observations. Each camera queue is bounded.
            status.send({'state': 'prepared'})
            while not begin.wait(.05):
                if stop.is_set():
                    status.send({'state': 'idle_closed'})
                    return
            # Do not intentionally include frames buffered during preparation.
            for subscriber in sockets:
                try:
                    subscriber.recv(zmq.NOBLOCK)
                except zmq.Again:
                    pass
        started_at = time.monotonic()
        store = EpisodeStore(settings['spool'], int(settings['max_cache_gb'] * 1e9),
                             int(settings['min_free_gb'] * 1e9),
                             {**metadata, 'recording_started_monotonic_s': started_at,
                              'recording_settings': settings,
                              'camera_config': config})
        receiver.bind(socket_path)
        receiver.setblocking(False)
        receiver.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
        poller = zmq.Poller()
        poller.register(receiver.fileno(), zmq.POLLIN)
        for subscriber in sockets:
            poller.register(subscriber, zmq.POLLIN)
        status.send({'state': 'ready', 'episode': str(store.path)})
        next_frame = Counter()
        incoming = Counter()
        invalid = Counter()
        last_frame = {}
        last_status = 0.0
        stop_since = None
        malformed = 0
        while True:
            now = time.monotonic()
            if stop.is_set():
                if stop_since is None:
                    stop_since = now
                if now - stop_since > 0.3:
                    break
            ready = dict(poller.poll(25))
            # Bound each drain batch to leave time for all three cameras.
            if receiver.fileno() in ready:
                for _ in range(100):
                    try:
                        data = receiver.recv(65536)
                    except BlockingIOError:
                        break
                    if store.reason:
                        continue
                    try:
                        event = json.loads(data)
                        if cutoff is not None:
                            emitted = event.get('emitted_monotonic_s', 0.0)
                            if (not isinstance(emitted, (int, float)) or not math.isfinite(emitted)
                                    or emitted < started_at or (cutoff.value and emitted > cutoff.value)):
                                continue  # Do not import idle time or the previous episode's tail.
                        event['recorder_received_monotonic_s'] = time.monotonic()
                        store.event(event)
                    except RecordingStopped:
                        pass
                    except (ValueError, KeyError, TypeError):
                        malformed += 1
            for subscriber, camera in sockets.items():
                if subscriber not in ready:
                    continue
                jpg = subscriber.recv()
                received = time.monotonic()
                incoming[camera] += 1
                last_frame[camera] = received
                if store.reason or stop_since is not None or stop.is_set():
                    continue
                if len(jpg) < 4 or not jpg.startswith(b'\xff\xd8') or not jpg.endswith(b'\xff\xd9'):
                    invalid[camera] += 1
                    continue
                if received < next_frame[camera]:
                    continue
                period = 1.0 / settings['image_fps']
                next_frame[camera] = max(next_frame[camera] + period, received)
                try:
                    store.frame(camera, jpg, received, incoming[camera])
                except RecordingStopped:
                    pass
            if store.reason and not stopped_notice:
                status.send({'state': 'stopped', 'reason': store.reason,
                             'episode': str(store.path)})
                stopped_notice = True
                # No further image reception/decoding/network traffic for recording.
                for subscriber in list(sockets):
                    poller.unregister(subscriber)
                    subscriber.close()
                sockets.clear()
                if cutoff is not None:
                    break  # Segmented mode finalizes immediately on quota/write stop.
            if store.reason is None and stop_since is None and now - last_status >= 5.0:
                status.send({'state': 'progress', 'recording': store.reason is None,
                             'cache_gb': round(store.used / 1e9, 3),
                             'frames': dict(store.frames),
                             'camera_age_s': {c: None if c not in last_frame else round(now-last_frame[c], 2)
                                              for c in settings['cameras']}})
                last_status = now
        reason = 'L3_STOP' if reason_code is not None and reason_code.value == 1 else 'CONTROL_ENDED'
        store.finish(reason, progress=FinalizationProgress(status), extra={'recording_requested_stop_monotonic_s': cutoff.value if cutoff is not None else None,
                            'camera_messages_received': dict(incoming),
                            'invalid_jpegs': dict(invalid), 'malformed_telemetry': malformed,
                            'expected_cameras': settings['cameras'],
                            'camera_frames_missing': [c for c in settings['cameras'] if not store.frames[c]]})
        status.send({'state': 'finished', 'episode': str(store.path), 'reason': store.reason})
    except BaseException as exc:
        if store is not None and not store.closed:
            try:
                store.finish('RECORDER_ERROR', {'recorder_error': str(exc)},
                             progress=FinalizationProgress(status))
            except Exception:
                pass  # IN_PROGRESS remains if finalization itself failed.
        status.send({'state': 'error', 'error': str(exc)})
    finally:
        for subscriber in sockets:
            subscriber.close()
        context.term()
        receiver.close()
        Path(socket_path).unlink(missing_ok=True)
        status.close()
