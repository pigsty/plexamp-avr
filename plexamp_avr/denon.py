from __future__ import annotations

import json
import logging
import os
import re
import socket
import tempfile
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

LOGGER = logging.getLogger(__name__)

ZONES = ("z1", "z2", "z3")
# Queries sent after every (re)connect to populate the zone state.
STATUS_QUERIES = ("ZM?", "SI?", "MV?", "MU?", "Z2?", "Z2MU?", "Z3?", "Z3MU?")
# Denon recommends at least 50ms between commands.
COMMAND_INTERVAL_SECONDS = 0.05
VOLUME_RESTORE_SECONDS = 5.0
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


@dataclass
class _VolumeRestore:
    target: str | None
    echo: str
    ready: bool = False
    restoring: float | None = None
    timer: threading.Timer | None = None


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
        data_dir: str | Path | None = None,
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
        self._volume_path = Path(data_dir) / "volumes.json" if data_dir is not None else None
        self._volumes: dict[str, dict[str, float]] = {zone: {} for zone in ZONES}
        self._restores: dict[str, _VolumeRestore] = {}
        self._unconfirmed_inputs: dict[str, str] = {}
        self._superseded_inputs: dict[str, set[str]] = {}
        self._load_volumes()
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
            self._cancel_restores()
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
                    self._cancel_restores()
                    self._unconfirmed_inputs.clear()
                    self._socket = None
                    self._connected = False
                    self._zones = {zone: ZoneState() for zone in ZONES}
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
            head, value = line[:2], line[2:]
            zone = "z1" if head == "SI" else head.lower()
            pending = self._restores.get(zone)
            superseded = (pending is not None and value != pending.target
                          and value in self._superseded_inputs.get(zone, set()))
            if superseded:
                changed = False
            else:
                changed = self._apply(line)
                self._volume_event(line)
            self._condition.notify_all()
        if changed:
            self._notify()

    # Volume memory --------------------------------------------------------

    def _load_volumes(self) -> None:
        if self._volume_path is None:
            return
        try:
            data = json.loads(self._volume_path.read_text())
            if not isinstance(data, dict):
                return
            for zone in ZONES:
                entries = data.get(zone, {})
                if not isinstance(entries, dict):
                    continue
                for name, volume in entries.items():
                    try:
                        name = normalize_input(name)
                        format_volume(volume, allow_half=zone == "z1")
                    except (ValueError, TypeError):
                        continue
                    self._volumes[zone][name] = volume
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            LOGGER.warning("Could not load remembered AVR volumes from %s", self._volume_path)

    def _remember_volume(self, zone: str, volume: float) -> None:
        name = self._zones[zone].input
        if name is None or zone in self._unconfirmed_inputs:
            return
        try:
            name = normalize_input(name)
            format_volume(volume, allow_half=zone == "z1")
        except ValueError:
            return
        if self._volumes[zone].get(name) == volume:
            return
        self._volumes[zone][name] = volume
        if self._volume_path is None:
            return
        staging = None
        try:
            self._volume_path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, name = tempfile.mkstemp(prefix=".volumes-", suffix=".json", dir=self._volume_path.parent)
            staging = Path(name)
            with os.fdopen(descriptor, "w") as stream:
                json.dump(self._volumes, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(staging, self._volume_path)
        except OSError:
            LOGGER.warning("Could not persist remembered AVR volumes to %s", self._volume_path)
            try:
                if staging is not None:
                    staging.unlink(missing_ok=True)
            except OSError:
                pass

    def _cancel_restore(self, zone: str) -> None:
        self._superseded_inputs.pop(zone, None)
        pending = self._restores.pop(zone, None)
        if pending is not None and pending.timer is not None:
            pending.timer.cancel()

    def _cancel_restores(self) -> None:
        for zone in list(self._restores):
            self._cancel_restore(zone)

    def _schedule_restore(self, zone: str, pending: _VolumeRestore, delay: float) -> None:
        if pending.timer is not None:
            pending.timer.cancel()
        pending.timer = threading.Timer(delay, self._restore_volume, args=(zone, pending))
        pending.timer.daemon = True
        pending.timer.start()

    def _track_command(self, command: str, suppress_power_restore: bool) -> None:
        if command == "PWSTANDBY":
            self._cancel_restores()
            self._unconfirmed_inputs.clear()
            return
        head, value = command[:2], command[2:]
        zone = "z1" if head in {"SI", "ZM", "MV"} else head.lower()
        if zone not in ZONES or "?" in value:
            return
        if value == "OFF" and head in {"ZM", "Z2", "Z3"}:
            self._cancel_restore(zone)
            self._unconfirmed_inputs.pop(zone, None)
            return
        if head == "MV" or (head in {"Z2", "Z3"} and
                            (value.isdigit() or value in {"UP", "DOWN"})):
            self._cancel_restore(zone)
            return
        power_on = value == "ON" and head in {"ZM", "Z2", "Z3"}
        if power_on and suppress_power_restore:
            self._cancel_restore(zone)
            # Guard power-on transition observations until the helper selects
            # its destination, without scheduling an outgoing-input restore.
            self._restores[zone] = _VolumeRestore(None, "")
            return
        if not power_on and head not in {"SI", "Z2", "Z3"}:
            return
        try:
            target = (self._unconfirmed_inputs.get(zone, self._zones[zone].input)
                      if power_on else normalize_input(value))
        except ValueError:
            return
        if not power_on and head in {"Z2", "Z3"} and (
                value.startswith(_IGNORED_ZONE_EVENTS) or value.startswith("MU")):
            return
        superseded = set(self._superseded_inputs.get(zone, set()))
        previous = self._restores.get(zone)
        if previous is not None and previous.target is not None and (
                previous.echo == build_zone_command(zone, "input", previous.target)):
            superseded.add(previous.target)
        self._cancel_restore(zone)
        superseded.discard(target)
        if superseded:
            self._superseded_inputs[zone] = superseded
        if not power_on:
            self._unconfirmed_inputs[zone] = target
        pending = _VolumeRestore(target, command)
        self._restores[zone] = pending
        self._schedule_restore(zone, pending, VOLUME_RESTORE_SECONDS)

    def _volume_event(self, line: str) -> None:
        if line == "PWSTANDBY":
            self._cancel_restores()
            self._unconfirmed_inputs.clear()
            return
        head, value = line[:2], line[2:]
        zone = "z1" if head in {"SI", "ZM", "MV"} else head.lower()
        if zone not in ZONES:
            return
        if value == "OFF" and head in {"ZM", "Z2", "Z3"}:
            self._cancel_restore(zone)
            self._unconfirmed_inputs.pop(zone, None)
            return
        is_input = head == "SI" or (head in {"Z2", "Z3"} and value
                    and not value.isdigit() and value not in _RESERVED_INPUTS
                    and not value.startswith(_IGNORED_ZONE_EVENTS))
        if is_input and self._unconfirmed_inputs.get(zone) == value:
            self._unconfirmed_inputs.pop(zone)
        pending = self._restores.get(zone)
        if pending is not None and is_input and pending.target is not None and value != pending.target:
            self._cancel_restore(zone)
            self._unconfirmed_inputs.pop(zone, None)
            pending = None
        if pending is not None:
            if pending.target is None and is_input:
                pending.target = self._zones[zone].input
            if line == pending.echo:
                pending.ready = True
            if pending.restoring is None and pending.ready and pending.target is not None:
                self._schedule_restore(zone, pending, 0)
        volume = parse_volume(value) if head == "MV" or (head in {"Z2", "Z3"} and value.isdigit()) else None
        if volume is None:
            return
        if pending is not None:
            if (pending.ready and pending.target is not None
                    and pending.target not in self._volumes[zone]
                    and self._zones[zone].input == pending.target
                    and zone not in self._unconfirmed_inputs):
                self._cancel_restore(zone)
                self._remember_volume(zone, volume)
                return
            if pending.restoring != volume:
                return
            self._cancel_restore(zone)
            if self._zones[zone].input != pending.target:
                return
        self._remember_volume(zone, volume)

    def _restore_volume(self, zone: str, pending: _VolumeRestore) -> None:
        # Use the normal command lock so cancellation and restoration cannot
        # race a newer input/manual-volume command onto the wire.
        with self._send_lock:
            with self._condition:
                if self._restores.get(zone) is not pending or self._stop.is_set():
                    return
                pending.ready = True
                if pending.restoring is not None:
                    self._cancel_restore(zone)
                    return
                if pending.target is None:
                    return
                volume = self._volumes[zone].get(pending.target)
                if volume is None:
                    self._cancel_restore(zone)
                    return
                sock = self._socket
                if sock is None:
                    self._cancel_restore(zone)
                    return
            wait = self._last_send + COMMAND_INTERVAL_SECONDS - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            with self._condition:
                if self._restores.get(zone) is not pending or self._stop.is_set():
                    return
                pending.restoring = volume
                command = build_zone_command(zone, "volume", volume)
                try:
                    sock.sendall((command + "\r").encode("ascii"))
                except OSError:
                    self._cancel_restore(zone)
                    self._shutdown(sock)
                else:
                    self._schedule_restore(zone, pending, VOLUME_RESTORE_SECONDS)
                finally:
                    self._last_send = time.monotonic()

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
        return self._send(command)

    def _send(self, command: str, suppress_power_restore: bool = False) -> bool:
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
                with self._condition:
                    sock.sendall(payload)
                    self._track_command(command, suppress_power_restore)
            except OSError as err:
                LOGGER.warning("Denon Telnet command %r failed: %s", command, err)
                with self._condition:
                    self._cancel_restores()
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
            raise ValueError("volume must be between 0 and 98")
        self.command("MV", format_volume(volume))

    def power_on_and_configure(self, input_name: str, delay: float) -> None:
        with self._condition:
            self._cancel_restore("z1")
        self._send("ZMON", suppress_power_restore=True)
        time.sleep(delay)
        self.set_input(input_name)
