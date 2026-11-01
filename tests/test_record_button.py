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
from g1d_pico.recording.button import RecordPress, RecordingButton, RecordingRequests
from g1d_pico.recording.storage import EpisodeStore, verify_episode


class PressTests(unittest.TestCase):
    def test_press_starts_immediately_and_same_hold_cannot_stop(self):
        button = RecordPress()
        button.update(False, 10, 10, False)
        self.assertEqual(button.update(True, 10.1, 10.1, False), 'start')
        self.assertFalse(any(button.update(True, 10.1+i/10, 10.1+i/10, True)
                             for i in range(1, 25)))
        button.update(False, 12.6, 12.6, True)
        self.assertIsNone(button.update(True, 12.7, 12.7, True))
        actions = [button.update(True, 12.7+i/10, 12.7+i/10, True) for i in range(1, 25)]
        self.assertEqual(actions.count('stop'), 1)

    def test_short_press_during_recording_does_not_stop(self):
        button = RecordPress()
        button.update(False, 10, 10, True)
        button.update(True, 10.1, 10.1, True)
        self.assertIsNone(button.update(False, 10.2, 10.2, True))
        self.assertTrue(any('STOP_CANCELLED' in text for text in button.messages))

    def test_gap_cancels_stop_and_requires_release(self):
        button = RecordPress()
        button.update(False, 10, 10, True)
        button.update(True, 10.1, 10.1, True)
        self.assertIsNone(button.update(True, 10.1, 10.5, True))
        self.assertTrue(any('输入中断' in text for text in button.messages))
        self.assertFalse(any(button.update(True, 11+i/10, 11+i/10, True) for i in range(20)))

    def test_held_at_connection_and_repeated_snapshot_do_not_trigger(self):
        button = RecordPress()
        self.assertFalse(any(button.update(True, 10+i/10, 10+i/10, False) for i in range(20)))
        button.update(False, 12, 12, True)
        button.update(True, 12.1, 12.1, True)
        self.assertFalse(any(button.update(True, 12.1, 12.1+i/100, True) for i in range(120)))

    def test_contact_bounce_does_not_generate_second_action(self):
        button = RecordPress()
        button.update(False, 10, 10, False)
        self.assertEqual(button.update(True, 10.1, 10.1, False), 'start')
        button.update(False, 10.15, 10.15, False)
        self.assertIsNone(button.update(True, 10.2, 10.2, False))
        self.assertTrue(any('DEBOUNCE' in text for text in button.messages))

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
            button.step(SimpleNamespace())
            button.close()
            constructor.assert_not_called()


class RequestTests(unittest.TestCase):
    def test_start_then_hold_stop_with_deflected_stick_and_no_control_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            server = RecordingRequests(Path(directory) / 'control.sock')
            messages, actions = [], []
            button = RecordingButton(str(server.path), messages.append)
            state = {'state': 'waiting'}
            def request(action):
                actions.append(action)
                state['state'] = 'recording' if action == 'start' else 'saving'
                return state['state']
            try:
                base = time.monotonic()
                for i in range(55):
                    now = base+i*.1
                    # First press remains held across the recorder's state change.
                    # Second press stops despite displaced axes and pending homing.
                    sample = SimpleNamespace(record_button=i not in (0, 30),
                                             left_x=.9, left_y=-.8, received_at=now)
                    with patch('g1d_pico.recording.button.time.monotonic', return_value=now):
                        button.step(sample, control_ready=i < 30, preparation='homing')
                        server.poll(request, Mock(), lambda: dict(state))
                self.assertEqual(actions, ['start', 'stop'])
                self.assertTrue(any('STOP_HOLD' in text for text in messages))
                self.assertIsNone(button.pending)
            finally:
                button.close()
                server.close()

    def test_retry_ack_deduplicates_and_busy_request_is_not_replayed(self):
        with tempfile.TemporaryDirectory() as directory:
            server = RecordingRequests(Path(directory) / 'control.sock')
            client = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            client.bind(str(Path(directory) / 'client.sock'))
            client.settimeout(1)
            try:
                toggle = Mock(side_effect=['starting', 'busy'])
                for identity in ('a'*32, 'a'*32, 'b'*32, 'b'*32):
                    client.sendto(json.dumps({'id': identity, 'op': 'record', 'action': 'start',
                                             'time': time.monotonic()}).encode(), str(server.path))
                    server.poll(toggle, Mock(), lambda: {'state': 'waiting'})
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
                    sample = SimpleNamespace(record_button=i>0, left_x=.9, left_y=-.8, received_at=now)
                    with patch('g1d_pico.recording.button.time.monotonic', return_value=now):
                        button.step(sample)
                        server.poll(toggle, Mock(), lambda: {'state': 'waiting'})
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
