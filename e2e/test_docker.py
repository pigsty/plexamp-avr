import os
import queue
import socketserver
import subprocess
import tempfile
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


class PlexampHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        if (
            url.path != "/player/timeline/poll"
            or query.get("type") != ["music"]
            or query.get("wait") != ["1"]
            or "commandID" not in query
        ):
            self.send_error(400)
            return
        time.sleep(0.1)
        state = "stopped" if self.server.stopped.is_set() else "playing"
        body = f'<MediaContainer><Timeline type="music" state="{state}"/></MediaContainer>'.encode()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/xml")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, format, *args):
        pass


class AvrHandler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(2)
        data = b""
        while not data.endswith(b"\r") and len(data) < 128:
            chunk = self.request.recv(128)
            if not chunk:
                return
            data += chunk
        command = data.decode("ascii").strip()
        if command == "ZM?":
            reply = f"ZM{self.server.power}"
        elif command == "SI?":
            reply = f"SI{self.server.input_name}"
        else:
            if command.startswith("ZM"):
                self.server.power = command[2:]
            elif command.startswith("SI"):
                self.server.input_name = command[2:]
            self.server.commands.put(command)
            reply = command
        self.request.sendall((reply + "\r").encode("ascii"))


class DockerTests(unittest.TestCase):
    def docker(self, *args, check=True):
        return subprocess.run(
            ["docker", *args], check=check, capture_output=True, text=True, timeout=30
        )

    def start_server(self, server):
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1})
        thread.start()

        def cleanup():
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.addCleanup(cleanup)
        return server.server_address[1]

    def test_playback_configures_avr_and_idle_puts_it_in_standby(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        plexamp = HTTPServer(("127.0.0.1", 0), PlexampHandler)
        plexamp.stopped = threading.Event()
        plexamp_port = self.start_server(plexamp)
        avr = socketserver.TCPServer(("127.0.0.1", 0), AvrHandler)
        avr.power = "OFF"
        avr.input_name = "GAME"
        avr.commands = queue.Queue()
        avr_port = self.start_server(avr)
        config = Path(temporary.name) / "plexamp-avr.conf"
        config.write_text(
            "[plexamp-avr]\n"
            "plexamp_host=127.0.0.1\n"
            f"plexamp_port={plexamp_port}\n"
            "avr_host=127.0.0.1\n"
            f"avr_port={avr_port}\n"
            "avr_input=MEDIA PLAYER\n"
            "preset_volume=45\n"
            "power_on_delay_seconds=0\n"
            "off_timer_seconds=1\n"
            "request_timeout_seconds=2\n"
        )
        container = f"plexamp-avr-e2e-{uuid.uuid4().hex}"
        self.addCleanup(self.docker, "rm", "--force", container, check=False)
        self.docker(
            "run", "--detach", "--name", container, "--network", "host",
            "--mount", f"type=bind,src={config},dst=/etc/plexamp-avr.conf,readonly",
            os.environ.get("PLEXAMP_AVR_TEST_IMAGE", "plexamp-avr:e2e"),
        )
        try:
            for expected in ("ZMON", "SIMEDIA PLAYER", "MV45"):
                self.assertEqual(avr.commands.get(timeout=20), expected)
            plexamp.stopped.set()
            self.assertEqual(avr.commands.get(timeout=20), "ZMOFF")
            self.assertEqual(
                self.docker("inspect", "--format", "{{.State.Running}}", container).stdout.strip(),
                "true",
            )
        except Exception:
            logs = self.docker("logs", container, check=False)
            print(logs.stdout)
            print(logs.stderr)
            raise


if __name__ == "__main__":
    unittest.main()
