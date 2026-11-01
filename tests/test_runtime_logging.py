"""Runtime logs with synthetic subprocesses/files; never imports DDS or moves a robot."""
import io
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, Mock

from project_fixture import copy_runtime_project
from g1d_pico.runtime_logging import full_log_requested
from g1d_pico.runtime_logging.config import Settings
from g1d_pico.runtime_logging.files import create_log
from g1d_pico.runtime_logging.filter import TerminalFilter
from g1d_pico.runtime_logging.output import LogOutput

ROOT = Path(__file__).resolve().parents[1]
METRIC = 'ARMED L=(+0.30,+0.00) R=(+0.00,+0.00) -> vx=+0.300 m/s, yaw=+0.0 rad/s, lift=0'


class FilterTests(unittest.TestCase):
    def test_metrics_hidden_but_control_transitions_visible(self):
        select = TerminalFilter()
        self.assertIn('已启用', select.select(METRIC, 1)[0])
        self.assertEqual(select.select(METRIC, 2), [])
        result = select.select(METRIC.replace('ARMED ', 'DISARMED '), 3)
        self.assertIn('已暂停', result[0])
        self.assertNotIn('vx=', ''.join(result))
        self.assertEqual(select.select('DEX3 TRIGGER L=0.0 R=0.8', 3), [])
        self.assertEqual(select.select('left Dex3 feedback source: rt/dex3/state', 4), [])
        self.assertEqual(select.select('AGV TRACE diagnostics', 5), [])

    def test_unknown_multiline_exceptions_and_recording_are_not_hidden(self):
        select = TerminalFilter()
        for line in ('Traceback (most recent call last):', '  File "x", line 2',
                     '    raise RuntimeError()', 'RuntimeError: driver broken',
                     'SDK2 call failed: Move=1', 'REC READY 准备完成',
                     'REC STOP_HOLD 0.5/1.0 秒', 'unknown vendor diagnostic'):
            self.assertEqual(select.select(line, 1), [line])
        self.assertTrue(select.select('ARMS FOLLOW=HOLD/HOLD PITCH_NOT_FOLLOWING/WAIT_CENTER', 2))

    def test_wait_repeat_home_buckets_and_full_mode(self):
        select = TerminalFilter(wait_repeat_s=10)
        self.assertTrue(select.select('ARMS HOME_WAITING_FOR_PICO', 0))
        self.assertFalse(select.select('ARMS HOME_WAITING_FOR_PICO', 1))
        self.assertTrue(select.select('ARMS HOME_WAITING_FOR_PICO', 10))
        self.assertTrue(select.select('ARMS HOMING 10%', 11))
        self.assertFalse(select.select('ARMS HOMING 11%', 12))
        self.assertTrue(select.select('ARMS HOMING 20%', 13))
        self.assertEqual(TerminalFilter(full=True).select(METRIC, 1), [METRIC])

    def test_instruction_text_does_not_enable_full_mode(self):
        self.assertFalse(full_log_requested(['--instruction', '--full-log', '--scene', 'lab']))
        self.assertTrue(full_log_requested(['--full-log', '--instruction', 'cup', '--', '--with-hands']))


