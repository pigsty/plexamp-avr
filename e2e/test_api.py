import json
import tempfile
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from mocks import MockAvr, WebSocketClient, docker, free_port, mock_plexamp, print_logs, run_container, serve

HOST = "127.0.0.1"


class ApiTests(unittest.TestCase):
    """Runs the Docker image against mocks and exercises the HTTP API and WebSocket."""

    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        plexamp = mock_plexamp(stopped=True)
        cls.addClassCleanup(serve(plexamp))
        cls.avr = MockAvr(power="ON", input_name="GAME")
        cls.addClassCleanup(serve(cls.avr))
        cls.port = free_port()
        config = Path(cls.temporary.name) / "plexamp-avr.conf"
        config.write_text(
            "[plexamp-avr]\n"
            f"plexamp_host={HOST}\n"
            f"plexamp_port={plexamp.server_address[1]}\n"
            f"avr_host={HOST}\n"
            f"avr_port={cls.avr.server_address[1]}\n"
            "off_timer_seconds=3600\n"
            "request_timeout_seconds=2\n"
            f"web_host={HOST}\n"
            f"web_port={cls.port}\n"
            "avr_inputs=CD, tuner, GAME\n"
        )
        cls.container = run_container(config)
        cls.addClassCleanup(docker, "rm", "--force", cls.container, check=False)
        deadline = time.monotonic() + 30
        while True:
            try:
                status, body = cls.request("GET", "/api/status")
                if status == 200 and body["connected"] and body["zones"]["z3"]["muted"] is not None:
                    break
            except OSError:
                pass
            if time.monotonic() > deadline:
                print_logs(cls.container)
                raise AssertionError("API did not become ready")
            time.sleep(0.2)

    def setUp(self):
        self.avr.drain_commands()

    @classmethod
    def request(cls, method, path, body=None, content_type="application/json", raw=False):
        data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
        request = Request(f"http://{HOST}:{cls.port}{path}", data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", content_type)
        try:
            with urlopen(request, timeout=10) as response:
                payload = response.read()
                headers = response.headers
                status = response.status
        except HTTPError as err:
            payload = err.read()
            headers = err.headers
            status = err.code
        if raw:
            return status, headers, payload
        return status, json.loads(payload)

    def wait_for_zone(self, zone, **expected):
        deadline = time.monotonic() + 10
        while True:
            status, body = self.request("GET", f"/api/zones/{zone}")
            self.assertEqual(status, 200)
            if all(body[key] == value for key, value in expected.items()):
                return body
            if time.monotonic() > deadline:
                self.fail(f"zone {zone} state {body} does not match {expected}")
            time.sleep(0.1)

    @staticmethod
    def volume_command(prefix, volume):
        whole = int(volume)
        fraction = round((volume - whole) * 10)
        return f"{prefix}{whole:02d}" + (str(fraction) if fraction else "")

    def test_api_restores_separate_input_volumes_in_each_zone(self):
        for zone, input_prefix, volume_prefix, first_volume in (
            ("z1", "SI", "MV", 31.5),
            ("z2", "Z2", "Z2", 31),
            ("z3", "Z3", "Z3", 31),
        ):
            with self.subTest(zone=zone):
                for input_name, volume in (("DVD", first_volume), ("TV", 42)):
                    status, response = self.request("POST", f"/api/zones/{zone}/input", {"input": input_name})
                    self.assertEqual(status, 202, response)
                    self.assertEqual(self.avr.commands.get(timeout=10), input_prefix + input_name)
                    self.wait_for_zone(zone, input=input_name)
                    status, response = self.request("POST", f"/api/zones/{zone}/volume", {"volume": volume})
                    self.assertEqual(status, 202, response)
                    self.assertEqual(self.avr.commands.get(timeout=10), self.volume_command(volume_prefix, volume))
                    self.wait_for_zone(zone, volume=volume)
                for input_name, volume in (("DVD", first_volume), ("TV", 42)):
                    started = time.monotonic()
                    status, response = self.request("POST", f"/api/zones/{zone}/input", {"input": input_name})
                    self.assertEqual(status, 202, response)
                    self.assertEqual(self.avr.commands.get(timeout=10), input_prefix + input_name)
                    self.assertEqual(self.avr.commands.get(timeout=10), self.volume_command(volume_prefix, volume))
                    self.assertLess(time.monotonic() - started, 5, "matching input echoes should restore early")
                    self.wait_for_zone(zone, input=input_name, volume=volume)

    def test_website_loads(self):
        status, headers, body = self.request("GET", "/", raw=True)
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/html"))
        page = body.decode()
        self.assertIn("<title>Plexamp AVR</title>", page)
        for zone in ("Z1", "Z2", "Z3"):
            self.assertIn(f">{zone}</button>", page)
        self.assertIn('id="playback-state"', page)
        self.assertIn('id="idle-timer"', page)
        for path, content_type in (("/app.js", "text/javascript"), ("/style.css", "text/css")):
            self.assertIn(path.lstrip("/"), page)
            status, headers, body = self.request("GET", path, raw=True)
            self.assertEqual(status, 200)
            self.assertTrue(headers["Content-Type"].startswith(content_type))
            self.assertTrue(body)

    def test_status_reports_all_zones(self):
        status, body = self.request("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertTrue(body["connected"])
        self.assertEqual(body["playback"]["state"], "stopped")
        self.assertGreater(body["playback"]["idle_remaining_seconds"], 0)
        self.assertEqual(sorted(body["zones"]), ["z1", "z2", "z3"])
        for zone in body["zones"].values():
            self.assertEqual(sorted(zone), ["input", "muted", "power", "volume"])
        status, body = self.request("GET", "/api/inputs")
        self.assertEqual(status, 200)
        self.assertEqual(body["inputs"][:3], ["CD", "TUNER", "GAME"])

    def test_zone_commands(self):
        for zone, prefix, volume_prefix, mute_prefix, volume, volume_arg in (
            ("z1", "ZM", "MV", "MU", 45.5, "455"),
            ("z2", "Z2", "Z2", "Z2MU", 30, "30"),
            ("Z3", "Z3", "Z3", "Z3MU", 25, "25"),
        ):
            with self.subTest(zone=zone):
                input_prefix = "SI" if zone == "z1" else prefix
                status, previous = self.request("GET", f"/api/zones/{zone.lower()}")
                self.assertEqual(status, 200)
                cases = (
                    ("power", {"power": "off"}, f"{prefix}OFF", {"power": "off"}),
                    ("power", {"power": "on"}, f"{prefix}ON", {"power": "on"}),
                    ("input", {"input": "cd"}, f"{input_prefix}CD", {"input": "CD"}),
                    ("volume", {"volume": volume}, f"{volume_prefix}{volume_arg}", {"volume": volume}),
                    ("volume", {"volume": "up"}, f"{volume_prefix}UP", {}),
                    ("mute", {"muted": True}, f"{mute_prefix}ON", {"muted": True}),
                    ("mute", {"muted": False}, f"{mute_prefix}OFF", {"muted": False}),
                )
                for action, body, command, expected in cases:
                    status, response = self.request("POST", f"/api/zones/{zone}/{action}", body)
                    self.assertEqual(status, 202, response)
                    self.assertEqual(response, {"zone": zone.lower(), "command": command})
                    self.assertEqual(self.avr.commands.get(timeout=10), command)
                    if action == "power" and body["power"] == "on":
                        self.assertEqual(
                            self.avr.commands.get(timeout=10),
                            self.volume_command(volume_prefix, previous["volume"]),
                        )
                    if expected:
                        self.wait_for_zone(zone.lower(), **expected)

    def test_invalid_requests(self):
        cases = (
            ("GET", "/api/zones/z4", None, "application/json", 404),
            ("POST", "/api/zones/z4/power", {"power": "on"}, "application/json", 404),
            ("POST", "/api/zones/z1/reboot", {}, "application/json", 404),
            ("GET", "/api/zones/z1/power", None, "application/json", 405),
            ("POST", "/api/zones/z1/power", {"power": "maybe"}, "application/json", 400),
            ("POST", "/api/zones/z1/power", {}, "application/json", 400),
            ("POST", "/api/zones/z1/volume", {"volume": 99}, "application/json", 400),
            ("POST", "/api/zones/z2/volume", {"volume": 20.5}, "application/json", 400),
            ("POST", "/api/zones/z1/mute", {"muted": "yes"}, "application/json", 400),
            ("POST", "/api/zones/z1/input", {"input": "CD\rZMOFF"}, "application/json", 400),
            ("POST", "/api/zones/z1/power", b"{not json", "application/json", 400),
            ("POST", "/api/zones/z1/power", b"power=on", "application/x-www-form-urlencoded", 415),
            ("GET", "/api/ws", None, "application/json", 426),
            ("GET", "/nope", None, "application/json", 404),
        )
        for method, path, body, content_type, expected in cases:
            with self.subTest(method=method, path=path, body=body):
                status, response = self.request(method, path, body, content_type)
                self.assertEqual(status, expected)
                self.assertIn("error", response)
        self.assertTrue(self.avr.commands.empty(), "invalid requests must not reach the AVR")

    def test_websocket_rejects_cross_origin_connections(self):
        client = WebSocketClient(HOST, self.port, origin="http://evil.example")
        self.addCleanup(client.close)
        self.assertIn("403", client.handshake.splitlines()[0])

    def test_websocket_streams_status_updates(self):
        client = WebSocketClient(HOST, self.port, origin=f"http://{HOST}:{self.port}")
        self.addCleanup(client.close)
        self.assertIn("101", client.handshake.splitlines()[0])
        initial = client.receive()
        self.assertEqual(initial["type"], "status")
        self.assertTrue(initial["connected"])
        self.assertEqual(initial["playback"]["state"], "stopped")
        self.assertGreater(initial["playback"]["idle_remaining_seconds"], 0)
        self.assertEqual(sorted(initial["zones"]), ["z1", "z2", "z3"])

        # Changes made on the AVR itself (e.g. front panel) are pushed in realtime.
        self.avr.emit("Z3TUNER", "Z333")
        client.wait_for(lambda m: m["zones"]["z3"]["input"] == "TUNER" and m["zones"]["z3"]["volume"] == 33)

        # Changes made through the API are pushed as well.
        self.request("POST", "/api/zones/z2/power", {"power": "off"})
        self.assertEqual(self.avr.commands.get(timeout=10), "Z2OFF")
        client.wait_for(lambda m: m["zones"]["z2"]["power"] == "off")
        self.request("POST", "/api/zones/z2/power", {"power": "on"})
        self.assertEqual(self.avr.commands.get(timeout=10), "Z2ON")
        client.wait_for(lambda m: m["zones"]["z2"]["power"] == "on")
        self.assertEqual(
            self.avr.commands.get(timeout=10),
            self.volume_command("Z2", initial["zones"]["z2"]["volume"]),
        )

    def test_telnet_connection_is_persistent(self):
        for volume in (20, 21, 22):
            status, _ = self.request("POST", "/api/zones/z1/volume", {"volume": volume})
            self.assertEqual(status, 202)
        self.wait_for_zone("z1", volume=22)
        self.assertEqual(self.avr.connections, 1)


if __name__ == "__main__":
    unittest.main()
