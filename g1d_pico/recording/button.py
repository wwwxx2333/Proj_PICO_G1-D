"""L3: press to start, release then hold to stop; nonblocking local IPC only."""
from collections import OrderedDict
import json
import math
import os
from pathlib import Path
import socket
import time
import uuid


class RecordPress:
    """Each physical press has one fixed meaning, even if recording starts mid-press."""
    def __init__(self, stop_hold_s=1.0):
        self.stop_hold_s = stop_hold_s
        self.released = False
        self.last_sample = 0.0
        self.started = None
        self.action = None
        self.fired = False
        self.last_fire = -math.inf
        self.progress = 0
        self.messages = []

    def update(self, pressed, received_at, now, recording):
        self.messages = []
        fresh = (math.isfinite(received_at) and 0 < received_at <= now
                 and now - received_at <= .25)
        gap = self.last_sample and received_at - self.last_sample > .25
        if not fresh or gap:
            if self.started is not None and not self.fired:
                self.messages.append('REC CANCELLED L3 输入中断，本次按键已取消；松开后重新按。')
            self.released, self.started, self.action = False, None, None
            if not fresh:
                return None
        if received_at <= self.last_sample:
            return None
        self.last_sample = received_at
        if not pressed:
            if self.action == 'stop' and not self.fired:
                self.messages.append('REC STOP_CANCELLED 长按不足 1 秒，继续录制；结束需再次按住 L3。')
            self.released, self.started, self.action = True, None, None
            return None
        if self.started is None:
            if not self.released:
                return None
            self.released = False
            self.started = received_at
            self.action = 'stop' if recording else 'start'
            self.fired = False
            self.progress = 0
            if now - self.last_fire < .3:
                self.fired = True
                self.messages.append('REC DEBOUNCE 按键过快，已忽略；松开 L3 后重新按。')
                return None
            if self.action == 'start':
                self.fired, self.last_fire = True, now
                return 'start'
            self.messages.append('REC STOP_HOLD 已按下 L3，保持约 1 秒结束录制：0.0/1.0 秒。')
        if self.fired:
            return None
        duration = received_at - self.started
        if duration >= self.stop_hold_s:
            self.fired, self.last_fire = True, now
            return 'stop'
        progress = int(duration * 4)
        if progress > self.progress:
            self.progress = progress
            self.messages.append(f'REC STOP_HOLD 结束录制长按中：{duration:.1f}/1.0 秒。')
        return None


class RecordingButton:
    """Control-process client; recording failures never change motion commands."""
    def __init__(self, address=None, notice=print):
        self.address = address if address is not None else os.getenv('G1D_RECORD_CONTROL_SOCKET')
        self.notice = notice
        self.detector = RecordPress()
        self.socket = self.path = self.pending = None
        self.state = 'preparing'
        self.last_send = self.last_status = 0.0
        self.previous_ready = None
        if self.address:
            try:
                self.path = Path(self.address).parent / ('button-' + uuid.uuid4().hex[:12] + '.sock')
                self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
                self.socket.setblocking(False)
                self.socket.bind(str(self.path))
            except OSError as exc:
                self.notice(f'REC ERROR 录制按键连接失败：{exc}')
                self.close()

    def step(self, sample, *, control_ready=True, preparation=''):
        if self.socket is None:
            return
        try:
            self._step(sample, control_ready, preparation, time.monotonic())
        except (OSError, ValueError, TypeError) as exc:
            self.pending = None
            self.notice(f'REC ERROR 录制请求失败：{exc}；机器人控制继续。')

    def _send(self, value):
        try:
            self.socket.sendto(json.dumps(value).encode(), self.address)
        except OSError:
            pass  # Bounded retries; no waits in the robot control loop.

    def _step(self, sample, control_ready, preparation, now):
        for _ in range(16):
            try:
                reply = json.loads(self.socket.recv(4096))
            except BlockingIOError:
                break
            if not isinstance(reply, dict):
                continue
            self.state = reply.get('state', self.state)
            if self.pending and reply.get('id') == self.pending['id']:
                self.pending = None
                if reply.get('result') == 'expired':
                    self.notice('REC EXPIRED 录制请求已过期，请松开 L3 后重试。')
        action = self.detector.update(sample.record_button, sample.received_at, now,
                                      self.state in ('starting', 'recording'))
        for message in self.detector.messages:
            self.notice(message)
        fresh = 0 < sample.received_at <= now and now - sample.received_at <= .25
        ready = bool(control_ready and fresh)
        reason = preparation if fresh else '等待新鲜的 PICO 输入'
        if action or ready != self.previous_ready or now - self.last_status >= .5:
            # Sent before a recording request, on the same ordered local socket.
            self._send({'op': 'control_status', 'ready': ready, 'reason': reason, 'time': now})
            self.previous_ready, self.last_status = ready, now
        if action:
            if self.pending:
                self.notice('REC WAIT_ACK 上次请求尚未确认，请等待提示并松开 L3。')
            else:
                self.pending = {'op': 'record', 'action': action, 'id': uuid.uuid4().hex, 'time': now}
                self.last_send = 0.0
                self.notice('REC REQUEST 已收到 L3 按下，请求开始录制。' if action == 'start' else
                            'REC REQUEST 已收到 L3 长按，请求停止录制并保存。')
        if not self.pending:
            return
        if now - self.pending['time'] > 3.0:
            self.pending = None
            self.notice('REC NO_ACK 未收到录制确认，请以 REC 状态提示为准。')
            return
        if now - self.last_send >= .2:
            self.last_send = now
            self._send(self.pending)

    def close(self):
        if self.socket:
            self.socket.close()
        self.socket = None
        if self.path:
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                pass


class RecordingRequests:
    """Supervisor endpoint: explicit start/stop actions with retry deduplication."""
    def __init__(self, path):
        self.path = Path(path)
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.socket.setblocking(False)
        try:
            self.socket.bind(str(path))
        except OSError:
            self.socket.close()
            raise
        self.replies = OrderedDict()

    def poll(self, request_action, update_control, snapshot):
        for _ in range(16):
            try:
                raw, sender = self.socket.recvfrom(4096)
            except BlockingIOError:
                break
            try:
                request = json.loads(raw)
                timestamp = float(request['time'])
                if not math.isfinite(timestamp):
                    continue
                age = time.monotonic() - timestamp
                if request.get('op') == 'control_status':
                    if not 0 <= age <= 1.5 or not isinstance(request.get('ready'), bool):
                        continue
                    update_control(request['ready'], str(request.get('reason', ''))[:200])
                    response = snapshot()
                else:
                    identity, action = request['id'], request['action']
                    if (request.get('op') != 'record' or action not in ('start', 'stop')
                            or not isinstance(identity, str) or len(identity) != 32):
                        continue
                    if identity not in self.replies:
                        result = request_action(action) if 0 <= age <= 3.0 else 'expired'
                        self.replies[identity] = {'id': identity, 'result': result}
                        if len(self.replies) > 128:
                            self.replies.popitem(last=False)
                    response = {**snapshot(), **self.replies[identity]}
                self.socket.sendto(json.dumps(response).encode(), sender)
            except (ValueError, KeyError, TypeError, OSError):
                continue

    def close(self):
        self.socket.close()
        self.path.unlink(missing_ok=True)
