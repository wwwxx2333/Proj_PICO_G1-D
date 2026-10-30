"""PICO index triggers -> conservative paired Dex3-1 finger flexion."""
import time
import numpy as np
from .hand_bridge import HandBridge

# Nominal command limits from SDK2 g1_dex3_example.cpp, in left/right order.
# C++ independently checks these limits. Its 0.05 rad measurement tolerance
# must not turn into permission to send an out-of-range motion target.
JOINT_LOWER = (
    np.array([-1.05, -0.724, 0., -1.57, -1.75, -1.57, -1.75]),
    np.array([-1.05, -1.05, -1.75, 0., 0., 0., 0.]),
)
JOINT_UPPER = (
    np.array([1.05, 1.05, 1.75, 0., 0., 0., 0.]),
    np.array([1.05, 0.742, 0., 1.57, 1.75, 1.57, 1.75]),
)

# SDK2 g1_dex3_example.cpp motor order: thumb 0/1/2, middle 0/1, index 0/1.
# Thumb 0 is held at the measured spread angle. Remaining joints use 60% of
# the official bend limit. This is a grasp preset, not individual finger tracking.
FLEX_CLOSED = (
    np.array([0., 0.63, 1.05, -0.94, -1.05, -0.94, -1.05]),
    np.array([0., -0.63, -1.05, 0.94, 1.05, 0.94, 1.05]),
)
FEEDBACK_FREEZE_DELAY_S = 0.18  # Above the official LF topic's ~10 Hz interval.


