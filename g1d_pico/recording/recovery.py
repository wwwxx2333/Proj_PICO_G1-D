"""Offline audit and copy-out recovery. Never alter an original episode.

Only a malformed/incomplete final JSONL line may be omitted with explicit
--allow-truncated. Indexed image corruption or interior record damage is fatal.
Unindexed files remain in the original and are listed in the recovery report.
"""
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import shutil
import time

from .layout import SUPPORTED, Layout, ARTIFACTS
from .quality import event_quality, QualitySummary
from .storage import (SCHEMA, EPISODE_RE, EpisodeStore, FileLock, encode,
                      regular_files, safe_file, sha256, storage_usage, verify_episode)

CAMERAS = ('head', 'left_wrist', 'right_wrist')
MAX_LINE = 2 * 1024 * 1024


def json_object(raw):
    def invalid_constant(value):
        raise ValueError(f'Nonfinite JSON number: {value}')
    value = json.loads(raw, parse_constant=invalid_constant)
    if not isinstance(value, dict):
        raise ValueError('Expected a JSON object')
    return value


def rows(path, valid_bytes=None):
    """Stream complete lines; readers never allocate an entire trajectory."""
    if not path.exists():
        return
    with path.open('rb') as source:
        while valid_bytes is None or source.tell() < valid_bytes:
            raw = source.readline(MAX_LINE + 1)
            if not raw:
                return
            if len(raw) > MAX_LINE:
                raise ValueError(f'Oversized JSONL line in {path.name}')
            yield raw


def inspect_jsonl(path, visit):
    valid_bytes = count = 0
    digest = hashlib.sha256()
    if not path.exists():
        return dict(valid_bytes=0, total_bytes=0, rows=0, truncated=False,
                    prefix_sha256=digest.hexdigest())
    total = path.stat().st_size
    for raw in rows(path):
        try:
            if not raw.endswith(b'\n'):
                raise ValueError('Incomplete final line')
            record = json_object(raw)
        except (ValueError, UnicodeError):
            if valid_bytes + len(raw) != total:
                raise ValueError(f'Interior JSONL corruption in {path.name} at byte {valid_bytes}')
            break
        # Semantic corruption is not silently treated as a partial write.
        visit(record)
        digest.update(raw)
        valid_bytes += len(raw)
        count += 1
    return dict(valid_bytes=valid_bytes, total_bytes=total, rows=count,
                truncated=valid_bytes != total, prefix_sha256=digest.hexdigest())


