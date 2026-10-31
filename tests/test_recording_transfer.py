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
            'import subprocess, sys\nraise SystemExit(subprocess.call(["bash", "-c", sys.argv[-1]]))\n')
        fake_rsync = self.bin/'rsync'
        fake_rsync.write_text('#!' + sys.executable + '\n'
            'import os, pathlib, shutil, sys\n'
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
        self.environment['PATH'] = str(self.bin) + os.pathsep + self.environment['PATH']

    def tearDown(self):
        self.temp.cleanup()

    def episode(self):
        store = EpisodeStore(self.spool, 20_000_000, 0, {'instruction': 'test'})
        store.frame('head', b'jpeg fixture', 123, 1)
        store.finish()
        return store.path

    def fetch(self, delete=False):
        args = [sys.executable, '-m', 'g1d_pico.recording', 'fetch', '--host', 'fake-PC2',
                '--remote-root', str(self.project), '--remote-python', sys.executable,
                '--destination', str(self.destination)]
        if delete: args.append('--delete-after-verify')
        return subprocess.run(args, cwd=PROJECT, env=self.environment, capture_output=True,
                              text=True, timeout=20)

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


if __name__ == '__main__':
    unittest.main()
