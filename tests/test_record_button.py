"""L3 debounce, real adapter mapping and local acknowledged control; no robot."""
import asyncio
from contextlib import nullcontext
import json
import os
from pathlib import Path
import socket
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import numpy as np

from g1d_pico.input import FreshTeleVuer, TeleVuer
from g1d_pico.recording.button import LongPress, RecordingButton, RecordingRequests
from g1d_pico.recording.storage import EpisodeStore, verify_episode


class LongPressTests(unittest.TestCase):
    def test_one_hold_one_action_and_release_required(self):
        button = LongPress()
        self.assertFalse(button.update(False, 0, 0, 10, 10, .15))
        results = [button.update(True, 0, 0, 10+i/10, 10+i/10, .15) for i in range(1, 32)]
        self.assertEqual(sum(results), 1)
        button.update(False, 0, 0, 14, 14, .15)
        results = [button.update(True, 0, 0, 14+i/10, 14+i/10, .15) for i in range(1, 22)]
        self.assertEqual(sum(results), 1)

    def test_held_at_start_or_after_disconnect_needs_release(self):
        for disconnect in (False, True):
            button = LongPress()
            if disconnect:
                button.update(False, 0, 0, 10, 10, .15)
                button.update(True, 0, 0, 10.1, 10.1, .15)
                button.update(True, 0, 0, 10.1, 10.5, .15)
            self.assertFalse(any(button.update(True, 0, 0, 20+i/10, 20+i/10, .15)
                                 for i in range(30)))

    def test_deflection_cancels_and_does_not_trigger_on_recentering_while_held(self):
        button = LongPress()
        button.update(False, 0, 0, 10, 10, .15)
        button.update(True, 0, 0, 10.1, 10.1, .15)
        button.update(True, .3, 0, 10.2, 10.2, .15)
        self.assertFalse(any(button.update(True, 0, 0, 10.3+i/10, 10.3+i/10, .15)
                             for i in range(20)))

    def test_short_press_or_repeated_snapshot_cannot_trigger(self):
        button = LongPress()
        button.update(False, 0, 0, 10, 10, .15)
        self.assertFalse(button.update(True, 0, 0, 10.1, 10.1, .15))
        self.assertFalse(any(button.update(True, 0, 0, 10.1, 10.1+i/100, .15)
                             for i in range(120)))

    def test_real_vuer_l3_field_is_separate_from_a_b_x_y_and_axes(self):
        class Snapshot(list):
            def get_lock(self): return nullcontext()
        viewer = object.__new__(FreshTeleVuer)
        viewer._stick_snapshot = Snapshot([0.] * 10)
        viewer._pose_snapshot = Snapshot([0.] * 37)
        event = SimpleNamespace(value={
            'left': np.eye(4).ravel(order='F'), 'right': np.eye(4).ravel(order='F'),
            'leftState': {'thumbstick': True, 'thumbstickValue': [.03, -.02]},
            'rightState': {'thumbstick': False, 'thumbstickValue': [0, 0]}})
        with patch.object(TeleVuer, 'on_controller_move', new_callable=AsyncMock):
            asyncio.run(viewer.on_controller_move(event, None))
        sample = viewer.read_sticks()
        self.assertTrue(sample.record_button)
        self.assertFalse(sample.start_button or sample.stop_button or sample.home_button or sample.mode_button)
        self.assertEqual((sample.left_x, sample.left_y), (.03, -.02))

    def test_no_recording_launcher_creates_no_socket(self):
        with patch.dict(os.environ, {}, clear=True), patch('socket.socket') as constructor:
            button = RecordingButton()
            button.step(SimpleNamespace(), .15)
            button.close()
            constructor.assert_not_called()


class RequestTests(unittest.TestCase):
    def test_retry_ack_deduplicates_and_busy_request_is_not_replayed(self):
        with tempfile.TemporaryDirectory() as directory:
            server = RecordingRequests(Path(directory) / 'control.sock')
            client = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            client.bind(str(Path(directory) / 'client.sock'))
            client.settimeout(1)
            try:
                toggle = Mock(side_effect=['starting', 'busy'])
                for identity in ('a'*32, 'a'*32, 'b'*32, 'b'*32):
                    client.sendto(json.dumps({'id': identity, 'op': 'toggle',
                                             'time': time.monotonic()}).encode(), str(server.path))
                    server.poll(toggle)
                    reply = json.loads(client.recv(4096))
                    self.assertEqual(reply['result'], 'starting' if identity[0] == 'a' else 'busy')
                self.assertEqual(toggle.call_count, 2)
            finally:
                client.close()
                server.close()

    def test_real_button_client_sends_once_and_receives_ack(self):
        with tempfile.TemporaryDirectory() as directory:
            server = RecordingRequests(Path(directory) / 'control.sock')
            messages = []
            button = RecordingButton(str(server.path), messages.append)
            toggle = Mock(return_value='starting')
            try:
                # Monotonic time is injected only in the client, no robot fixtures.
                base = time.monotonic()
                for i in range(14):
                    now = base+i*.1
                    sample = SimpleNamespace(record_button=i>0, left_x=0, left_y=0, received_at=now)
                    with patch('g1d_pico.recording.button.time.monotonic', return_value=now):
                        button.step(sample, .15)
                        server.poll(toggle)
                self.assertEqual(toggle.call_count, 1)
                self.assertIsNone(button.pending)
                self.assertTrue(any('REC REQUEST' in text for text in messages))
            finally:
                button.close()
                server.close()

    def test_segment_counter_baseline_excludes_intentional_idle_period(self):
        with tempfile.TemporaryDirectory() as directory:
            for initial in (100, 1000):
                store = EpisodeStore(directory, 10_000_000, 0, {'recording_mode': 'manual_l3'})
                for seq, dropped in ((initial, 50), (initial+3, 52)):
                    store.event({'stream': 'state', 'producer': 'p', 'seq': seq,
                                 'dropped_total': dropped, 'data': {}})
                store.finish('L3_STOP')
                manifest = verify_episode(store.path)
                self.assertEqual(manifest['sequence_gaps']['p'], 2)
                self.assertEqual(manifest['producer_reported_drops']['p'], 2)
                self.assertEqual(manifest['sequence_baselines']['p'], initial-1)
