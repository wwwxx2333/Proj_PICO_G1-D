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

from project_fixture import copy_runtime_project

from g1d_pico.recording.recorder import CAMERAS, run_worker
from g1d_pico.recording.storage import verify_episode
from g1d_pico.recording.layout import Layout
from g1d_pico.recording.telemetry import Telemetry

PROJECT = Path(__file__).resolve().parents[1]

# Synthetic operator for subprocess tests: local sockets only, no PICO or DDS.
CONTROL_HELPER = r"""
import json, os, pathlib, socket, time, uuid
from g1d_pico.recording.telemetry import emit
from g1d_pico.recording.runner import load_settings
from g1d_pico.config_paths import RECORDING_CONFIG
spool = pathlib.Path(load_settings(RECORDING_CONFIG)['spool'])
def wait_until(predicate, timeout=10):
    end = time.monotonic()+timeout
    while not predicate():
        if time.monotonic()>end: raise RuntimeError('fixture wait timed out')
        time.sleep(.02)
def control_request(value):
    address = os.environ['G1D_RECORD_CONTROL_SOCKET']
    local = str(pathlib.Path(address).parent / ('test-'+uuid.uuid4().hex[:8]+'.sock'))
    client = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    client.bind(local)
    client.settimeout(3)
    try:
        client.sendto(json.dumps({**value, 'time':time.monotonic()}).encode(), address)
        return json.loads(client.recv(4096))
    finally:
        client.close()
        pathlib.Path(local).unlink(missing_ok=True)
def control_ready(ready=True):
    return control_request({'op':'control_status','ready':ready,'reason':'fixture homing pending'})
def record_toggle(identity=None, action='stop'):
    return control_request({'op':'record','action':action,'id':identity or uuid.uuid4().hex})['result']
def start_recording():
    wait_until(lambda: control_ready()['ready'])
    assert record_toggle(action='start') == 'starting'
    wait_until(lambda: pathlib.Path(os.environ['G1D_RECORD_SOCKET']).exists())
    time.sleep(.1)
"""



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
        self.project = copy_runtime_project(self.root / 'project')

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

    def run_supervisor(self, settings, script, start=True):
        config = self.project/'config/recording.jsonc'
        config.write_text('// JSONC recording fixture\n' + json.dumps(settings))
        return subprocess.run([sys.executable, '-m', 'g1d_pico.recording', 'run',
                               '--instruction', 'offline test', '--', sys.executable, '-u', '-c',
                               CONTROL_HELPER + ('start_recording()\n' if start else '') + script],
                              cwd=self.project, capture_output=True, text=True, timeout=25)

    def test_prepare_before_press_and_reject_start_during_homing(self):
        script = r"""
wait_until(lambda: control_ready(False)['state'] == 'waiting')
assert list(spool.glob('episode_*')) == []
assert record_toggle(action='start') == 'not_ready'
time.sleep(.2)
assert list(spool.glob('episode_*')) == []
start_recording()
print('AFTER_HOME_RECORDED', flush=True)
time.sleep(.2)
"""
        result = self.run_supervisor(self.settings([], 60000), script, start=False)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assertIn('REC NOT_READY', result.stdout)
        episode, = (self.root/'spool').glob('episode_*')
        verify_episode(episode)

    def test_camera_setup_failure_is_reported_before_l3_without_stopping_control(self):
        cameras = FakeCameras()
        cameras.config['head_camera']['enable_zmq'] = False
        try:
            script = "wait_until(lambda: control_ready()['state'] == 'error')\nprint('CONTROL_CONTINUES')"
            result = self.run_supervisor(self.settings(['head'], cameras.port), script, start=False)
            self.assertEqual(result.returncode, 2, result.stdout+result.stderr)
            self.assertIn('ZMQ is disabled', result.stdout)
            self.assertIn('CONTROL_CONTINUES', result.stdout)
            self.assertEqual(list((self.root/'spool').glob('episode_*')), [])
        finally:
            cameras.close()

    def test_supervisor_captures_console_exit_code_and_does_not_upload(self):
        metric = 'ARMED L=(+0.30,+0.00) R=(+0.00,+0.00) -> vx=+0.300 m/s'
        result = self.run_supervisor(self.settings([], 60000),
            "from g1d_pico.recording.telemetry import emit; "
            "emit('control_cycle', {'armed': False}); "
            f"print({metric!r}); print('CONTROL_FINISHED'); raise SystemExit(7)")
        self.assertEqual(result.returncode, 7, result.stderr + result.stdout)
        episode, = (self.root/'spool').glob('episode_*')
        manifest = verify_episode(episode)
        self.assertIn('CONTROL_FINISHED', (episode/Layout(manifest['schema']).console).read_text())
        self.assertIn(metric, (episode/Layout(manifest['schema']).console).read_text())
        log, = (self.project/'logs/runtime').glob('run_*.log')
        self.assertIn(metric, log.read_text())
        self.assertNotIn(metric, result.stdout)
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
        config = self.project/'config/recording.jsonc'
        config.write_text('// JSONC recording fixture\n' + json.dumps(self.settings([], 60000)))
        script = CONTROL_HELPER + "start_recording()\nprint('CONTROL_READY', flush=True); time.sleep(20)"
        proc = subprocess.Popen([sys.executable, '-u', '-m', 'g1d_pico.recording', 'run',
                                '--', sys.executable, '-u', '-c', script], cwd=self.project,
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
        config = self.project/'config/recording.jsonc'
        config.write_text('// JSONC recording fixture\n' + json.dumps(self.settings([], 60000)))
        marker = self.root/'bridge_received_sigint'
        bridge_script = ("import signal,time,pathlib; "
                         f"signal.signal(signal.SIGINT,lambda *a:pathlib.Path({str(marker)!r}).write_text('bad')); "
                         "print('BRIDGE_READY',flush=True); time.sleep(20)")
        script = CONTROL_HELPER + "start_recording()\n" + ("import subprocess,sys,time\n"
                  f"p=subprocess.Popen([sys.executable,'-u','-c',{bridge_script!r}])\n"
                  "try:\n time.sleep(20)\n"
                  "except KeyboardInterrupt:\n p.terminate(); p.wait(); print('ORDERLY_PARENT_STOP',flush=True)\n")
        proc = subprocess.Popen([sys.executable, '-u', '-m', 'g1d_pico.recording', 'run',
                                 '--', sys.executable, '-u', '-c', script],
                                cwd=self.project, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
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

    def test_no_l3_creates_no_episode_even_when_a_b_events_are_received(self):
        result = self.run_supervisor(self.settings([], 60000),
            "emit('control_cycle', {'armed': True}); print('NO_RECORDING'); "
            "emit('control_cycle', {'armed': False})", start=False)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assertIn('REC PREPARING', result.stdout)
        self.assertEqual(list((self.root/'spool').glob('episode_*')), [])

    def test_two_manual_episodes_exclude_idle_and_reject_duplicate_request(self):
        script = r"""
print('IDLE_BEFORE', flush=True)
start_recording()
emit('control_cycle', {'armed': True})
print('FIRST_EPISODE', flush=True)
time.sleep(.2)
emit('control_cycle', {'armed': False})
print('B_DOES_NOT_STOP_RECORDING', flush=True)
time.sleep(.15)
identity = uuid.uuid4().hex
assert record_toggle(identity) == 'saving'
assert record_toggle(identity) == 'saving'
wait_until(lambda: len(list(spool.glob('episode_*/manifest.json'))) == 1)
wait_until(lambda: not pathlib.Path(os.environ['G1D_RECORD_SOCKET']).exists())
print('IDLE_BETWEEN', flush=True)
time.sleep(.3)
start_recording()
print('SECOND_EPISODE', flush=True)
emit('control_cycle', {'armed': True})
time.sleep(.2)
assert record_toggle() == 'saving'
wait_until(lambda: len(list(spool.glob('episode_*/manifest.json'))) == 2)
wait_until(lambda: not pathlib.Path(os.environ['G1D_RECORD_SOCKET']).exists())
print('CONTROL_STILL_ALIVE', flush=True)
time.sleep(.2)
"""
        result = self.run_supervisor(self.settings([], 60000), script, start=False)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        episodes = sorted((self.root/'spool').glob('episode_*'))
        self.assertEqual(len(episodes), 2)
        logs = []
        session_ids = set()
        for episode in episodes:
            manifest = verify_episode(episode)
            self.assertEqual(manifest['recording_stop_reason'], 'L3_STOP')
            self.assertEqual(sum(manifest['sequence_gaps'].values()), 0)
            logs.append((episode/Layout(manifest['schema']).console).read_text())
            metadata = json.loads((episode/'metadata.json').read_text())
            session_ids.add(metadata['session_id'])
            rows = [json.loads(line) for line in ''.join((episode/name).read_text() for name in Layout(manifest['schema']).telemetry_files(episode)).splitlines()]
            self.assertTrue(all(metadata['recording_started_monotonic_s'] <= row['emitted_monotonic_s']
                                <= manifest['recording_requested_stop_monotonic_s'] for row in rows))
        self.assertEqual(len(session_ids), 1)
        self.assertIn('FIRST_EPISODE', logs[0])
        self.assertIn('B_DOES_NOT_STOP_RECORDING', logs[0])
        self.assertIn('SECOND_EPISODE', logs[1])
        self.assertNotIn('IDLE_', ''.join(logs))
        self.assertIn('CONTROL_STILL_ALIVE', result.stdout)
        self.assertEqual(result.stdout.count('REC SAVED'), 2)

    def test_start_failure_does_not_exit_control(self):
        script = "wait_until(lambda: control_ready()['ready'])\nassert record_toggle(action='start') == 'starting'\ntime.sleep(.6)\nprint('CONTROL_SURVIVED_FULL_CACHE')"
        settings = self.settings([], 60000, .01)
        (self.root/'spool').mkdir()
        (self.root/'spool'/'existing.bin').write_bytes(b'x'*10_000_000)
        result = self.run_supervisor(settings, script, start=False)
        self.assertIn('CONTROL_SURVIVED_FULL_CACHE', result.stdout)
        self.assertIn('CACHE_LIMIT', result.stdout)
        self.assertIn('REC ERROR', result.stdout)
        self.assertNotEqual(result.returncode, 0)  # The failed recording is not silently successful.
        self.assertEqual(list((self.root/'spool').glob('episode_*')), [])



if __name__ == '__main__':
    unittest.main()
