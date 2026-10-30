"""Crash fixtures and copy-out recovery; no real robot or network connection."""
import json
from pathlib import Path
import tempfile
import unittest

from g1d_pico.recording.recovery import recover_episode
from g1d_pico.recording.runner import CONTROL_LOCK
from g1d_pico.recording.storage import EpisodeStore, FileLock, sha256, verify_episode


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        store = EpisodeStore(self.root/'original', 4_000_000, 0, {'instruction': 'test'})
        for camera in ('head', 'left_wrist', 'right_wrist'):
            store.frame(camera, b'\xff\xd8fixture\xff\xd9', 12.5, 2)
        store.event({'stream': 'arm_state', 'producer': 'p', 'seq': 1,
                     'data': {'q': [0]*14, 'received_at': 12.0}})
        store.event({'stream': 'bridge_exit', 'producer': 'p', 'seq': 2,
                     'data': {'bridge': 'dex3_left', 'exit_code': -6, 'escalation': 'none'}})
        store.event({'stream': 'console', 'producer': 'p', 'seq': 3, 'data': {'text': '退出\n'}})
        for handle in store.handles.values():
            handle.close()  # Crash fixture: files exist but there is no manifest.
        self.source = store.path
        self.dest = self.root/'recovered'

    def tearDown(self):
        self.temp.cleanup()

    def hashes(self):
        return {str(p.relative_to(self.source)): sha256(p) for p in self.source.rglob('*') if p.is_file()}

    def recover(self, **kwargs):
        return recover_episode(self.source, notice=lambda text: None, **kwargs)

    def test_audit_is_read_only_and_does_not_claim_control_ended(self):
        before = self.hashes()
        report = self.recover()
        self.assertEqual(report['frame_counts'], dict(head=1, left_wrist=1, right_wrist=1))
        self.assertFalse(report['control_end_observed'])
        self.assertEqual(report['tail_completeness'], 'unknown')
        self.assertEqual(self.hashes(), before)
        self.assertFalse(self.dest.exists())

    def test_copy_out_preserves_original_timestamps_and_abnormal_exit(self):
        before = self.hashes()
        report = self.recover(output_root=self.dest)
        output = Path(report['recovered_episode'])
        manifest = verify_episode(output)
        self.assertEqual(manifest['recording_stop_reason'], 'RECOVERED_UNFINALIZED')
        self.assertFalse(manifest['automatic_source_deletion_allowed'])
        self.assertIsNone(manifest['task_success'])
        self.assertEqual(manifest['bridge_exits']['dex3_left']['exit_code'], -6)
        self.assertEqual((output/'telemetry.jsonl').read_bytes(), (self.source/'telemetry.jsonl').read_bytes())
        frame = json.loads((output/'frames.jsonl').read_text().splitlines()[0])
        self.assertEqual(frame['received_monotonic_s'], 12.5)
        self.assertEqual(self.hashes(), before)
        self.assertNotEqual(output.name, self.source.name)

    def test_partial_tail_requires_explicit_option_and_original_is_unchanged(self):
        telemetry = self.source/'telemetry.jsonl'
        valid = telemetry.read_bytes()
        with telemetry.open('ab') as handle: handle.write(b'{"stream":')
        before = self.hashes()
        self.assertTrue(self.recover()['truncated'])
        with self.assertRaisesRegex(ValueError, 'allow-truncated'):
            self.recover(output_root=self.dest)
        result = self.recover(output_root=self.dest, allow_truncated=True)
        output = Path(result['recovered_episode'])
        self.assertEqual((output/'telemetry.jsonl').read_bytes(), valid)
        self.assertTrue(verify_episode(output)['recovery']['truncated'])
        self.assertEqual(self.hashes(), before)

    def test_truncated_frame_index_keeps_orphan_image_only_in_original(self):
        frames = self.source/'frames.jsonl'
        lines = frames.read_bytes().splitlines(keepends=True)
        frames.write_bytes(b''.join(lines[:2]) + lines[2][:20])
        result = self.recover(output_root=self.dest, allow_truncated=True)
        self.assertIn('images/right_wrist/00000000.jpg', result['ignored_files'])
        manifest = verify_episode(result['recovered_episode'])
        self.assertNotIn('right_wrist', manifest['frame_counts'])
        self.assertTrue((self.source/'images/right_wrist/00000000.jpg').exists())

    def test_interior_corruption_is_not_silently_trimmed(self):
        telemetry = self.source/'telemetry.jsonl'
        telemetry.write_bytes(b'broken\n' + telemetry.read_bytes())
        with self.assertRaisesRegex(ValueError, 'Interior'):
            self.recover(output_root=self.dest, allow_truncated=True)

    def test_indexed_image_corruption_refuses_recovery(self):
        (self.source/'images/head/00000000.jpg').write_bytes(b'damaged')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            self.recover(output_root=self.dest, allow_truncated=True)
        self.assertFalse(self.dest.exists())

    def test_output_cannot_expand_robot_spool_and_control_lock_is_respected(self):
        with self.assertRaisesRegex(ValueError, 'outside'):
            self.recover(output_root=self.source.parent/'new')
        with FileLock(CONTROL_LOCK, shared=True):
            with self.assertRaises(RuntimeError): self.recover()

    def test_symlink_is_refused(self):
        (self.source/'external').symlink_to(self.root)
        with self.assertRaisesRegex(ValueError, 'Symlink'):
            self.recover()


if __name__ == '__main__':
    unittest.main()
