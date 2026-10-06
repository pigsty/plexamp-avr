import socket
import threading
import time
import unittest
from unittest.mock import Mock, patch

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
        with self.lock:
            return self.lock.wait_for(predicate, timeout)

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
        self.assertTrue(self.server.wait(lambda: len(self.server.connections) == 1))
        self.server.send(b"ZMON\rSICD\rMV455\rMVMAX 98\rMUON\rZ2OFF\rZ2TUNER\rZ230\rZ2CVFL 50\rZ3MUOFF\r")
        self.assertTrue(self.server.wait(lambda: snapshots and snapshots[-1]["zones"]["z3"]["muted"] is False))
        zones = self.client.snapshot()["zones"]
        self.assertEqual(zones["z1"], {"power": "on", "input": "CD", "volume": 45.5, "muted": True})
        self.assertEqual(zones["z2"], {"power": "off", "input": "TUNER", "volume": 30, "muted": None})
        self.server.send(b"PWSTANDBY\r")
        self.assertTrue(self.server.wait(lambda: snapshots[-1]["zones"]["z1"]["power"] == "off"))

    def test_reconnects_after_connection_drops(self):
        self.assertTrue(self.client.wait_connected(2))
        self.assertTrue(self.server.wait(lambda: len(self.server.connections) == 1))
        self.server.drop()
        self.assertTrue(self.server.wait(lambda: len(self.server.connections) == 2))
        self.assertEqual(self.client.power_state(), "ON")

    def test_disconnect_cancels_restore_and_discards_stale_input(self):
        self.assertTrue(self.client.wait_connected(2))
        self.assertTrue(self.server.wait(lambda: "Z3MU?" in self.server.commands))
        for line in ("SICD", "MV40", "SITV", "MV20"):
            self.client._handle_line(line)
        self.client.send("SICD")
        pending = self.client._restores["z1"]
        self.server.drop()
        self.assertTrue(self.server.wait(lambda: len(self.server.connections) == 2))
        self.client._restore_volume("z1", pending)
        self.assertNotIn("z1", self.client._restores)
        self.assertIsNone(self.client.snapshot()["zones"]["z1"]["input"])
        self.assertEqual(self.client._volumes["z1"]["CD"], 40)


