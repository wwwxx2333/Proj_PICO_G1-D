"""Offline Dex3 mapping and state tests; no DDS initialization."""
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
import asyncio
import time
import unittest
import numpy as np
from g1d_pico.input import FreshTeleVuer, TeleVuer, PoseSample
from g1d_pico.hands import FLEX_CLOSED, JOINT_LOWER, JOINT_UPPER, HandSession
from g1d_pico.hand_bridge import HandBridge, HandState, parse_hand_state


def sample(t, triggers=(0., 0.)):
    eye=np.eye(4)
    return PoseSample(eye,eye,eye,(False,False),t,t,triggers)


class HandsTests(unittest.TestCase):
    def test_trigger_snapshot_is_coherent_and_invalid_value_is_rejected(self):
        class Snapshot(list):
            def get_lock(self): return nullcontext()
        viewer=object.__new__(FreshTeleVuer)
        viewer._stick_snapshot=Snapshot([0.]*8)
        viewer._pose_snapshot=Snapshot([0.]*37)
        viewer._head_snapshot=Snapshot([0.]*17)
        event=SimpleNamespace(value={
            'left':np.eye(4).ravel(order='F'), 'right':np.eye(4).ravel(order='F'),
            'leftState':{'thumbstickValue':[0,0], 'triggerValue':0.2},
            'rightState':{'thumbstickValue':[0,0], 'triggerValue':0.8}})
        with patch.object(TeleVuer,'on_controller_move',new_callable=AsyncMock):
            asyncio.run(viewer.on_controller_move(event,None))
            first=viewer.read_poses()
            self.assertEqual(first.triggers,(0.2,0.8))
            event.value['rightState']['triggerValue']=float('nan')
            asyncio.run(viewer.on_controller_move(event,None))
            self.assertEqual(viewer.read_poses().received_at,first.received_at)

    def test_open_and_close_are_mirrored_and_inside_official_limits(self):
        self.assertGreater(FLEX_CLOSED[0][1],0)
        self.assertLess(FLEX_CLOSED[0][3],0)
        self.assertLess(FLEX_CLOSED[1][1],0)
        self.assertGreater(FLEX_CLOSED[1][3],0)
        from g1d_pico.hands import HandSession
        self.assertEqual(FLEX_CLOSED[0][0],0)
        self.assertEqual(FLEX_CLOSED[1][0],0)

    def make_session(self):
        now=time.monotonic()
        bridges=[Mock(),Mock()]
        for bridge in bridges:
            bridge.wait_ready.return_value=HandState(now,0,np.zeros(7))
            bridge.state.return_value=HandState(now,1,np.zeros(7))
        bridges[0].side,bridges[1].side='left','right'
        with patch('g1d_pico.hands.HandBridge',side_effect=bridges):
            session=HandSession(Path('unused'),'unused')
        return session,bridges

    def test_enable_requires_released_triggers_then_moves_only_triggered_hand(self):
        session,bridges=self.make_session()
        now=time.monotonic()
        with self.assertRaisesRegex(ValueError,'release both'):
            session.start(sample(now,(0.,0.3)))
        bridges[0].enable.assert_not_called()
        session.start(sample(now))
        session.step(sample(now))
        for bridge in bridges:
            bridge.keepalive.assert_called_once_with(now)
            bridge.target.assert_not_called()
        bridges[0].state.return_value=HandState(time.monotonic()+0.001,1,np.zeros(7))
        session.step(sample(time.monotonic(),(0.8,0.)))
        left=bridges[0].target.call_args.args[0]
        self.assertTrue(np.any(np.abs(left)>0))
        bridges[1].target.assert_not_called()
        bridges[0].state.return_value=HandState(time.monotonic()+0.002,1,np.zeros(7))
        session.step(sample(time.monotonic(),(0.,0.)))
        self.assertEqual(bridges[0].target.call_count,2)
        bridges[1].target.assert_not_called()
        session.stop()
        for bridge in bridges: bridge.hold.assert_called_once()
        session.step(sample(time.monotonic(),(1.,1.)))
        self.assertFalse(session.enabled)

    def test_grasp_position_mismatch_does_not_disarm_or_require_trigger_release(self):
        session, bridges = self.make_session()
        now = time.monotonic()
        session.start(sample(now))
        measured = np.zeros(7)
        measured[1] = 0.1
        sent = measured.copy()
        sent[1] = 0.3
        bridges[0].state.return_value = HandState(now+0.001, 1, measured,
                                                now+0.001, sent, FLEX_CLOSED[0], 2)
        session.step(sample(time.monotonic(), (1., 0.)))
        self.assertTrue(session.enabled)
        self.assertFalse(session.needs_release[0])
        self.assertIn('left=BLOCKED[1]', session.status)
        np.testing.assert_allclose(bridges[0].target.call_args.args[0], FLEX_CLOSED[0])
        bridges[0].state.return_value = HandState(now+0.002, 1, measured,
                                                now+0.002, sent, FLEX_CLOSED[0], 2)
        session.step(sample(time.monotonic(), (0.9, 0.)))
        np.testing.assert_allclose(bridges[0].target.call_args.args[0], 0.9*FLEX_CLOSED[0])
        bridges[0].hold.assert_not_called()
        bridges[1].target.assert_not_called()

    def test_disarmed_feedback_gap_does_not_block_a(self):
        session,bridges=self.make_session()
        bridges[0].state.return_value=HandState(time.monotonic()-0.5,0,np.zeros(7))
        session.step(sample(time.monotonic()))
        self.assertEqual(session.status,'WAITING_FOR_A')
        self.assertFalse(session.enabled)
        session.start(sample(time.monotonic()))
        for bridge in bridges:
            bridge.enable.assert_called_once()
        self.assertTrue(session.enabled)

    def test_armed_feedback_gap_holds_only_affected_hand(self):
        session,bridges=self.make_session()
        now=time.monotonic()
        session.start(sample(now))
        bridges[0].state.return_value=HandState(now-0.5,1,np.zeros(7))
        bridges[1].state.return_value=HandState(now+0.001,1,np.zeros(7))
        session.step(sample(time.monotonic(),(0.8,0.8)))
        self.assertTrue(session.enabled)
        self.assertIn('left=FEEDBACK_HOLD',session.status)
        bridges[0].target.assert_not_called()
        bridges[0].keepalive.assert_called()
        bridges[1].target.assert_called_once()
        for bridge in bridges: bridge.release.assert_not_called()
        bridges[0].state.return_value=HandState(now+0.002,1,np.zeros(7))
        bridges[1].state.return_value=HandState(now+0.003,1,np.zeros(7))
        session.step(sample(time.monotonic(),(0.8,0.8)))
        bridges[0].target.assert_not_called()  # release trigger before moving
        bridges[0].state.return_value=HandState(now+0.004,1,np.zeros(7))
        session.step(sample(time.monotonic(),(0.,0.)))
        bridges[0].state.return_value=HandState(now+0.005,1,np.zeros(7))
        session.step(sample(time.monotonic(),(0.8,0.)))
        bridges[0].target.assert_called_once()

    def test_bridge_loss_of_tracking_retries_takeover(self):
        session,bridges=self.make_session()
        session.start(sample(time.monotonic()))
        bridges[0].state.return_value=HandState(time.monotonic(),0,np.zeros(7))
        session.step(sample(time.monotonic()))
        self.assertTrue(session.enabled)
        self.assertEqual(bridges[0].enable.call_count,2)
        for bridge in bridges: bridge.release.assert_not_called()

    def test_state_received_during_snapshot_is_not_a_false_timeout(self):
        session,bridges=self.make_session()
        session.enabled=True
        clock=[100.0]

        def receive_new_state():
            # A reader can update the state while state() releases the GIL.
            clock[0]+=0.0001
            return HandState(clock[0],1,np.zeros(7))

        for bridge in bridges:
            bridge.state.side_effect=receive_new_state
        with patch('g1d_pico.hands.time.monotonic',side_effect=lambda:clock[0]):
            states=session._states()
        self.assertEqual(len(states),2)

    def test_disarmed_snapshot_update_does_not_report_stale(self):
        session,bridges=self.make_session()
        clock=[100.0]

        def receive_new_state():
            clock[0]+=0.0001
            return HandState(clock[0],0,np.zeros(7))

        for bridge in bridges:
            bridge.state.side_effect=receive_new_state
        with patch('g1d_pico.hands.time.monotonic',side_effect=lambda:clock[0]):
            session.step(sample(clock[0]))
        self.assertEqual(session.status,'WAITING_FOR_A')

    def test_feedback_age_does_not_revoke_ownership(self):
        session,bridges=self.make_session()
        session.enabled=True
        for timestamp in (99.749,100.001):
            with self.subTest(timestamp=timestamp):
                bridges[0].state.return_value=HandState(timestamp,1,np.zeros(7))
                bridges[1].state.return_value=HandState(100.,1,np.zeros(7))
                with patch('g1d_pico.hands.time.monotonic',return_value=100.):
                    self.assertEqual(session._states()[0].received_at,timestamp)
        for bridge in bridges:
            bridge.target.assert_not_called()

    def test_margin_pose_is_held_until_trigger_then_commands_stay_in_limits(self):
        for side in range(2):
            for joint in range(7):
                for limits,direction in ((JOINT_LOWER,-1.),(JOINT_UPPER,1.)):
                    with self.subTest(side=side,joint=joint,direction=direction):
                        session,bridges=self.make_session()
                        measured=np.zeros(7)
                        measured[joint]=limits[side][joint]+direction*0.01
                        now=time.monotonic()
                        bridges[side].state.return_value=HandState(now,1,measured)
                        session.start(sample(now))
                        session.step(sample(now))
                        for bridge in bridges:
                            bridge.keepalive.assert_called_once_with(now)
                            bridge.target.assert_not_called()
                        triggers=[0.,0.]
                        triggers[side]=0.2
                        bridges[side].state.return_value=HandState(now+0.001,1,measured)
                        session.step(sample(time.monotonic(),tuple(triggers)))
                        target=bridges[side].target.call_args.args[0]
                        self.assertTrue(np.all(target>=JOINT_LOWER[side]))
                        self.assertTrue(np.all(target<=JOINT_UPPER[side]))
                        # The other hand continues holding its own C++ pose.
                        bridges[1-side].target.assert_not_called()

    def test_keepalive_pipe_message_carries_original_input_time(self):
        bridge=object.__new__(HandBridge)
        bridge.process=Mock()
        bridge.process.poll.return_value=None
        bridge.process.stdin.fileno.return_value=123
        with patch('g1d_pico.hand_bridge.os.write',
                   side_effect=lambda fd,data:len(data)) as write:
            bridge.keepalive(100.125)
        write.assert_called_once_with(123,b'KEEPALIVE 100.125000000\n')

    def test_release_pipe_message(self):
        bridge=object.__new__(HandBridge)
        bridge.process=Mock()
        bridge.process.poll.return_value=None
        bridge.process.stdin.fileno.return_value=123
        with patch('g1d_pico.hand_bridge.os.write',
                   side_effect=lambda fd,data:len(data)) as write:
            bridge.release()
        write.assert_called_once_with(123,b'RELEASE\n')

    def test_application_can_enable_base_and_arms_while_hands_wait_for_feedback(self):
        from g1d_pico import __main__ as app
        from g1d_pico.input import StickSample
        now=time.monotonic()
        viewer,arms,hands,base=Mock(),Mock(),Mock(),Mock()
        arms.home_complete=True
        viewer.read_sticks.side_effect=[
            StickSample(0,0,0,0,True,False,now),
            StickSample(0,-1,0,0,False,False,now),KeyboardInterrupt()]
        viewer.read_poses.return_value=sample(now)
        hands.start.side_effect=ValueError('Dex3 feedback is stale')
        with patch('sys.argv',['app','--no-camera','--live','--base-only',
                               '--with-arms','--with-hands','--skip-arm-home']), \
             patch('g1d_pico.input.FreshTeleVuer',return_value=viewer), \
             patch('g1d_pico.arms.ArmSession',return_value=arms), \
             patch('g1d_pico.hands.HandSession',return_value=hands), \
             patch.object(app,'AgvBridge',return_value=base), \
             patch.object(app.time,'sleep'):
            app.main()
        arms.start.assert_called_once()
        hands.start.assert_called_once()
        self.assertTrue(any(call.args[0].forward_mps>0 for call in base.send.call_args_list))
        base.close.assert_called_once()

    def test_application_keeps_base_and_arms_after_hand_feedback_gap(self):
        from g1d_pico import __main__ as app
        from g1d_pico.input import StickSample
        now=time.monotonic()
        session,bridges=self.make_session()
        reads=[0]
        def left_state():
            reads[0]+=1
            timestamp=now if reads[0]==1 else now-0.5
            return HandState(timestamp,1,np.zeros(7))
        bridges[0].state.side_effect=left_state
        viewer,arms,base=Mock(),Mock(),Mock()
        arms.home_complete=True
        viewer.read_sticks.side_effect=[
            StickSample(0,0,0,0,True,False,now),
            StickSample(0,-1,0,0,False,False,now),KeyboardInterrupt()]
        viewer.read_poses.return_value=sample(now)
        with patch('sys.argv',['app','--no-camera','--live','--base-only',
                               '--with-arms','--with-hands','--skip-arm-home']), \
             patch('g1d_pico.input.FreshTeleVuer',return_value=viewer), \
             patch('g1d_pico.arms.ArmSession',return_value=arms), \
             patch('g1d_pico.hands.HandSession',return_value=session), \
             patch.object(app,'AgvBridge',return_value=base), \
             patch.object(app.time,'sleep'):
            app.main()
        self.assertTrue(session.enabled)
        self.assertTrue(any(call.args[0].forward_mps>0 for call in base.send.call_args_list))
        for bridge in bridges: bridge.release.assert_not_called()
        arms.close.assert_called_once()

    def test_application_keeps_base_and_arms_enabled_during_blocked_grasp(self):
        from g1d_pico import __main__ as app
        from g1d_pico.input import StickSample
        now = time.monotonic()
        session, bridges = self.make_session()
        measured = np.zeros(7)
        sent = np.zeros(7)
        sent[1] = 0.2
        bridges[0].state.side_effect = [
            HandState(now, 1, measured),
            HandState(now+0.001, 1, measured, now+0.001, sent, FLEX_CLOSED[0], 2),
            HandState(now+0.002, 1, measured, now+0.002, sent, FLEX_CLOSED[0], 2)]
        viewer, arms, base = Mock(), Mock(), Mock()
        arms.home_complete = True
        viewer.read_sticks.side_effect = [
            StickSample(0,0,0,0,True,False,now),
            StickSample(0,-1,0,0,False,False,now), KeyboardInterrupt()]
        viewer.read_poses.side_effect = [sample(now), sample(now, (1.,0.))]
        with patch('sys.argv',['app','--no-camera','--live','--base-only',
                               '--with-arms','--with-hands','--skip-arm-home']), \
             patch('g1d_pico.input.FreshTeleVuer',return_value=viewer), \
             patch('g1d_pico.arms.ArmSession',return_value=arms), \
             patch('g1d_pico.hands.HandSession',return_value=session), \
             patch.object(app,'AgvBridge',return_value=base), \
             patch.object(app.time,'sleep'):
            app.main()
        self.assertTrue(session.enabled)
        self.assertIn('left=BLOCKED[1]', session.status)
        arms.start.assert_called_once()
        arms.stop.assert_not_called()
        self.assertTrue(any(call.args[0].forward_mps>0 for call in base.send.call_args_list))
        for bridge in bridges:
            bridge.hold.assert_not_called()

    def test_application_b_disarms_hands_and_stops_base(self):
        from g1d_pico import __main__ as app
        from g1d_pico.input import StickSample
        now=time.monotonic()
        viewer,arms,hands,base=Mock(),Mock(),Mock(),Mock()
        arms.home_complete=True
        viewer.read_sticks.side_effect=[
            StickSample(0,0,0,0,True,False,now),
            StickSample(0,-1,0,0,False,True,now),KeyboardInterrupt()]
        viewer.read_poses.return_value=sample(now)
        with patch('sys.argv',['app','--no-camera','--live','--base-only',
                               '--with-arms','--with-hands','--skip-arm-home']), \
             patch('g1d_pico.input.FreshTeleVuer',return_value=viewer), \
             patch('g1d_pico.arms.ArmSession',return_value=arms), \
             patch('g1d_pico.hands.HandSession',return_value=hands), \
             patch.object(app,'AgvBridge',return_value=base), \
             patch.object(app.time,'sleep'):
            app.main()
        arms.start.assert_called_once()
        hands.start.assert_called_once()
        hands.stop.assert_called()
        self.assertEqual(base.send.call_args_list[-1].args[0].forward_mps,0.)
        hands.close.assert_called_once()
        arms.close.assert_called_once()

    def test_state_keeps_source_timestamp(self):
        self.assertEqual(HandState(10.,1,np.zeros(7)).received_at,10.)

    def test_dead_hand_does_not_prevent_other_hand_from_holding(self):
        session,bridges=self.make_session()
        session.start(sample(time.monotonic()))
        bridges[0].hold.side_effect=RuntimeError('left Dex3 bridge exited: 1')
        session.stop()
        bridges[1].hold.assert_called_once()
        self.assertFalse(session.enabled)

    def test_hand_close_failure_still_closes_both_bridges(self):
        session, bridges = self.make_session()
        bridges[1].close.side_effect = RuntimeError('DDS shutdown failed')
        with self.assertRaisesRegex(RuntimeError, 'DDS shutdown'):
            session.close()
        for bridge in bridges:
            bridge.close.assert_called_once()
        self.assertEqual(session.bridges, [])

    def test_hand_close_failure_does_not_skip_arm_and_camera_cleanup(self):
        from g1d_pico import __main__ as app
        viewer, arms, hands, base = Mock(), Mock(), Mock(), Mock()
        viewer.read_sticks.side_effect = KeyboardInterrupt()
        arms.home_complete = True
        hands.close.side_effect = RuntimeError('DDS shutdown failed')
        with patch('sys.argv', ['app', '--no-camera', '--live', '--base-only',
                               '--with-arms', '--with-hands', '--skip-arm-home']), \
             patch('g1d_pico.input.FreshTeleVuer', return_value=viewer), \
             patch('g1d_pico.arms.ArmSession', return_value=arms), \
             patch('g1d_pico.hands.HandSession', return_value=hands), \
             patch.object(app, 'AgvBridge', return_value=base):
            with self.assertRaises(SystemExit) as result:
                app.main()
        self.assertEqual(result.exception.code, 1)
        for resource in (base, hands, arms, viewer): resource.close.assert_called_once()

    def test_bridge_failure_stops_all_control_without_traceback(self):
        from g1d_pico import __main__ as app
        from g1d_pico.input import StickSample
        from io import StringIO
        from contextlib import redirect_stdout
        now=time.monotonic()
        viewer,arms,hands,base=Mock(),Mock(),Mock(),Mock()
        arms.home_complete=True
        viewer.read_sticks.return_value=StickSample(0,0,0,0,False,False,now)
        viewer.read_poses.return_value=sample(now)
        hands.step.side_effect=RuntimeError('left Dex3 bridge exited: 1')
        output=StringIO()
        with patch('sys.argv',['app','--no-camera','--live','--base-only',
                               '--with-arms','--with-hands','--skip-arm-home']), \
             patch('g1d_pico.input.FreshTeleVuer',return_value=viewer), \
             patch('g1d_pico.arms.ArmSession',return_value=arms), \
             patch('g1d_pico.hands.HandSession',return_value=hands), \
             patch.object(app,'AgvBridge',return_value=base), \
             patch.object(app.time,'sleep'), redirect_stdout(output):
            with self.assertRaises(SystemExit) as exit_result:
                app.main()
        self.assertEqual(exit_result.exception.code,1)
        self.assertIn('Control fault: left Dex3 bridge exited: 1',output.getvalue())
        self.assertEqual(base.send.call_args.args[0].forward_mps,0.)
        hands.stop.assert_called()
        arms.stop.assert_called()
        base.close.assert_called_once()
        hands.close.assert_called_once()
        arms.close.assert_called_once()
        viewer.close.assert_called_once()


