"""Outermost launch supervisor; capture native stdout/stderr, preserve SIGINT order."""
import codecs
import json
import os
import selectors
import signal
import subprocess
import sys
import threading
import time

from . import active, full_log_requested
from .config import Settings
from .files import create_log
from .output import LogOutput, timestamp


class ConsoleForwarder:
    """Recorder output before terminal filtering; never discard its display queue.

    Under the runtime supervisor stdout is a continuously drained local pipe.
    The supervisor, not the robot or recorder, owns disk and terminal I/O.
    """
    def __init__(self):
        self.lock = threading.Lock()

    def write(self, text):
        with self.lock:
            sys.stdout.write(text)
            sys.stdout.flush()


def _read_output(pipe, output, stop):
    decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
    pending = ''
    selector = selectors.DefaultSelector()
    try:
        os.set_blocking(pipe.fileno(), False)
        selector.register(pipe, selectors.EVENT_READ)
        while not stop.is_set():
            if not selector.select(.1):
                continue
            chunk = os.read(pipe.fileno(), 8192)
            if not chunk:
                break
            pending += decoder.decode(chunk)
            while '\n' in pending or len(pending) >= 16384:
                position = pending.find('\n')
                length = min(position if position >= 0 else 16384, 16384)
                output.line(pending[:length])
                pending = pending[length+(position == length):]
        pending += decoder.decode(b'', final=True)
        if pending:
            output.line(pending)
    except (OSError, ValueError) as exc:
        output.notice(f'LOG_CAPTURE_ERROR stdout/stderr 接收异常：{exc}')
    finally:
        selector.close()
        pipe.close()


def run_logged(command, settings=None):
    if not command:
        raise ValueError('A command after -- is required')
    if active():
        # Normal launchers never reach here recursively, but do not create a second log.
        return subprocess.call(command)
    settings = settings or Settings.load()
    path, handle, removed = create_log(settings.directory, settings.max_files)
    full = full_log_requested(command)
    try:
        # Validate initial writing before any robot process can be started.
        handle.write(f'[{timestamp()}] LOG_START mode={"full" if full else "essential"} '
                     f'command={json.dumps(command, ensure_ascii=False)}\n')
        handle.flush()
    except BaseException:
        handle.close()
        raise
    output = LogOutput(handle, settings, full)
    output.notice(f'LOG FILE 完整运行日志：{path}')
    output.notice(f'LOG MODE 终端={"全部" if full else "必要信息"}；文件始终保存全部日志。')
    if removed:
        output.notice(f'LOG ROTATE 已淘汰 {removed} 份最旧的已关闭运行日志。')
    child = reader = None
    stop_reader = threading.Event()
    previous = {}
    result = 1
    try:
        environment = os.environ.copy()
        environment.update(G1D_RUNTIME_LOG_ACTIVE='1', G1D_RUNTIME_LOG_PATH=str(path),
                           PYTHONUNBUFFERED='1', NO_COLOR='1', TERM='dumb')
        child = subprocess.Popen(command, env=environment, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, start_new_session=True, bufsize=0)
        def forward(signum, _frame):
            # Forward to the immediate supervisor only. It retains its existing
            # first-Ctrl+C orderly shutdown / second-Ctrl+C cancel semantics.
            # The child has a separate session; SSH hangup must still initiate
            # cleanup instead of leaving detached robot control behind.
            forwarded = signal.SIGINT if signum == signal.SIGHUP else signum
            if child.poll() is None:
                try:
                    child.send_signal(forwarded)
                except ProcessLookupError:
                    pass
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            previous[signum] = signal.signal(signum, forward)
        reader = threading.Thread(target=_read_output, args=(child.stdout, output, stop_reader),
                                  daemon=True, name='runtime-log-reader')
        reader.start()
        last_report = 0.0
        while child.poll() is None:
            if time.monotonic()-last_report >= 1:
                output.report_health()
                last_report = time.monotonic()
            time.sleep(.05)
        result = child.wait()
    except BaseException as exc:
        output.notice(f'LOG_SUPERVISOR_ERROR {type(exc).__name__}: {exc}')
        raise
    finally:
        if child is not None and child.poll() is None:
            child.send_signal(signal.SIGINT)
            # Do not kill a recorder that is still finalizing. Further user
            # interrupts are forwarded to its existing shutdown handler.
            child.wait()
        if reader is not None:
            reader.join(timeout=3)
            if reader.is_alive():
                output.notice('LOG_CAPTURE_WARNING 子进程退出后输出管道仍未关闭，结束收集。')
                stop_reader.set()
                reader.join(timeout=1)
        output.notice(f'LOG END 程序已退出 exit_code={result}；运行日志：{path}')
        healthy = output.close()
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    if result < 0:
        return 128-result
    return result if result else (0 if healthy else 2)
