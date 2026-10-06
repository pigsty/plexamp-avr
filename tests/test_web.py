import json
import unittest
from unittest.mock import Mock
from urllib.request import urlopen

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
        self.avr = Mock(spec=DenonClient)
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
        self.assertEqual(status["zones"], {})
        self.assertEqual(status["playback"], {"state": "paused", "idle_remaining_seconds": 33})

    def test_playback_and_avr_updates_share_the_status_stream(self):
        client = self.server.register()
        self.addCleanup(self.server.unregister, client)
        self.plexamp.poll_with_retry.return_value = PlaybackState("paused")
        self.controller.step()
        message = json.loads(client.get(timeout=1))
        self.assertEqual(message["type"], "status")
        self.assertEqual(message["playback"], {"state": "paused", "idle_remaining_seconds": 60})
        self.assertTrue(message["connected"])
        self.clock.monotonic.return_value = 27
        listener = self.avr.add_listener.call_args.args[0]
        listener({"connected": False, "zones": {}})
        message = json.loads(client.get(timeout=1))
        self.assertFalse(message["connected"])
        self.assertEqual(message["playback"]["idle_remaining_seconds"], 33)
        self.plexamp.poll_with_retry.return_value = PlaybackState("playing")
        self.controller.step()
        message = json.loads(client.get(timeout=1))
        self.assertEqual(message["playback"], {"state": "playing", "idle_remaining_seconds": None})

    def test_absent_controller_reports_unknown(self):
        self.server.controller = None
        try:
            self.assertEqual(self.server.snapshot()["playback"], {"state": "unknown", "idle_remaining_seconds": None})
        finally:
            self.server.controller = self.controller


if __name__ == "__main__":
    unittest.main()
