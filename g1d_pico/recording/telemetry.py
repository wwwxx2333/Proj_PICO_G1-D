"""Best-effort, bounded local telemetry; never raises into robot control.

No disk writes, DDS subscriptions, TCP, waits or retries in emit(). A missing
recorder or a full Unix datagram queue drops observations, not control commands.
"""
from dataclasses import fields, is_dataclass
import json
import os
import socket
import threading
import time


def jsonable(value):
    if is_dataclass(value):
        return {f.name: jsonable(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if hasattr(value, 'tolist'):
        return value.tolist()
    return value


class Telemetry:
    def __init__(self, address=None):
        self.address = address if address is not None else os.getenv('G1D_RECORD_SOCKET')
        self.socket = None
        self.lock = threading.Lock()
        self.seq = 0
        self.dropped = 0
        if self.address:
            try:
                self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
                self.socket.setblocking(False)
            except OSError:
                self.address = None

    def emit(self, stream, payload):
        if not self.address or self.socket is None:
            return False
        if not self.lock.acquire(blocking=False):
            self.dropped += 1
            return False
        try:
            self.seq += 1
            event = dict(stream=stream, producer=f'python:{os.getpid()}', seq=self.seq,
                         emitted_monotonic_s=time.monotonic(), dropped_total=self.dropped,
                         data=jsonable(payload))
            data = json.dumps(event, separators=(',', ':'), ensure_ascii=False,
                              allow_nan=False).encode('utf-8')
            if len(data) > 60000:
                raise ValueError('telemetry message too large')
            self.socket.sendto(data, self.address)
            return True
        except Exception:
            self.dropped += 1
            return False
        finally:
            self.lock.release()

    def close(self):
        if self.socket:
            self.socket.close()
        self.socket = None


_sink = None

def emit(stream, payload):
    global _sink
    # No socket is created in the ordinary, non-recording launch.
    if not os.getenv('G1D_RECORD_SOCKET'):
        return False
    if _sink is None:
        _sink = Telemetry()
    return _sink.emit(stream, payload)
