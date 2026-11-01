"""Real MP4 codec tests, source immutability, time gaps, and failure handling."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from g1d_pico.recording.layout import SCHEMA
from g1d_pico.recording.storage import EpisodeStore, sha256, verify_episode
from g1d_pico.recording.video import CameraTimeline, Frame, make_video, validate_output_root

try:
    import cv2
    import numpy as np
except ImportError:
    cv2 = None


class TimelineTests(unittest.TestCase):
    def test_receive_times_do_not_use_future_frames_or_compress_gaps(self):
        timeline = CameraTimeline([Frame(100.1, 'one.jpg', 0), Frame(101.0, 'two.jpg', 1)])
        self.assertEqual(timeline.select(100, .25)[1], 'NO FRAME')
        self.assertEqual(timeline.select(100.2, .25)[0].index, 0)
        self.assertEqual(timeline.select(100.6, .25)[1], 'GAP')
        self.assertEqual(timeline.select(101.0, .25)[0].index, 1)

    def test_output_inside_any_episode_rejected(self):
        with self.assertRaisesRegex(ValueError, 'outside'):
            validate_output_root('/tmp/raw/episode_20261101_120000_abcd1234/more/videos')


@unittest.skipIf(cv2 is None, 'OpenCV not installed')
class VideoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.output = self.root/'videos'

    def tearDown(self):
        self.temp.cleanup()

    def episode(self, v2=False, invalid_jpeg=False):
        meta = {'instruction': '拿起杯子', 'recording_started_monotonic_s': 100}
        if v2: meta['schema'] = SCHEMA
        store = EpisodeStore(self.root/'raw', 20_000_000, 0, meta)
        for stamp in (100.1, 100.2, 100.8):
            color = (0, 255, 0) if stamp < 100.8 else (255, 0, 0)
            image = np.full((48, 64, 3), color, dtype=np.uint8)
            jpg = cv2.imencode('.jpg', image)[1].tobytes() if not invalid_jpeg else b'bad JPEG'
            store.frame('head', jpg, stamp, 1)
            store.frame('left_wrist', jpg, stamp, 1)
        store.finish(extra={'recording_requested_stop_monotonic_s': 101})
        return store.path

    def render(self, path, **kwargs):
        return make_video(path, self.output, fps=10, width=320, notice=lambda _: None, **kwargs)

    def test_v1_and_v2_real_mp4_decode_duration_gap_and_missing_camera(self):
        for v2 in (False, True):
            with self.subTest(v2=v2):
                path = self.episode(v2)
                before = {str(p.relative_to(path)): sha256(p) for p in path.rglob('*') if p.is_file()}
                video = self.render(path)
                after = {str(p.relative_to(path)): sha256(p) for p in path.rglob('*') if p.is_file()}
                self.assertEqual(before, after)
                verify_episode(path)
                report = json.loads(video.with_name('video.json').read_text())
                self.assertEqual(report['output_frames'], 10)
                self.assertEqual(report['video_duration_s'], 1)
                self.assertEqual(report['cameras']['right_wrist']['output_status_counts']['NO FRAME'], 10)
                self.assertGreater(report['cameras']['head']['output_status_counts']['GAP'], 0)
                cap = cv2.VideoCapture(str(video))
                decoded = []
                while True:
                    okay, frame = cap.read()
                    if not okay: break
                    decoded.append(frame)
                cap.release()
                self.assertEqual(len(decoded), 10)
                self.assertEqual(decoded[0].shape, (280, 320, 3))
                # Center of the head panel: black during gap, green/blue at actual receive times.
                self.assertLess(decoded[5][100, 160].max(), 15)
                self.assertGreater(decoded[2][100, 160, 1], 200)
                self.assertGreater(decoded[8][100, 160, 0], 200)

    def test_repeat_skips_same_preview_but_refuses_changed_options_or_damaged_video(self):
        path = self.episode()
        video = self.render(path)
        stamp = video.stat().st_mtime_ns
        self.assertEqual(self.render(path), video)
        self.assertEqual(stamp, video.stat().st_mtime_ns)
        with self.assertRaisesRegex(ValueError, 'differs'):
            self.render(path, max_gap=.5)
        video.write_bytes(b'broken')
        with self.assertRaisesRegex(ValueError, 'damaged'):
            self.render(path)

    def test_invalid_jpeg_or_encoder_failure_never_publishes_video_or_touches_raw(self):
        for invalid in (True, False):
            path = self.episode(invalid_jpeg=invalid)
            if invalid:
                with self.assertRaisesRegex(ValueError, 'decode'):
                    self.render(path)
            else:
                with patch('g1d_pico.recording.video.verify_video_file', side_effect=RuntimeError('disk full')):
                    with self.assertRaisesRegex(RuntimeError, 'disk full'):
                        self.render(path)
            self.assertFalse((self.output/path.name).exists())
            self.assertEqual(list(self.output.glob('.*.video-*')), [])
            verify_episode(path)

    def test_keyboard_interrupt_removes_only_private_partial_preview(self):
        path = self.episode()
        with patch('g1d_pico.recording.video.CameraTimeline.tile', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt): self.render(path)
        self.assertFalse((self.output/path.name).exists())
        verify_episode(path)

    def test_cli_accepts_multiple_episodes(self):
        paths = [self.episode(), self.episode(True)]
        result = subprocess.run([sys.executable, '-m', 'g1d_pico.recording', 'video',
                                 *(str(p) for p in paths), '--output-root', str(self.output),
                                 '--fps', '10', '--width', '320'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        for path in paths: self.assertTrue((self.output/path.name/'overview.mp4').is_file())

    def test_unfinalized_input_refused(self):
        path = self.episode()
        (path/'IN_PROGRESS').write_text('unfinished')
        with self.assertRaisesRegex(ValueError, 'finalized'): self.render(path)
        self.assertFalse(self.output.exists())


if __name__ == '__main__':
    unittest.main()
