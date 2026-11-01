"""Always drain producers; bounded file/terminal queues are independent."""
from datetime import datetime
import os
import queue
import sys
import threading
import time

from .filter import ANSI, TerminalFilter


def timestamp():
    return datetime.now().astimezone().isoformat(timespec='milliseconds')


class LogOutput:
    def __init__(self, handle, settings, full=False, terminal=None):
        self.handle = handle
        self.settings = settings
        self.terminal = sys.stdout if terminal is None else terminal
        self.filter = TerminalFilter(full, settings.wait_repeat_s)
        self.file_queue = queue.Queue(settings.queue_capacity)
        self.terminal_queue = queue.Queue(settings.queue_capacity)
        self.file_error = self.terminal_error = None
        self.file_drops = self.terminal_drops = 0
        self._reported = (0, 0, None, None)
        self._lock = threading.Lock()
        self.closing = threading.Event()
        self.file_done = threading.Event()
        self.terminal_closing = threading.Event()
        self.writer = threading.Thread(target=self._write_file, daemon=True, name='runtime-log-file')
        self.display = threading.Thread(target=self._write_terminal, daemon=True, name='runtime-log-terminal')
        self.writer.start()
        self.display.start()

    def _enqueue(self, target, text, file):
        try:
            if file and self.file_error:
                raise queue.Full
            target.put_nowait(text)
        except queue.Full:
            with self._lock:
                if file: self.file_drops += 1
                else: self.terminal_drops += 1

    def line(self, text):
        # Call from one reader thread; the filter's state belongs to that thread.
        text = ANSI.sub('', text).rstrip('\r\n')
        stamp = timestamp()
        self._enqueue(self.file_queue, f'[{stamp}] {text}\n', file=True)
        for displayed in self.filter.select(text, time.monotonic()):
            self._enqueue(self.terminal_queue, f'[{stamp}] {displayed}\n', file=False)

    def notice(self, text, *, file=True):
        text = f'[{timestamp()}] {text}\n'
        if file:
            self._enqueue(self.file_queue, text, file=True)
        self._enqueue(self.terminal_queue, text, file=False)

    def report_health(self):
        with self._lock:
            current = self.file_drops, self.terminal_drops, self.file_error, self.terminal_error
        if current != self._reported:
            self._reported = current
            self.notice(f'LOG_HEALTH file_dropped={current[0]} terminal_dropped={current[1]} '
                        f'file_error={current[2]!r} terminal_error={current[3]!r}; '
                        '日志可能不完整，机器人控制不等待日志写盘。', file=not bool(self.file_error))

    def _write_file(self):
        last_flush = time.monotonic()
        try:
            while not (self.closing.is_set() and self.file_queue.empty()):
                try:
                    self.handle.write(self.file_queue.get(timeout=.1))
                except queue.Empty:
                    pass
                if time.monotonic()-last_flush >= self.settings.flush_interval_s:
                    self.handle.flush()
                    last_flush = time.monotonic()
            self.handle.flush()
            os.fsync(self.handle.fileno())
        except (OSError, ValueError) as exc:
            self.file_error = str(exc)
        finally:
            try:
                self.handle.close()  # Also releases the active-file flock.
            except (OSError, ValueError) as exc:
                self.file_error = self.file_error or str(exc)
            self.file_done.set()

    def _write_terminal(self):
        try:
            while not (self.terminal_closing.is_set() and self.terminal_queue.empty()):
                try:
                    text = self.terminal_queue.get(timeout=.1)
                except queue.Empty:
                    continue
                self.terminal.write(text)
                self.terminal.flush()
        except (OSError, ValueError) as exc:
            # A closed SSH/stdout stream must not stop file logging or the robot.
            self.terminal_error = str(exc)
            if self.terminal is sys.stdout:
                # Python flushes stdout again at interpreter shutdown. Redirect
                # only this supervisor's failed display FD, so a broken terminal
                # cannot turn successful control/recording into exit status 120.
                try:
                    with open(os.devnull, 'w') as discard:
                        os.dup2(discard.fileno(), self.terminal.fileno())
                except (OSError, ValueError):
                    pass

    def close(self, timeout=10.0):
        # Called only after control/recording shutdown and pipe draining.
        self.report_health()
        self.closing.set()
        self.writer.join(timeout=timeout)
        if self.writer.is_alive():
            self.file_error = 'runtime log flush exceeded 10s; buffered tail may be incomplete'
        # Late fsync errors cannot be written to the same failed file.
        if self.file_error:
            self.notice('LOG_ERROR 运行日志未完整保存：' + self.file_error, file=False)
        self.terminal_closing.set()
        self.display.join(timeout=1.0)
        return not self.file_error and self.file_drops == 0
