"""Slow durability and EOF/abort regression tests, without DDS."""
import multiprocessing as mp
import time
import tempfile
import unittest
from unittest.mock import patch

from g1d_pico.recording.recorder import FinalizationProgress
from g1d_pico.recording.shutdown import wait_for_recorder
from g1d_pico.recording.storage import EpisodeStore, verify_episode


def slow_finalizer(root, send):
    import os
    store = EpisodeStore(root, 10_000_000, 0, {})
    for i in range(30):
        store.frame('head', b'\xff\xd8x\xff\xd9', i/20, i+1)
    original_fsync = os.fsync
    def slow(fd):
        time.sleep(1.0)
        original_fsync(fd)
    with patch('g1d_pico.recording.storage.os.fsync', side_effect=slow):
        store.finish(progress=FinalizationProgress(send))
    send.send({'state': 'finished', 'episode': str(store.path)})
    send.close()


class FakeWorker:
    def __init__(self, alive_calls=5, exitcode=0):
        self.remaining = alive_calls
        self.exitcode = exitcode
        self.terminated = False
    def is_alive(self):
        self.remaining -= 1
        return self.remaining >= 0
    def join(self, **kwargs): pass
    def terminate(self):
        self.terminated = True
        self.remaining = -1
    def kill(self): self.terminate()


class EofPipe:
    def poll(self, *args): return True
    def recv(self): raise EOFError


class ShutdownTests(unittest.TestCase):
    def test_eof_does_not_prematurely_terminate_worker_or_claim_success(self):
        worker = FakeWorker()
        messages = []
        self.assertFalse(wait_for_recorder(worker, EofPipe(), messages.append, lambda: False, .01))
        self.assertFalse(worker.terminated)
        self.assertTrue(any('no reported progress' in text for text in messages))

    def test_only_explicit_abort_terminates_worker(self):
        worker = FakeWorker(100)
        self.assertFalse(wait_for_recorder(worker, EofPipe(), lambda text: None, lambda: True))
        self.assertTrue(worker.terminated)

    def test_already_observed_finished_status_is_preserved(self):
        worker = FakeWorker(alive_calls=0)
        self.assertTrue(wait_for_recorder(worker, EofPipe(), lambda text: None,
                                         lambda: False, finished=True))

    def test_write_failure_during_exit_is_not_reported_as_saved(self):
        class StatusPipe:
            messages = [{'state': 'stopped', 'reason': 'WRITE_ERROR'},
                        {'state': 'finished', 'reason': 'WRITE_ERROR'}]
            def poll(self, *args): return bool(self.messages)
            def recv(self): return self.messages.pop(0)
        observed = []
        self.assertFalse(wait_for_recorder(
            FakeWorker(alive_calls=0), StatusPipe(), lambda text: None,
            lambda: False, on_message=observed.append))
        self.assertEqual(len(observed), 2)

    def test_progressing_real_process_survives_more_than_thirty_seconds(self):
        with tempfile.TemporaryDirectory() as root:
            context = mp.get_context('spawn')
            receive, send = context.Pipe(duplex=False)
            worker = context.Process(target=slow_finalizer, args=(root, send))
            worker.start()
            send.close()
            messages = []
            start = time.monotonic()
            try:
                self.assertTrue(wait_for_recorder(worker, receive, messages.append, lambda: False, 5))
                self.assertGreater(time.monotonic()-start, 30)
                from pathlib import Path
                episode, = Path(root).glob('episode_*')
                self.assertEqual(verify_episode(episode)['frame_counts']['head'], 30)
                self.assertTrue(any('sync_files' in text for text in messages))
            finally:
                if worker.is_alive(): worker.kill()
                worker.join()
                receive.close()


if __name__ == '__main__':
    unittest.main()