class VolumeMemoryTests(unittest.TestCase):
    def setUp(self):
        self.client = DenonClient("unused")
        self.client._socket = Mock()
        self.client._connected = True
        self.client.wait_connected = Mock(return_value=True)
        self.addCleanup(self.client.stop)
        self.clock = patch("plexamp_avr.denon.COMMAND_INTERVAL_SECONDS", 0)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def commands(self):
        return [call.args[0].decode().strip() for call in self.client._socket.sendall.call_args_list]

    def observe(self, *lines):
        for line in lines:
            self.client._handle_line(line)

    def seed(self, prefix="SI", volume_prefix="MV"):
        self.observe(prefix + "CD", volume_prefix + "40", prefix + "TV", volume_prefix + "20")

    def finish(self, zone="z1"):
        pending = self.client._restores[zone]
        self.client._restore_volume(zone, pending)

    def test_input_timeout_restores_without_blocking_or_reassigning_cached_volume(self):
        self.seed()
        started = time.monotonic()
        self.assertTrue(self.client.send("SICD"))
        self.assertLess(time.monotonic() - started, 1)
        pending = self.client._restores["z1"]
        self.assertEqual(pending.timer.interval, 5)
        self.observe("MV25")
        self.assertEqual(self.client._volumes["z1"], {"CD": 40, "TV": 20})
        self.finish()
        self.assertEqual(self.commands(), ["SICD", "MV40"])
        self.observe("MV40")
        self.assertEqual(self.client._volumes["z1"]["TV"], 20)

    def test_matching_echo_restores_early_even_if_input_unchanged(self):
        self.seed()
        with patch("plexamp_avr.denon.VOLUME_RESTORE_SECONDS", .2):
            self.client.send("SITV")
        self.observe("SITV")
        self.assertTrue(self.wait(lambda: "MV20" in self.commands()))
        self.observe("MV20")
        self.assertNotIn("z1", self.client._restores)

    @staticmethod
    def wait(predicate):
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(.005)
        return False

    def test_real_timeout_without_echo(self):
        self.seed()
        with patch("plexamp_avr.denon.VOLUME_RESTORE_SECONDS", .03):
            self.client.send("SICD")
            self.assertTrue(self.wait(lambda: "MV40" in self.commands()))

    def test_transition_events_do_not_overwrite_destination(self):
        self.seed()
        self.client.send("SICD")
        # Hold the command lock to simulate several events in one AVR packet.
        with self.client._send_lock:
            self.observe("SICD", "MV20", "MV25")
            self.assertEqual(self.client._volumes["z1"]["CD"], 40)
        self.assertTrue(self.wait(lambda: "MV40" in self.commands()))
        self.observe("MV40", "MV45")
        self.assertEqual(self.client._volumes["z1"]["CD"], 45)

    def test_unchanged_volume_event_is_recorded_for_first_use_input(self):
        self.observe("SICD", "MV40")
        self.client.send("SIGAME")
        self.observe("SIGAME")
        self.assertTrue(self.wait(lambda: not self.client._restores))
        self.observe("MV40")
        self.assertEqual(self.client._volumes["z1"], {"CD": 40, "GAME": 40})
        self.assertEqual(self.commands(), ["SIGAME"])

    def test_first_use_volume_in_same_packet_as_input_echo_is_remembered(self):
        self.observe("SITV", "MV20")
        self.client.send("SIGAME")
        with self.client._send_lock:
            self.observe("SIGAME", "MV30")
            self.assertEqual(self.client._volumes["z1"], {"TV": 20, "GAME": 30})
            self.assertNotIn("z1", self.client._restores)
        self.assertEqual(self.commands(), ["SIGAME"])

    def test_first_use_volume_before_input_echo_is_not_remembered(self):
        self.observe("SITV", "MV20")
        self.client.send("SIGAME")
        self.observe("MV30")
        self.assertEqual(self.client._volumes["z1"], {"TV": 20})
        with self.client._send_lock:
            self.observe("SIGAME", "MV30")
            self.assertEqual(self.client._volumes["z1"], {"TV": 20, "GAME": 30})

    def test_all_zones_restore_independently(self):
        for zone, prefix, volume_prefix in (("z1", "SI", "MV"), ("z2", "Z2", "Z2"), ("z3", "Z3", "Z3")):
            self.seed(prefix, volume_prefix)
            self.client.send(prefix + "CD")
            self.finish(zone)
            self.assertIn(volume_prefix + "40", self.commands())
            self.observe(prefix + "CD", volume_prefix + "40")

    def test_power_on_current_input_and_unknown_input(self):
        self.seed()
        self.client.send("ZMON")
        self.finish()
        self.assertIn("MV20", self.commands())
        self.client._cancel_restores()
        self.client._zones["z1"].input = None
        self.client.send("ZMON")
        self.finish()
        pending = self.client._restores["z1"]
        self.assertTrue(pending.ready)
        self.assertIsNone(pending.target)
        self.observe("SICD")
        self.assertTrue(self.wait(lambda: "MV40" in self.commands()))

    def test_power_on_after_unconfirmed_input_uses_destination(self):
        self.seed()
        self.client.send("SICD")
        self.client.send("ZMON")
        self.finish()
        self.assertEqual(self.commands(), ["SICD", "ZMON", "MV40"])

    def test_cold_start_does_not_restore_or_attribute_volume_before_input(self):
        self.observe("MV35", "ZMON", "SICD")
        self.assertEqual(self.client._volumes["z1"], {})
        self.assertEqual(self.commands(), [])
        self.observe("MV35")
        self.assertEqual(self.client._volumes["z1"]["CD"], 35)

    def test_rapid_switch_and_manual_volume_cancel_outdated_restore(self):
        self.seed()
        self.client.send("SICD")
        old = self.client._restores["z1"]
        self.client.send("SITV")
        self.client._restore_volume("z1", old)
        self.client.send("MV30")
        self.observe("SITV", "MV30")
        self.assertEqual(self.commands(), ["SICD", "SITV", "MV30"])
        self.assertEqual(self.client._volumes["z1"]["TV"], 30)

    def test_unsolicited_different_input_cancels_restore_in_each_zone(self):
        for zone, input_prefix, volume_prefix in (("z1", "SI", "MV"), ("z2", "Z2", "Z2"), ("z3", "Z3", "Z3")):
            with self.subTest(zone=zone):
                self.seed(input_prefix, volume_prefix)
                self.client.send(input_prefix + "CD")
                pending = self.client._restores[zone]
                self.observe(input_prefix + "DVD", volume_prefix + "18")
                self.client._restore_volume(zone, pending)
                self.assertNotIn(zone, self.client._restores)
                self.assertEqual(self.client._volumes[zone]["CD"], 40)
                self.assertEqual(self.client._volumes[zone]["DVD"], 18)
                self.assertNotIn(volume_prefix + "40", self.commands())

    def test_superseded_command_echo_does_not_cancel_latest_restore(self):
        self.seed()
        self.client.send("SICD")
        self.client.send("SITV")
        pending = self.client._restores["z1"]
        self.observe("SICD", "MV25")
        self.assertIs(self.client._restores["z1"], pending)
        self.assertEqual(self.client.snapshot()["zones"]["z1"]["input"], "TV")
        self.assertEqual(self.client._volumes["z1"]["CD"], 40)
        self.observe("SITV")
        self.assertTrue(self.wait(lambda: "MV20" in self.commands()))
        self.observe("MV20")
        self.assertNotIn("z1", self.client._restores)

    def test_acknowledged_input_is_not_treated_as_a_superseded_echo(self):
        self.seed()
        with patch.object(self.client, "_schedule_restore"):
            self.client.send("SICD")
            self.observe("SICD")
            self.assertTrue(self.client._restores["z1"].ready)
            self.client.send("SITV")
            pending = self.client._restores["z1"]
            self.observe("SICD", "MV18")
        self.client._restore_volume("z1", pending)
        self.assertNotIn("z1", self.client._restores)
        self.assertEqual(self.client.snapshot()["zones"]["z1"]["input"], "CD")
        self.assertEqual(self.client._volumes["z1"]["CD"], 18)
        self.assertEqual(self.commands(), ["SICD", "SITV"])

    def test_superseded_input_echo_is_ignored_only_once(self):
        self.seed()
        with patch.object(self.client, "_schedule_restore"):
            self.client.send("SICD")
            self.client.send("SITV")
            pending = self.client._restores["z1"]
            self.observe("SICD")
            self.assertIs(self.client._restores["z1"], pending)
            self.assertEqual(self.client.snapshot()["zones"]["z1"]["input"], "TV")
            self.observe("SICD", "MV18")
        self.client._restore_volume("z1", pending)
        self.assertNotIn("z1", self.client._restores)
        self.assertEqual(self.client._volumes["z1"]["CD"], 18)
        self.assertEqual(self.commands(), ["SICD", "SITV"])

    def test_authoritative_input_clears_marker_after_manual_volume_cancel(self):
        self.seed()
        self.client.send("SICD")
        self.client.send("MV35")
        self.observe("SIDVD", "MV18")
        self.assertNotIn("z1", self.client._unconfirmed_inputs)
        self.assertEqual(self.client._volumes["z1"]["DVD"], 18)
        self.client.send("ZMON")
        self.finish()
        self.assertEqual(self.commands(), ["SICD", "MV35", "ZMON", "MV18"])

    def test_unsolicited_input_during_restore_cancels_recording_gate(self):
        self.seed()
        self.client.send("SICD")
        self.finish()
        self.observe("SIDVD", "MV18")
        self.assertNotIn("z1", self.client._restores)
        self.assertEqual(self.client._volumes["z1"]["DVD"], 18)
        self.assertEqual(self.client._volumes["z1"]["CD"], 40)

    def test_manual_volume_before_input_echo_does_not_corrupt_old_input(self):
        self.seed()
        self.client.send("SICD")
        self.client.send("MV35")
        self.observe("MV35")
        self.assertEqual(self.client._volumes["z1"]["TV"], 20)
        self.observe("SICD", "MV35")
        self.assertEqual(self.client._volumes["z1"]["CD"], 35)

    def test_off_standby_and_stop_cancel(self):
        self.seed()
        for cancel in (lambda: self.client.send("ZMOFF"),
                       lambda: self.observe("ZMOFF"),
                       lambda: self.client.send("PWSTANDBY"),
                       lambda: self.observe("PWSTANDBY"),
                       self.client.stop):
            self.client.send("SICD")
            old = self.client._restores["z1"]
            cancel()
            self.client._restore_volume("z1", old)
            self.assertNotIn("z1", self.client._restores)
        self.assertNotIn("MV40", self.commands())

    def test_combined_helper_does_not_restore_outgoing_input(self):
        self.seed()
        with patch("plexamp_avr.denon.time.sleep", side_effect=lambda delay: self.observe("ZMON", "MV60")):
            self.client.power_on_and_configure("CD", .01)
        self.assertEqual(self.commands(), ["ZMON", "SICD"])
        self.assertEqual(self.client._volumes["z1"], {"CD": 40, "TV": 20})
        self.finish()
        self.assertEqual(self.commands()[-1], "MV40")

    def test_restore_send_failure_is_handled(self):
        self.seed()
        self.client.send("SICD")
        self.client._socket.sendall.side_effect = OSError("disconnected")
        self.finish()
        self.assertNotIn("z1", self.client._restores)

    def test_restoration_preserves_command_spacing(self):
        self.seed()
        times = []
        self.client._socket.sendall.side_effect = lambda payload: times.append(time.monotonic())
        with patch("plexamp_avr.denon.COMMAND_INTERVAL_SECONDS", .02):
            self.client.send("SICD")
            self.finish()
        self.assertGreaterEqual(times[1] - times[0], .019)

    def test_concurrent_input_commands_leave_only_last_wire_target_pending(self):
        self.seed()
        threads = [threading.Thread(target=self.client.send, args=(command,))
                   for command in ("SICD", "SITV", "SIGAME") * 5]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        commands = self.commands()
        self.assertEqual(len(commands), 15)
        pending = self.client._restores["z1"]
        self.assertEqual(pending.echo, commands[-1])
        self.assertEqual(pending.target, commands[-1][2:])


if __name__ == "__main__":
    unittest.main()
