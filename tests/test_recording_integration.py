"""Local synthetic streams/subprocesses only. Never imports DDS or moves a robot."""
import json
import multiprocessing as mp
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import zmq

from g1d_pico.recording.recorder import CAMERAS, run_worker
from g1d_pico.recording.storage import verify_episode
from g1d_pico.recording.telemetry import Telemetry

PROJECT = Path(__file__).resolve().parents[1]


class FakeCameras:
    def __init__(self, jpg_size=3000):
        self.context = zmq.Context()
        self.rep = self.context.socket(zmq.REP)
        self.port = self.rep.bind_to_random_port('tcp://127.0.0.1')
        self.pubs = []
        self.config = {}
        for key in CAMERAS.values():
            pub = self.context.socket(zmq.PUB)
            port = pub.bind_to_random_port('tcp://127.0.0.1')
            self.pubs.append(pub)
            self.config[key] = {'enable_zmq': True, 'zmq_port': port, 'image_shape': [480,640]}
        self.jpg = b'\xff\xd8' + b'x'*jpg_size + b'\xff\xd9'
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.serve)
        self.thread.start()

    def serve(self):
        next_frame = 0.0
        while not self.stop.is_set():
            if self.rep.poll(5):
                self.rep.recv()
                self.rep.send_json(self.config)
            if time.monotonic() >= next_frame:
                for pub in self.pubs:
                    pub.send(self.jpg)
                next_frame = time.monotonic() + 1/30

    def close(self):
        self.stop.set()
        self.thread.join()
        self.rep.close(0)
        for pub in self.pubs:
            pub.close(0)
        self.context.term()


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='record-test-')
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def settings(self, cameras, port, max_gb=.02):
        return dict(spool=str(self.root/'spool'), max_cache_gb=max_gb, min_free_gb=0,
                    image_fps=20, cameras=cameras, camera_config_port=port)

    def test_three_local_streams_and_telemetry_end_to_end(self):
        cameras = FakeCameras()
        context = mp.get_context('spawn')
        receive, send = context.Pipe(duplex=False)
        stop = context.Event()
        address = str(self.root/'record.sock')
        worker = context.Process(target=run_worker, args=(self.settings(list(CAMERAS), cameras.port),
                                 {'instruction': 'offline test'}, address, send, stop))
        sink = None
        try:
            worker.start()
            send.close()
            self.assertTrue(receive.poll(10))
            start = receive.recv()
            self.assertEqual(start['state'], 'ready', start)
            sink = Telemetry(address)
            deadline = time.monotonic() + 1.2
            while time.monotonic() < deadline:
                sink.emit('arm_state', {'state': {'received_at': 1.0, 'q': [0]*14}})
                sink.emit('console', {'text': '测试日志\n'})
                time.sleep(.01)
            stop.set()
            worker.join(timeout=10)
            self.assertFalse(worker.is_alive())
            manifest = verify_episode(start['episode'])
            for name in CAMERAS:
                self.assertGreater(manifest['frame_counts'][name], 10)
                self.assertLess(manifest['frame_counts'][name], 35)
            self.assertGreater(manifest['stream_counts']['arm_state'], 20)
            self.assertIn('测试日志', (Path(start['episode'])/'console.log').read_text())
        finally:
            stop.set()
            if worker.is_alive(): worker.terminate()
            worker.join(timeout=3)
            if sink: sink.close()
            receive.close()
            cameras.close()

    def run_supervisor(self, settings, script):
        config = self.root/'recording.json'
        config.write_text(json.dumps(settings))
        return subprocess.run([sys.executable, '-m', 'g1d_pico.recording', 'run', '--config', str(config),
                               '--instruction', 'offline test', '--', sys.executable, '-u', '-c', script],
                              cwd=PROJECT, capture_output=True, text=True, timeout=25)

    def test_supervisor_captures_console_exit_code_and_does_not_upload(self):
        result = self.run_supervisor(self.settings([], 60000),
            "from g1d_pico.recording.telemetry import emit; "
            "emit('control_cycle', {'armed': False}); print('CONTROL_FINISHED'); raise SystemExit(7)")
        self.assertEqual(result.returncode, 7, result.stderr + result.stdout)
        episode, = (self.root/'spool').glob('episode_*')
        manifest = verify_episode(episode)
        self.assertIn('CONTROL_FINISHED', (episode/'console.log').read_text())
        self.assertEqual(manifest['stream_counts']['session_end'], 1)
        self.assertFalse((self.root/'spool'/'.incoming').exists())

    def test_quota_stop_does_not_interrupt_control_subprocess(self):
        cameras = FakeCameras(jpg_size=140_000)
        try:
            result = self.run_supervisor(self.settings(list(CAMERAS), cameras.port, .01),
                "import time; print('START'); time.sleep(1.8); print('CONTROL_STILL_RUNNING_AFTER_QUOTA')")
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertIn('CONTROL_STILL_RUNNING_AFTER_QUOTA', result.stdout)
            self.assertIn('CACHE_LIMIT', result.stdout)
            episode, = (self.root/'spool').glob('episode_*')
            manifest = verify_episode(episode)
            self.assertEqual(manifest['recording_stop_reason'], 'CACHE_LIMIT')
            self.assertLess(sum(p.stat().st_size for p in episode.rglob('*') if p.is_file()), 10_000_000)
        finally:
            cameras.close()

    def test_sigint_flushes_recording_after_control_exits(self):
        config = self.root/'recording.json'
        config.write_text(json.dumps(self.settings([], 60000)))
        script = "import time; print('CONTROL_READY', flush=True); time.sleep(20)"
        proc = subprocess.Popen([sys.executable, '-u', '-m', 'g1d_pico.recording', 'run', '--config', str(config),
                                '--', sys.executable, '-u', '-c', script], cwd=PROJECT,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            for _ in range(20):
                line = proc.stdout.readline()
                if 'CONTROL_READY' in line: break
            else: self.fail('control never started')
            proc.send_signal(signal.SIGINT)
            output, _ = proc.communicate(timeout=15)
            episode, = (self.root/'spool').glob('episode_*')
            self.assertTrue(verify_episode(episode)['finalized'], output)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            proc.stdout.close()

    def test_first_interrupt_reaches_parent_before_its_bridge_child(self):
        config = self.root/'recording.json'
        config.write_text(json.dumps(self.settings([], 60000)))
        marker = self.root/'bridge_received_sigint'
        bridge_script = ("import signal,time,pathlib; "
                         f"signal.signal(signal.SIGINT,lambda *a:pathlib.Path({str(marker)!r}).write_text('bad')); "
                         "print('BRIDGE_READY',flush=True); time.sleep(20)")
        script = ("import subprocess,sys,time\n"
                  f"p=subprocess.Popen([sys.executable,'-u','-c',{bridge_script!r}])\n"
                  "try:\n time.sleep(20)\n"
                  "except KeyboardInterrupt:\n p.terminate(); p.wait(); print('ORDERLY_PARENT_STOP',flush=True)\n")
        proc = subprocess.Popen([sys.executable, '-u', '-m', 'g1d_pico.recording', 'run',
                                 '--config', str(config), '--', sys.executable, '-u', '-c', script],
                                cwd=PROJECT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            for _ in range(20):
                if 'BRIDGE_READY' in proc.stdout.readline(): break
            else: self.fail('bridge fixture never started')
            proc.send_signal(signal.SIGINT)
            output, _ = proc.communicate(timeout=15)
            self.assertEqual(proc.returncode, 0, output)
            self.assertIn('ORDERLY_PARENT_STOP', output)
            self.assertFalse(marker.exists())
            episode, = (self.root/'spool').glob('episode_*')
            self.assertEqual(verify_episode(episode)['control_exit_code'], 0)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            proc.stdout.close()


if __name__ == '__main__':
    unittest.main()
