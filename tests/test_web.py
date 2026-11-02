import json
import unittest
from unittest.mock import Mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from plexamp_avr.config import Config
from plexamp_avr.denon import DenonClient
from plexamp_avr.plexamp import PlaybackState
from plexamp_avr.service import AvrController
from plexamp_avr.web import WebServer


class PlaybackStatusTests(unittest.TestCase):
    def setUp(self):
        self.clock = Mock()
        self.clock.monotonic.return_value = 0
        self.plexamp = Mock()
        self.avr = Mock(spec=DenonClient, max_volume=75)
        self.avr.snapshot.return_value = {"connected": True, "zones": {}}
        self.avr.is_on.return_value = False
        self.controller = AvrController(self.plexamp, self.avr, Config(off_timer_seconds=60), self.clock)
        self.server = WebServer(self.avr, "127.0.0.1", 0, controller=self.controller)
        self.server.start()
        self.addCleanup(self.server.stop)

    def test_status_api_includes_current_playback_and_remaining_time(self):
        self.plexamp.poll_with_retry.return_value = PlaybackState("paused")
        self.controller.step()
        self.clock.monotonic.return_value = 27
        with urlopen(f"http://127.0.0.1:{self.server.port}/api/status", timeout=5) as response:
            status = json.load(response)
        self.assertTrue(status["connected"])
        self.assertEqual(status["max_volume"], 75)
        self.assertEqual(status["zones"], {})
        self.assertEqual(status["playback"], {"state": "paused", "idle_remaining_seconds": 33})

    def test_playback_and_avr_updates_share_the_status_stream(self):
        client = self.server.register()
        self.addCleanup(self.server.unregister, client)
        self.plexamp.poll_with_retry.return_value = PlaybackState("paused")
        self.controller.step()
        message = json.loads(client.get(timeout=1))
        self.assertEqual(message["type"], "status")
        self.assertEqual(message["max_volume"], 75)
        self.assertEqual(message["playback"], {"state": "paused", "idle_remaining_seconds": 60})
        self.assertTrue(message["connected"])
        self.clock.monotonic.return_value = 27
        listener = self.avr.add_listener.call_args.args[0]
        self.avr.snapshot.return_value = {"connected": False, "zones": {}}
        listener({"connected": False, "zones": {}})
        message = json.loads(client.get(timeout=1))
        self.assertFalse(message["connected"])
        self.assertEqual(message["playback"]["idle_remaining_seconds"], 33)
        self.plexamp.poll_with_retry.return_value = PlaybackState("playing")
        self.controller.step()
        message = json.loads(client.get(timeout=1))
        self.assertEqual(message["playback"], {"state": "playing", "idle_remaining_seconds": None})

    def test_delayed_callback_does_not_broadcast_a_stale_avr_snapshot(self):
        client = self.server.register()
        self.addCleanup(self.server.unregister, client)
        old_snapshot = {"connected": True, "zones": {}}
        self.avr.snapshot.return_value = {"connected": False, "zones": {}}
        listener = self.avr.add_listener.call_args.args[0]
        listener(old_snapshot)
        message = json.loads(client.get(timeout=1))
        self.assertFalse(message["connected"])

    def test_absent_controller_reports_unknown(self):
        self.server.controller = None
        try:
            self.assertEqual(self.server.snapshot()["playback"], {"state": "unknown", "idle_remaining_seconds": None})
        finally:
            self.server.controller = self.controller


class VolumeLimitApiTests(unittest.TestCase):
    def setUp(self):
        self.avr = DenonClient("unused", max_volume=65)
        self.avr._socket = Mock()
        self.avr._connected = True
        self.avr.wait_connected = Mock(return_value=True)
        self.addCleanup(self.avr.stop)
        self.server = WebServer(self.avr, "127.0.0.1", 0)
        self.server.start()
        self.addCleanup(self.server.stop)

    def test_custom_ceiling_is_exposed_and_numeric_commands_are_capped(self):
        with urlopen(f"http://127.0.0.1:{self.server.port}/api/status", timeout=5) as response:
            self.assertEqual(json.load(response)["max_volume"], 65)
        for zone, prefix in (("z1", "MV"), ("z2", "Z2"), ("z3", "Z3")):
            with self.subTest(zone=zone):
                request = Request(
                    f"http://127.0.0.1:{self.server.port}/api/zones/{zone}/volume",
                    data=b'{"volume": 90}', headers={"Content-Type": "application/json"},
                )
                with urlopen(request, timeout=5) as response:
                    self.assertEqual(response.status, 202)
                    self.assertEqual(json.load(response)["command"], prefix + "65")
                self.avr._socket.sendall.assert_called_with((prefix + "65\r").encode())

    def test_increase_with_unknown_volume_returns_explicit_error(self):
        request = Request(
            f"http://127.0.0.1:{self.server.port}/api/zones/z1/volume",
            data=b'{"volume": "up"}', headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(HTTPError) as caught:
            urlopen(request, timeout=5)
        self.assertEqual(caught.exception.code, 400)
        self.assertIn("current volume", json.load(caught.exception)["error"])
        self.avr._socket.sendall.assert_not_called()

    def test_saved_volumes_are_exposed_in_api_and_status_stream(self):
        self.avr._handle_line("SICD")
        self.avr._handle_line("MV455")
        with urlopen(f"http://127.0.0.1:{self.server.port}/api/status", timeout=5) as response:
            self.assertEqual(json.load(response)["saved_volumes"],
                             {"z1": {"CD": 45.5}, "z2": {}, "z3": {}})
        client = self.server.register()
        self.addCleanup(self.server.unregister, client)
        self.avr._handle_line("MV50")
        message = json.loads(client.get(timeout=1))
        self.assertEqual(message["saved_volumes"]["z1"], {"CD": 50})


if __name__ == "__main__":
    unittest.main()
