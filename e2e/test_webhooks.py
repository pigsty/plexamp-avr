import json
import tempfile
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from mocks import MockAvr, docker, free_port, mock_plexamp, print_logs, run_container, serve, webhook_receiver

HOST = "127.0.0.1"


class WebhookTests(unittest.TestCase):
    """Configures webhooks through the API with a mounted config store and checks they fire."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        plexamp = mock_plexamp(stopped=True)
        self.addCleanup(serve(plexamp))
        self.avr = MockAvr(power="ON", input_name="GAME")
        self.addCleanup(serve(self.avr))
        self.receiver = webhook_receiver()
        self.addCleanup(serve(self.receiver))
        self.data_dir = Path(temporary.name) / "data"
        self.data_dir.mkdir()
        self.port = free_port()
        self.config = Path(temporary.name) / "plexamp-avr.conf"
        self.config.write_text(
            "[plexamp-avr]\n"
            f"plexamp_host={HOST}\n"
            f"plexamp_port={plexamp.server_address[1]}\n"
            f"avr_host={HOST}\n"
            f"avr_port={self.avr.server_address[1]}\n"
            "off_timer_seconds=3600\n"
            "request_timeout_seconds=2\n"
            f"web_host={HOST}\n"
            f"web_port={self.port}\n"
        )

    def start(self):
        container = run_container(self.config, self.data_dir)
        self.addCleanup(docker, "rm", "--force", container, check=False)
        deadline = time.monotonic() + 30
        while True:
            try:
                status, body = self.request("GET", "/api/status")
                if status == 200 and body["connected"] and body["zones"]["z3"]["muted"] is not None:
                    return container
            except OSError:
                pass
            if time.monotonic() > deadline:
                print_logs(container)
                raise AssertionError("API did not become ready")
            time.sleep(0.2)

    def request(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        request = Request(f"http://{HOST}:{self.port}{path}", data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except HTTPError as err:
            return err.code, json.loads(err.read())

    def test_webhooks_are_persisted_and_called_on_avr_events(self):
        container = self.start()
        url = f"http://{HOST}:{self.receiver.server_address[1]}"
        try:
            status, z1_off = self.request("POST", "/api/webhooks", {
                "name": "Z1 off", "zone": "z1", "event": "power", "value": "off",
                "method": "POST", "url": f"{url}/ok/z1-off", "body": '{"state": "off"}',
            })
            self.assertEqual(status, 201, z1_off)
            status, z2_cd = self.request("POST", "/api/webhooks", {
                "name": "Z2 CD", "zone": "z2", "event": "input", "value": "CD",
                "method": "PUT", "url": f"{url}/fail/z2-cd",
            })
            self.assertEqual(status, 201, z2_cd)
            store = self.data_dir / "webhooks.json"
            self.assertTrue(store.exists())
            saved = json.loads(docker("exec", container, "cat", "/data/webhooks.json").stdout)
            self.assertEqual(saved["webhooks"], [z1_off, z2_cd])

            # Restart the container: webhooks are loaded from the mounted config store.
            docker("rm", "--force", container)
            container = self.start()
            self.assertEqual(self.request("GET", "/api/webhooks"), (200, {"webhooks": [z1_off, z2_cd]}))

            self.avr.emit("Z2CD")
            self.assertEqual(self.receiver.requests.get(timeout=10), ("PUT", "/fail/z2-cd", b""))
            self.request("POST", "/api/zones/z1/power", {"power": "off"})
            self.assertEqual(self.receiver.requests.get(timeout=10), ("POST", "/ok/z1-off", b'{"state": "off"}'))
            self.assertTrue(self.receiver.requests.empty())

            deadline = time.monotonic() + 10
            while True:
                logs = docker("logs", container).stderr
                if "-> 204" in logs and "-> 500" in logs:
                    break
                if time.monotonic() > deadline:
                    self.fail(f"webhook calls were not logged:\n{logs}")
                time.sleep(0.2)
            self.assertIn(f"INFO Webhook 'Z1 off' (Z1 power off): POST {url}/ok/z1-off -> 204", logs)
            self.assertIn(f"INFO Webhook 'Z2 CD' (Z2 input CD): PUT {url}/fail/z2-cd -> 500", logs)
        except Exception:
            print_logs(container)
            raise


if __name__ == "__main__":
    unittest.main()
