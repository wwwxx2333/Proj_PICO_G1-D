"""One writer per L3-delimited episode, supervised without blocking robot control."""
import time
from pathlib import Path

from .recorder import run_worker
from .shutdown import wait_for_recorder


class EpisodeSession:
    def __init__(self, context, settings, metadata, address, notice):
        self.context, self.settings, self.metadata = context, settings, metadata
        self.address, self.notice = address, notice
        self.worker = self.receive = self.stop = None
        self.cutoff = self.reason_code = None
        self.state = 'waiting'
        self.episode = None
        self.finished = self.failed = self.had_failure = False
        self.eof = False
        self.started = self.last_progress = self.last_warning = 0.0

    def waiting_notice(self):
        self.notice('REC WAITING 等待录制：左摇杆回中，松开后长按 L3 约 1 秒开始；A/B 只控制运动。')

    def toggle(self):
        if self.worker is None:
            self._start()
            return 'starting'
        if self.state == 'recording':
            self._stop(manual=True)
            return 'saving'
        self.notice('REC BUSY 正在启动或保存，请等待状态完成，松开 L3 后再操作；本次按键不排队。')
        return 'busy'

    def _start(self):
        self.receive, send = self.context.Pipe(duplex=False)
        self.stop = self.context.Event()
        self.cutoff = self.context.Value('d', 0.0)
        self.reason_code = self.context.Value('i', 0)
        self.finished = self.failed = self.eof = False
        self.episode = None
        self.started = self.last_progress = self.last_warning = time.monotonic()
        self.state = 'starting'
        self.worker = self.context.Process(
            target=run_worker,
            args=(self.settings, {**self.metadata, 'recording_mode': 'manual_l3'},
                  self.address, send, self.stop, self.cutoff, self.reason_code),
            name='g1d-episode-writer')
        try:
            self.worker.start()
        except Exception as exc:
            self.receive.close()
            self.worker = self.receive = None
            self.state = 'waiting'
            self.had_failure = True
            self.notice(f'REC ERROR 无法启动录制进程：{exc}；机器人控制继续。')
        finally:
            send.close()
        if self.worker is not None:
            self.notice('REC STARTING 正在启动录制，请等待 REC RECORDING 提示。')

    def _stop(self, manual):
        if self.state in ('starting', 'recording'):
            self.cutoff.value = time.monotonic()
            self.reason_code.value = int(manual)
            self.stop.set()
            self.state = 'saving'
            self.last_progress = time.monotonic()
            self.notice('REC SAVING 正在保存当前任务，停止新增采集；机器人控制继续。')

    def _message(self, message):
        state = message.get('state')
        if state == 'ready':
            self.episode = message['episode']
            if self.state == 'starting':
                self.state = 'recording'
                self.notice(f'REC RECORDING 正在录制：{self.episode}\n'
                            '再次将左摇杆回中并长按 L3 约 1 秒结束本条；B 暂停运动但继续录制。')
        elif state == 'progress' and self.state == 'recording':
            self.notice(f'REC RECORDING 正在录制 frames={message["frames"]} '
                        f'cache_gb={message["cache_gb"]} camera_age_s={message["camera_age_s"]}')
        elif state == 'stopped':
            self.failed |= message.get('reason') == 'WRITE_ERROR'
            self.state = 'saving'
            self.notice(f'REC STOPPED 录制停止：{message["reason"]}；正在收尾，机器人控制继续。')
        elif state == 'finalizing':
            self.state = 'saving'
            self.notice(f'REC SAVING 正在保存 stage={message["stage"]} '
                        f'{message["completed"]}/{message["total"]}')
        elif state == 'finished':
            self.finished = True
            self.notice(f'REC FINALIZED 文件收尾完成，reason={message["reason"]}；等待录制进程退出。')
        elif state == 'error':
            self.failed = True
            self.notice(f'REC ERROR 录制失败：{message["error"]}；机器人控制继续。')
        self.last_progress = time.monotonic()

    def poll(self):
        if self.worker is None:
            return
        for _ in range(64):
            if self.eof or not self.receive.poll():
                break
            try:
                self._message(self.receive.recv())
            except EOFError:
                self.eof = True
        if not self.worker.is_alive():
            self.worker.join()
            # Reap only after the final status messages have been drained.
            if not self.eof and self.receive.poll():
                return
            self._reap(self.finished and not self.failed and self.worker.exitcode == 0)
            self.waiting_notice()
        elif (time.monotonic() - self.last_progress > 60
              and time.monotonic() - self.last_warning > 60):
            self.last_warning = time.monotonic()
            self.notice(f'REC WAIT 当前状态 {self.state}，超过 60 秒没有进度；仍等待，禁止启动下一条。')

    def _reap(self, successful):
        self.had_failure |= not successful
        if successful:
            self.notice(f'REC SAVED 保存完成：{self.episode}')
        else:
            self.notice(f'REC ERROR 本条录制未确认成功，请保留已有目录检查：{self.episode or "未创建任务目录"}')
        self.receive.close()
        self.worker.close()
        # A killed writer cannot unlink its socket in finally. Only remove it
        # after the process is reaped; no next writer is allowed before this.
        Path(self.address).unlink(missing_ok=True)
        self.worker = self.receive = None
        self.state = 'waiting'

    def finish_control(self, abort_requested):
        if self.worker is not None:
            self._stop(manual=False)
            successful = wait_for_recorder(
                self.worker, self.receive, self.notice, abort_requested,
                finished=self.finished, failed=self.failed, on_message=self._message)
            self._reap(successful)
        return not self.had_failure