class HandProtocolTests(unittest.TestCase):
    def packet(self):
        return ('HANDSTATE2 10.0 1 ' + ' '.join(['0.1']*7) + ' 10.2 '
                + ' '.join(['0.2']*7) + ' ' + ' '.join(['0.8']*7) + ' 2 4 8').split()

    def test_reports_source_time_successful_write_and_three_distinct_positions(self):
        state = parse_hand_state(self.packet())
        self.assertEqual(state.received_at, 10.)
        self.assertEqual(state.sent_at, 10.2)
        np.testing.assert_allclose(state.q, 0.1)
        np.testing.assert_allclose(state.sent_q, 0.2)
        np.testing.assert_allclose(state.requested_q, 0.8)
        self.assertEqual((state.blocked_mask, state.opening_mask, state.limited_mask), (2,4,8))
        # A newer DDS write is not new joint feedback.
        fields = self.packet()
        fields[10] = '11.0'
        self.assertEqual(parse_hand_state(fields).received_at, 10.)

    def test_old_bridge_requires_rebuild_instead_of_silent_unsafe_fallback(self):
        with self.assertRaisesRegex(ValueError, 'rebuild'):
            parse_hand_state(('HANDSTATE 10 1 ' + '0 '*7).split())

    def test_invalid_telemetry_is_rejected(self):
        for index, value in ((1,'nan'),(10,'nan'),(11,'nan'),(18,'inf'),
                             (25,'128'),(25,'4'),(25,'1'),(2,'3')):
            with self.subTest(index=index,value=value):
                fields = self.packet()
                fields[index] = value
                with self.assertRaises(ValueError):
                    parse_hand_state(fields)
        with self.assertRaises(ValueError):
            parse_hand_state(self.packet()[:-1])


if __name__=='__main__': unittest.main()
