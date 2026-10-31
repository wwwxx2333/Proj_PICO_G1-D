"""Quota-limited episode store and checksum verification (standard library)."""
from collections import Counter
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import time
import uuid

SCHEMA = 'g1d-lab-recording-v1'
RESERVE = 512 * 1024  # Reserved inside quota for final manifest and directory metadata.
EPISODE_RE = re.compile(r'episode_[0-9]{8}_[0-9]{6}_[a-f0-9]{8}\Z')


def encode(value):
    return (json.dumps(value, ensure_ascii=False, allow_nan=False,
                       separators=(',', ':')) + '\n').encode('utf-8')


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def regular_files(root):
    for parent, directories, files in os.walk(root, followlinks=False):
        for name in directories + files:
            path = Path(parent) / name
            if path.is_symlink():
                raise ValueError(f'Symlinks are not allowed in recording storage: {path}')
        for name in files:
            path = Path(parent) / name
            if not path.is_file():
                raise ValueError(f'Not a regular file: {path}')
            yield path


def storage_usage(root):
    return sum(max(p.stat().st_size, p.stat().st_blocks * 512) for p in regular_files(root))


class FileLock:
    def __init__(self, path, shared=False):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.file = Path(path).open('a+b')
        try:
            fcntl.flock(self.file, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError('Control/recording/transfer is already active; finish it first')

    def close(self):
        self.file.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class RecordingStopped(Exception):
    pass


class EpisodeStore:
    """Single writer under a spool lock. Quota covers ALL retained episodes.

    Content is unbuffered; allocation is charged in filesystem blocks. A reserved
    finalization allowance is kept inside the configured quota. Other programs
    filling the filesystem are handled by the free-space guard and write errors.
    """
    def __init__(self, root, max_bytes, min_free_bytes, metadata):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes
        self.min_free_bytes = min_free_bytes
        self.used = storage_usage(self.root)
        if self.used + 2 * RESERVE >= max_bytes:
            raise RecordingStopped('CACHE_LIMIT: transfer/clean existing episodes first')
        if shutil.disk_usage(self.root).free < min_free_bytes + RESERVE:
            raise RecordingStopped('LOW_DISK_SPACE')
        name = datetime.now().strftime('episode_%Y%m%d_%H%M%S_') + uuid.uuid4().hex[:8]
        self.path = self.root / name
        self.path.mkdir()
        self.block = max(4096, os.statvfs(self.root).f_frsize)
        self.used += RESERVE  # Directories, marker and final manifest allowance.
        self.handles = {}
        self.hashes = {}
        self.sizes = {}
        self.frames = Counter()
        self.streams = Counter()
        self.dropped = {}
        self.last_seq = {}
        self.sequence_baselines = {}
        self.drop_baselines = {}
        self.segmented = metadata.get('recording_mode') == 'manual_l3'
        self.sequence_gaps = Counter()
        self.reason = None
        self.stop_monotonic = None
        self.write_error = None
        self.last_space_check = 0.0
        self.closed = False
        self.bridge_exits = {}
        self.control_exit_code = None
        (self.path / 'IN_PROGRESS').write_text('Unfinalized. Do not train on or delete automatically.\n')
        for camera in ('head', 'left_wrist', 'right_wrist'):
            (self.path / 'images' / camera).mkdir(parents=True)
        description = dict(clock_anchor={'pc2_monotonic_s': time.monotonic(), 'unix_time_ns': time.time_ns()},
                           base_world_pose=None, base_world_pose_available=False)
        description.update(metadata)  # Recovery keeps the original clock anchor.
        description.update(schema=SCHEMA, episode_id=name,
                           created_utc=datetime.now(timezone.utc).isoformat())
        self.write('metadata.json', encode(description))

    def stop(self, reason):
        if self.reason is None:
            self.reason = reason
            self.stop_monotonic = time.monotonic()

    def _check(self, additional):
        if self.reason:
            raise RecordingStopped(self.reason)
        if self.used + additional > self.max_bytes:
            self.stop('CACHE_LIMIT')
            raise RecordingStopped(self.reason)
        now = time.monotonic()
        if now - self.last_space_check > 0.25:
            self.last_space_check = now
            if shutil.disk_usage(self.root).free < self.min_free_bytes + RESERVE + additional:
                self.stop('LOW_DISK_SPACE')
                raise RecordingStopped(self.reason)

    def _growth(self, name, n):
        old = self.sizes.get(name, 0)
        allocated = lambda x: math.ceil(x / self.block) * self.block
        return allocated(old + n) - allocated(old)

    def write(self, name, data):
        growth = self._growth(name, len(data))
        self._check(growth)
        try:
            if name not in self.handles:
                self.handles[name] = (self.path / name).open('xb', buffering=0)
                self.hashes[name] = hashlib.sha256()
                self.sizes[name] = 0
            view = memoryview(data)
            while view:
                n = self.handles[name].write(view)
                if not n:
                    raise OSError('short recording write')
                view = view[n:]
            self.hashes[name].update(data)
            self.sizes[name] += len(data)
            self.used += growth
        except OSError as exc:
            self.write_error = str(exc)
            self.stop('WRITE_ERROR')
            raise RecordingStopped(self.reason) from exc

    def event(self, event):
        stream = event.get('stream', '')
        if not re.fullmatch(r'[a-z_]+', stream):
            raise ValueError('invalid telemetry stream')
        producer = str(event.get('producer', 'unknown'))
        seq = int(event.get('seq', 0))
        # A control process lives across episodes; its counters also include
        # intentional non-recording time. Use first observed values as baselines.
        total_drops = int(event.get('dropped_total', 0))
        if self.segmented and producer not in self.last_seq:
            self.sequence_baselines[producer] = seq - 1
            self.last_seq[producer] = seq - 1
            self.drop_baselines[producer] = total_drops
        previous = self.last_seq.get(producer, 0)
        if seq > previous:
            self.sequence_gaps[producer] += max(0, seq - previous - 1)
            self.last_seq[producer] = seq
        self.dropped[producer] = max(self.dropped.get(producer, 0), max(0, total_drops - self.drop_baselines.get(producer, 0)))
        self.write('telemetry.jsonl', encode(event))
        self.streams[stream] += 1
        if stream == 'bridge_exit':
            self.bridge_exits[event['data']['bridge']] = event['data']
        if stream == 'session_end':
            self.control_exit_code = event['data'].get('control_exit_code')
        if stream == 'console':
            self.write('console.log', event['data']['text'].encode('utf-8', errors='replace'))

    def frame(self, camera, jpg, received_at, received_seq):
        if camera not in ('head', 'left_wrist', 'right_wrist'):
            raise ValueError('invalid camera name')
        index = self.frames[camera]
        name = f'images/{camera}/{index:08d}.jpg'
        record = dict(camera=camera, frame_index=index, path=name, bytes=len(jpg),
                      sha256=hashlib.sha256(jpg).hexdigest(), received_seq=received_seq,
                      received_monotonic_s=received_at, timestamp_kind='pc2_zmq_receive',
                      exposure_timestamp=None, source_frame_id=None)
        row = encode(record)
        # Reserve BOTH image and its index before writing either.
        growth = math.ceil(len(jpg) / self.block) * self.block
        self._check(growth + self._growth('frames.jsonl', len(row)))
        try:
            with (self.path / name).open('xb', buffering=0) as image:
                view = memoryview(jpg)
                while view:
                    n = image.write(view)
                    if not n:
                        raise OSError('short JPEG write')
                    view = view[n:]
            self.used += growth
            self.write('frames.jsonl', row)
            self.frames[camera] += 1
        except (OSError, RecordingStopped) as exc:
            self.write_error = str(exc)
            self.stop('WRITE_ERROR')
            raise RecordingStopped(self.reason) from exc

    def finish(self, reason='CONTROL_ENDED', extra=None, progress=None):
        if self.closed:
            return
        self.stop(reason)
        report = progress or (lambda **kwargs: None)
        try:
            for index, (name, handle) in enumerate(self.handles.items()):
                report(stage='sync_metadata', completed=index, total=len(self.handles), current=name)
                os.fsync(handle.fileno())
                handle.close()
            # Final durability work happens after control has ended. JPEG files
            # have already been closed, but may still have dirty page-cache data.
            sync_tree(self.path, progress=progress)
            report(stage='publish_manifest', completed=0, total=1, current='manifest.json')
            manifest = dict(schema=SCHEMA, episode_id=self.path.name,
                finalized=True, recording_stop_reason=self.reason,
                recording_stopped_monotonic_s=self.stop_monotonic,
                finalized_utc=datetime.now(timezone.utc).isoformat(),
                task_success=None, write_error=self.write_error,
                control_exit_code=self.control_exit_code, bridge_exits=self.bridge_exits,
                frame_counts=dict(self.frames), stream_counts=dict(self.streams),
                producer_reported_drops=self.dropped, sequence_gaps=dict(self.sequence_gaps),
                sequence_baselines=self.sequence_baselines, producer_drop_baselines=self.drop_baselines,
                timestamp_quality='Software receive times; NOT hardware-synchronized exposure times',
                files={name: {'bytes': self.sizes[name], 'sha256': digest.hexdigest()}
                       for name, digest in self.hashes.items()}, **(extra or {}))
            content = encode(manifest)
            if len(content) >= RESERVE // 2:
                raise RuntimeError('manifest exceeds reserved size')
            temp = self.path / 'manifest.json.tmp'
            with temp.open('xb') as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            temp.replace(self.path / 'manifest.json')
            (self.path / 'IN_PROGRESS').unlink()
            fd = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            self.closed = True
            report(stage='publish_manifest', completed=1, total=1, current='manifest.json')
        finally:
            for handle in self.handles.values():
                handle.close()


def safe_file(root, name):
    path = Path(name)
    if path.is_absolute() or not path.parts or any(p in ('.', '..') for p in path.parts):
        raise ValueError(f'Unsafe recording path: {name}')
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root.resolve()) or (root / path).is_symlink():
        raise ValueError(f'Unsafe recording path: {name}')
    return root / path


