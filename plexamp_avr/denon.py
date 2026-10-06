from __future__ import annotations

import logging
import re
import socket
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from typing import Any, Callable

LOGGER = logging.getLogger(__name__)

ZONES = ("z1", "z2", "z3")
# Queries sent after every (re)connect to populate the zone state.
STATUS_QUERIES = ("ZM?", "SI?", "MV?", "MU?", "Z2?", "Z2MU?", "Z3?", "Z3MU?")
# Denon recommends at least 50ms between commands.
COMMAND_INTERVAL_SECONDS = 0.05
# Zone 2/3 events that do not describe power, input, volume or mute.
_IGNORED_ZONE_EVENTS = ("CV", "CS", "HPF", "PS", "SLP", "STBY", "QUICK", "SMART", "HDA", "SS")
_INPUT_PATTERN = re.compile(r"[A-Z0-9][A-Z0-9 ./+\-]{0,31}")
_RESERVED_INPUTS = {"ON", "OFF", "UP", "DOWN", "MUON", "MUOFF"}

Listener = Callable[[dict[str, Any]], None]


@dataclass
class ZoneState:
    power: str | None = None
    input: str | None = None
    volume: float | None = None
    muted: bool | None = None


def normalize_zone(zone: str) -> str | None:
    value = zone.strip().lower()
    if not value.startswith("z"):
        value = f"z{value}"
    return value if value in ZONES else None


def parse_volume(value: str) -> float | None:
    if not value.isdigit() or len(value) not in (2, 3):
        return None
    volume = float(value[:2])
    if len(value) == 3:
        volume += int(value[2]) / 10
    return volume


def format_volume(volume: float, allow_half: bool = True) -> str:
    if isinstance(volume, bool) or not isinstance(volume, (int, float)) or not 0 <= volume <= 98:
        raise ValueError("volume must be a number between 0 and 98")
    if not allow_half:
        if volume != int(volume):
            raise ValueError("volume must be a whole number for this zone")
        return f"{int(volume):02d}"
    whole, half = divmod(round(volume * 2), 2)
    return f"{whole:02d}5" if half else f"{whole:02d}"


def normalize_input(name: Any) -> str:
    if not isinstance(name, str):
        raise ValueError("input must be a string")
    value = name.strip().upper()
    if not _INPUT_PATTERN.fullmatch(value) or value in _RESERVED_INPUTS or value.isdigit():
        raise ValueError("invalid input name")
    return value


def _on_off(value: Any, field: str) -> str:
    if isinstance(value, bool):
        return "ON" if value else "OFF"
    if isinstance(value, str) and value.strip().lower() in {"on", "off"}:
        return value.strip().upper()
    raise ValueError(f"{field} must be \"on\" or \"off\"")


def build_zone_command(zone: str, action: str, value: Any) -> str:
    """Build the telnet command for a power, input, volume or mute change."""
    main = zone == "z1"
    prefix = "" if main else zone.upper()
    if action == "power":
        return f"{'ZM' if main else prefix}{_on_off(value, 'power')}"
    if action == "input":
        return f"{'SI' if main else prefix}{normalize_input(value)}"
    if action == "volume":
        volume_prefix = "MV" if main else prefix
        if isinstance(value, str) and value.strip().lower() in {"up", "down"}:
            return f"{volume_prefix}{value.strip().upper()}"
        return f"{volume_prefix}{format_volume(value, allow_half=main)}"
    if action == "mute":
        if not isinstance(value, bool):
            raise ValueError("muted must be true or false")
        return f"{prefix}MU{'ON' if value else 'OFF'}"
    raise ValueError(f"unknown action {action!r}")


