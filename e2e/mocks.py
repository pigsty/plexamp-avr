"""Mock Plexamp HTTP server, Denon AVR telnet server and helpers for E2E tests."""
import base64
import json
import os
import queue
import socket
import socketserver
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
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


def mock_plexamp(stopped=False):
    server = HTTPServer(("127.0.0.1", 0), PlexampHandler)
    server.stopped = threading.Event()
    if stopped:
        server.stopped.set()
    return server


def _volume(value):
    whole, half = divmod(round(value * 2), 2)
    return f"{whole:02d}5" if half else f"{whole:02d}"


class AvrHandler(socketserver.BaseRequestHandler):
    """Persistent telnet session: many \\r-terminated commands per connection."""

    def handle(self):
        server = self.server
        with server.lock:
            server.connections += 1
            server.clients.add(self.request)
        buffer = b""
        try:
            while True:
                chunk = self.request.recv(1024)
                if not chunk:
                    return
                buffer += chunk
                while b"\r" in buffer:
                    line, buffer = buffer.split(b"\r", 1)
                    command = line.decode("ascii").strip()
                    if command:
                        server.process(command, self.request)
        except OSError:
            pass
        finally:
            with server.lock:
                server.clients.discard(self.request)


class MockAvr(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, power="OFF", input_name="GAME"):
        super().__init__(("127.0.0.1", 0), AvrHandler)
        self.lock = threading.Lock()
        self.clients = set()
        self.connections = 0
        self.commands = queue.Queue()
        self.zones = {
            zone: {"power": power, "input": input_name, "volume": 40.0, "mute": "OFF"}
            for zone in ("Z1", "Z2", "Z3")
        }

    @property
    def power(self):
        return self.zones["Z1"]["power"]

    def emit(self, *lines):
        """Send unsolicited events to every connected client, like a real AVR."""
        data = "".join(f"{line}\r" for line in lines).encode("ascii")
        with self.lock:
            clients = list(self.clients)
        for client in clients:
            try:
                client.sendall(data)
            except OSError:
                pass

    def drain_commands(self):
        while True:
            try:
                self.commands.get_nowait()
            except queue.Empty:
                return

    def process(self, command, client):
        main = self.zones["Z1"]
        replies = {
            "ZM?": [f"ZM{main['power']}"],
            "SI?": [f"SI{main['input']}"],
            "MV?": [f"MV{_volume(main['volume'])}", "MVMAX 98"],
            "MU?": [f"MU{main['mute']}"],
        }
        for zone in ("Z2", "Z3"):
            state = self.zones[zone]
            replies[f"{zone}?"] = [
                f"{zone}{state['power']}", f"{zone}{state['input']}", f"{zone}{_volume(state['volume'])}",
            ]
            replies[f"{zone}MU?"] = [f"{zone}MU{state['mute']}"]
        if command.endswith("?"):
            data = "".join(f"{line}\r" for line in replies.get(command, []))
            client.sendall(data.encode("ascii"))
            return
        self.commands.put(command)
        events = self._apply(command)
        if events:
            self.emit(*events)

    def _apply(self, command):
        if command[:2] in ("ZM", "SI", "MV", "MU"):
            zone, head, value = "Z1", command[:2], command[2:]
            if head == "ZM":
                key = "power"
            elif head == "SI":
                key = "input"
            elif head == "MV":
                key = "volume"
            else:
                key = "mute"
        elif command[:2] in ("Z2", "Z3"):
            zone, value = command[:2], command[2:]
            if value in ("ON", "OFF"):
                key = "power"
            elif value in ("MUON", "MUOFF"):
                key, value = "mute", value[2:]
            elif value in ("UP", "DOWN") or value.isdigit():
                key = "volume"
            else:
                key = "input"
        else:
            return []
        state = self.zones[zone]
        step = 0.5 if zone == "Z1" else 1
        if key == "volume":
            if value == "UP":
                state["volume"] = min(98, state["volume"] + step)
            elif value == "DOWN":
                state["volume"] = max(0, state["volume"] - step)
            else:
                state["volume"] = int(value[:2]) + (int(value[2]) / 10 if len(value) == 3 else 0)
            value = _volume(state["volume"])
        else:
            state[key] = value
        prefixes = {"power": "ZM", "input": "SI", "volume": "MV", "mute": "MU"} if zone == "Z1" else {
            "power": zone, "input": zone, "volume": zone, "mute": f"{zone}MU"}
        return [f"{prefixes[key]}{value}"]


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class WebSocketClient:
    """Minimal RFC 6455 client for reading the status stream."""

    def __init__(self, host, port, path="/api/ws", timeout=10, origin=None):
        self.sock = socket.create_connection((host, port), timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(
            (
                f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                + (f"Origin: {origin}\r\n" if origin else "")
                + "\r\n"
            ).encode()
        )
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = self.sock.recv(1)
            if not chunk:
                raise ConnectionError("handshake failed")
            response += chunk
        self.handshake = response.decode()

    def _read(self, size):
        data = b""
        while len(data) < size:
            chunk = self.sock.recv(size - len(data))
            if not chunk:
                raise ConnectionError("WebSocket closed")
            data += chunk
        return data

    def receive(self):
        """Return the next text message, decoded as JSON."""
        while True:
            first, second = self._read(2)
            length = second & 0x7F
            if length == 126:
                (length,) = struct.unpack("!H", self._read(2))
            elif length == 127:
                (length,) = struct.unpack("!Q", self._read(8))
            payload = self._read(length)
            opcode = first & 0x0F
            if opcode == 0x1:
                return json.loads(payload)
            if opcode == 0x8:
                raise ConnectionError("WebSocket closed by server")

    def wait_for(self, predicate, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.sock.settimeout(max(0.1, deadline - time.monotonic()))
            message = self.receive()
            if predicate(message):
                return message
        raise AssertionError("expected WebSocket message not received")

    def close(self):
        try:
            mask = os.urandom(4)
            payload = struct.pack("!H", 1000)
            masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
            self.sock.sendall(bytes([0x88, 0x80 | len(payload)]) + mask + masked)
        except OSError:
            pass
        self.sock.close()


def docker(*args, check=True):
    import subprocess

    return subprocess.run(["docker", *args], check=check, capture_output=True, text=True, timeout=30)


def serve(server):
    """Run a server in a background thread; returns a cleanup callable."""
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1})
    thread.start()

    def cleanup():
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    return cleanup


def run_container(config_path):
    """Start the image's default entrypoint with the given config; returns the container name."""
    import uuid

    container = f"plexamp-avr-e2e-{uuid.uuid4().hex}"
    docker(
        "run", "--detach", "--name", container, "--network", "host",
        "--mount", f"type=bind,src={config_path},dst=/etc/plexamp-avr.conf,readonly",
        os.environ.get("PLEXAMP_AVR_TEST_IMAGE", "plexamp-avr:e2e"),
    )
    return container


def print_logs(container):
    logs = docker("logs", container, check=False)
    print(logs.stdout)
    print(logs.stderr)
