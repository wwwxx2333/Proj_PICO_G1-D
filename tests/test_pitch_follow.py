"""Configuration, telemetry and session regressions; all robot I/O mocked."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import numpy as np
from g1d_pico.arm_bridge import ArmBridge, ArmState, parse_arm_state
from g1d_pico.body_config import BodyConfig
from g1d_pico.arms import ArmSession
from g1d_pico.input import PoseSample

ROOT=Path(__file__).resolve().parents[1]


class PitchFollowingTests(unittest.TestCase):
    def test_new_configuration_defaults_file_and_validation(self):
        config=BodyConfig.load(ROOT/'config/body.jsonc')
        self.assertEqual(config.waist_pitch_max_error_rad, .10)
        self.assertEqual(config.waist_pitch_stall_timeout_s, 1.5)
        self.assertEqual(config.waist_pitch_min_progress_rad, .005)
        for key in ('waist_pitch_max_error_rad','waist_pitch_stall_timeout_s','waist_pitch_min_progress_rad'):
            for bad in (0, 100, float('nan'), float('inf'), True):
                with self.assertRaises(ValueError): BodyConfig(**{key:bad})
        # Old JSON can omit new keys and gets the validated defaults.
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'body.jsonc'; path.write_text('{}')
            self.assertEqual(BodyConfig.load(path), BodyConfig())

    def test_configuration_is_forwarded_to_cpp_process(self):
        with tempfile.TemporaryDirectory() as directory:
            binary=Path(directory)/'bridge'; binary.touch()
            process=Mock()
            with patch('g1d_pico.arm_bridge.subprocess.Popen', return_value=process) as popen, \
                 patch('g1d_pico.arm_bridge.require_current_limits') as contract, \
                 patch('g1d_pico.arm_bridge.os.set_blocking'), \
                 patch('g1d_pico.arm_bridge.threading.Thread'):
                ArmBridge(binary,'eth0',pitch_max_error_rad=.12,pitch_stall_timeout_s=2,
                          pitch_min_progress_rad=.006)
            contract.assert_called_once()
            args=popen.call_args.args[0]
            self.assertEqual(len(args),13)
            self.assertEqual(list(map(float,args[-3:])),[.12,2,.006])

    @staticmethod
    def packet(limit=0, following=0):
        return ('STATE6 100 1 '+'0 '*14+f'0.2 0.3 0.2 0 0 0.3 0 {int(bool(limit))} '
                f'99.98 {limit} {following} -0.08 1.5 -0.1').split()

    def test_pitch_status_has_distinct_reasons_and_validated_packet(self):
        for limit,text in ((1,'ANGLE_MIN'),(2,'ANGLE_MAX'),(4,'FOLLOW_ERROR'),(6,'ANGLE_MAX+FOLLOW_ERROR')):
            state=parse_arm_state(self.packet(limit))
            self.assertIn('PITCH_LIMIT='+text,state.pitch_diagnostics())
        for following,text in ((1,'WAIT_CENTER'),(2,'WAIT_PUSH')):
            state=parse_arm_state(self.packet(following=following))
            self.assertEqual(state.status,1) # Global upper-body ownership remains active.
            self.assertIn('PITCH_NOT_FOLLOWING/'+text,state.pitch_diagnostics())
            self.assertEqual(state.pitch_requested_rate,-.08)
            self.assertEqual(state.pitch_stall_elapsed_s,1.5)
            self.assertEqual(state.pitch_stall_error_rad,-.1)
        for index,bad in ((26,'3'),(26,'7'),(27,'3'),(28,'nan'),(28,'.2'),(29,'-1'),(30,'inf'),(24,'1')):
            packet=self.packet(); packet[index]=bad
            with self.assertRaises(ValueError): parse_arm_state(packet)
        with self.assertRaisesRegex(ValueError,'rebuild'): parse_arm_state(['STATE5'])

    def session(self, following):
        state=ArmState(100,1,np.zeros(14),np.array([0.,.3]),pitch_target=.3,
                       pitch_received_at=100,pitch_following=following,
                       pitch_requested_rate=-.08,pitch_stall_elapsed_s=1.5,pitch_stall_error_rad=-.1)
        bridge,ik=Mock(),Mock()
        bridge.state.return_value=bridge.wait_ready.return_value=state
        ik.checked_q.side_effect=lambda q:q.copy()
        ik.poses.return_value=[np.eye(4),np.eye(4)]
        ik.gravity.return_value=np.zeros(14)
        config=BodyConfig(waist_pitch_max_error_rad=.12,waist_pitch_stall_timeout_s=2,
                         waist_pitch_min_progress_rad=.006)
        with patch('g1d_pico.arms.ArmBridge',return_value=bridge) as constructor, \
             patch('g1d_pico.arms.DualArmIK',return_value=ik):
            session=ArmSession(Path('unused'),'eth0',Path('unused'),body_config=config)
        self.assertEqual(constructor.call_args.kwargs['pitch_max_error_rad'],.12)
        self.assertEqual(constructor.call_args.kwargs['pitch_stall_timeout_s'],2)
        self.assertEqual(constructor.call_args.kwargs['pitch_min_progress_rad'],.006)
        session.enabled=True; session.waiting=False; session.last_step=100
        return session,bridge

    def test_local_pitch_hold_does_not_disarm_arm_session(self):
        sample=PoseSample(np.eye(4),np.eye(4),np.eye(4),(False,False),100,100)
        for following in (1,2):
            session,bridge=self.session(following)
            with patch('g1d_pico.arms.time.monotonic',return_value=100.01):
                session.step(sample,waist_rate=.1,pitch_rate=-.08,pitch_enabled=True)
            self.assertTrue(session.enabled)
            bridge.hold.assert_not_called()
            bridge.enable.assert_not_called()
            self.assertEqual(bridge.target.call_args.args[3],.1)
            self.assertIn('PITCH_NOT_FOLLOWING/',session.status)
            self.assertIn('error=+0.00deg',session.status)
            self.assertIn('request=-0.080rad/s',session.status)

    def test_stale_feedback_remains_separate_from_local_following_fault(self):
        session,bridge=self.session(1)
        bridge.state.return_value=replace(bridge.state.return_value,pitch_received_at=99)
        sample=PoseSample(np.eye(4),np.eye(4),np.eye(4),(False,False),100,100)
        with patch('g1d_pico.arms.time.monotonic',return_value=100.01):
            with self.assertRaisesRegex(RuntimeError,'rt/waistpitchstate'):
                session.step(sample,pitch_enabled=True)
        bridge.target.assert_not_called()


if __name__=='__main__': unittest.main()
