"""HTTP API, WebSocket status stream and web UI for the Denon AVR."""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import queue
import re
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

from .denon import DenonClient, build_zone_command, normalize_zone
from .service import AvrController
from .webhooks import WebhookStore

LOGGER = logging.getLogger(__name__)

STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json; charset=utf-8"),
    "/sw.js": ("sw.js", "text/javascript; charset=utf-8"),
    "/apple-touch-icon.png": ("apple-touch-icon.png", "image/png"),
    "/icon-192.png": ("icon-192.png", "image/png"),
    "/icon-512.png": ("icon-512.png", "image/png"),
}
# Request body field used by each zone action.
ACTIONS = {"power": "power", "input": "input", "volume": "volume", "mute": "muted"}
MAX_BODY_BYTES = 4096
MAX_WEBHOOK_BODY_BYTES = 65536
MAX_FRAME_BYTES = 65536
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
WS_PING_SECONDS = 25.0
_ZONE_PATH = re.compile(r"/api/zones/([^/]+)")
_ACTION_PATH = re.compile(r"/api/zones/([^/]+)/([^/]+)")
_WEBHOOK_PATH = re.compile(r"/api/webhooks/([0-9a-f]{1,64})")
_CLOSED = None


def _offer(target: queue.Queue, item: Any) -> None:
    """Queue an item, dropping the oldest one when the client is too slow."""
    while True:
        try:
            target.put_nowait(item)
            return
        except queue.Full:
            try:
                target.get_nowait()
            except queue.Empty:
                pass


