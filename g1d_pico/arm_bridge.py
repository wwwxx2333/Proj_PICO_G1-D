"""Local pipe transport to the independent G1-D arm DDS process."""

from dataclasses import dataclass, field
import os
from pathlib import Path
import subprocess
import threading
import time
import numpy as np
from .recording.telemetry import emit
from .bridge_contract import require_current_limits


@dataclass(frozen=True)
class ArmState:
    received_at: float  # Original PC2 CLOCK_MONOTONIC time of valid DDS feedback.
    status: int        # 0=no ownership, 1=tracking permitted, 2=holding after disarm.
    q: np.ndarray
    waist: np.ndarray = field(default_factory=lambda: np.zeros(2))

    waist_target: float = 0.0
    waist_velocity: float = 0.0
    waist_limited: bool = False
    pitch_target: float = 0.0
    pitch_velocity: float = 0.0
    pitch_limited: bool = False
    pitch_received_at: float = 0.0  # Dedicated rt/waistpitchstate time, never lowstate time.
    pitch_limit: int = 0  # Bitmask: 1=angle min, 2=angle max, 4=following error.
    pitch_following: int = 0  # 0=following, 1=wait centre, 2=wait new push.
    pitch_requested_rate: float = 0.0
    pitch_stall_elapsed_s: float = 0.0
    pitch_stall_error_rad: float = 0.0

    def pitch_diagnostics(self):
        reasons = [name for bit, name in ((1, 'ANGLE_MIN'), (2, 'ANGLE_MAX'), (4, 'FOLLOW_ERROR'))
                   if self.pitch_limit & bit]
        result = 'PITCH_LIMIT=' + ('+'.join(reasons) if reasons else 'NONE')
        if self.pitch_following:
            result += ' PITCH_NOT_FOLLOWING/' + ('WAIT_CENTER' if self.pitch_following == 1 else 'WAIT_PUSH')
        return result

    def is_fresh(self, now):
        return 0 < self.received_at <= now and now - self.received_at <= 0.25

    def pitch_is_fresh(self, now):
        return 0 < self.pitch_received_at <= now and now - self.pitch_received_at <= 0.25


def parse_arm_state(fields):
    if fields and fields[0] in ('STATE', 'STATE2', 'STATE3', 'STATE4', 'STATE5'):
        raise ValueError('old arm bridge protocol; rebuild with deploy/build_on_pc2.sh')
    if not fields or fields[0] != 'STATE6' or len(fields) != 31:
        raise ValueError('invalid STATE6 arm packet')
    timestamp, status = float(fields[1]), int(fields[2])
    values = np.array([float(v) for v in fields[3:21]])
    limited = int(fields[21])
    pitch = np.array([float(v) for v in fields[22:24]])
    pitch_limited = int(fields[24])
    pitch_timestamp = float(fields[25])
    pitch_limit, pitch_following = int(fields[26]), int(fields[27])
    diagnostics = np.array([float(v) for v in fields[28:31]])
    if (not np.isfinite(timestamp) or timestamp <= 0 or not np.isfinite(values).all()
            or not np.isfinite(pitch).all() or pitch_limited not in (0, 1)
            or not np.isfinite(pitch_timestamp) or pitch_timestamp <= 0
            or pitch_limit not in (0, 1, 2, 4, 5, 6) or pitch_following not in (0, 1, 2)
            or bool(pitch_limit) != bool(pitch_limited)
            or not np.isfinite(diagnostics).all() or abs(diagnostics[0]) > 0.15
            or diagnostics[1] < 0
            or status not in (0, 1, 2) or limited not in (0, 1)):
        raise ValueError('non-finite or invalid arm/waist state')
    return ArmState(timestamp, status, values[:14], values[14:16],
                    values[16], values[17], bool(limited), pitch[0], pitch[1], bool(pitch_limited),
                    pitch_timestamp, pitch_limit, pitch_following, *diagnostics)


