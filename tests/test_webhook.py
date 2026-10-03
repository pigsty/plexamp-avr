import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from plexamp_avr.config import Config
from plexamp_avr.webhook import WebhookClient


class RecordingHandler(BaseHTTPRequestHandler):
    def _record(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        self.server.requests.append((self.command, self.path, body))
        self.send_response(204)
        self.end_headers()

    do_POST = do_GET = do_PUT = _record

    def log_message(self, *args):
        pass


class WebhookClientTests(unittest.TestCase):
    def setUp(self):
        self.server = HTTPServer(("127.0.0.1", 0), RecordingHandler)
        self.server.requests = []
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_posts_json_payloads(self):
        client = WebhookClient(f"{self.base}/start", f"{self.base}/expired", background=False)
        client.idle_timer_started("paused", 900)
        client.idle_timer_expired("paused", 900)
        (m1, p1, b1), (m2, p2, b2) = self.server.requests
        self.assertEqual((m1, p1, m2, p2), ("POST", "/start", "POST", "/expired"))
        start, expired = json.loads(b1), json.loads(b2)
        self.assertEqual(start["event"], "idle_timer_started")
        self.assertEqual(start["timeout_seconds"], 900)
        self.assertEqual(expired["event"], "idle_timer_expired")
        self.assertEqual(expired["state"], "paused")

    def test_get_method_sends_no_body(self):
        client = WebhookClient(f"{self.base}/start", None, method="get", background=False)
        client.idle_timer_started("stopped", 60)
        client.idle_timer_expired("stopped", 60)
        self.assertEqual(self.server.requests, [("GET", "/start", b"")])

    def test_failures_are_swallowed(self):
        client = WebhookClient("http://127.0.0.1:1/nope", None, timeout=1, background=False)
        client.idle_timer_started("stopped", 60)

    def test_background_delivery_preserves_order(self):
        client = WebhookClient(f"{self.base}/start", f"{self.base}/expired")
        client.idle_timer_started("paused", 900)
        client.idle_timer_expired("paused", 900)
        for _ in range(100):
            if len(self.server.requests) == 2:
                break
            threading.Event().wait(0.05)
        self.assertEqual([path for _, path, _ in self.server.requests], ["/start", "/expired"])


class ConfigTests(unittest.TestCase):
    def test_webhook_options_parsed(self):
        with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as handle:
            handle.write(
                "[plexamp-avr]\n"
                "idle_timer_start_webhook_url = http://example/hook?a=%20b\n"
                "idle_timer_expired_webhook_url =\n"
                "webhook_method = put\n"
            )
        self.addCleanup(os.unlink, handle.name)
        config = Config.from_file(handle.name)
        self.assertEqual(config.idle_timer_start_webhook_url, "http://example/hook?a=%20b")
        self.assertIsNone(config.idle_timer_expired_webhook_url)
        self.assertEqual(config.webhook_method, "PUT")

    def test_invalid_webhook_method_rejected(self):
        with self.assertRaises(ValueError):
            Config(webhook_method="POTS")


if __name__ == "__main__":
    unittest.main()
