"""Process boundary between Python/XR and the official SDK2 C++ client."""

import os
from pathlib import Path
import subprocess

from .control import MotionCommand
from .bridge_contract import require_current_limits


class AgvBridge:
    def __init__(
        self, binary: Path, network_interface: str,
        height_min_m: float | None, height_max_m: float | None, mode: str,
    ):
        if not binary.is_file():
            raise FileNotFoundError(f"SDK2 bridge not found: {binary}")
        if mode not in {"base", "lift", "both"}:
            raise ValueError(f"unsupported bridge mode: {mode}")
        # Use the matching DDS libraries placed beside the built executable.
        runtime_lib = binary.resolve().parent / "lib"
        environment = os.environ.copy()
        if runtime_lib.is_dir():
            environment["LD_LIBRARY_PATH"] = os.pathsep.join(
                part for part in (str(runtime_lib), environment.get("LD_LIBRARY_PATH", "")) if part
            )
        limits = (height_min_m, height_max_m) if mode != "base" else (0.0, 1.0)
        require_current_limits(binary, environment)
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