def inspect_episode(path, notice=print):
    path = Path(path)
    if path.is_symlink() or not path.is_dir() or not EPISODE_RE.fullmatch(path.name):
        raise ValueError('Expected an original episode directory, not a symlink')
    path = path.resolve()
    files = {str(p.relative_to(path)): p for p in regular_files(path)}
    metadata_path = safe_file(path, 'metadata.json')
    if metadata_path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError('Oversized metadata')
    metadata = json_object(metadata_path.read_bytes())
    if metadata.get('schema') not in SUPPORTED or metadata.get('episode_id') != path.name:
        raise ValueError('Metadata schema/episode mismatch')
    layout = Layout(metadata['schema'])
    event_files = layout.telemetry_files(path)
    artifacts = [n for n in ARTIFACTS if n in files] if not layout.legacy else []
    expected = {'metadata.json', layout.frames, layout.console, *event_files, *artifacts,
                'IN_PROGRESS', 'manifest.json', 'manifest.json.tmp', 'quality/summary.json'}
    quality = QualitySummary()
    counts = Counter()
    image_bytes = 0
    last_notice = time.monotonic()
    last_time = {}

    def frame(record):
        nonlocal image_bytes, last_notice
        camera = record.get('camera')
        if camera not in CAMERAS:
            raise ValueError('Unknown camera in index')
        index = counts[camera]
        name = layout.image(camera, index)
        if record.get('frame_index') != index or record.get('path') != name:
            raise ValueError(f'Non-contiguous image index: {record.get("path")}')
        timestamp = record.get('received_monotonic_s')
        if (not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool)
                or not math.isfinite(timestamp) or timestamp < 0 or timestamp < last_time.get(camera, 0)):
            raise ValueError(f'Invalid image timestamp: {name}')
        received_seq = record.get('received_seq')
        if not isinstance(received_seq, int) or isinstance(received_seq, bool) or received_seq < 1:
            raise ValueError(f'Invalid received sequence: {name}')
        image = safe_file(path, name)
        if not 4 <= image.stat().st_size <= 16*1024*1024:
            raise ValueError(f'Invalid JPEG size: {name}')
        if image.stat().st_size != record.get('bytes') or sha256(image) != record.get('sha256'):
            raise ValueError(f'Indexed image missing or checksum mismatch: {name}')
        with image.open('rb') as source:
            start = source.read(2)
            source.seek(-2, 2)
            end = source.read(2)
        if start != b'\xff\xd8' or end != b'\xff\xd9':
            raise ValueError(f'JPEG boundary markers missing: {name}')
        counts[camera] += 1
        last_time[camera] = timestamp
        image_bytes += image.stat().st_size
        expected.add(name)
        if time.monotonic()-last_notice >= 2:
            notice(f'Recovery audit: verified {sum(counts.values())} images')
            last_notice = time.monotonic()

    streams = Counter()
    gaps = Counter()
    sequences = {}
    drops = {}
    bridge_exits = {}
    control_exit_code = None
    control_end_observed = False
    observed_end = max(last_time.values(), default=0)

    def event(record):
        nonlocal control_exit_code, control_end_observed, observed_end
        emitted = record.get('emitted_monotonic_s')
        if isinstance(emitted,(int,float)) and math.isfinite(emitted):
            observed_end = max(observed_end,emitted)
        stream = record.get('stream')
        if not isinstance(stream, str) or not isinstance(record.get('data'), dict):
            raise ValueError('Invalid telemetry record')
        producer = str(record.get('producer', 'unknown'))
        seq = record.get('seq', 0)
        if not isinstance(seq, int) or seq < 0:
            raise ValueError('Invalid telemetry sequence')
        previous = sequences.get(producer, 0)
        if seq > previous:
            gaps[producer] += max(0, seq-previous-1)
            sequences[producer] = seq
        drops[producer] = max(drops.get(producer, 0), int(record.get('dropped_total', 0)))
        streams[stream] += 1
        quality.observe({**record, 'quality': event_quality(record, metadata.get('dataset_settings', {}).get('max_state_age_s', .25))})
        if stream == 'bridge_exit':
            bridge_exits[record['data']['bridge']] = record['data']
        if stream == 'session_end':
            control_end_observed = True
            control_exit_code = record['data'].get('control_exit_code')

    indices = {layout.frames: inspect_jsonl(path/layout.frames, frame)}
    for name in event_files:
        indices[name] = inspect_jsonl(path/name, event)
    ignored = sorted(set(files)-expected)
    source_hashes = {name: sha256(files[name]) for name in
                     ('metadata.json', layout.frames, layout.console, *event_files, *artifacts) if name in files}
    # Separate files preserve per-producer sequences but not total receive order.
    # Do not report gaps obtained by traversing streams in filename order.
    if not layout.legacy:
        gaps.clear()
    return dict(source_episode=str(path), source_episode_id=path.name,
                audited_utc=datetime.now(timezone.utc).isoformat(),
                metadata=metadata, jsonl=indices, quality=quality.report(),
                last_source_monotonic_s=max(observed_end, max(last_time.values(),default=0)),
                sequence_gap_audit='legacy traversal' if layout.legacy else 'not reconstructed across routed files',
                frame_counts=dict(counts), stream_counts=dict(streams),
                sequence_gaps=dict(gaps), producer_reported_drops=drops,
                bridge_exits=bridge_exits, control_exit_code=control_exit_code,
                control_end_observed=control_end_observed,
                truncated=any(v['truncated'] for v in indices.values()),
                ignored_files=ignored, source_file_sha256=source_hashes,
                estimated_copy_bytes=image_bytes+sum(files[n].stat().st_size for n in source_hashes),
                tail_completeness='unknown', automatic_source_deletion_allowed=False)


def copy_prefix(store, source, name, size):
    with source.open('rb') as handle:
        remaining = size
        while remaining:
            data = handle.read(min(1024*1024, remaining))
            if not data:
                raise ValueError(f'Source changed while copying {name}')
            store.write(name, data)
            remaining -= len(data)