class DenonClient:
    """Keeps a single persistent telnet connection to a Denon AVR.

    A background thread owns the connection, reconnects when it drops and
    tracks the power, input, volume and mute state of zones 1-3 from the
    events the AVR sends.
    """

    def __init__(
        self,
        address: str,
        port: int = 23,
        timeout: float = 2.0,
        reconnect_delay: float = 2.0,
        keepalive_seconds: float = 30.0,
    ):
        self.address = address
        self.port = port
        self.timeout = timeout
        self.reconnect_delay = reconnect_delay
        self.keepalive_seconds = keepalive_seconds
        self._zones = {zone: ZoneState() for zone in ZONES}
        self._connected = False
        self._socket: socket.socket | None = None
        self._condition = threading.Condition()
        self._send_lock = threading.Lock()
        self._start_lock = threading.Lock()
        self._lines: deque[tuple[int, str]] = deque(maxlen=200)
        self._line_seq = 0
        self._last_send = 0.0
        self._listeners: list[Listener] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        LOGGER.info("Denon client initialized with address: %s:%d", self.address, self.port)

    # Connection lifecycle -------------------------------------------------

    def start(self) -> None:
        with self._start_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="denon-telnet", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._condition:
            sock = self._socket
        if sock is not None:
            self._shutdown(sock)
        if self._thread is not None:
            self._thread.join(timeout=5)

    @property
    def connected(self) -> bool:
        return self._connected

    def wait_connected(self, timeout: float) -> bool:
        self.start()
        with self._condition:
            return self._condition.wait_for(lambda: self._connected, timeout)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                sock = socket.create_connection((self.address, self.port), self.timeout)
            except OSError as err:
                LOGGER.warning("Denon Telnet connection to %s:%d failed: %s", self.address, self.port, err)
                self._stop.wait(self.reconnect_delay)
                continue
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            sock.settimeout(min(1.0, self.keepalive_seconds))
            with self._condition:
                self._socket = sock
                self._connected = True
                self._condition.notify_all()
            LOGGER.info("Connected to Denon AVR at %s:%d", self.address, self.port)
            self._notify()
            try:
                for query in STATUS_QUERIES:
                    if not self.send(query):
                        break
                self._read_loop(sock)
            except OSError as err:
                if not self._stop.is_set():
                    LOGGER.warning("Denon Telnet connection lost: %s", err)
            finally:
                with self._condition:
                    self._socket = None
                    self._connected = False
                    self._condition.notify_all()
                sock.close()
                self._notify()
            self._stop.wait(self.reconnect_delay)

    def _read_loop(self, sock: socket.socket) -> None:
        buffer = ""
        last_activity = time.monotonic()
        probed = False
        while not self._stop.is_set():
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                idle = time.monotonic() - last_activity
                if idle >= self.keepalive_seconds * 2:
                    raise ConnectionError("AVR stopped responding")
                if idle >= self.keepalive_seconds and not probed:
                    probed = True
                    self.send("ZM?")
                continue
            if not chunk:
                raise ConnectionError("connection closed by AVR")
            last_activity = time.monotonic()
            probed = False
            buffer += chunk.decode("ascii", errors="replace")
            *lines, buffer = buffer.split("\r")
            for line in lines:
                line = line.strip()
                if line:
                    self._handle_line(line)
            if len(buffer) > 4096:
                buffer = ""

    @staticmethod
    def _shutdown(sock: socket.socket) -> None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    # State tracking ------------------------------------------------------

    def add_listener(self, listener: Listener) -> None:
        self._listeners.append(listener)

    def remove_listener(self, listener: Listener) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    def snapshot(self) -> dict[str, Any]:
        with self._condition:
            return {
                "connected": self._connected,
                "zones": {zone: asdict(state) for zone, state in self._zones.items()},
            }

    def _notify(self) -> None:
        snapshot = self.snapshot()
        for listener in list(self._listeners):
            try:
                listener(snapshot)
            except Exception:  # pragma: no cover - defensive
                LOGGER.exception("Denon state listener failed")

    def _handle_line(self, line: str) -> None:
        LOGGER.debug("Received reply from Denon AVR: %r", line)
        with self._condition:
            self._line_seq += 1
            self._lines.append((self._line_seq, line))
            changed = self._apply(line)
            self._condition.notify_all()
        if changed:
            self._notify()

    def _set(self, zone: str, field: str, value: Any) -> bool:
        state = self._zones[zone]
        if getattr(state, field) == value:
            return False
        setattr(state, field, value)
        return True

    def _apply(self, line: str) -> bool:
        if line == "PWSTANDBY":
            return any([self._set(zone, "power", "off") for zone in ZONES])
        head, value = line[:2], line[2:]
        if head == "ZM" and value in {"ON", "OFF"}:
            return self._set("z1", "power", value.lower())
        if head == "SI" and value:
            return self._set("z1", "input", value)
        if head == "MV":
            volume = parse_volume(value)
            return volume is not None and self._set("z1", "volume", volume)
        if head == "MU" and value in {"ON", "OFF"}:
            return self._set("z1", "muted", value == "ON")
        if head in {"Z2", "Z3"} and value:
            zone = head.lower()
            if value in {"ON", "OFF"}:
                return self._set(zone, "power", value.lower())
            if value in {"MUON", "MUOFF"}:
                return self._set(zone, "muted", value == "MUON")
            if value.isdigit():
                volume = parse_volume(value)
                return volume is not None and self._set(zone, "volume", volume)
            if not value.startswith(_IGNORED_ZONE_EVENTS):
                return self._set(zone, "input", value)
        return False

    # Commands -------------------------------------------------------------

    def send(self, command: str) -> bool:
        """Send a raw command over the persistent connection."""
        if "\r" in command or "\n" in command:
            raise ValueError("command must not contain line breaks")
        payload = (command + "\r").encode("ascii")
        if not self.wait_connected(self.timeout):
            LOGGER.warning("Denon Telnet command %r failed: not connected", command)
            return False
        with self._send_lock:
            with self._condition:
                sock = self._socket
            if sock is None:
                LOGGER.warning("Denon Telnet command %r failed: not connected", command)
                return False
            wait = self._last_send + COMMAND_INTERVAL_SECONDS - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            LOGGER.debug("Sending command to Denon AVR: %r", command)
            try:
                sock.sendall(payload)
            except OSError as err:
                LOGGER.warning("Denon Telnet command %r failed: %s", command, err)
                self._shutdown(sock)
                return False
            finally:
                self._last_send = time.monotonic()
        return True

    def command(self, cmd: str, arg: str = "") -> list[str]:
        """Send a command; for queries (arg "?") wait for the matching reply."""
        with self._condition:
            start_seq = self._line_seq
        if not self.send(f"{cmd}{arg}") or arg != "?":
            return []
        deadline = time.monotonic() + self.timeout
        with self._condition:
            while True:
                for seq, line in self._lines:
                    if seq > start_seq and line.startswith(cmd):
                        return [line]
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    LOGGER.warning("No reply from Denon AVR for %r", f"{cmd}{arg}")
                    return []
                self._condition.wait(remaining)

    def power_state(self) -> str:
        for reply in self.command("ZM", "?"):
            return reply[2:].upper()
        return "UNKNOWN"

    def input_name(self) -> str | None:
        for reply in self.command("SI", "?"):
            return reply[2:]
        return None

    def is_on(self) -> bool:
        return self.power_state() == "ON"

    def power_on(self) -> None:
        self.command("ZM", "ON")

    def standby(self) -> None:
        self.command("ZM", "OFF")

    def set_input(self, input_name: str) -> None:
        self.command("SI", input_name)

    def set_volume(self, volume: float) -> None:
        if volume > 98 or volume < 0:
            raise ValueError("preset volume must be between 0 and 98")
        self.command("MV", format_volume(volume))

    def power_on_and_configure(self, input_name: str, volume: float | None, delay: float) -> None:
        self.power_on()
        time.sleep(delay)
        self.set_input(input_name)
        if volume is not None:
            self.set_volume(volume)
