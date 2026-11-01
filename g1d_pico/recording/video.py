"""Host-only, timestamp-aligned MP4 review copies of immutable v1/v2 episodes.

The top panel shows the original head image (including stereo concatenation).
Wrist cameras occupy the bottom two panels. Missing or stale images are black,
not silently held through an outage. Nothing here controls or contacts PC2.
"""
from collections import Counter
from dataclasses import dataclass
import json
import math
from pathlib import Path
import tempfile
import time

from .annotations import time_bounds
from .layout import CAMERAS, Layout
from .storage import EPISODE_RE, FileLock, pretty, safe_file, sha256, sync_tree, verify_episode

VIDEO_SCHEMA = 'g1d-review-video-v1'
DEFAULT_FPS = 20.0
DEFAULT_WIDTH = 1280
DEFAULT_MAX_GAP = 0.25


def video_libraries():
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        raise RuntimeError('Video requires OpenCV and NumPy in the HOST Python environment '
                           '(python -m pip install opencv-python-headless numpy).') from exc
    return cv2, np


def check_video_dependencies():
    """Check the actual MP4 encoder, not just whether cv2 can be imported."""
    cv2, np = video_libraries()
    try:
        with tempfile.TemporaryDirectory(prefix='g1d-video-check-') as directory:
            path = Path(directory)/'check.mp4'
            writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*'mp4v'), 20, (32, 32))
            try:
                if not writer.isOpened():
                    raise RuntimeError('OpenCV cannot open the mp4v encoder. Use an OpenCV build with FFmpeg.')
                writer.write(np.zeros((32, 32, 3), dtype=np.uint8))
            finally:
                writer.release()
            reader = cv2.VideoCapture(str(path))
            try:
                if not reader.read()[0]:
                    raise RuntimeError('MP4 codec check failed: the generated frame cannot be decoded.')
            finally:
                reader.release()
    except cv2.error as exc:
        raise RuntimeError(f'OpenCV video initialization failed: {exc}') from exc


def validate_output_root(output_root):
    root = Path(output_root).expanduser().resolve()
    # Protect raw episodes even when the selected output is several levels inside one.
    for parent in (root, *root.parents):
        if EPISODE_RE.fullmatch(parent.name) or (parent/'metadata.json').is_file():
            raise ValueError('Video output must be outside all raw episode directories')
    return root


@dataclass(frozen=True)
class Frame:
    timestamp: float
    path: str
    index: int


class CameraTimeline:
    """Advance monotonically; only the currently displayed JPEG is decoded/cached."""
    def __init__(self, frames):
        self.frames = frames
        self.cursor = -1
        self.cached_index = None
        self.cached_image = None
        self.displayed = set()
        self.status_counts = Counter()

    def select(self, timestamp, max_gap):
        while self.cursor + 1 < len(self.frames) and self.frames[self.cursor+1].timestamp <= timestamp + 1e-9:
            self.cursor += 1
        if self.cursor < 0:
            return None, 'NO FRAME', None
        frame = self.frames[self.cursor]
        age = max(0.0, timestamp-frame.timestamp)
        if age > max_gap + 1e-9:
            return None, 'GAP', age
        return frame, 'OK', age

    def tile(self, episode, timestamp, max_gap, width, height, label):
        cv2, np = video_libraries()
        frame, status, age = self.select(timestamp, max_gap)
        tile = np.zeros((height, width, 3), dtype=np.uint8)
        if frame is not None:
            if self.cached_index != frame.index:
                image = cv2.imread(str(safe_file(episode, frame.path)), cv2.IMREAD_COLOR)
                if image is None:
                    raise ValueError(f'Cannot decode JPEG: {frame.path}')
                scale = min(width/image.shape[1], (height-28)/image.shape[0])
                size = (max(1, round(image.shape[1]*scale)), max(1, round(image.shape[0]*scale)))
                self.cached_image = cv2.resize(image, size, interpolation=cv2.INTER_AREA)
                self.cached_index = frame.index
            image = self.cached_image
            x = (width-image.shape[1])//2
            y = 28+(height-28-image.shape[0])//2
            tile[y:y+image.shape[0], x:x+image.shape[1]] = image
            self.displayed.add(frame.index)
        self.status_counts[status] += 1
        suffix = '' if age is None else f' age={age:.3f}s'
        color = (150, 255, 150) if status == 'OK' else (60, 80, 255)
        cv2.putText(tile, f'{label}: {status}{suffix}', (8, 20), cv2.FONT_HERSHEY_SIMPLEX,
                    .5, color, 1, cv2.LINE_AA)
        return tile


