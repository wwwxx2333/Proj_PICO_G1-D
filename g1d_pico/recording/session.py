"""Prepare each writer before L3; starting an episode never initializes robot control."""
import time
from pathlib import Path

from .recorder import run_worker
from .shutdown import wait_for_recorder


class EpisodeSession:
    def __init__(self, context, settings, metadata, address, notice):
        self.context, self.settings, self.metadata = context, settings, metadata
        self.address, self.notice = address, notice
        self.worker = self.receive = self.stop = self.begin = None
        self.cutoff = self.reason_code = None
        self.state = 'idle'
        self.episode = None
        self.finished = self.failed = self.had_failure = False
        self.eof = False
        self.last_progress = self.last_warning = 0.0
        self.control_ready = False
        self.control_reason = '等待 PICO 连接与手臂启动归位'
        self.control_status_at = 0.0
        self.announced_ready = None

    def set_control_ready(self, ready, reason):
        self.control_ready, self.control_reason = ready, reason
        self.control_status_at = time.monotonic()
        self._announce_ready()

    def snapshot(self):
        return {'state': self.state, 'ready': self.state == 'waiting' and self._control_is_ready()}

    def _control_is_ready(self):
        return self.control_ready and time.monotonic() - self.control_status_at <= 1.5

    def _announce_ready(self):
        if self.state != 'waiting':
            return
        ready = self._control_is_ready()
        if ready == self.announced_ready:
            return
        self.announced_ready = ready
        if ready:
            self.notice('REC READY 准备完成：按一下 L3 开始录制；录制中松开后长按约 1 秒结束。A/B 只控制运动。')
        else:
            self.notice('REC PREPARING 录制进程已预热，等待控制准备完成；当前不录制。')

    def request(self, action):
        # Stopping an active episode never depends on homing, A/B, or stick axes.
        if action == 'stop':
            if self.state == 'recording':
                self._stop(manual=True)
                return 'saving'
            if self.state == 'starting':
                self._stop(manual=True)
                return 'saving'
            self.notice('REC NO_ACTIVE 当前没有可停止的录制，或已经在保存。')
            return self.state
        if self.state in ('starting', 'recording', 'saving', 'closing'):
            self.notice('REC BUSY 已在录制或保存，本次开始请求不排队。')
            return self.state
        if self.worker is None:
            self.prepare()
            self.notice('REC PREPARING 正在重新准备录制；看到 REC READY 后再按 L3。')
            return 'preparing'
        if self.state != 'waiting' or not self._control_is_ready():
            reason = '录制进程尚未预热完成' if self.state != 'waiting' else (
                self.control_reason or '控制准备状态尚未更新')
            self.notice(f'REC NOT_READY 尚未开始录制：{reason}；看到 REC READY 后松开并重新按 L3。')
            return 'not_ready'
        self.state = 'starting'
        self.announced_ready = None
        self.begin.set()
        self.notice('REC STARTING 开始保存本条数据；不触发手臂归位或运动接管。')
        return 'starting'

    def prepare(self):
        if self.worker is not None:
            return
        self.receive, send = self.context.Pipe(duplex=False)
        self.stop, self.begin = self.context.Event(), self.context.Event()
        self.cutoff = self.context.Value('d', 0.0)
        self.reason_code = self.context.Value('i', 0)
        self.finished = self.failed = self.eof = False
        self.episode = None
        self.announced_ready = None
        self.last_progress = self.last_warning = time.monotonic()
        self.state = 'preparing'
        self.worker = self.context.Process(
            target=run_worker,
            args=(self.settings, {**self.metadata, 'recording_mode': 'manual_l3'},
                  self.address, send, self.stop, self.cutoff, self.reason_code, self.begin),
            name='g1d-episode-writer')
        try:
            self.worker.start()
        except Exception as exc:
            self.receive.close()
            self.worker.close()
            self.worker = self.receive = None
            self.state = 'error'
            self.had_failure = True
            self.notice(f'REC ERROR 录制进程准备失败：{exc}；机器人控制继续。')
        finally:
            send.close()
        if self.worker is not None:
            self.notice('REC PREPARING 正在预热录制进程和相机连接；尚未创建任务。')

    def _stop(self, manual):
        if self.state in ('starting', 'recording'):
            self.cutoff.value = time.monotonic()
            self.reason_code.value = int(manual)
            self.stop.set()
            self.state = 'saving'
            self.last_progress = time.monotonic()
            self.notice('REC SAVING 已停止新增采集，正在保存当前任务。')

    def _message(self, message):
        state = message.get('state')
        if state == 'prepared':
            if self.state == 'preparing':
                self.state = 'waiting'
                self._announce_ready()
        elif state == 'idle_closed':
            self.finished = True
        elif state == 'ready':
            self.episode = message['episode']
            if self.state == 'starting':
                self.state = 'recording'
                self.notice(f'REC RECORDING 正在录制：{self.episode}\n'
                            '松开 L3 后再次长按约 1 秒结束；无需摇杆回中，B 暂停运动但继续录制。')
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
            if not self.eof and self.receive.poll():
                return
            successful = self.finished and not self.failed and self.worker.exitcode == 0
            self._reap(successful)
            if successful:
                self.prepare()  # Prepare next episode now, never in response to its start press.
        elif self.state == 'waiting':
            self._announce_ready()
        elif (time.monotonic() - self.last_progress > 60
              and time.monotonic() - self.last_warning > 60):
            self.last_warning = time.monotonic()
            self.notice(f'REC WAIT 当前状态 {self.state}，超过 60 秒没有进度；仍等待。')

    def _reap(self, successful):
        self.had_failure |= not successful
        if successful and self.episode:
            self.notice(f'REC SAVED 保存完成：{self.episode}')
        elif successful:
            self.notice('REC CLOSED 尚未开始录制，无任务需要保存。')
        else:
            self.notice(f'REC ERROR 本条未确认成功，请保留目录检查：{self.episode or "未创建任务目录"}；L3 可重新准备。')
        self.receive.close()
        self.worker.close()
        Path(self.address).unlink(missing_ok=True)
        self.worker = self.receive = None
        self.state = 'idle' if successful else 'error'

    def finish_control(self, abort_requested):
        if self.worker is not None:
            if self.state in ('preparing', 'waiting'):
                self.state = 'closing'
                self.stop.set()
            else:
                self._stop(manual=False)
            successful = wait_for_recorder(
                self.worker, self.receive, self.notice, abort_requested,
                finished=self.finished, failed=self.failed, on_message=self._message)
            self._reap(successful and not self.failed)
        return not self.had_failure
