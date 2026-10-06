import socket
import threading
import time
import unittest

from plexamp_avr.denon import DenonClient, build_zone_command, format_volume, normalize_zone, parse_volume


class FakeAvrServer:
    """Accepts telnet connections, records commands and answers ZM? queries."""

    def __init__(self):
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.connections = []
        self.commands = []
        self.lock = threading.Condition()
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                connection, _ = self.listener.accept()
            except OSError:
                return
            with self.lock:
                self.connections.append(connection)
                self.lock.notify_all()
            threading.Thread(target=self._serve, args=(connection,), daemon=True).start()

    def _serve(self, connection):
        buffer = b""
        while True:
            try:
                chunk = connection.recv(1024)
            except OSError:
                return
            if not chunk:
                return
            buffer += chunk
            while b"\r" in buffer:
                line, buffer = buffer.split(b"\r", 1)
                command = line.decode()
                with self.lock:
                    self.commands.append(command)
                    self.lock.notify_all()
                if command == "ZM?":
                    try:
                        connection.sendall(b"ZMON\r")
                    except OSError:
                        return

    def wait(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() > deadline:
                return False
            time.sleep(0.01)
        return True

    def send(self, data):
        with self.lock:
            connection = self.connections[-1]
        connection.sendall(data)

    def drop(self):
        with self.lock:
            for connection in self.connections:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                connection.close()

    def close(self):
        self.listener.close()
        self.drop()


class HelperTests(unittest.TestCase):
    def test_volume_parsing_and_formatting(self):
        self.assertEqual(parse_volume("45"), 45)
        self.assertEqual(parse_volume("455"), 45.5)
        self.assertEqual(parse_volume("05"), 5)
        self.assertIsNone(parse_volume("MAX 98"))
        self.assertEqual(format_volume(45), "45")
        self.assertEqual(format_volume(5.5), "055")
        self.assertEqual(format_volume(0), "00")
        with self.assertRaises(ValueError):
            format_volume(98.5)
        with self.assertRaises(ValueError):
            format_volume(20.5, allow_half=False)

    def test_zone_commands(self):
        self.assertEqual(build_zone_command("z1", "power", "on"), "ZMON")
        self.assertEqual(build_zone_command("z2", "power", "off"), "Z2OFF")
        self.assertEqual(build_zone_command("z1", "input", "media player"), "SIMEDIA PLAYER")
        self.assertEqual(build_zone_command("z3", "input", "SAT/CBL"), "Z3SAT/CBL")
        self.assertEqual(build_zone_command("z1", "volume", 50.5), "MV505")
        self.assertEqual(build_zone_command("z2", "volume", "down"), "Z2DOWN")
        self.assertEqual(build_zone_command("z3", "volume", 40), "Z340")
        self.assertEqual(build_zone_command("z1", "mute", True), "MUON")
        self.assertEqual(build_zone_command("z2", "mute", False), "Z2MUOFF")
        for action, value in (("input", "CD\rZMOFF"), ("input", "ON"), ("input", 1), ("mute", "on"),
                              ("volume", True), ("volume", "loud"), ("power", 1)):
            with self.subTest(action=action, value=value), self.assertRaises(ValueError):
                build_zone_command("z2", action, value)

    def test_normalize_zone(self):
        self.assertEqual(normalize_zone("Z2"), "z2")
        self.assertEqual(normalize_zone("3"), "z3")
        self.assertIsNone(normalize_zone("z4"))


class DenonClientTests(unittest.TestCase):
    def setUp(self):
        self.server = FakeAvrServer()
        self.addCleanup(self.server.close)
        self.client = DenonClient("127.0.0.1", self.server.port, timeout=2, reconnect_delay=0.1)
        self.addCleanup(self.client.stop)

    def test_commands_share_one_persistent_connection(self):
        self.assertEqual(self.client.power_state(), "ON")
        self.client.set_input("CD")
        self.client.set_volume(45)
        self.assertTrue(self.server.wait(lambda: "MV45" in self.server.commands))
        self.assertEqual(len(self.server.connections), 1)

    def test_events_update_zone_state_and_notify_listeners(self):
        snapshots = []
        self.client.add_listener(snapshots.append)
        self.assertTrue(self.client.wait_connected(2))
        self.server.send(b"ZMON\rSICD\rMV455\rMVMAX 98\rMUON\rZ2OFF\rZ2TUNER\rZ230\rZ2CVFL 50\rZ3MUOFF\r")
        self.assertTrue(self.server.wait(lambda: snapshots and snapshots[-1]["zones"]["z3"]["muted"] is False))
        zones = self.client.snapshot()["zones"]
        self.assertEqual(zones["z1"], {"power": "on", "input": "CD", "volume": 45.5, "muted": True})
        self.assertEqual(zones["z2"], {"power": "off", "input": "TUNER", "volume": 30, "muted": None})
        self.server.send(b"PWSTANDBY\r")
        self.assertTrue(self.server.wait(lambda: snapshots[-1]["zones"]["z1"]["power"] == "off"))

    def test_reconnects_after_connection_drops(self):
        self.assertTrue(self.client.wait_connected(2))
        self.server.drop()
        self.assertTrue(self.server.wait(lambda: len(self.server.connections) == 2))
        self.assertEqual(self.client.power_state(), "ON")


if __name__ == "__main__":
    unittest.main()