class HandSession:
    def __init__(self, binary, interface):
        self.bridges = []
        self.enabled = False
        self.status = 'WAITING_FOR_A'
        try:
            for side in ('left', 'right'):
                bridge = HandBridge(binary, interface, side)
                self.bridges.append(bridge)
                bridge.wait_ready()
        except BaseException:
            self.close()
            raise
        self.requested = [np.zeros(7), np.zeros(7)]
        self.spread = [0., 0.]
        self.moved_trigger = [False, False]
        self.last_feedback = [0., 0.]
        self.feedback_paused = [False, False]
        self.needs_release = [False, False]

    def _states(self):
        states = [bridge.state() for bridge in self.bridges]
        if any(state is None for state in states):
            raise ValueError('Dex3 has not received an initial hand state')
        return states

    def start(self, sample):
        if not sample.is_fresh(time.monotonic(), require_head=False):
            raise ValueError('fresh PICO hand controller data required')
        if any(v > 0.05 for v in sample.triggers):
            raise ValueError('release both index triggers before pressing A')
        states = self._states()
        # Prepare bounded references for later trigger motion. Until the first
        # trigger use, KEEPALIVE leaves C++ holding its own measured ARM pose.
        self.requested = [np.clip(state.q, lower, upper)
                        for state, lower, upper in zip(states, JOINT_LOWER, JOINT_UPPER)]
        self.spread = [q[0] for q in self.requested]
        self.moved_trigger = [False, False]
        self.last_feedback = [state.received_at for state in states]
        self.feedback_paused = [False, False]
        self.needs_release = [False, False]
        enabled_bridges = []
        try:
            for bridge in self.bridges:
                bridge.enable(sample.received_at)
                enabled_bridges.append(bridge)
        except BaseException:
            for bridge in enabled_bridges:
                try:
                    bridge.hold()
                except (OSError, RuntimeError):
                    pass
            raise
        self.enabled = True
        self.status = 'TAKING_OVER'
        self._phases = [None, None]

    def step(self, sample):
        if not self.enabled:
            # Keep checking process health while disarmed.
            states = [bridge.state() for bridge in self.bridges]
            self.status = ('WAITING_FOR_A' if all(state is not None for state in states)
                           else 'WAITING_FOR_INITIAL_DEX3_STATE')
            return
        states = self._states()
        now = time.monotonic()
        error = sample.freshness_error(now, require_head=False)
        if error:
            raise ValueError(error)
        phases = []
        for side, (bridge, state, value) in enumerate(zip(self.bridges, states, sample.triggers)):
            if state.status != 1:
                # Takeover still needs a new measurement after the ARM event.
                self.feedback_paused[side] = True
                self.needs_release[side] |= value > 0.05
                bridge.enable(sample.received_at)
                phases.append('WAIT_TAKEOVER')
                continue
            if now - state.received_at >= FEEDBACK_FREEZE_DELAY_S:
                self.feedback_paused[side] = True
                self.needs_release[side] |= value > 0.05
                bridge.keepalive(sample.received_at)
                phases.append('FEEDBACK_HOLD')
                continue
            if state.received_at <= self.last_feedback[side]:
                bridge.keepalive(sample.received_at)
                phases.append('WAIT_TRIGGER_RELEASE' if self.needs_release[side]
                              else self._motion_label(state) if self.moved_trigger[side]
                              else 'POSE_HOLD')
                continue
            self.last_feedback[side] = state.received_at
            if self.feedback_paused[side]:
                # This recovery rule applies to a feedback gap, never to normal
                # contact. A blocked grasp with fresh feedback needs no release.
                self.spread[side] = float(np.clip(state.q[0], JOINT_LOWER[side][0],
                                                JOINT_UPPER[side][0]))
                self.moved_trigger[side] = False
                self.feedback_paused[side] = False
            if self.needs_release[side]:
                if value > 0.05:
                    bridge.keepalive(sample.received_at)
                    phases.append('WAIT_TRIGGER_RELEASE')
                    continue
                self.needs_release[side] = False
            if value > 0.05:
                self.moved_trigger[side] = True
            if not self.moved_trigger[side]:
                bridge.keepalive(sample.received_at)
                phases.append('POSE_HOLD')
                continue
            closed = FLEX_CLOSED[side].copy()
            closed[0] = self.spread[side]
            open_pose = np.zeros(7)
            open_pose[0] = self.spread[side]
            self.requested[side] = open_pose + float(value) * (closed - open_pose)
            # Send operator intent, not an independently integrated setpoint.
            # C++ alone applies grasp anchors, measurement lead and speed limits.
            # Position mismatch is expected on contact; it is not a global fault.
            bridge.target(self.requested[side], sample.received_at)
            phases.append(self._motion_label(state))
        self.status = ('TRIGGER L={:.2f} R={:.2f} '.format(*sample.triggers)
                       + ' '.join(f'{bridge.side}={phase}'
                                  for bridge, phase in zip(self.bridges, phases)))
        self._report_transitions(phases, states, now)

    @staticmethod
    def _motion_label(state):
        labels = []
        for name, mask in (('BLOCKED', state.blocked_mask), ('OPENING', state.opening_mask)):
            if mask:
                joints = ','.join(str(i) for i in range(7) if mask & (1 << i))
                labels.append(f'{name}[{joints}]')
        if state.limited_mask:
            labels.append('LEAD_LIMIT')
        return '+'.join(labels) or 'FOLLOW'

    def _report_transitions(self, phases, states, now):
        # The instantaneous lead clamp can toggle with measurement noise. Keep
        # it in the periodic status, not as a new mode event every control tick.
        modes = [phase.replace('+LEAD_LIMIT', '').replace('LEAD_LIMIT', 'FOLLOW')
                 for phase in phases]
        previous = getattr(self, '_phases', [None, None])
        for bridge, phase, old, state in zip(self.bridges, modes, previous, states):
            if phase == old:
                continue
            def vector(q):
                return 'none' if q is None else '[' + ','.join(f'{v:.4f}' for v in q) + ']'
            print(f'Dex3 mode t={now:.3f} side={bridge.side} {old or "START"}->{phase} '
                  f'feedback_age_ms={(now-state.received_at)*1000:.1f} '
                  f'sent_at={state.sent_at:.6f} '
                  f'requested={vector(state.requested_q)} sent={vector(state.sent_q)} '
                  f'measured={vector(state.q)}', flush=True)
        self._phases = modes

    def suspend(self, reason):
        """Drop hand ownership without stopping base or arm teleoperation."""
        if not self.enabled:
            return
        self.enabled = False
        for bridge in self.bridges:
            bridge.release()
        self.status = 'PAUSED / WAITING_FOR_A'
        print(f'Dex3 paused: {reason}; hand commands stopped.', flush=True)

    def stop(self):
        if self.enabled:
            self.enabled = False
            self.status = 'HOLD'
            for bridge in self.bridges:
                # One hand may have exited; still hold the healthy hand.
                try:
                    bridge.hold()
                except (OSError, RuntimeError):
                    pass

    def close(self):
        errors = []
        for bridge in reversed(self.bridges):
            try:
                bridge.close()
            except (OSError, RuntimeError) as exc:
                errors.append(str(exc))
        self.bridges.clear()
        if errors:
            raise RuntimeError('; '.join(errors))