class FilesTests(unittest.TestCase):
    def test_101st_log_evicts_oldest_and_preserves_foreign_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            foreign = root/'manual.log'
            foreign.write_text('keep')
            logs=[]
            for _ in range(101):
                path, handle, removed = create_log(root, 100)
                handle.write('test\n'); handle.close()
                logs.append(path)
            self.assertEqual(len(list(root.glob('run_*.log'))), 100)
            self.assertFalse(logs[0].exists())
            self.assertTrue(logs[-1].exists())
            self.assertEqual(foreign.read_text(), 'keep')
            self.assertEqual(removed, 1)

    def test_active_log_and_symlink_are_never_deleted(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first, active, _ = create_log(root, 1)
            try:
                with self.assertRaisesRegex(RuntimeError, 'active'):
                    create_log(root, 1)
                self.assertTrue(first.exists())
            finally:
                active.close()
            outside=root/'outside.txt'; outside.write_text('keep')
            link=root/'run_20200101_000000_000000_00000000.log'; link.symlink_to(outside)
            newer, handle, _=create_log(root, 1); handle.close()
            self.assertTrue(link.is_symlink())
            self.assertEqual(outside.read_text(), 'keep')
            self.assertTrue(newer.exists())

    def test_config_bounds_prevent_more_than_100_files(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'logging.jsonc'
            values=dict(directory=temp,max_files=100,flush_interval_s=1,wait_repeat_s=10,queue_capacity=128)
            path.write_text(json.dumps(values)); self.assertEqual(Settings.load(path).max_files,100)
            for value in (101, 0, True, 1.5):
                path.write_text(json.dumps({**values,'max_files':value}))
                with self.assertRaises(ValueError): Settings.load(path)


class OutputTests(unittest.TestCase):
    def test_terminal_filter_does_not_filter_file(self):
        with tempfile.TemporaryDirectory() as temp:
            path, handle, _=create_log(Path(temp),100)
            terminal=io.StringIO()
            output=LogOutput(handle,Settings(Path(temp)),terminal=terminal)
            output.line(METRIC)
            output.line('DEX3 TRIGGER L=0.0 R=0.8')
            output.line('REC RECORDING 开始录制')
            self.assertTrue(output.close())
            saved=path.read_text()
            self.assertIn(METRIC,saved)
            self.assertIn('DEX3 TRIGGER',saved)
            self.assertNotIn('vx=',terminal.getvalue())
            self.assertIn('REC RECORDING',terminal.getvalue())
            self.assertRegex(saved,r'^\[\d{4}-\d\d-\d\dT.*[+-]\d\d:\d\d\]')

    def test_slow_file_does_not_block_producer_and_reports_overflow(self):
        with tempfile.TemporaryDirectory() as temp:
            path, actual, _=create_log(Path(temp),100)
            blocked=threading.Event(); release=threading.Event()
            class Slow:
                def write(self,text): blocked.set(); release.wait(5); actual.write(text)
                def flush(self): actual.flush()
                def fileno(self): return actual.fileno()
                def close(self): actual.close()
            terminal=io.StringIO()
            output=LogOutput(Slow(),Settings(Path(temp),queue_capacity=128),terminal=terminal)
            try:
                output.line('block writer'); self.assertTrue(blocked.wait(2))
                start=time.monotonic()
                for i in range(1000): output.line(f'DEX3 TRIGGER L={i} R=0')
                self.assertLess(time.monotonic()-start,1)
                self.assertGreater(output.file_drops,0)
                output.report_health()
            finally:
                release.set(); output.close()
            self.assertIn('LOG_HEALTH',terminal.getvalue())

    def test_file_failure_warns_and_does_not_raise_into_producer(self):
        class Broken:
            def write(self,text): raise OSError('disk full fixture')
            def close(self): pass
        terminal=io.StringIO()
        output=LogOutput(Broken(),Settings(Path('/tmp')),terminal=terminal)
        output.line('first')
        self.assertTrue(output.file_done.wait(2))
        output.line('CONTROL_STILL_ALIVE')
        self.assertFalse(output.close())
        self.assertIn('disk full fixture',terminal.getvalue())
        self.assertIn('CONTROL_STILL_ALIVE',terminal.getvalue())


class BridgeOutputTests(unittest.TestCase):
    def test_diagnostics_are_forwarded_without_exposing_state_protocol(self):
        from g1d_pico.arm_bridge import ArmBridge
        from g1d_pico.hand_bridge import HandBridge
        arm = 'STATE6 100 1 ' + '0 '*14 + '0.2 0.3 0 0 0 0 0 0 100 0 0 0 0 0'
        hand = 'HANDSTATE2 100 1 ' + '0 '*7 + '100 ' + '0 '*17
        for cls, packet in ((ArmBridge, arm), (HandBridge, hand)):
            with self.subTest(bridge=cls.__name__):
                bridge = cls.__new__(cls)
                bridge.process = Mock(stdout=io.BytesIO(('SDK 诊断\n'+packet+'\n').encode()))
                bridge._lock = threading.Lock()
                bridge._error = bridge._state = None
                bridge.side = 'left'
                terminal = io.StringIO()
                with patch('sys.stderr', terminal), patch(cls.__module__+'.emit') as emit:
                    bridge._read_states()
                self.assertIsNone(bridge._error)
                self.assertEqual(bridge._state.received_at, 100)
                self.assertEqual(terminal.getvalue(), 'SDK 诊断\n')
                emit.assert_called_once()


class LaunchTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=copy_runtime_project(Path(self.temp.name)/'project')
        self.env=os.environ.copy()
        for key in ('G1D_RUNTIME_LOG_ACTIVE','G1D_RUNTIME_LOG_PATH'):
            self.env.pop(key,None)

    def run_script(self,script,full=False):
        return subprocess.run([sys.executable,'-u','-m','g1d_pico.runtime_logging','--',
                               sys.executable,'-u','-c',script,*(['--full-log'] if full else [])],
                              cwd=self.root,env=self.env,capture_output=True,text=True,timeout=15)

    def log_text(self):
        files=list((self.root/'logs/runtime').glob('run_*.log'))
        self.assertEqual(len(files),1)
        return files[0].read_text()

    def test_full_stdout_stderr_multibyte_and_incomplete_last_line(self):
        script=f"import os,sys; print({METRIC!r}); os.write(2,'错误中文\\n'.encode()); os.write(1,b'partial tail'); sys.exit(7)"
        result=self.run_script(script)
        self.assertEqual(result.returncode,7,result.stdout+result.stderr)
        saved=self.log_text()
        self.assertIn(METRIC,saved)
        self.assertIn('错误中文',saved)
        self.assertIn('partial tail',saved)
        self.assertIn('错误中文',result.stdout)
        self.assertNotIn('vx=',result.stdout)

    def test_full_option_prints_periodic_details(self):
        result=self.run_script(f'print({METRIC!r})',full=True)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn(METRIC,result.stdout)
        self.assertIn(METRIC,self.log_text())

    def test_multiline_traceback_survives_default_filter(self):
        result=self.run_script("raise ValueError('fixture boom')")
        self.assertNotEqual(result.returncode,0)
        for output in (result.stdout,self.log_text()):
            self.assertIn('Traceback (most recent call last)',output)
            self.assertIn('ValueError: fixture boom',output)

    def test_closed_terminal_does_not_stop_file_or_child(self):
        proc=subprocess.Popen([sys.executable,'-m','g1d_pico.runtime_logging','--',sys.executable,'-u','-c',
                               "import time; print('BEFORE'); time.sleep(.2); print('AFTER')"],cwd=self.root,
                              env=self.env,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
        proc.stdout.close()
        self.assertEqual(proc.wait(timeout=10),0)
        self.assertIn('AFTER',self.log_text())

    def test_sigint_reaches_control_parent_before_bridge(self):
        marker=self.root/'bridge_sigint'
        bridge=f"import signal,time,pathlib; signal.signal(signal.SIGINT,lambda *a:pathlib.Path({str(marker)!r}).touch()); time.sleep(20)"
        script=("import subprocess,sys,time\n"+f"p=subprocess.Popen([sys.executable,'-c',{bridge!r}])\n"
                "print('CONTROL_READY',flush=True)\ntry: time.sleep(20)\n"
                "except KeyboardInterrupt:\n p.terminate(); p.wait(); print('CONTROL_CLEANUP')\n")
        proc=subprocess.Popen([sys.executable,'-u','-m','g1d_pico.runtime_logging','--',sys.executable,'-u','-c',script],
                              cwd=self.root,env=self.env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
        try:
            for _ in range(10):
                if 'CONTROL_READY' in proc.stdout.readline(): break
            else: self.fail('parent did not start')
            proc.send_signal(signal.SIGINT)
            output,_=proc.communicate(timeout=10)
            self.assertEqual(proc.returncode,0,output)
            self.assertIn('CONTROL_CLEANUP',self.log_text())
            self.assertFalse(marker.exists())
        finally:
            if proc.poll() is None: proc.kill(); proc.wait()
            proc.stdout.close()

    def test_second_interrupt_is_forwarded_during_finalization(self):
        script = """
import signal,time
count = 0
def interrupt(*_):
    global count
    count += 1
    print('FINALIZING' if count == 1 else 'CANCEL_SAVE', flush=True)
signal.signal(signal.SIGINT, interrupt)
print('CONTROL_READY', flush=True)
while count < 2: time.sleep(.02)
"""
        proc = subprocess.Popen([sys.executable,'-u','-m','g1d_pico.runtime_logging','--',
                                 sys.executable,'-u','-c',script], cwd=self.root, env=self.env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            for expected in ('CONTROL_READY', 'FINALIZING'):
                for _ in range(10):
                    if expected in proc.stdout.readline(): break
                else: self.fail('missing '+expected)
                proc.send_signal(signal.SIGINT)
            output, _ = proc.communicate(timeout=10)
            self.assertEqual(proc.returncode, 0, output)
            self.assertIn('CANCEL_SAVE', self.log_text())
        finally:
            if proc.poll() is None: proc.kill(); proc.wait()
            proc.stdout.close()

    def test_terminal_hangup_requests_orderly_child_shutdown(self):
        script = ("import time\nprint('CONTROL_READY',flush=True)\ntry: time.sleep(20)\n"
                  "except KeyboardInterrupt: print('HANGUP_CLEANUP',flush=True)\n")
        proc = subprocess.Popen([sys.executable,'-u','-m','g1d_pico.runtime_logging','--',
                                 sys.executable,'-u','-c',script], cwd=self.root, env=self.env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            for _ in range(10):
                if 'CONTROL_READY' in proc.stdout.readline(): break
            else: self.fail('control did not start')
            proc.send_signal(signal.SIGHUP)
            output, _ = proc.communicate(timeout=10)
            self.assertEqual(proc.returncode, 0, output)
            self.assertIn('HANGUP_CLEANUP', self.log_text())
        finally:
            if proc.poll() is None: proc.kill(); proc.wait()
            proc.stdout.close()

    def install_fake_robot_launch(self):
        (self.root/'deploy').mkdir()
        for name in ('runtime_logging.sh','run_pc2_base.sh','run_pc2_arms.sh','record_pc2.sh'):
            shutil.copy2(ROOT/'deploy'/name,self.root/'deploy'/name)
        def executable(path,text):
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text('#!/usr/bin/env bash\n'+text+'\n');path.chmod(0o755)
        executable(self.root/'bin/uname','echo aarch64')
        executable(self.root/'bin/ip','if [[ $1 == -4 ]]; then echo "4: wlan0 inet 192.168.1.118/24"; fi')
        executable(self.root/'bin/openssl','exit 0')
        executable(self.root/'bin/runtime',f'if [[ $1 == -c || $1 == - ]]; then exit 0; fi\nexec "{sys.executable}" "$@"')
        for name in ('g1d_agv_bridge','g1d_arm_bridge','g1d_hand_bridge'):
            executable(self.root/'bridge/build-arm64'/name,'exit 99')
        (self.root/'.local/https').mkdir(parents=True)
        (self.root/'.local/https/cert.pem').touch();(self.root/'.local/https/key.pem').touch()
        (self.root/'g1d_pico/__main__.py').write_text(f'print({METRIC!r})\nprint("FAKE_CONTROL_FINISHED")\n')
        (self.root/'config/recording.jsonc').write_text(json.dumps(dict(
            spool='.local/test-spool', max_cache_gb=.02, min_free_gb=0,
            image_fps=20, cameras=[], camera_config_port=60000)))
        self.env['PATH']=str(self.root/'bin')+os.pathsep+self.env['PATH']
        self.env['PICO_G1D_PYTHON']=str(self.root/'bin/runtime')

    def test_nested_real_launchers_share_one_log_and_full_flag(self):
        self.install_fake_robot_launch()
        result=subprocess.run(['bash','deploy/record_pc2.sh','--full-log','--','--with-hands'],
                              cwd=self.root,env=self.env,capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn(METRIC,result.stdout)
        saved=self.log_text()
        self.assertIn('FAKE_CONTROL_FINISHED',saved)
        self.assertIn('REC SESSION_END',saved)

    def test_preflight_failure_is_automatically_logged(self):
        self.install_fake_robot_launch()
        (self.root/'.local/https/cert.pem').unlink()
        result=subprocess.run(['bash','deploy/run_pc2_arms.sh'],cwd=self.root,env=self.env,
                              capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,2,result.stdout+result.stderr)
        self.assertIn('Missing runtime or certificates',self.log_text())
