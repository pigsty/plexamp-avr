import json
import queue
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from plexamp_avr.web import WebServer
from plexamp_avr.webhooks import WebhookDispatcher, WebhookStore, validate_webhook, zone_events


def hook(**overrides):
    data = {"name": "Amp", "zone": "z1", "event": "power", "value": "off", "method": "POST", "url": "http://127.0.0.1:1/x"}
    data.update(overrides)
    return data


def zones(**overrides):
    result = {zone: {"power": "on", "input": "CD", "volume": 40.0, "muted": False} for zone in ("z1", "z2", "z3")}
    for zone, values in overrides.items():
        result[zone] = {**result[zone], **values}
    return result


class RecordingHandler(BaseHTTPRequestHandler):
    def _handle(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(length)
        self.server.requests.put((self.command, self.path, self.headers.get("Content-Type"), body))
        self.server.request_headers.put(dict(self.headers.items()))
        status = 404 if self.path == "/missing" else 200
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_GET = do_PUT = do_POST = _handle

    def log_message(self, format, *args):
        pass


class Receiver:
    def __init__(self):
        self.server = HTTPServer(("127.0.0.1", 0), RecordingHandler)
        self.server.requests = queue.Queue()
        self.server.request_headers = queue.Queue()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class ValidationTests(unittest.TestCase):
    def test_normalizes_values(self):
        result = validate_webhook(hook(zone="2", event="input", value=" cd ", method="put", url=" https://h/p ", headers={"X-Test": " yes "}))
        self.assertEqual(result["zone"], "z2")
        self.assertEqual(result["value"], "CD")
        self.assertEqual(result["method"], "PUT")
        self.assertEqual(result["url"], "https://h/p")
        self.assertEqual(result["headers"], {"X-Test": " yes "})
        self.assertEqual(validate_webhook(hook())["headers"], {})
        self.assertTrue(result["enabled"])
        self.assertEqual(validate_webhook(hook(event="input", value=""))["value"], "")
        self.assertEqual(validate_webhook(hook(event="mute", value=True))["value"], "on")

    def test_rejects_invalid_values(self):
        for overrides in (
            {"zone": "z4"},
            {"event": "volume"},
            {"value": "maybe"},
            {"value": ""},
            {"method": "DELETE"},
            {"url": "ftp://host/x"},
            {"url": "file:///etc/passwd"},
            {"url": "http://host/a b"},
            {"url": "http://user:pass@<ip>/hook"},
            {"url": "http://host:not-a-port/hook"},
            {"url": "not a url"},
            {"enabled": "yes"},
            {"body": 5},
            {"headers": []},
            {"headers": {"Bad Header": "value"}},
            {"headers": {"X-Test": "bad\r\nInjected: yes"}},
            {"headers": {"X-Test": 5}},
            {"event": "input", "value": "CD\rZMOFF"},
        ):
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValueError):
                    validate_webhook(hook(**overrides))


class StoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name) / "store"

    def test_persists_webhooks(self):
        store = WebhookStore(self.directory)
        self.assertEqual(store.list(), [])
        created = store.create(hook(headers={"X-Test": "persisted"}))
        second = store.create(hook(name="Second", zone="z2"))
        updated = store.update(created["id"], hook(name="Renamed"))
        self.assertEqual(updated["name"], "Renamed")
        self.assertIsNone(store.update("ffff", hook()))
        reloaded = WebhookStore(self.directory)
        self.assertEqual(reloaded.list(), [updated, second])
        self.assertTrue(reloaded.delete(second["id"]))
        self.assertFalse(reloaded.delete(second["id"]))
        self.assertEqual(WebhookStore(self.directory).list(), [updated])
        path = self.directory / "webhooks.json"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        path.chmod(0o644)
        reloaded.create(hook())
        self.assertEqual(path.stat().st_mode & 0o777, 0o644)

    def test_skips_invalid_entries_and_fixes_ids(self):
        self.directory.mkdir()
        (self.directory / "webhooks.json").write_text(json.dumps({"webhooks": [
            {"id": "abc", **hook()}, {"id": "abc", **hook()}, {"id": "../x", **hook()}, {"zone": "bad"},
        ]}))
        store = WebhookStore(self.directory)
        ids = [item["id"] for item in store.list()]
        self.assertEqual(len(ids), 3)
        self.assertEqual(ids[0], "abc")
        self.assertEqual(len(set(ids)), 3)


