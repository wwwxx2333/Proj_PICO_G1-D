"""Writer prewarming/lifecycle regressions with local files and IPC, no DDS."""
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
        self.settings = dict(spool=str(self.root/'spool'), max_cache_gb=.02, min_free_gb=0,
                             image_fps=20, cameras=[], camera_config_port=60000)
        self.session = EpisodeSession(mp.get_context('spawn'), self.settings, {},
                                      str(self.root/'telemetry.sock'), self.messages.append)
        self.session.prepare()

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

    def start(self):
        self.wait_state('waiting')
        self.session.set_control_ready(True, '')
        self.assertEqual(self.session.request('start'), 'starting')
        self.wait_state('recording')

    def test_prepared_writer_has_no_episode_and_l3_reuses_same_process(self):
        self.wait_state('waiting')
        pid = self.session.worker.pid
        self.assertFalse(list((self.root/'spool').glob('episode_*')))
        self.assertFalse(Path(self.session.address).exists())
        self.assertEqual(self.session.request('start'), 'not_ready')
        self.start()
        self.assertEqual(pid, self.session.worker.pid)
        self.session.set_control_ready(False, 'homing requested later')
        self.assertEqual(self.session.request('stop'), 'saving')
        self.wait_state('waiting')
        episode, = (self.root/'spool').glob('episode_*')
        self.assertEqual(verify_episode(episode)['recording_stop_reason'], 'L3_STOP')

    def test_starting_and_saving_presses_are_not_queued(self):
        self.assertEqual(self.session.request('start'), 'not_ready')
        self.start()
        self.assertEqual(self.session.request('start'), 'recording')
        self.assertEqual(self.session.request('stop'), 'saving')
        self.assertEqual(self.session.request('start'), 'saving')
        self.wait_state('waiting')
        self.assertEqual(len(list((self.root/'spool').glob('episode_*'))), 1)

    def test_exit_without_l3_saves_no_empty_episode(self):
        self.assertTrue(self.session.finish_control(lambda: False))
        self.assertEqual(list((self.root/'spool').glob('episode_*')), [])
        self.assertTrue(any('REC CLOSED' in m for m in self.messages))

    def test_exit_during_start_reports_actual_saved_directory(self):
        self.wait_state('waiting')
        self.session.set_control_ready(True, '')
        self.session.request('start')
        self.assertTrue(self.session.finish_control(lambda: False))
        episode, = (self.root/'spool').glob('episode_*')
        self.assertEqual(verify_episode(episode)['recording_stop_reason'], 'CONTROL_ENDED')
        self.assertIn(f'REC SAVED 保存完成：{episode}', self.messages)

    def test_killed_writer_preserves_data_and_retry_only_prepares(self):
        self.start()
        first = Path(self.session.episode)
        os.kill(self.session.worker.pid, signal.SIGKILL)
        self.wait_state('error')
        self.assertTrue((first/'IN_PROGRESS').exists())
        self.assertFalse(Path(self.session.address).exists())
        self.assertEqual(self.session.request('start'), 'preparing')
        self.wait_state('waiting')
        self.assertEqual(len(list((self.root/'spool').glob('episode_*'))), 1)
        self.start()
        second = Path(self.session.episode)
        self.session.request('stop')
        self.wait_state('waiting')
        verify_episode(second)
        self.assertTrue((first/'IN_PROGRESS').exists())
        self.assertTrue(self.session.had_failure)

    def test_write_error_is_not_success(self):
        self.session._message({'state': 'stopped', 'reason': 'WRITE_ERROR'})
        self.assertTrue(self.session.failed)
