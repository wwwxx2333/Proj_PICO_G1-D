"""PC2-only loopback server: receive PICO motion through an SSH tunnel."""

import argparse
import math
from pathlib import Path
import socket

from .bridge import AgvBridge
from .control import MotionCommand
from .limits import AGV_FORWARD_MPS, AGV_YAW_RPS, LIFT_NORMALIZED, valid_height_window


def _handshake(line: bytes) -> tuple[str, float, float]:
    try:
        version, mode, lower, upper = line.decode("ascii").split()
        lower, upper = float(lower), float(upper)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("invalid bridge handshake") from exc
    if version != "G1D1" or mode not in {"base", "lift", "both"}:
        raise ValueError("unsupported bridge protocol or mode")
    # Base-only mode carries unused legacy height placeholders (0, 1).
    if (not all(math.isfinite(x) for x in (lower, upper)) or not 0 <= lower < upper
            or (mode != 'base' and not valid_height_window(lower, upper))):
        raise ValueError("invalid height limits")
    return mode, lower, upper


def _command(line: bytes) -> MotionCommand:
    try:
        values = [float(value) for value in line.decode("ascii").split()]
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("invalid motion command") from exc
    if len(values) != 3 or not all(math.isfinite(value) for value in values):
        raise ValueError("invalid motion command")
    if any(abs(v) > cap for v, cap in zip(values, (AGV_FORWARD_MPS, AGV_YAW_RPS, LIFT_NORMALIZED))):
        raise ValueError("motion command exceeds hard limit")
    return MotionCommand(*values)


def _read_handshake(connection: socket.socket) -> bytes:
    line = bytearray()
    connection.settimeout(3.0)
    while len(line) <= 128:
        chunk = connection.recv(1)
        if not chunk:
            raise ConnectionError("client closed before handshake")
        if chunk == b"\n":
            return bytes(line)
        line.extend(chunk)
    raise ValueError("handshake too long")


def handle_client(connection: socket.socket, bridge_factory) -> None:
    """Run one operator session; closing it also closes the SDK2 bridge."""
    bridge = None
    try:
        mode, lower, upper = _handshake(_read_handshake(connection))
        bridge = bridge_factory(lower, upper, mode)
        connection.sendall(b"READY\n")
        connection.settimeout(0.1)
        pending = bytearray()
        while True:
            try:
                chunk = connection.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                break
            pending.extend(chunk)
            if len(pending) > 1024:
                raise ValueError("motion command stream too long")
            while b"\n" in pending:
                line, _, remaining = pending.partition(b"\n")
                pending = bytearray(remaining)
                bridge.send(_command(line))
    finally:
        if bridge is not None:
            bridge.close()
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--network-interface", default="eth0")
    parser.add_argument("--bridge-bin", type=Path, default=Path(__file__).resolve().parents[1] / "bridge/build-arm64/g1d_agv_bridge")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be in 1..65535")

    def start_bridge(lower: float, upper: float, mode: str) -> AgvBridge:
        return AgvBridge(args.bridge_bin, args.network_interface, lower, upper, mode)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", args.port))
        server.listen(1)
        print(f"PC2 bridge listening on 127.0.0.1:{args.port}; DDS on {args.network_interface}", flush=True)
        while True:
            try:
                connection, _ = server.accept()
                try:
                    handle_client(connection, start_bridge)
                except (ConnectionError, OSError, RuntimeError, ValueError) as exc:
                    print(f"Operator session ended: {exc}", flush=True)
            except KeyboardInterrupt:
                print("PC2 bridge stopped.", flush=True)
                break


if __name__ == "__main__":
    main()