class ArmBridge:
    def __init__(self, binary: Path, interface: str, speed_rad_s=0.30, *,
                 yaw_limit_rad=0.5235987756, yaw_speed_rad_s=0.10, yaw_accel_rad_s2=0.20,
                 pitch_min_rad=0.0, pitch_max_rad=0.5235987756,
                 pitch_speed_rad_s=0.08, pitch_accel_rad_s2=0.16,
                 pitch_max_error_rad=0.10, pitch_stall_timeout_s=1.5,
                 pitch_min_progress_rad=0.005):
        if not binary.is_file():
            raise FileNotFoundError(f"Compile the G1-D arm bridge first: {binary}")
        runtime_lib = binary.resolve().parent / "lib"
        if not runtime_lib.is_dir():
            runtime_lib = Path(__file__).resolve().parents[1] / "bridge/lib"
        environment = os.environ.copy()
        environment["LD_LIBRARY_PATH"] = os.pathsep.join(filter(None, (
            str(runtime_lib), environment.get("LD_LIBRARY_PATH"))))
        parameters = (speed_rad_s, yaw_limit_rad, yaw_speed_rad_s, yaw_accel_rad_s2,
                      pitch_min_rad, pitch_max_rad, pitch_speed_rad_s, pitch_accel_rad_s2,
                      pitch_max_error_rad, pitch_stall_timeout_s, pitch_min_progress_rad)
        require_current_limits(binary, environment)
        self.process = subprocess.Popen([str(binary), interface, *map(str, parameters)], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, env=environment)
        os.set_blocking(self.process.stdin.fileno(), False)
        self._lock = threading.Lock()
        self._state = None
        self._error = None
        self._reader = threading.Thread(target=self._read_states, daemon=True)
        self._reader.start()

    def _read_states(self):
        try:
            for raw in self.process.stdout:
                fields = raw.decode("ascii").split()
                # SDK diagnostics may also be printed on stdout.
                if not fields or not fields[0].startswith('STATE'):
                    continue
                state = parse_arm_state(fields)
                with self._lock:
                    self._state = state
                emit('arm_state', {'state': state})
        except (OSError, UnicodeError, ValueError) as exc:
            self._error = str(exc)

    def state(self):
        if self._error:
            raise RuntimeError(self._error)
        if self.process.poll() is not None:
            if self.process.returncode == 2:
                raise RuntimeError("Arm bridge rejected startup parameters; check config and rebuild "
                                   "for yaw/pitch support with deploy/build_on_pc2.sh")
            raise RuntimeError(f"Arm bridge exited: {self.process.returncode}")
        with self._lock:
            return self._state

    def wait_ready(self, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.state()
            now = time.monotonic()
            if state and state.is_fresh(now) and state.pitch_is_fresh(now):
                return state
            time.sleep(0.02)
        raise RuntimeError("No fresh upper-body feedback on PC2 eth0; both rt/lowstate and "
                           "rt/waistpitchstate are required. No arm/pitch takeover has started")

    def _send(self, text):
        if self.process.poll() is not None:
            raise RuntimeError("arm process has exited")
        data = (text + "\n").encode("ascii")
        if os.write(self.process.stdin.fileno(), data) != len(data):
            raise RuntimeError("arm command pipe did not accept the complete command")
        emit('bridge_command', {'bridge': 'arms', 'protocol_line': text})

    def enable(self, timestamp):
        self._send(f"ARM {timestamp:.9f}")

    def target(self, q, timestamp, tau, waist_rate=0.0, *, pitch_rate=0.0, pitch_enabled=False):
        # One source timestamp covers arms, gravity torque and both waist axes.
        values = [*q, *tau, waist_rate, pitch_rate]
        self._send(f"TARGET6 {timestamp:.9f} " + " ".join(f"{v:.9f}" for v in values)
                   + f" {int(pitch_enabled)}")

    def hold_pitch(self):
        self._send("PITCH_HOLD")

    def hold(self):
        self._send("HOLD")

    def close(self):
        try:
            self.hold()
        except (OSError, RuntimeError):
            pass
        self.process.stdin.close()
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self._reader.join(timeout=1)
        self.process.stdout.close()