def verify_episode(path):
    path = Path(path)
    if not EPISODE_RE.fullmatch(path.name) or (path / 'IN_PROGRESS').exists():
        raise ValueError(f'Not a finalized episode: {path}')
    manifest = json.loads((path / 'manifest.json').read_text())
    if (manifest.get('schema') != SCHEMA or manifest.get('episode_id') != path.name
            or manifest.get('finalized') is not True or manifest.get('write_error')):
        raise ValueError('Invalid or write-damaged manifest')
    expected = {'manifest.json'}
    for name, info in manifest['files'].items():
        file = safe_file(path, name)
        if file.stat().st_size != info['bytes'] or sha256(file) != info['sha256']:
            raise ValueError(f'Checksum mismatch: {name}')
        expected.add(name)
    counts = Counter()
    frames = path / 'frames.jsonl'
    if frames.exists():
        if 'frames.jsonl' not in manifest['files']:
            raise ValueError('Unchecked frame index')
        with frames.open() as source:
            for line in source:
                frame = json.loads(line)
                name = frame['path']
                camera = frame['camera']
                if name != f'images/{camera}/{counts[camera]:08d}.jpg' or name in expected:
                    raise ValueError('Invalid or duplicate image index')
                image = safe_file(path, name)
                if image.stat().st_size != frame['bytes'] or sha256(image) != frame['sha256']:
                    raise ValueError(f'Image checksum mismatch: {name}')
                expected.add(name)
                counts[camera] += 1
    if dict(counts) != manifest['frame_counts']:
        raise ValueError('Frame counts disagree with index')
    actual = {str(p.relative_to(path)) for p in regular_files(path)}
    if actual != expected:
        raise ValueError(f'Unexpected/missing files: {actual ^ expected}')
    return manifest


def sync_tree(root, progress=None):
    """Flush regular files and directories before acknowledging durable storage."""
    report = progress or (lambda **kwargs: None)
    files = []
    report(stage='enumerate_files', completed=0, total=None, current='')
    for path in regular_files(root):
        files.append(path)
        report(stage='enumerate_files', completed=len(files), total=None, current=str(path.relative_to(root)))
    for index, path in enumerate(files):
        report(stage='sync_files', completed=index, total=len(files), current=str(path.relative_to(root)))
        with path.open('rb') as source:
            os.fsync(source.fileno())
    report(stage='sync_files', completed=len(files), total=len(files), current='')
    directories = [Path(root)] + [p for p in Path(root).rglob('*') if p.is_dir()]
    for index, directory in enumerate(reversed(directories)):
        report(stage='sync_directories', completed=index, total=len(directories), current=str(directory))
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    report(stage='sync_directories', completed=len(directories), total=len(directories), current='')
