"""Exercise the Wi-Fi transport with a fake SDK2 bridge; no robot is used."""

import socket
import threading
import unittest

from g1d_pico.bridge import RemoteAgvBridge
from g1d_pico.control import MotionCommand
from g1d_pico.remote_server import handle_client


class FakeBridge:
    def __init__(self, lower, upper, mode):
        self.limits = (lower, upper)
        self.mode = mode
        self.commands = []
        self.closed = False

    def send(self, command):
        self.commands.append(command)

    def close(self):
        self.closed = True


class RemoteBridgeTests(unittest.TestCase):
    def test_command_reaches_pc2_and_disconnect_closes_bridge(self):
        bridges = []
        errors = []
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)

            def serve_one():
                connection, _ = listener.accept()
                try:
                    handle_client(connection, lambda *args: bridges.append(FakeBridge(*args)) or bridges[-1])
                except Exception as exc:
                    errors.append(exc)

            worker = threading.Thread(target=serve_one)
            worker.start()
            remote = RemoteAgvBridge(listener.getsockname()[1], 0.02, 0.40, "both")
            remote.send(MotionCommand(0.03, -0.04, 0.05))
            remote.close()
            worker.join(timeout=2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(bridges[0].limits, (0.02, 0.40))
        self.assertEqual(bridges[0].commands[0], MotionCommand(0.03, -0.04, 0.05))
        self.assertEqual(bridges[0].commands[-1], MotionCommand())
        self.assertTrue(bridges[0].closed)

    def test_out_of_range_command_ends_operator_session(self):
        bridges = []
        errors = []
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)

            def serve_one():
                connection, _ = listener.accept()
                try:
                    handle_client(connection, lambda *args: bridges.append(FakeBridge(*args)) or bridges[-1])
                except Exception as exc:
                    errors.append(exc)

            worker = threading.Thread(target=serve_one)
            worker.start()
            with socket.create_connection(listener.getsockname()) as client:
                client.sendall(b"G1D1 base 0 1\n")
                self.assertEqual(client.recv(6), b"READY\n")
                client.sendall(b"1.6 0 0\n")
            worker.join(timeout=2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ValueError)
        self.assertEqual(bridges[0].commands, [])
        self.assertTrue(bridges[0].closed)


if __name__ == "__main__":
    unittest.main()
