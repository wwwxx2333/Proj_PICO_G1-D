"""Writer lifecycle regressions using temporary files and local IPC only."""
import multiprocessing as mp
import os
from pathlib import Path
import signal
import tempfile
import time
import unittest

from g1d_pico.recording.session import EpisodeSession
from g1d_pico.recording.storage import verify_episode


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.messages = []
        settings = dict(spool=str(self.root/'spool'), max_cache_gb=.02, min_free_gb=0,
                        image_fps=20, cameras=[], camera_config_port=60000)
        self.session = EpisodeSession(mp.get_context('spawn'), settings, {},
                                      str(self.root/'telemetry.sock'), self.messages.append)

    def tearDown(self):
        if self.session.worker is not None:
            self.session.finish_control(lambda: True)
        self.temporary.cleanup()

    def wait_state(self, state):
        deadline = time.monotonic()+10
        while self.session.state != state:
            self.session.poll()
            if time.monotonic() > deadline: self.fail(str(self.messages))
            time.sleep(.01)

    def test_starting_and_saving_presses_are_discarded(self):
        self.assertEqual(self.session.toggle(), 'starting')
        self.assertEqual(self.session.toggle(), 'busy')
        self.wait_state('recording')
        self.assertEqual(self.session.toggle(), 'saving')
        self.assertEqual(self.session.toggle(), 'busy')
        self.wait_state('waiting')
        for _ in range(5): self.session.poll()
        episodes = list((self.root/'spool').glob('episode_*'))
        self.assertEqual(len(episodes), 1)
        self.assertEqual(verify_episode(episodes[0])['recording_stop_reason'], 'L3_STOP')

    def test_exit_during_startup_still_reports_actual_saved_directory(self):
        self.session.toggle()
        self.assertTrue(self.session.finish_control(lambda: False))
        episode, = (self.root/'spool').glob('episode_*')
        self.assertEqual(verify_episode(episode)['recording_stop_reason'], 'CONTROL_ENDED')
        self.assertIn(f'REC SAVED 保存完成：{episode}', self.messages)

    def test_killed_writer_keeps_original_data_and_can_start_a_new_episode(self):
        self.session.toggle()
        self.wait_state('recording')
        first = Path(self.session.episode)
        os.kill(self.session.worker.pid, signal.SIGKILL)
        self.wait_state('waiting')
        self.assertTrue((first/'IN_PROGRESS').exists())
        self.assertFalse(Path(self.session.address).exists())
        self.session.toggle()
        self.wait_state('recording')
        second = Path(self.session.episode)
        self.assertNotEqual(first, second)
        self.session.toggle()
        self.wait_state('waiting')
        verify_episode(second)
        self.assertTrue((first/'IN_PROGRESS').exists())
        self.assertTrue(self.session.had_failure)

    def test_write_error_is_not_reported_as_success(self):
        self.session._message({'state': 'stopped', 'reason': 'WRITE_ERROR'})
        self.assertTrue(self.session.failed)
        self.assertEqual(self.session.state, 'saving')