class EventTests(unittest.TestCase):
    def test_detects_changes(self):
        before = zones()
        after = zones(z1={"power": "off"}, z2={"input": "TUNER", "volume": 41.0}, z3={"muted": True})
        self.assertEqual(
            zone_events(before, after),
            [("z1", "power", "off"), ("z2", "input", "TUNER"), ("z3", "mute", "on")],
        )

    def test_ignores_initial_state(self):
        unknown = {zone: {"power": None, "input": None, "volume": None, "muted": None} for zone in ("z1", "z2", "z3")}
        self.assertEqual(zone_events(unknown, zones()), [])


class DispatcherTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = WebhookStore(temporary.name)
        self.receiver = Receiver()
        self.addCleanup(self.receiver.close)
        self.dispatcher = WebhookDispatcher(self.store, timeout=2)
        self.dispatcher.start()
        self.addCleanup(self.dispatcher.stop)

    def test_calls_matching_webhooks_and_logs_status(self):
        url = self.receiver.url
        self.store.create(hook(name="Z1 off", url=f"{url}/z1off", body='{"a": 1}'))
        self.store.create(hook(name="Z1 on", value="on", url=f"{url}/z1on"))
        self.store.create(hook(name="Disabled", enabled=False, url=f"{url}/disabled"))
        self.store.create(hook(name="Z2 CD", zone="z2", event="input", value="CD", method="PUT", url=f"{url}/z2cd", body="text"))
        self.store.create(hook(name="Z2 any", zone="z2", event="input", value="", method="GET", url=f"{url}/z2any", body="ignored"))
        self.store.create(hook(name="Missing", zone="z3", event="mute", value="on", url=f"{url}/missing"))

        with self.assertLogs("plexamp_avr.webhooks", level="INFO") as logs:
            self.dispatcher.handle_snapshot({"zones": zones(z2={"input": "TUNER"})})
            self.dispatcher.handle_snapshot({"zones": zones(z1={"power": "off"}, z2={"input": "CD"}, z3={"muted": True})})
            received = [self.receiver.server.requests.get(timeout=5) for _ in range(4)]
            self.dispatcher.stop()
        self.assertEqual(received, [
            ("POST", "/z1off", "application/json", b'{"a": 1}'),
            ("PUT", "/z2cd", "text/plain; charset=utf-8", b"text"),
            ("GET", "/z2any", None, b""),
            ("POST", "/missing", None, b""),
        ])
        self.assertTrue(self.receiver.server.requests.empty())
        output = "\n".join(logs.output)
        self.assertIn(f"INFO:plexamp_avr.webhooks:Webhook 'Z1 off' (Z1 power off): POST {url}/z1off -> 200", output)
        self.assertIn(f"Webhook 'Z2 CD' (Z2 input CD): PUT {url}/z2cd -> 200", output)
        self.assertIn(f"INFO:plexamp_avr.webhooks:Webhook 'Missing' (Z3 mute on): POST {url}/missing -> 404", output)

    def test_sends_custom_headers(self):
        webhook = self.store.create(hook(
            url=f"{self.receiver.url}/headers",
            headers={"Authorization": "Bearer abc", "X-Webhook": "projector"},
        ))

        self.assertEqual(self.dispatcher.call(webhook, "Z1 power off"), 200)
        self.receiver.server.requests.get(timeout=5)
        received_headers = self.receiver.server.request_headers.get(timeout=5)
        self.assertEqual(received_headers["Authorization"], "Bearer abc")
        self.assertEqual(received_headers["X-Webhook"], "projector")

    def test_url_userinfo_becomes_basic_auth_and_is_redacted_from_logs(self):
        url = f"http://test-user:p%40ss@127.0.0.1:{self.receiver.server.server_port}/auth"
        webhook = self.store.create(hook(url=url))

        with self.assertLogs("plexamp_avr.webhooks", level="INFO") as logs:
            self.assertEqual(self.dispatcher.call(webhook, "Z1 power off"), 200)

        self.receiver.server.requests.get(timeout=5)
        received_headers = self.receiver.server.request_headers.get(timeout=5)
        self.assertEqual(received_headers["Authorization"], "Basic dGVzdC11c2VyOnBAc3M=")
        self.assertNotIn("test-user", "\n".join(logs.output))
        self.assertNotIn("p%40ss", "\n".join(logs.output))

    def test_invalid_url_encoding_is_reported_not_raised(self):
        webhook = validate_webhook(hook(url=f"{self.receiver.url}/caf\N{LATIN SMALL LETTER E WITH ACUTE}"))
        with self.assertLogs("plexamp_avr.webhooks", level="INFO") as logs:
            result = self.dispatcher.call(webhook, "Z1 power off")
        self.assertIsNone(result)
        self.assertTrue(any("-> failed" in line for line in logs.output))
        self.assertTrue(self.receiver.server.requests.empty())

    def test_api_command_does_not_repeat_recent_avr_event(self):
        self.store.create(hook(url=f"{self.receiver.url}/api-off"))
        self.dispatcher.handle_snapshot({"zones": zones()})
        self.dispatcher.handle_snapshot({"zones": zones(z1={"power": "off"})})

        self.assertEqual(self.receiver.server.requests.get(timeout=5)[1], "/api-off")
        self.dispatcher.handle_command("z1", "power", "off")
        self.assertTrue(self.receiver.server.requests.empty())

    def test_logs_connection_failures(self):
        with self.assertLogs("plexamp_avr.webhooks", level="INFO") as logs:
            self.assertIsNone(self.dispatcher.call(validate_webhook(hook(url="http://127.0.0.1:1/x")), "Z1 power off"))
        self.assertIn("INFO:plexamp_avr.webhooks:Webhook 'Amp' (Z1 power off): POST http://127.0.0.1:1/x -> failed", logs.output)
        self.assertIn("failed", logs.output[0])


