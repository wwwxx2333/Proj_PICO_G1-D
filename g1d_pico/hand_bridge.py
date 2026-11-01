"""Local, timestamped pipe transport to one SDK2 Dex3-1 process per hand."""
from dataclasses import dataclass
import os
import sys
from pathlib import Path
import subprocess
import threading
import time
import numpy as np
from .recording.telemetry import emit


@dataclass(frozen=True)
class HandState:
    received_at: float  # PC2 source CLOCK_MONOTONIC, not Python receipt time.
    status: int          # 0 not owned, 1 tracking, 2 holding.
    q: np.ndarray
    sent_at: float = 0.0  # Last successful DDS Write; not an execution ACK.
    sent_q: np.ndarray | None = None
    requested_q: np.ndarray | None = None
    blocked_mask: int = 0
    opening_mask: int = 0
    limited_mask: int = 0


def parse_hand_state(fields):
    """Decode versioned telemetry without replacing its source timestamps."""
    if not fields:
        raise ValueError('empty hand state packet')
    if fields[0] == 'HANDSTATE':
        raise ValueError('outdated Dex3 bridge; rebuild with deploy/build_on_pc2.sh '
                         'and sync Python together with the C++ bridge')
    if fields[0] != 'HANDSTATE2' or len(fields) != 28:
        raise ValueError('invalid HANDSTATE2 packet')
    timestamp, status, sent_at = float(fields[1]), int(fields[2]), float(fields[10])
    q = np.array([float(v) for v in fields[3:10]])
    sent = np.array([float(v) for v in fields[11:18]])
    requested = np.array([float(v) for v in fields[18:25]])
    masks = tuple(int(v) for v in fields[25:28])
    if (not np.isfinite([timestamp, sent_at]).all() or timestamp <= 0 or sent_at < 0
            or not all(np.isfinite(values).all() for values in (q, sent, requested))
            or status not in (0, 1, 2) or any(v < 0 or v > 127 for v in masks)
            or masks[0] & masks[1] or (masks[0] | masks[1]) & 1):
        raise ValueError('non-finite or invalid hand telemetry')
    return HandState(timestamp, status, q, sent_at, sent, requested, *masks)

class HandBridge:
    def __init__(self, binary: Path, interface: str, side: str):
        if side not in ('left', 'right'):
            raise ValueError('side must be left or right')
        if not binary.is_file():
            raise FileNotFoundError(f'Compile Dex3-1 hand bridge first: {binary}')
        runtime_lib = binary.resolve().parent / 'lib'
        if not runtime_lib.is_dir():
            runtime_lib = Path(__file__).resolve().parents[1] / 'bridge/lib'
        environment = os.environ.copy()
        environment['LD_LIBRARY_PATH'] = os.pathsep.join(filter(None, (
            str(runtime_lib), environment.get('LD_LIBRARY_PATH'))))
        self.process = subprocess.Popen([str(binary), interface, side], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, env=environment)
        os.set_blocking(self.process.stdin.fileno(), False)
        self.side = side
        self._closed = False
        self._lock = threading.Lock()
        self._state = None
        self._error = None
        self._reader = threading.Thread(target=self._read_states, daemon=True)
        self._reader.start()

    def _read_states(self):
        try:
            for raw in self.process.stdout:
                fields = raw.decode("utf-8", errors="replace").split()
                if not fields or not fields[0].startswith('HANDSTATE'):
                    sys.stderr.write(raw.decode('utf-8', errors='replace'))
                    sys.stderr.flush()
                    continue
                state = parse_hand_state(fields)
                with self._lock:
                    self._state = state
                emit('hand_state', {'side': self.side, 'state': state})
        except (OSError, UnicodeError, ValueError) as exc:
            self._error = str(exc)

    def state(self):
        if self._error:
            raise RuntimeError(self._error)
        if self.process.poll() is not None:
            raise RuntimeError(f'{self.side} Dex3 bridge exited: {self.process.returncode}')
        with self._lock:
            return self._state

    def wait_ready(self, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.state()
            if state is not None:
                return state
            time.sleep(0.02)
        raise RuntimeError(f'No {self.side} Dex3 state received on PC2 eth0')

    def _send(self, line):
        if self.process.poll() is not None:
            raise RuntimeError(f'{self.side} Dex3 bridge exited')
        data = (line + '\n').encode('ascii')
        if os.write(self.process.stdin.fileno(), data) != len(data):
            raise RuntimeError(f'{self.side} Dex3 command pipe incomplete')
        if os.getenv('G1D_RECORD_SOCKET'):
            emit('bridge_command', {'bridge': 'dex3_' + self.side, 'protocol_line': line})

    def enable(self, timestamp):
        self._send(f'ARM {timestamp:.9f}')

    def keepalive(self, timestamp):
        """Renew operator input while keeping the C++ takeover target intact."""
        self._send(f'KEEPALIVE {timestamp:.9f}')

    def target(self, q, timestamp):
        self._send(f'TARGET {timestamp:.9f} ' + ' '.join(f'{x:.9f}' for x in q))

    def hold(self):
        self._send('HOLD')

    def release(self):
        """Explicitly stop hand-command publication."""
        self._send('RELEASE')

    def close(self):
        if self._closed:
            return
        self._closed = True
        escalation = 'none'
        try:
            self.hold()
        except (OSError, RuntimeError):
            pass
        self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            escalation = 'terminate'
            self.process.terminate()
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                escalation = 'kill'
                self.process.kill()
                self.process.wait()
        self._reader.join(timeout=1)
        self.process.stdout.close()
        result = {'bridge': 'dex3_' + self.side, 'exit_code': self.process.returncode,
                  'escalation': escalation, 'phase': 'shutdown'}
        emit('bridge_exit', result)
        print(f"{self.side} Dex3 bridge shutdown: exit={self.process.returncode}, "
              f"escalation={escalation}", flush=True)
        if self.process.returncode != 0:
            raise RuntimeError(f'{self.side} Dex3 shutdown failed: exit={self.process.returncode}')
