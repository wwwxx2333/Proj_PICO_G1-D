"""G1-D startup homing and controller-clutched official-method IK session."""
import time
import numpy as np
import pinocchio as pin
from .arm_bridge import ArmBridge
from .arm_config import ArmConfig
from .body_config import BodyConfig
from .arm_home import HomeTrajectory
from .arm_ik import DualArmIK, IKTargetRejected
from .arm_math import Clutch


class ArmSession:
    def __init__(self, binary, interface, urdf, startup_home=False, config=None, *, body_config=None):
        self.config = config or ArmConfig()
        self.body_config = body_config or BodyConfig()
        self.ik = DualArmIK(urdf, self.config.filter_weights)  # Includes IPOPT warm-up before creating DDS processes.
        self.bridge = ArmBridge(binary, interface, self.config.bridge_speed_rad_s,
                                yaw_limit_rad=self.body_config.waist_yaw_limit_rad,
                                yaw_speed_rad_s=self.body_config.waist_yaw_speed_rad_s,
                                yaw_accel_rad_s2=self.body_config.waist_yaw_accel_rad_s2,
                                pitch_min_rad=self.body_config.waist_pitch_min_rad,
                                pitch_max_rad=self.body_config.waist_pitch_max_rad,
                                pitch_speed_rad_s=self.body_config.waist_pitch_speed_rad_s,
                                pitch_accel_rad_s2=self.body_config.waist_pitch_accel_rad_s2,
                                pitch_max_error_rad=self.body_config.waist_pitch_max_error_rad,
                                pitch_stall_timeout_s=self.body_config.waist_pitch_stall_timeout_s,
                                pitch_min_progress_rad=self.body_config.waist_pitch_min_progress_rad)
        self.enabled = False
        self.clutches = [None, None]
        self.home_complete = not startup_home
        self.home_cancelled = False
        self.countdown_since = None
        self.home = None
        self.status = "HOME_WAITING_FOR_PICO" if startup_home else "WAITING_FOR_A"
        self.last_ik_warning = -float("inf")
        try:
            state = self.bridge.wait_ready()
            self.command = self.ik.checked_q(state.q)
        except BaseException:
            self.bridge.close()
            raise

    def _state(self):
        state = self.bridge.state()
        now = time.monotonic()
        if state is None or not state.is_fresh(now):
            raise RuntimeError("arm feedback is stale")
        if not state.pitch_is_fresh(now):
            raise RuntimeError("rt/waistpitchstate feedback is stale; "
                               f"age={1000*(now-state.pitch_received_at):.1f} ms (limit 250 ms)")
        self.ik.checked_q(state.q)
        return state

    def _activate(self, sample):
        state = self._state()
        self.command = state.q.copy()
        self.ik.reset(self.command)
        self.clutches = [None, None]
        self.bridge.enable(sample.received_at)
        self.enabled = True
        self.waiting_since = time.monotonic()
        self.waiting = True
        self.last_step = self.waiting_since

    def start(self, sample):
        if not self.home_complete:
            raise ValueError("startup homing must finish before teleoperation")
        error = sample.freshness_error(time.monotonic())
        if error:
            raise ValueError(error)
        if any(sample.grips):
            raise ValueError("release both grip buttons before pressing A")
        self._activate(sample)
        self.status = "TAKING_OVER"

    def stop(self):
        if self.enabled:
            self.enabled = False
            self.clutches = [None, None]
            self.status = "HOLD"
            self.bridge.hold()

    def request_home(self):
        """Left-Y edge: hold immediately, then reuse the guarded home sequence."""
        self.stop()
        self.home_complete = False
        self.home_cancelled = False
        self.countdown_since = None
        self.home = None
        self.status = "HOME_REQUESTED (left Y)"
        print("Left Y: base stopped; release grips and centre sticks for arm homing", flush=True)

    def hold_pitch(self):
        """Stop pitch integration before IK work when the vertical mode changes."""
        self.bridge.hold_pitch()

    def cancel_home(self, reason):
        self.stop()
        self.home_cancelled = True
        self.countdown_since = None
        self.home = None
        self.status = "HOME_CANCELLED (A retries, B stops)"
        print(f"Startup home stopped: {reason}; no automatic restart", flush=True)

    def _guard(self, sample, state, now):
        # Head yaw is frozen at clutch capture; it is not a continuous input.
        error = sample.freshness_error(now, require_head=False)
        if error:
            raise ValueError(error)
        if self.waiting:
            if state.status != 1:
                if now - self.waiting_since > 0.3:
                    raise RuntimeError("arm takeover was not acknowledged")
                return False
            self.waiting = False
        if state.status != 1:
            raise ValueError("arm watchdog entered HOLD; press A with grips released")
        if np.max(np.abs(self.command - state.q)) > 0.30:
            raise ValueError("arm following error exceeds 0.30 rad")
        return True

    def _send_target(self, sample, state, waist_rate=0.0, stick_timestamp=None, *,
                     pitch_rate=0.0, pitch_enabled=False):
        # Use the actual supported pose for gravity; C++ separately ramps torque.
        tau = self.ik.gravity(state.q, state.waist)
        # Use the older source time, never let fresh poses mask stale joystick input.
        timestamp = sample.received_at if stick_timestamp is None else min(sample.received_at, stick_timestamp)
        self.bridge.target(self.command, timestamp, tau, waist_rate,
                           pitch_rate=pitch_rate, pitch_enabled=pitch_enabled)

    def step_home(self, sample, neutral, stop=False, retry=False):
        """Called instead of teleop while startup home is pending; base stays zero."""
        state = self._state()
        now = time.monotonic()
        if stop:
            if not self.home_cancelled:
                self.cancel_home("PICO B pressed")
            return
        if self.home_cancelled:
            if not retry:
                return
            self.home_cancelled = False
        if not self.enabled:
            ready = sample.is_fresh(now, require_head=False) and neutral and not any(sample.grips)
            if not ready:
                self.countdown_since = None
                self.status = "HOME_WAITING_FOR_PICO (release grips, centre sticks)"
                return
            if self.countdown_since is None:
                self.countdown_since = now
            remaining = 3.0 - (now - self.countdown_since)
            self.status = f"HOME_COUNTDOWN {max(remaining, 0):.1f}s (B cancels)"
            if remaining > 0:
                return
            self._activate(sample)
            self.home = None
            self.status = "HOME_TAKING_OVER"
            return
        if not self._guard(sample, state, now):
            return
        if not neutral or any(sample.grips):
            raise ValueError("release grips and centre sticks throughout startup homing")
        if self.home is None:
            self.home = HomeTrajectory(state.q, np.zeros(14), now, speed=self.config.home_speed_rad_s)
            print(f"Homing 14 arm joints to zero over >= {self.home.duration:.1f}s; "
                  "base locked; B cancels", flush=True)
        self.command, done = self.home.step(now, state.q)
        self._send_target(sample, state)
        self.status = f"HOMING {min(100*(now-self.home.started)/self.home.duration, 100):.0f}%"
        if done:
            self.stop()
            self.home_complete = True
            self.status = "HOME_DONE / WAITING_FOR_A"
            print("Home reached and measured stable. Press A to enable teleoperation.", flush=True)

    @staticmethod
    def _bounded_target(current, target, translation_step=0.02, rotation_step=0.10):
        """Limit each Cartesian request before optimization, not just joints after it."""
        result = target.copy()
        delta = target[:3, 3] - current[:3, 3]
        result[:3, 3] = current[:3, 3] + delta * min(1., translation_step / max(np.linalg.norm(delta), 1e-12))
        rotation = pin.log3(target[:3, :3] @ current[:3, :3].T)
        rotation *= min(1., rotation_step / max(np.linalg.norm(rotation), 1e-12))
        result[:3, :3] = pin.exp3(rotation) @ current[:3, :3]
        return result

    def step(self, sample, waist_rate=0.0, stick_timestamp=None, *,
             pitch_rate=0.0, pitch_enabled=False):
        state = self._state()
        if not self.enabled:
            return
        now = time.monotonic()
        if not self._guard(sample, state, now):
            return
        dt = min(max(now - self.last_step, 0), 1.0 / self.config.control_hz)
        targets = self.ik.poses(self.command)
        measured_poses = self.ik.poses(state.q)
        changed = False
        for side, controller in enumerate((sample.left, sample.right)):
            if not sample.grips[side]:
                changed |= self.clutches[side] is not None
                self.clutches[side] = None
                continue
            if self.clutches[side] is None:
                error = sample.freshness_error(now, require_head=True)
                if error:
                    raise ValueError(error)
                self.clutches[side] = Clutch.capture(controller, measured_poses[side], sample.head, self.config)
                changed = True
            targets[side] = self._bounded_target(
                targets[side], self.clutches[side].target(controller),
                self.config.cartesian_speed_m_s * dt, self.config.rotation_speed_rad_s * dt)
        if changed:
            self.ik.reset(self.command)
        failed = False
        if any(sample.grips):
            try:
                solution = self.ik.solve(targets, self.command, sample.grips)
                # Independent arm scaling prevents one large move slowing the other arm.
                for side in range(2):
                    region = slice(side * 7, side * 7 + 7)
                    delta = solution[region] - self.command[region]
                    scale = min(1., self.config.joint_speed_rad_s * dt / max(np.max(np.abs(delta)), 1e-12))
                    self.command[region] += scale * delta
            except IKTargetRejected as exc:
                failed = True
                # Successful future samples can recover; no invalid/debug iterate is sent.
                self.ik.reset(self.command)
                if now - self.last_ik_warning >= 1.0:
                    print(f"IK_RETRY: {exc}", flush=True)
                    self.last_ik_warning = now
        self.last_step = now
        self._send_target(sample, state, waist_rate, stick_timestamp,
                          pitch_rate=pitch_rate, pitch_enabled=pitch_enabled)
        self.status = "IK_RETRY (holding last target)" if failed else "FOLLOW=" + "/".join(
            "ON" if g else "HOLD" for g in sample.grips)
        if any(sample.grips) and not failed:
            self.status += f" IK={self.ik.last_solve_ms:.1f}ms"
        self.status += (f" WAIST={np.rad2deg(state.waist[0]):+.1f}deg"
                        f" target={np.rad2deg(state.waist_target):+.1f}deg"
                        f" rate={state.waist_velocity:+.3f}rad/s"
                        + (' LIMITED' if state.waist_limited else ''))
        self.status += (f" PITCH={np.rad2deg(state.waist[1]):+.1f}deg"
                        f" target={np.rad2deg(state.pitch_target):+.1f}deg"
                        f" rate={state.pitch_velocity:+.3f}rad/s"
                        f" request={state.pitch_requested_rate:+.3f}rad/s"
                        f" error={np.rad2deg(state.pitch_target-state.waist[1]):+.2f}deg"
                        f" feedback_age={1000*(now-state.pitch_received_at):.1f}ms"
                        f" stall_elapsed={state.pitch_stall_elapsed_s:.2f}s "
                        + state.pitch_diagnostics())

    def close(self):
        self.bridge.close()