class FakeAvr:
    def __init__(self):
        self.commands = []

    def add_listener(self, listener):
        pass

    def remove_listener(self, listener):
        pass

    def send(self, command):
        self.commands.append(command)
        return True


class ApiTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = temporary.name
        self.store = WebhookStore(self.directory)
        self.dispatcher = WebhookDispatcher(self.store)
        self.dispatcher.start()
        self.addCleanup(self.dispatcher.stop)
        self.dispatcher.handle_snapshot({"zones": zones()})
        self.avr = FakeAvr()
        self.server = WebServer(
            self.avr,
            "127.0.0.1",
            0,
            ("MPLAY", "BD"),
            self.store,
            {"MPLAY": "Apple TV"},
            self.dispatcher.handle_command,
        )
        self.server.start()
        self.addCleanup(self.server.stop)

    def request(self, method, path, body=None, content_type="application/json"):
        data = None if body is None else json.dumps(body).encode()
        request = Request(f"http://127.0.0.1:{self.server.port}{path}", data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", content_type)
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except HTTPError as err:
            return err.code, json.loads(err.read())

    def test_crud(self):
        self.assertEqual(self.request("GET", "/api/webhooks"), (200, {"webhooks": []}))
        status, created = self.request("POST", "/api/webhooks", hook())
        self.assertEqual(status, 201)
        path = f"/api/webhooks/{created['id']}"
        self.assertEqual(self.request("GET", path), (200, created))
        status, updated = self.request("PUT", path, hook(value="on", method="GET"))
        self.assertEqual(status, 200)
        self.assertEqual((updated["value"], updated["method"]), ("on", "GET"))
        self.assertEqual(self.request("GET", "/api/webhooks"), (200, {"webhooks": [updated]}))
        saved = json.loads((Path(self.directory) / "webhooks.json").read_text())
        self.assertEqual(saved, {"webhooks": [updated]})
        self.assertEqual(self.request("DELETE", path), (200, {"deleted": created["id"]}))
        self.assertEqual(self.request("GET", path)[0], 404)
        self.assertEqual(self.request("GET", "/api/webhooks"), (200, {"webhooks": []}))

    def test_inputs_api_includes_aliases_without_replacing_input_names(self):
        self.assertEqual(
            self.request("GET", "/api/inputs"),
            (200, {"inputs": ["MPLAY", "BD"], "aliases": {"MPLAY": "Apple TV"}}),
        )

    def test_api_commands_fire_webhooks_and_avr_echoes_do_not_duplicate_them(self):
        receiver = Receiver()
        self.addCleanup(receiver.close)
        self.store.create(hook(url=f"{receiver.url}/api-off"))
        self.store.create(hook(event="input", value="MPLAY", url=f"{receiver.url}/api-input"))
        self.store.create(hook(event="mute", value="on", url=f"{receiver.url}/api-mute"))

        current_zones = zones()
        commands = (
            ("power", {"power": "off"}, "ZMOFF", "/api-off", {"power": "off"}),
            ("input", {"input": "MPLAY"}, "SIMPLAY", "/api-input", {"input": "MPLAY"}),
            ("mute", {"muted": True}, "MUON", "/api-mute", {"muted": True}),
        )
        for action, body, command, expected_path, state_change in commands:
            with self.subTest(action=action):
                status, response = self.request("POST", f"/api/zones/z1/{action}", body)
                self.assertEqual(status, 202)
                self.assertEqual(response["command"], command)
                self.assertEqual(receiver.server.requests.get(timeout=5)[1], expected_path)
                self.assertEqual(self.avr.commands[-1], command)

                current_zones["z1"].update(state_change)
                self.dispatcher.handle_snapshot({"zones": current_zones})
                self.assertTrue(receiver.server.requests.empty())

    def test_serves_pwa_install_assets(self):
        for path, expected_type in (
            ("/manifest.webmanifest", "application/manifest+json"),
            ("/sw.js", "text/javascript"),
            ("/apple-touch-icon.png", "image/png"),
            ("/icon-192.png", "image/png"),
            ("/icon-512.png", "image/png"),
        ):
            with self.subTest(path=path), urlopen(f"http://127.0.0.1:{self.server.port}{path}", timeout=5) as response:
                self.assertEqual(response.status, 200)
                self.assertTrue(response.headers["Content-Type"].startswith(expected_type))
                self.assertTrue(response.read())

        with urlopen(f"http://127.0.0.1:{self.server.port}/", timeout=5) as response:
            page = response.read().decode()
        self.assertIn('name="apple-mobile-web-app-capable" content="yes"', page)
        self.assertIn('rel="apple-touch-icon" href="/apple-touch-icon.png"', page)
        self.assertIn('rel="manifest" href="/manifest.webmanifest"', page)

    def test_errors(self):
        cases = (
            ("POST", "/api/webhooks", hook(url="ftp://x"), "application/json", 400),
            ("POST", "/api/webhooks", hook(), "text/plain", 415),
            ("PUT", "/api/webhooks/abc", hook(), "application/json", 404),
            ("DELETE", "/api/webhooks/abc", None, "application/json", 404),
            ("DELETE", "/api/webhooks", None, "application/json", 405),
            ("POST", "/api/webhooks/abc", hook(), "application/json", 405),
            ("GET", "/api/webhooks/not-an-id", None, "application/json", 404),
            ("PUT", "/api/zones/z1/power", {"power": "on"}, "application/json", 404),
        )
        for method, path, body, content_type, expected in cases:
            with self.subTest(method=method, path=path):
                status, response = self.request(method, path, body, content_type)
                self.assertEqual(status, expected)
                self.assertIn("error", response)

    def test_logs_api_requests_and_invalid_reasons(self):
        with self.assertLogs("plexamp_avr.web", level="INFO") as logs:
            self.request("GET", "/api/webhooks")
            self.request("POST", "/api/webhooks", hook(url="ftp://x"))

        output = "\n".join(logs.output)
        self.assertIn("INFO:plexamp_avr.web:API request GET /api/webhooks from 127.0.0.1", output)
        self.assertIn(
            "WARNING:plexamp_avr.web:Invalid API request POST /api/webhooks -> 400: "
            "url must be an absolute http:// or https:// URL",
            output,
        )


if __name__ == "__main__":
    unittest.main()
