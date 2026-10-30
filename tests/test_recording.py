"""Offline recording/storage fault tests. No DDS, robot, SSH or real cameras."""
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from g1d_pico.recording.storage import (EpisodeStore, FileLock, RecordingStopped,
                                       RESERVE, sha256, verify_episode)
from g1d_pico.recording.telemetry import Telemetry
from g1d_pico.recording.runner import load_settings


class RecordingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def store(self, max_bytes=3_000_000):
        return EpisodeStore(self.root, max_bytes, 0, {'instruction': '测试搬运'})

    def test_roundtrip_frame_state_console_and_original_timestamps(self):
        store = self.store()
        store.frame('head', b'\xff\xd8fixture\xff\xd9', 12.5, 4)
        store.event({'stream': 'arm_state', 'producer': 'python:1', 'seq': 1,
                     'data': {'received_at': 10.0, 'q': [0.1]*14}, 'emitted_monotonic_s': 13.0})
        store.event({'stream': 'console', 'producer': 'python:1', 'seq': 2,
                     'data': {'text': 'ARMS HOLD\n'}})
        store.finish()
        manifest = verify_episode(store.path)
        self.assertEqual(manifest['frame_counts'], {'head': 1})
        self.assertEqual((store.path/'console.log').read_text(), 'ARMS HOLD\n')
        frame = json.loads((store.path/'frames.jsonl').read_text())
        self.assertEqual(frame['received_monotonic_s'], 12.5)
        self.assertIsNone(frame['exposure_timestamp'])
        self.assertFalse((store.path/'IN_PROGRESS').exists())

    def test_total_spool_quota_counts_previous_episodes_and_latches(self):
        first = self.store(4_000_000)
        first.frame('head', b'x'*1_200_000, 1, 1)
        first.finish()
        second = self.store(2_700_000)
        with self.assertRaisesRegex(RecordingStopped, 'CACHE_LIMIT'):
            second.frame('head', b'x'*1_200_000, 2, 1)
        with self.assertRaises(RecordingStopped):
            second.write('later.jsonl', b'{}\n')
        second.finish()
        self.assertEqual(verify_episode(second.path)['recording_stop_reason'], 'CACHE_LIMIT')
        self.assertTrue((first.path/'images/head/00000000.jpg').exists())
        total = sum(p.stat().st_size for p in self.root.rglob('*') if p.is_file())
        self.assertLess(total, 2_700_000)

    def test_failed_start_preserves_existing_episodes(self):
        first = self.store()
        first.finish()
        before = set(self.root.iterdir())
        with self.assertRaises(RecordingStopped):
            self.store(1)
        self.assertEqual(before, set(self.root.iterdir()))

    def test_low_disk_space_stops_recording(self):
        store = self.store()
        disk = type('Usage', (), {'free': 0})()
        with patch('g1d_pico.recording.storage.shutil.disk_usage', return_value=disk):
            store.last_space_check = 0
            with self.assertRaisesRegex(RecordingStopped, 'LOW_DISK_SPACE'):
                store.write('telemetry.jsonl', b'{}\n')
        store.finish()
        self.assertEqual(verify_episode(store.path)['recording_stop_reason'], 'LOW_DISK_SPACE')

    def test_corrupt_image_missing_file_and_extra_file_are_rejected(self):
        store = self.store()
        store.frame('head', b'jpeg', 1, 1)
        store.finish()
        image = store.path/'images/head/00000000.jpg'
        image.write_bytes(b'bad!')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            verify_episode(store.path)
        image.write_bytes(b'jpeg')
        (store.path/'extra.txt').write_text('unindexed')
        with self.assertRaisesRegex(ValueError, 'Unexpected'):
            verify_episode(store.path)

    def test_unfinalized_episode_is_never_accepted(self):
        store = self.store()
        with self.assertRaisesRegex(ValueError, 'finalized'):
            verify_episode(store.path)
        store.finish()

    def test_sequence_gaps_are_visible_not_filled_with_fake_samples(self):
        store = self.store()
        for seq in (2, 5):
            store.event({'stream': 'arm_state', 'producer': 'p', 'seq': seq,
                         'dropped_total': 3, 'data': {'received_at': 1.0}})
        store.finish()
        manifest = verify_episode(store.path)
        self.assertEqual(manifest['sequence_gaps']['p'], 3)
        self.assertEqual(manifest['producer_reported_drops']['p'], 3)

    def test_transfer_excludes_control_and_other_recordings(self):
        with FileLock(self.root/'activity', shared=True):
            with FileLock(self.root/'activity', shared=True):
                with self.assertRaises(RuntimeError):
                    FileLock(self.root/'activity')
        with FileLock(self.root/'activity'):
            with self.assertRaises(RuntimeError):
                FileLock(self.root/'activity', shared=True)

    def test_telemetry_absent_or_full_never_waits_or_raises(self):
        sink = Telemetry(str(self.root/'missing.sock'))
        started = time.monotonic()
        for _ in range(1000):
            self.assertFalse(sink.emit('state', {'q': [0]*14}))
        self.assertLess(time.monotonic()-started, 1)
        self.assertEqual(sink.dropped, 1000)
        sink.close()
        receiver = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        receiver.bind(str(self.root/'receiver.sock'))
        sink = Telemetry(str(self.root/'receiver.sock'))
        for _ in range(1000):
            sink.emit('state', {'q': [0]*14})
        self.assertGreater(sink.dropped, 0)
        sink.close()
        receiver.close()

    def test_cpp_observer_also_drops_without_blocking_control(self):
        binary = Path(__file__).resolve().parents[1]/'.test-build/recording_sink_test'
        if not binary.is_file():
            self.skipTest('Build tests/recording_sink_test.cpp to run native IPC test')
        address = str(self.root/'native.sock')
        receiver = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            receiver.bind(address)
            receiver.settimeout(1)
            environment = {**os.environ, 'G1D_RECORD_SOCKET': address}
            result = subprocess.run([str(binary)], env=environment, capture_output=True,
                                    text=True, timeout=3)
            self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
            message = json.loads(receiver.recv(60000))
            self.assertEqual(message['data']['q'], [1, 2, 3])
            self.assertFalse(message['data']['owned'])
            self.assertEqual(message['emitted_monotonic_s'], 123.25)
        finally:
            receiver.close()

    def test_invalid_config_rejected(self):
        config = self.root/'config.json'
        config.write_text(json.dumps(dict(spool=str(self.root), max_cache_gb=-1,
            min_free_gb=2, image_fps=20, cameras=['head'], camera_config_port=60000)))
        with self.assertRaisesRegex(ValueError, 'max_cache_gb'):
            load_settings(config)

    def test_write_error_is_latched_and_unfit_for_auto_deletion(self):
        store = self.store()
        store.write('telemetry.jsonl', b'{}\n')
        handle = store.handles['telemetry.jsonl']
        class BadFile:
            def write(self, data): raise OSError('disk I/O error')
            def fileno(self): return handle.fileno()
            def close(self): handle.close()
        store.handles['telemetry.jsonl'] = BadFile()
        with self.assertRaisesRegex(RecordingStopped, 'WRITE_ERROR'):
            store.write('telemetry.jsonl', b'{}\n')
        store.finish()
        with self.assertRaisesRegex(ValueError, 'damaged'):
            verify_episode(store.path)


if __name__ == '__main__':
    unittest.main()