def recover_episode(path, output_root=None, allow_truncated=False, notice=print):
    from .runner import CONTROL_LOCK
    original = Path(path).absolute()
    # Same lock order as recording and transfer. Audit never modifies episode files.
    with FileLock(CONTROL_LOCK), FileLock(original.parent/'.lock'):
        report = inspect_episode(original, notice)
        if output_root is None:
            return report
        if report['truncated'] and not allow_truncated:
            raise ValueError('Partial final JSONL line found. Audit first; use --allow-truncated '
                             'to copy only complete records into a NEW episode.')
        destination = Path(output_root).expanduser().resolve()
        if destination.is_relative_to(original.parent.resolve()):
            raise ValueError('Recovery output must be outside the source spool/backup directory')
        destination.mkdir(parents=True, exist_ok=True)
        # Account for per-image block rounding, metadata and a free-space margin.
        required = report['estimated_copy_bytes'] + sum(report['frame_counts'].values())*4096 + 16*1024*1024
        if shutil.disk_usage(destination).free < required + 100*1024*1024:
            raise ValueError('Insufficient free space for a separate recovery copy')
        metadata = dict(report['metadata'])
        layout = Layout(metadata['schema'])
        for key in ('episode_id', 'created_utc'):
            metadata.pop(key, None)
        metadata['recovered_from'] = report['source_episode_id']
        if not layout.legacy:
            # Freeze the original snapshots, never substitute this host's configuration.
            metadata['_artifacts'] = {}
            for name in ARTIFACTS:
                source = original/name
                if source.exists():
                    if source.stat().st_size > 4*1024*1024:
                        raise ValueError(f'Oversized artifact: {name}')
                    text = source.read_bytes().decode("utf-8")
                    if name.endswith('.json'): json_object(text)
                    metadata['_artifacts'][name] = text
        destination_lock = FileLock(destination/'.lock')
        try:
            store = EpisodeStore(destination, storage_usage(destination)+required, 100*1024*1024, metadata)
        except BaseException:
            destination_lock.close()
            raise
        try:
            copied = 0
            last_notice = time.monotonic()
            for raw in rows(original/layout.frames, report['jsonl'][layout.frames]['valid_bytes']):
                frame = json_object(raw)
                jpg = safe_file(original, frame['path']).read_bytes()
                if hashlib.sha256(jpg).hexdigest() != frame['sha256']:
                    raise ValueError(f'Source image changed: {frame["path"]}')
                store.frame(frame['camera'], jpg, frame['received_monotonic_s'], frame['received_seq'])
                copied += 1
                if time.monotonic()-last_notice >= 2:
                    notice(f'Recovery copy: {copied}/{sum(report["frame_counts"].values())} images')
                    last_notice = time.monotonic()
            for name in (*layout.telemetry_files(original), layout.console):
                source = original/name
                if source.exists():
                    count = report['jsonl'][name]['valid_bytes'] if name.endswith('.jsonl') else source.stat().st_size
                    copy_prefix(store, source, name, count)
                    if name.endswith('.jsonl') and not layout.legacy:
                        for raw in rows(source, count):
                            event = json_object(raw)
                            store.quality.observe({**event, 'quality': event_quality(event, store.max_state_age)})
            for name, digest in report['source_file_sha256'].items():
                if sha256(original/name) != digest:
                    raise ValueError(f'Source changed during recovery: {name}')
            store.streams.update(report['stream_counts'])
            store.sequence_gaps.update(report['sequence_gaps'])
            store.dropped.update(report['producer_reported_drops'])
            store.bridge_exits = report['bridge_exits']
            store.control_exit_code = report['control_exit_code']
            saved_report = {k: v for k, v in report.items() if k != 'metadata'}
            store.write('recovery.json', encode(saved_report))
            store.finish('RECOVERED_UNFINALIZED', extra={
                'recovery': dict(source_episode_id=report['source_episode_id'],
                                 truncated=report['truncated'], tail_completeness='unknown',
                                 control_end_observed=report['control_end_observed'],
                                 last_source_monotonic_s=report['last_source_monotonic_s']),
                'automatic_source_deletion_allowed': False})
            verify_episode(store.path)
        except BaseException:
            # Preserve partial output too; never delete or "finalize" on failure.
            for handle in store.handles.values():
                handle.close()
            notice(f'Recovery incomplete; original unchanged; partial copy retained at {store.path}')
            raise
        finally:
            destination_lock.close()
        return dict(recovered_episode=str(store.path), **saved_report)