class WebServer:
    def __init__(
        self,
        avr: DenonClient,
        host: str,
        port: int,
        inputs: Iterable[str] = (),
        webhooks: WebhookStore | None = None,
        input_aliases: dict[str, str] | None = None,
        command_listener: Callable[[str, str, str], None] | None = None,
        controller: AvrController | None = None,
    ):
        self.avr = avr
        self.webhooks = webhooks
        self.inputs = list(dict.fromkeys(inputs))
        self.input_aliases = dict(input_aliases or {})
        self.command_listener = command_listener
        self.controller = controller
        self._clients: set[queue.Queue] = set()
        self._clients_lock = threading.Lock()
        self._static = {
            name: resources.files(__package__).joinpath("web", name).read_bytes()
            for name, _ in STATIC_FILES.values()
        }
        self.httpd = ThreadingHTTPServer((host, port), RequestHandler)
        self.httpd.daemon_threads = True
        self.httpd.app = self  # type: ignore[attr-defined]
        self._thread: threading.Thread | None = None
        avr.add_listener(self._broadcast)
        if controller is not None:
            controller.add_listener(self._broadcast_playback)

    @property
    def port(self) -> int:
        return self.httpd.server_address[1]

    def start(self) -> None:
        self._thread = threading.Thread(target=self.httpd.serve_forever, name="web", daemon=True)
        self._thread.start()
        LOGGER.info("Web UI and API listening on %s:%d", *self.httpd.server_address[:2])

    def stop(self) -> None:
        self.avr.remove_listener(self._broadcast)
        if self.controller is not None:
            self.controller.remove_listener(self._broadcast_playback)
        self.httpd.shutdown()
        self.httpd.server_close()
        with self._clients_lock:
            for client in self._clients:
                _offer(client, _CLOSED)
        if self._thread is not None:
            self._thread.join(timeout=5)

    def register(self) -> queue.Queue:
        client: queue.Queue = queue.Queue(maxsize=50)
        with self._clients_lock:
            self._clients.add(client)
        return client

    def unregister(self, client: queue.Queue) -> None:
        with self._clients_lock:
            self._clients.discard(client)

    def snapshot(self) -> dict[str, Any]:
        return self._with_playback(self.avr.snapshot())

    def _with_playback(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        playback = self.controller.playback_snapshot() if self.controller is not None else {
            "state": "unknown", "idle_remaining_seconds": None,
        }
        return {**snapshot, "playback": playback}

    def _broadcast_playback(self) -> None:
        self._broadcast(self.avr.snapshot())

    def _broadcast(self, snapshot: dict[str, Any]) -> None:
        snapshot = self._with_playback(snapshot)
        message = json.dumps({"type": "status", **snapshot})
        with self._clients_lock:
            for client in self._clients:
                _offer(client, message)


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "plexamp-avr"
    protocol_version = "HTTP/1.1"

    @property
    def app(self) -> WebServer:
        return self.server.app  # type: ignore[attr-defined]

    def _is_api_request(self) -> bool:
        path = urlsplit(self.path).path
        return path == "/api" or path.startswith("/api/")

    def parse_request(self) -> bool:
        if not super().parse_request():
            return False
        if self._is_api_request():
            LOGGER.info("API request %s %s from %s", self.command, urlsplit(self.path).path, self.address_string())
        return True

    def log_message(self, format: str, *args: Any) -> None:
        LOGGER.debug("%s - %s", self.address_string(), format % args)

    def _send(self, status: int, body: bytes, content_type: str, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: Any, headers: dict[str, str] | None = None) -> None:
        self._send(status, json.dumps(payload).encode(), "application/json", headers)

    def _error(self, status: int, message: str, headers: dict[str, str] | None = None) -> None:
        if self._is_api_request() and 400 <= status < 500:
            LOGGER.warning("Invalid API request %s %s -> %d: %s", self.command, urlsplit(self.path).path, status, message)
        self._json(status, {"error": message}, headers)

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/api/ws":
            self._websocket()
        elif path == "/api/status":
            self._json(200, self.app.snapshot())
        elif path == "/api/inputs":
            self._json(200, {"inputs": self.app.inputs, "aliases": self.app.input_aliases})
        elif match := _ZONE_PATH.fullmatch(path):
            zone = normalize_zone(match[1])
            if zone is None:
                self._error(404, "unknown zone")
                return
            snapshot = self.app.avr.snapshot()
            self._json(200, {"zone": zone, "connected": snapshot["connected"], **snapshot["zones"][zone]})
        elif _ACTION_PATH.fullmatch(path):
            self._error(405, "use POST", {"Allow": "POST"})
        elif path.startswith("/api/webhooks"):
            self._webhooks(path)
        elif path in STATIC_FILES:
            name, content_type = STATIC_FILES[path]
            self._send(200, self.app._static[name], content_type)
        else:
            self._error(404, "not found")

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path.startswith("/api/webhooks"):
            self._webhooks(path)
            return
        match = _ACTION_PATH.fullmatch(path)
        if match is None or match[2] not in ACTIONS:
            self._discard_body()
            self._error(404, "not found")
            return
        body = self._read_json()
        if body is _CLOSED:
            return
        zone = normalize_zone(match[1])
        if zone is None:
            self._error(404, "unknown zone")
            return
        action = match[2]
        field = ACTIONS[action]
        if not isinstance(body, dict) or field not in body:
            self._error(400, f"expected a JSON object with a {field!r} field")
            return
        try:
            command = build_zone_command(zone, action, body[field])
        except ValueError as err:
            self._error(400, str(err))
            return
        if not self.app.avr.send(command):
            self._error(503, "AVR is not connected")
            return
        if self.app.command_listener is not None and action in {"power", "input", "mute"}:
            if action == "power":
                event_value = "off" if command.endswith("OFF") else "on"
            elif action == "input":
                event_value = command[2:]
            else:
                event_value = "on" if body[field] else "off"
            self.app.command_listener(zone, action, event_value)
        self._json(202, {"zone": zone, "command": command})

    def do_PUT(self) -> None:
        self._webhooks(urlsplit(self.path).path)

    def do_DELETE(self) -> None:
        self._webhooks(urlsplit(self.path).path)

    def _webhooks(self, path: str) -> None:
        """Handle the webhook config API (GET/POST /api/webhooks, GET/PUT/DELETE /api/webhooks/{id})."""
        store = self.app.webhooks
        match = _WEBHOOK_PATH.fullmatch(path)
        if store is None or (path != "/api/webhooks" and match is None):
            self._discard_body()
            self._error(404, "not found")
            return
        allowed = "GET, POST" if match is None else "GET, PUT, DELETE"
        method = "GET" if self.command == "HEAD" else self.command
        if method not in allowed.split(", "):
            self._discard_body()
            self._error(405, f"use {allowed}", {"Allow": allowed})
            return
        if match is None and method == "GET":
            self._json(200, {"webhooks": store.list()})
            return
        if match is not None and method in {"GET", "DELETE"}:
            self._discard_body()
            webhook_id = match[1]
            if method == "GET":
                webhook = store.get(webhook_id)
                if webhook is None:
                    self._error(404, "unknown webhook")
                else:
                    self._json(200, webhook)
                return
            try:
                deleted = store.delete(webhook_id)
            except OSError as err:
                LOGGER.error("Could not save webhooks: %s", err)
                self._error(500, "could not save webhooks")
                return
            if not deleted:
                self._error(404, "unknown webhook")
                return
            LOGGER.info("Deleted webhook %s", webhook_id)
            self._json(200, {"deleted": webhook_id})
            return
        body = self._read_json(MAX_WEBHOOK_BODY_BYTES)
        if body is _CLOSED:
            return
        try:
            if match is None:
                webhook = store.create(body)
            else:
                webhook = store.update(match[1], body)
        except (KeyError, ValueError) as err:
            self._error(400, str(err))
            return
        except OSError as err:
            LOGGER.error("Could not save webhooks: %s", err)
            self._error(500, "could not save webhooks")
            return
        if webhook is None:
            self._error(404, "unknown webhook")
            return
        LOGGER.info("%s webhook %s (%s)", "Created" if match is None else "Updated", webhook["id"], webhook["name"])
        self._json(201 if match is None else 200, webhook)

    def _discard_body(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if 0 <= length <= MAX_BODY_BYTES:
            self.rfile.read(length)
        else:
            self.close_connection = True

    def _read_json(self, max_bytes: int = MAX_BODY_BYTES) -> Any:
        """Read a JSON request body; sends an error and returns None on failure."""
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self.close_connection = True
            self._error(411, "Content-Length required")
            return _CLOSED
        if length < 0 or length > max_bytes:
            self.close_connection = True
            self._error(413, "request body too large")
            return _CLOSED
        raw = self.rfile.read(length)
        content_type = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if content_type != "application/json":
            self._error(415, "Content-Type must be application/json")
            return _CLOSED
        try:
            body = json.loads(raw)
        except ValueError:
            self._error(400, "invalid JSON")
            return _CLOSED
        return body if body is not None else {}

    def _websocket(self) -> None:
        key = self.headers.get("Sec-WebSocket-Key", "").strip()
        upgrade = self.headers.get("Upgrade", "").lower() == "websocket"
        connection = "upgrade" in self.headers.get("Connection", "").lower()
        if not (upgrade and connection and key):
            self._error(426, "WebSocket upgrade required", {"Upgrade": "websocket"})
            return
        origin = self.headers.get("Origin")
        if origin is not None and urlsplit(origin).netloc.lower() != self.headers.get("Host", "").lower():
            self._error(403, "cross-origin WebSocket connections are not allowed")
            return
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.wfile.flush()
        self.close_connection = True
        WebSocketSession(self, self.app).run()


class WebSocketSession:
    """Pushes AVR status snapshots to one WebSocket client (RFC 6455)."""

    def __init__(self, handler: RequestHandler, app: WebServer):
        self.rfile = handler.rfile
        self.wfile = handler.wfile
        self.connection = handler.connection
        self.app = app
        self.closed = threading.Event()
        self._write_lock = threading.Lock()

    def run(self) -> None:
        client = self.app.register()
        reader = threading.Thread(target=self._read_loop, args=(client,), daemon=True)
        reader.start()
        try:
            self._send_frame(0x1, json.dumps({"type": "status", **self.app.snapshot()}).encode())
            while not self.closed.is_set():
                try:
                    message = client.get(timeout=WS_PING_SECONDS)
                except queue.Empty:
                    self._send_frame(0x9, b"")
                    continue
                if message is _CLOSED:
                    if not self.closed.is_set():
                        self._send_frame(0x8, struct.pack("!H", 1001))
                    break
                self._send_frame(0x1, message.encode())
        except OSError:
            pass
        finally:
            self.app.unregister(client)
            self.closed.set()
            try:
                self.connection.shutdown(2)
            except OSError:
                pass

    def _read_loop(self, client: queue.Queue) -> None:
        try:
            while True:
                opcode, payload = self._read_frame()
                if opcode == 0x8:
                    self._send_frame(0x8, payload[:2])
                    break
                if opcode == 0x9:
                    self._send_frame(0xA, payload)
        except (OSError, ValueError):
            pass
        finally:
            self.closed.set()
            _offer(client, _CLOSED)

    def _read_exact(self, size: int) -> bytes:
        data = self.rfile.read(size)
        if len(data) < size:
            raise ConnectionError("WebSocket closed")
        return data

    def _read_frame(self) -> tuple[int, bytes]:
        first, second = self._read_exact(2)
        opcode = first & 0x0F
        length = second & 0x7F
        if length == 126:
            (length,) = struct.unpack("!H", self._read_exact(2))
        elif length == 127:
            (length,) = struct.unpack("!Q", self._read_exact(8))
        if not second & 0x80:
            raise ValueError("client frames must be masked")
        if length > MAX_FRAME_BYTES:
            raise ValueError("frame too large")
        mask = self._read_exact(4)
        data = self._read_exact(length)
        return opcode, bytes(byte ^ mask[index % 4] for index, byte in enumerate(data))

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        length = len(payload)
        if length < 126:
            header = struct.pack("!BB", 0x80 | opcode, length)
        elif length < 65536:
            header = struct.pack("!BBH", 0x80 | opcode, 126, length)
        else:
            header = struct.pack("!BBQ", 0x80 | opcode, 127, length)
        with self._write_lock:
            self.wfile.write(header + payload)
            self.wfile.flush()
