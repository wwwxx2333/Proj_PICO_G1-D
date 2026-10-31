"""L3 long press and acknowledged local requests; never wait in the control loop."""
from collections import OrderedDict
import json
import math
import os
from pathlib import Path
import socket
import time
import uuid


class LongPress:
    """Require fresh, centred samples for one second and a release between presses."""
    def __init__(self, hold_s=1.0):
        self.hold_s = hold_s
        self.released = False
        self.started = None
        self.last_sample = 0.0

    def update(self, pressed, x, y, received_at, now, deadzone):
        fresh = (math.isfinite(received_at) and 0 < received_at <= now
                 and now - received_at <= .25)
        if not fresh:
            self.released, self.started = False, None
            return False
        # Re-reading the same snapshot cannot extend a button hold.
        if received_at <= self.last_sample:
            return False
        if self.last_sample and received_at - self.last_sample > .25:
            self.released, self.started = False, None
        self.last_sample = received_at
        if not pressed:
            self.released, self.started = True, None
            return False
        if not all(math.isfinite(v) and abs(v) <= deadzone for v in (x, y)):
            self.released, self.started = False, None
            return False
        if not self.released:
            return False
        if self.started is None:
            self.started = received_at
        if received_at - self.started < self.hold_s:
            return False
        self.released, self.started = False, None
        return True


class RecordingButton:
    """Control-process client. Duplicate retries keep the same request id."""
    def __init__(self, address=None, notice=print):
        self.address = address if address is not None else os.getenv('G1D_RECORD_CONTROL_SOCKET')
        self.notice = notice
        self.detector = LongPress()
        self.socket = None
        self.path = None
        self.pending = None
        self.last_send = 0.0
        if self.address:
            try:
                self.path = Path(self.address).parent / ('button-' + uuid.uuid4().hex[:12] + '.sock')
                self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
                self.socket.setblocking(False)
                self.socket.bind(str(self.path))
            except OSError as exc:
                self.notice(f'REC ERROR 录制按键连接失败：{exc}')
                self.close()

    def step(self, sample, deadzone):
        if self.socket is None:
            return
        try:
            self._step(sample, deadzone, time.monotonic())
        except (OSError, ValueError, TypeError) as exc:
            # This optional channel cannot disarm or exit robot control.
            self.pending = None
            self.notice(f'REC ERROR 录制请求失败：{exc}；机器人控制继续。')

    def _step(self, sample, deadzone, now):
        for _ in range(8):
            try:
                raw = self.socket.recv(4096)
            except BlockingIOError:
                break
            reply = json.loads(raw)
            if not isinstance(reply, dict):
                continue
            if self.pending and reply.get('id') == self.pending['id']:
                self.pending = None
                if reply.get('result') == 'expired':
                    self.notice('REC EXPIRED 录制请求已过期，请松开 L3 后重新长按。')
        clicked = self.detector.update(sample.record_button, sample.left_x, sample.left_y,
                                       sample.received_at, now, deadzone)
        if clicked:
            if self.pending:
                self.notice('REC WAIT_ACK 上次录制请求尚未确认，请等待并松开 L3。')
            else:
                self.pending = {'op': 'toggle', 'id': uuid.uuid4().hex, 'time': now}
                self.last_send = 0.0
                self.notice('REC REQUEST 已收到 L3 长按，正在请求切换录制状态。')
        if not self.pending:
            return
        if now - self.pending['time'] > 3.0:
            self.pending = None
            self.notice('REC NO_ACK 未收到录制确认，请以 REC 状态提示为准；不要假定已开始或停止。')
            return
        if now - self.last_send >= .2:
            self.last_send = now
            try:
                self.socket.sendto(json.dumps(self.pending).encode(), self.address)
            except OSError:
                pass  # Bounded retries; no sleeps and no robot-command changes.

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
    """Supervisor endpoint; a repeated datagram must never toggle twice."""
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

    def poll(self, toggle):
        for _ in range(16):
            try:
                raw, sender = self.socket.recvfrom(4096)
            except BlockingIOError:
                break
            try:
                request = json.loads(raw)
                identity = request['id']
                timestamp = float(request['time'])
                if (request.get('op') != 'toggle' or not isinstance(identity, str)
                        or len(identity) != 32 or not math.isfinite(timestamp)):
                    continue
            except (ValueError, KeyError, TypeError):
                continue
            if identity not in self.replies:
                age = time.monotonic() - timestamp
                result = toggle() if 0 <= age <= 3.0 else 'expired'
                self.replies[identity] = json.dumps({'id': identity, 'result': result}).encode()
                if len(self.replies) > 128:
                    self.replies.popitem(last=False)
            try:
                self.socket.sendto(self.replies[identity], sender)
            except OSError:
                pass  # A client retry receives the cached acknowledgement.

    def close(self):
        self.socket.close()
        self.path.unlink(missing_ok=True)
