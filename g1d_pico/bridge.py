"""Process boundary between Python/XR and the official SDK2 C++ client."""

import os
from pathlib import Path
import socket
import subprocess

from .control import MotionCommand


class AgvBridge:
    def __init__(
        self, binary: Path, network_interface: str,
        height_min_m: float | None, height_max_m: float | None, mode: str,
    ):
        if not binary.is_file():
            raise FileNotFoundError(f"SDK2 bridge not found: {binary}")
        if mode not in {"base", "lift", "both"}:
            raise ValueError(f"unsupported bridge mode: {mode}")
        # Prefer libraries built for this binary's architecture.  The host
        # bridge keeps its existing bridge/lib bundle; PC2 uses build-arm64/lib.
        runtime_lib = binary.resolve().parent / "lib"
        if not runtime_lib.is_dir():
            runtime_lib = Path(__file__).resolve().parents[1] / "bridge" / "lib"
        environment = os.environ.copy()
        if runtime_lib.is_dir():
            environment["LD_LIBRARY_PATH"] = os.pathsep.join(
                part for part in (str(runtime_lib), environment.get("LD_LIBRARY_PATH", "")) if part
            )
        limits = (height_min_m, height_max_m) if mode != "base" else (0.0, 1.0)
        self._process = subprocess.Popen(
            [str(binary), network_interface, str(limits[0]), str(limits[1]), mode],
            stdin=subprocess.PIPE,
            env=environment,
        )
        assert self._process.stdin is not None
        os.set_blocking(self._process.stdin.fileno(), False)

    def send(self, command: MotionCommand) -> None:
        if self._process.poll() is not None:
            raise RuntimeError(f"SDK2 bridge exited with code {self._process.returncode}")
        data = f"{command.forward_mps:.6f} {command.yaw_rps:.6f} {command.lift_normalized:.6f}\n".encode()
        assert self._process.stdin is not None
        written = os.write(self._process.stdin.fileno(), data)
        if written != len(data):
            raise RuntimeError("SDK2 bridge did not accept the complete command")

    def close(self) -> None:
        try:
            self.send(MotionCommand())
        except (OSError, RuntimeError):
            pass
        if self._process.stdin is not None:
            self._process.stdin.close()
        try:
            self._process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            self._process.terminate()
            try:
                self._process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()


class RemoteAgvBridge:
    """Send commands through a local SSH port forward to the PC2 bridge server."""

    def __init__(
        self, port: int, height_min_m: float | None,
        height_max_m: float | None, mode: str,
    ):
        if not 1 <= port <= 65535:
            raise ValueError("remote bridge port must be in 1..65535")
        if mode not in {"base", "lift", "both"}:
            raise ValueError(f"unsupported bridge mode: {mode}")
        limits = (height_min_m, height_max_m) if mode != "base" else (0.0, 1.0)
        self._socket = socket.create_connection(("127.0.0.1", port), timeout=3.0)
        try:
            self._socket.settimeout(3.0)
            self._socket.sendall(f"G1D1 {mode} {limits[0]:.6f} {limits[1]:.6f}\n".encode())
            reply = bytearray()
            while not reply.endswith(b"\n") and len(reply) < 32:
                chunk = self._socket.recv(1)
                if not chunk:
                    break
                reply.extend(chunk)
            if reply != b"READY\n":
                raise RuntimeError(f"PC2 bridge rejected connection: {bytes(reply)!r}")
            self._socket.settimeout(0.1)
        except BaseException:
            self._socket.close()
            raise

    def send(self, command: MotionCommand) -> None:
        line = (f"{command.forward_mps:.6f} {command.yaw_rps:.6f} "
                f"{command.lift_normalized:.6f}\n")
        self._socket.sendall(line.encode())

    def close(self) -> None:
        try:
            self.send(MotionCommand())
        except OSError:
            pass
        self._socket.close()
