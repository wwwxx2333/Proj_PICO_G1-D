"""Exercise the real transfer protocol with local SSH/rsync stand-ins.

No connection leaves the machine. The fake SSH executes the same remote lease
helper locally; the fake rsync copies fixtures and can inject corruption.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from project_fixture import copy_runtime_project
from g1d_pico.recording.storage import EpisodeStore, FileLock, verify_episode

PROJECT = Path(__file__).resolve().parents[1]


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.bin = self.root/'bin'
        self.bin.mkdir()
        fake_ssh = self.bin/'ssh'
        fake_ssh.write_text('#!' + sys.executable + '\n'
            
            'import json, os, subprocess, sys\n'
            'with open(os.environ["SSH_CALLS"],"a") as f: f.write(json.dumps(sys.argv[1:])+"\\n")\n'
            'if "-O" in sys.argv: raise SystemExit(0)\n'
            'raise SystemExit(subprocess.call(["bash", "-c", sys.argv[-1]]))\n')
        fake_rsync = self.bin/'rsync'
        fake_rsync.write_text('#!' + sys.executable + '\n'
            'import json, os, pathlib, shlex, shutil, sys\n'
            'with open(os.environ["RSYNC_CALLS"],"a") as f: f.write(json.dumps(sys.argv[1:])+"\\n")\n'
            'if os.getenv("RECORD_TEST_DISCONNECT"): raise SystemExit(255)\n'
            'source = pathlib.Path(sys.argv[-2].split(":",1)[1])\n'
            'dest = pathlib.Path(sys.argv[-1])\n'
            'shutil.copytree(source, dest, dirs_exist_ok=True)\n'
            'if os.getenv("RECORD_TEST_CORRUPT"):\n'
            '    next(dest.rglob("*.jpg")).write_bytes(b"broken")\n')
        fake_ssh.chmod(0o755)
        fake_rsync.chmod(0o755)
        self.spool = self.root/'robot_spool'
        self.destination = self.root/'host_dataset'
        self.project = copy_runtime_project(self.root / 'remote_project')
        self.config = self.project/'config/recording.jsonc'
        self.config.write_text('// JSONC transfer fixture\n' + json.dumps(dict(spool=str(self.spool), max_cache_gb=.02,
            min_free_gb=0, image_fps=20, cameras=[], camera_config_port=60000)))
        self.environment = os.environ.copy()
        self.environment['SSH_CALLS'] = str(self.root/'ssh_calls.jsonl')
        self.environment['RSYNC_CALLS'] = str(self.root/'rsync_calls.jsonl')
        self.environment['PATH'] = str(self.bin) + os.pathsep + self.environment['PATH']

    def tearDown(self):
        self.temp.cleanup()

    def episode(self, schema=None):
        store = EpisodeStore(self.spool, 20_000_000, 0, {'instruction': 'test', **({'schema':schema} if schema else {})})
        store.frame('head', b'jpeg fixture', 123, 1)
        store.finish()
        return store.path

    def fetch(self, delete=False, extra=()):
        args = [sys.executable, '-m', 'g1d_pico.recording', 'fetch', '--host', 'fake-PC2',
                '--remote-root', str(self.project), '--remote-python', sys.executable,
                '--destination', str(self.destination)]
        args.extend(extra)
        if delete: args.append('--delete-after-verify')
        return subprocess.run(args, cwd=PROJECT, env=self.environment, capture_output=True,
                              text=True, timeout=20)

    def test_v2_nested_files_transfer_without_flat_path_assumptions(self):
        from g1d_pico.recording.layout import SCHEMA
        original = self.episode(SCHEMA)
        result = self.fetch(delete=True)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        copied = self.destination/original.name
        self.assertEqual(verify_episode(copied)['schema'], SCHEMA)
        self.assertTrue((copied/'observations/images/head/00000000.jpg').is_file())
        self.assertFalse(original.exists())

    def test_verified_transfer_then_explicit_delete(self):
        original = self.episode()
        result = self.fetch(delete=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(original.exists())
        self.assertTrue(verify_episode(self.destination/original.name)['finalized'])

    def test_default_transfer_keeps_pc2_copy_and_repeat_is_idempotent(self):
        original = self.episode()
        for _ in range(2):
            result = self.fetch()
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(original.exists())
        self.assertTrue(verify_episode(self.destination/original.name)['finalized'])

    def test_corrupted_transfer_never_deletes_source_or_finalizes_host_copy(self):
        original = self.episode()
        self.environment['RECORD_TEST_CORRUPT'] = '1'
        result = self.fetch(delete=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(original.exists())
        self.assertFalse((self.destination/original.name).exists())
        verify_episode(original)

    def test_transfer_refused_while_control_is_active(self):
        original = self.episode()
        with FileLock(self.project / '.local/control_session.lock', shared=True):
            result = self.fetch(delete=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(original.exists())
        self.assertFalse((self.destination/original.name).exists())

    def test_unfinalized_is_not_transferred_or_deleted(self):
        store = EpisodeStore(self.spool, 20_000_000, 0, {})
        try:
            result = self.fetch(delete=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue((store.path/'IN_PROGRESS').exists())
            self.assertFalse((self.destination/store.path.name).exists())
        finally:
            store.finish()

    def test_recovered_copy_is_never_automatically_deleted(self):
        store = EpisodeStore(self.spool, 20_000_000, 0, {})
        store.finish('RECOVERED_UNFINALIZED', extra={'automatic_source_deletion_allowed': False})
        result = self.fetch(delete=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(store.path.exists())
        self.assertIn('manual review', result.stdout)
        verify_episode(self.destination/store.path.name)

    def test_multiple_episodes_share_one_master_and_fail_closed_slaves(self):
        import shlex
        for _ in range(3): self.episode()
        result = self.fetch()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = [json.loads(line) for line in (self.root/'ssh_calls.jsonl').read_text().splitlines()]
        masters = [call for call in calls if '-O' not in call]
        self.assertEqual(len(masters), 1)
        master = masters[0]
        self.assertIn('ControlMaster=yes', master)
        self.assertIn('ControlPersist=no', master)
        socket = master[master.index('-S')+1]
        self.assertFalse(Path(socket).parent.exists())
        copies = [json.loads(line) for line in (self.root/'rsync_calls.jsonl').read_text().splitlines()]
        self.assertEqual(len(copies), 3)
        for copy in copies:
            slave = shlex.split(copy[copy.index('-e')+1])
            self.assertEqual(slave[slave.index('-S')+1], socket)
            self.assertIn('BatchMode=yes', slave)
            self.assertIn('ProxyCommand=false', slave)

    def test_disconnect_keeps_source_and_partial_directory_without_traceback(self):
        original = self.episode()
        self.environment['RECORD_TEST_DISCONNECT'] = '1'
        result = self.fetch(delete=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('Traceback', result.stderr)
        self.assertIn('NOT deleted', result.stderr)
        self.assertTrue(original.exists())
        self.assertTrue((self.destination/'.incoming'/original.name).exists())
        self.assertFalse((self.destination/original.name).exists())
        self.environment.pop('RECORD_TEST_DISCONNECT')
        result = self.fetch(delete=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(original.exists())

    def test_bad_video_option_fails_before_connecting(self):
        self.episode()
        result = self.fetch(extra=['--video-output-root', str(self.root/'videos')])
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root/'ssh_calls.jsonl').exists())

    def video_episode(self, bad_jpeg=False):
        try:
            import cv2
            import numpy as np
        except ImportError:
            self.skipTest('OpenCV not installed')
        store = EpisodeStore(self.spool, 20_000_000, 0, {'recording_started_monotonic_s': 100})
        jpg = cv2.imencode('.jpg', np.zeros((32, 32, 3), dtype=np.uint8))[1].tobytes()
        for t in (100.0, 100.1): store.frame('head', b'bad' if bad_jpeg else jpg, t, 1)
        store.finish(extra={'recording_requested_stop_monotonic_s': 100.2})
        return store.path

    def test_fetch_video_success_and_verified_raw_deletion_are_independent(self):
        original = self.video_episode()
        videos = self.root/'previews'
        result = self.fetch(delete=True, extra=['--with-video', '--video-output-root', str(videos)])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(original.exists())
        self.assertTrue((videos/original.name/'overview.mp4').is_file())
        verify_episode(self.destination/original.name)
        self.assertLess(result.stdout.index('PC2 cache cleared'), result.stdout.index('VIDEO START'))

    def test_video_error_keeps_verified_raw_and_continues_other_videos(self):
        bad = self.video_episode(bad_jpeg=True)
        good = self.video_episode()
        videos = self.root/'videos'
        result = self.fetch(delete=True, extra=['--with-video'])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('VIDEO ERROR', result.stdout)
        self.assertNotIn('Traceback', result.stderr)
        for path in (bad, good): verify_episode(self.destination/path.name)
        self.assertFalse((videos/bad.name).exists())
        self.assertTrue((videos/good.name/'overview.mp4').is_file())


if __name__ == '__main__':
    unittest.main()