def load_timelines(episode, layout):
    groups = {camera: [] for camera in CAMERAS}
    path = episode/layout.frames
    if path.is_file():
        with path.open() as handle:
            for line in handle:
                row = json.loads(line)
                stamp = row.get('received_monotonic_s')
                if type(stamp) not in (int, float) or not math.isfinite(stamp):
                    raise ValueError('Image index has an invalid receive timestamp')
                group = groups[row['camera']]
                if group and stamp < group[-1].timestamp:
                    raise ValueError(f'Non-monotonic image timestamps: {row["camera"]}')
                group.append(Frame(stamp, row['path'], row['frame_index']))
    if not any(groups.values()):
        raise ValueError('Episode contains no images to convert')
    return {camera: CameraTimeline(frames) for camera, frames in groups.items()}


def verify_video_file(path, expected_frames, width, height, fps):
    cv2, _ = video_libraries()
    reader = cv2.VideoCapture(str(path))
    try:
        if not reader.isOpened():
            raise RuntimeError('Generated MP4 cannot be opened')
        if round(reader.get(cv2.CAP_PROP_FRAME_COUNT)) != expected_frames:
            raise RuntimeError('Generated MP4 frame count differs; check disk space/encoder')
        if abs(reader.get(cv2.CAP_PROP_FPS)-fps) > .01:
            raise RuntimeError('Generated MP4 frame rate differs')
        for index in (0, expected_frames-1):
            reader.set(cv2.CAP_PROP_POS_FRAMES, index)
            okay, frame = reader.read()
            if not okay or frame.shape[:2] != (height, width):
                raise RuntimeError('Generated MP4 first/last frame failed validation')
    finally:
        reader.release()


