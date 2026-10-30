"""Dedicated pitch feedback must not be fabricated or refreshed by arm data."""
import unittest
from unittest.mock import Mock, patch
import numpy as np
from g1d_pico.arm_bridge import ArmState, ArmBridge, parse_arm_state
from g1d_pico.arms import ArmSession


class PitchFeedbackTests(unittest.TestCase):
    @staticmethod
    def state(arm_time=100.0, pitch_time=99.98):
        return ArmState(arm_time, 1, np.zeros(14), np.array([0.2, 0.3]),
                        pitch_received_at=pitch_time)

    def test_lowstate_time_cannot_mask_stale_or_missing_pitch(self):
        for stamp in (0.0, 99.7, 100.2):
            state = self.state(pitch_time=stamp)
            self.assertTrue(state.is_fresh(100.1))
            self.assertFalse(state.pitch_is_fresh(100.1))
        state = self.state()
        self.assertTrue(state.pitch_is_fresh(100.1))

    def test_old_bridge_and_invalid_pitch_timestamp_are_rejected(self):
        for version in ('STATE', 'STATE2', 'STATE3', 'STATE4', 'STATE5'):
            with self.assertRaisesRegex(ValueError, 'rebuild'): parse_arm_state([version])
        for stamp in ('0', '-1', 'nan', 'inf'):
            fields = ('STATE6 100 1 '+'0 '*14+'0.2 0.3 0 0 0 0 0 0 '+stamp+' 0 0 0 0 0').split()
            with self.assertRaises(ValueError): parse_arm_state(fields)

    def test_startup_requires_dedicated_feedback_before_enable(self):
        for state in (None, self.state(pitch_time=0), self.state(pitch_time=99)):
            bridge = object.__new__(ArmBridge)
            bridge.state, bridge._send = Mock(return_value=state), Mock()
            with patch('g1d_pico.arm_bridge.time.monotonic', side_effect=[100, 100, 100, 106]), \
                 patch('g1d_pico.arm_bridge.time.sleep'):
                with self.assertRaisesRegex(RuntimeError, 'rt/waistpitchstate'):
                    bridge.wait_ready()
            bridge._send.assert_not_called()
        bridge.state.return_value = self.state()
        with patch('g1d_pico.arm_bridge.time.monotonic', return_value=100.1):
            self.assertIs(bridge.wait_ready(), bridge.state.return_value)

    def test_session_rejects_stale_pitch_before_gravity_or_target(self):
        session = object.__new__(ArmSession)
        session.bridge, session.ik = Mock(), Mock()
        session.bridge.state.return_value = self.state(pitch_time=99)
        with patch('g1d_pico.arms.time.monotonic', return_value=100.1):
            with self.assertRaisesRegex(RuntimeError, 'rt/waistpitchstate'):
                session.step(Mock(), pitch_rate=.08, pitch_enabled=True)
        session.bridge.target.assert_not_called()
        session.ik.gravity.assert_not_called()
        session.bridge.state.return_value = self.state()
        with patch('g1d_pico.arms.time.monotonic', return_value=100.1):
            np.testing.assert_allclose(session._state().waist, [.2, .3])


if __name__ == '__main__':
    unittest.main()