def make_video(episode, output_root, fps=DEFAULT_FPS, width=DEFAULT_WIDTH,
               max_gap=DEFAULT_MAX_GAP, notice=print):
    """Verify raw input and atomically publish overview.mp4 plus provenance JSON.

    Source timestamps are resampled to CFR; duration is rounded up by <1/fps.
    Output is a review derivative, never a replacement for raw training images.
    """
    episode = Path(episode).expanduser().resolve()
    root = validate_output_root(output_root)
    if root == episode.parent:
        raise ValueError('Use a separate videos directory, not the raw episode parent')
    if not math.isfinite(fps) or not 1 <= fps <= 60:
        raise ValueError('Video fps must be between 1 and 60')
    if type(width) is not int or width < 320 or width > 3840 or width % 4:
        raise ValueError('Video width must be divisible by 4, between 320 and 3840')
    if not math.isfinite(max_gap) or not 0 < max_gap <= 10:
        raise ValueError('Video max_gap must be >0 and <=10 seconds')
    notice(f'VIDEO VERIFY {episode.name}')
    manifest = verify_episode(episode)
    metadata = json.loads((episode/'metadata.json').read_text())
    start, end = time_bounds(metadata, manifest)
    if end <= start:
        raise ValueError('Episode has no positive recording duration')
    timelines = load_timelines(episode, Layout(manifest['schema']))
    if not any(any(start-max_gap <= frame.timestamp < end for frame in timeline.frames)
               for timeline in timelines.values()):
        raise ValueError('No images overlap the recorded time interval')
    count = max(1, math.ceil((end-start)*fps))
    row_height = width*3//8
    height = 40+2*row_height
    identity = dict(schema=VIDEO_SCHEMA, source_episode_id=episode.name,
                    source_manifest_sha256=sha256(episode/'manifest.json'),
                    fps=float(fps), width=width, height=height, max_gap_s=float(max_gap), codec='mp4v')
    root.mkdir(parents=True, exist_ok=True)
    destination = root/episode.name
    with FileLock(root/f'.{episode.name}.video.lock'):
        if destination.exists() or destination.is_symlink():
            report_path = destination/'video.json'
            if destination.is_symlink() or not report_path.is_file():
                raise ValueError(f'Video destination already exists: {destination}; use a new output root')
            report = json.loads(report_path.read_text())
            video = destination/'overview.mp4'
            if (any(report.get(k) != v for k, v in identity.items()) or video.is_symlink()
                    or not video.is_file() or sha256(video) != report.get('video_sha256')):
                raise ValueError(f'Existing video differs or is damaged: {destination}; use a new output root')
            notice(f'VIDEO EXISTS {video}')
            return video
        check_video_dependencies()
        cv2, np = video_libraries()
        # Failed/cancelled conversion only removes our private temporary output.
        with tempfile.TemporaryDirectory(prefix=f'.{episode.name}.video-', dir=root) as directory:
            stage = Path(directory)
            video = stage/'overview.mp4'
            writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*'mp4v'), fps, (width, height))
            if not writer.isOpened():
                writer.release()
                raise RuntimeError('Cannot create MP4 output; check codec and output disk')
            notice(f'VIDEO START {episode.name}: {end-start:.2f}s, {count} frames, {width}x{height}')
            last_notice = time.monotonic()
            try:
                for index in range(count):
                    stamp = start+index/fps
                    canvas = np.zeros((height, width, 3), dtype=np.uint8)
                    cv2.putText(canvas, f't={index/fps:.2f}s / {end-start:.2f}s  |  PC2 receive-time preview',
                                (8, 26), cv2.FONT_HERSHEY_SIMPLEX, .6, (255, 255, 255), 1, cv2.LINE_AA)
                    canvas[40:40+row_height] = timelines['head'].tile(episode, stamp, max_gap, width, row_height, 'HEAD')
                    for side, offset in (('left_wrist', 0), ('right_wrist', width//2)):
                        canvas[40+row_height:, offset:offset+width//2] = timelines[side].tile(
                            episode, stamp, max_gap, width//2, row_height, side.upper())
                    writer.write(canvas)
                    if time.monotonic()-last_notice >= 5:
                        notice(f'VIDEO PROGRESS {episode.name}: {index+1}/{count}')
                        last_notice = time.monotonic()
            except cv2.error as exc:
                raise RuntimeError(f'Video encoding failed: {exc}') from exc
            finally:
                writer.release()
            verify_video_file(video, count, width, height, fps)
            report = dict(identity, source_schema=manifest['schema'],
                          source_start_monotonic_s=start, source_end_monotonic_s=end,
                          source_duration_s=end-start, video_duration_s=count/fps, output_frames=count,
                          time_basis='PC2 ZMQ receive timestamps, not hardware exposure synchronization',
                          frame_policy='latest frame at/before output time; black if missing or older than max_gap_s',
                          layout='head on top (stereo retained), left/right wrist below; no audio',
                          purpose='review only; raw JPEG/state files are unchanged',
                          cameras={camera: {'source_frames': len(timeline.frames),
                              'distinct_displayed_frames': len(timeline.displayed),
                              'output_status_counts': dict(timeline.status_counts)}
                              for camera, timeline in timelines.items()},
                          video_sha256=sha256(video))
            (stage/'video.json').write_bytes(pretty(report))
            sync_tree(stage)
            stage.rename(destination)
        notice(f'VIDEO SAVED {destination/"overview.mp4"}')
        return destination/'overview.mp4'
