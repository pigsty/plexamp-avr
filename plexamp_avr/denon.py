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
STATUS_QUERIES = ("ZM?", "SI?", "MV?", "MU?", "MS?", "Z2?", "Z2MU?", "Z3?", "Z3MU?")
# Denon recommends at least 50ms between commands.
COMMAND_INTERVAL_SECONDS = 0.05
VOLUME_RESTORE_SECONDS = 5.0
# The AVR may ignore volume commands briefly after an input switch.
VOLUME_RESTORE_ATTEMPTS = 3
VOLUME_RESTORE_RETRY_SECONDS = 1.0
INPUT_STATUS_TIMEOUT_SECONDS = 5.0
# Number of recent AVR status updates and commands kept for the debug view.
HISTORY_SIZE = 25
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
    sound_mode: str | None = None


@dataclass
class _VolumeRestore:
    target: str | None
    echo: str
    ready: bool = False
    restoring: float | None = None
    attempts: int = 0
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


def describe_update(line: str) -> dict[str, Any] | None:
    """Classify an AVR event that drives webhooks or volume memory.

    Returns ``{"zone", "event", "value"}`` for power, input, volume and mute
    updates, or None for events the service ignores (sound mode, channel
    levels, ...).
    """
    if line == "PWSTANDBY":
        return {"zone": "all", "event": "power", "value": "standby"}
    head, value = line[:2], line[2:]
    if head == "ZM" and value in {"ON", "OFF"}:
        return {"zone": "z1", "event": "power", "value": value.lower()}
    if head == "SI" and value:
        return {"zone": "z1", "event": "input", "value": value}
    if head == "MV":
        volume = parse_volume(value)
        return None if volume is None else {"zone": "z1", "event": "volume", "value": volume}
    if head == "MU" and value in {"ON", "OFF"}:
        return {"zone": "z1", "event": "mute", "value": value.lower()}
    if head in {"Z2", "Z3"} and value:
        zone = head.lower()
        if value in {"ON", "OFF"}:
            return {"zone": zone, "event": "power", "value": value.lower()}
        if value in {"MUON", "MUOFF"}:
            return {"zone": zone, "event": "mute", "value": value[2:].lower()}
        if value.isdigit():
            volume = parse_volume(value)
            return None if volume is None else {"zone": zone, "event": "volume", "value": volume}
        if not value.startswith(_IGNORED_ZONE_EVENTS):
            return {"zone": zone, "event": "input", "value": value}
    return None


def build_zone_command(zone: str, action: str, value: Any) -> str:
    """Build the telnet command for a supported zone control."""
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
    if action == "sound-mode":
        if not main:
            raise ValueError("sound mode is only available for the main zone")
        commands = {
            "movie": "MSMOVIE",
            "music": "MSMUSIC",
            "game": "MSGAME",
            "pure": "MSPURE DIRECT",
        }
        if not isinstance(value, str) or value not in commands:
            raise ValueError("mode must be movie, music, game or pure")
        return commands[value]
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
        max_volume: int = 75,
    ):
        if isinstance(max_volume, bool) or not isinstance(max_volume, int) or not 0 <= max_volume <= 98:
            raise ValueError("max_volume must be an integer between 0 and 98")
        self.max_volume = max_volume
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
        self._update_history: deque[dict[str, Any]] = deque(maxlen=HISTORY_SIZE)
        self._command_history: deque[dict[str, Any]] = deque(maxlen=HISTORY_SIZE)
        self._line_seq = 0
        self._last_send = 0.0
        self._listeners: list[Listener] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._volume_path = Path(data_dir) / "volumes.json" if data_dir is not None else None
        self._volumes: dict[str, dict[str, float]] = {zone: {} for zone in ZONES}
        self._restores: dict[str, _VolumeRestore] = {}
        self._unconfirmed_inputs: dict[str, str] = {}
        self._input_status_timers: dict[str, threading.Timer] = {}
        # Input each zone was on when it powered off; only used to pick the
        # volume to restore at the next power-on, never published.
        self._power_off_inputs: dict[str, str] = {}
        self._superseded_inputs: dict[str, set[str]] = {}
        self._load_volumes()
        LOGGER.info("Denon client initialized with address: %s:%d (MAX_VOLUME=%s)",
                    self.address, self.port, self.max_volume)

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
            self._cancel_input_status_queries()
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
                    self._cancel_input_status_queries()
                    self._unconfirmed_inputs.clear()
                    self._socket = None
                    self._connected = False
                    self._zones = {zone: ZoneState() for zone in ZONES}
                    self._power_off_inputs.clear()
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
                "saved_volumes": {zone: dict(volumes) for zone, volumes in self._volumes.items()},
            }

    def history(self) -> dict[str, list[dict[str, Any]]]:
        """Recent relevant status updates and sent commands, newest first."""
        with self._condition:
            return {
                "updates": [dict(entry) for entry in reversed(self._update_history)],
                "commands": [dict(entry) for entry in reversed(self._command_history)],
            }

    def _record_command(self, command: str, ok: bool, note: str | None = None) -> None:
        entry: dict[str, Any] = {"time": time.time(), "command": command, "ok": ok}
        if note:
            entry["note"] = note
        with self._condition:
            self._command_history.append(entry)

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
            update = describe_update(line)
            if update is not None:
                self._update_history.append({"time": time.time(), "line": line, **update})
            head, value = line[:2], line[2:]
            zone = "z1" if head == "SI" else head.lower()
            pending = self._restores.get(zone)
            superseded = (pending is not None and value != pending.target
                          and value in self._superseded_inputs.get(zone, set()))
            if superseded:
                self._superseded_inputs[zone].discard(value)
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
            LOGGER.info("Loaded %d saved volume level(s) from %s",
                        sum(len(volumes) for volumes in self._volumes.values()), self._volume_path)
            LOGGER.debug("Saved volume levels: %s", self._volumes)
        except FileNotFoundError:
            LOGGER.debug("No saved volume levels at %s yet", self._volume_path)
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
        LOGGER.debug("Saving volume %s for %s input %s (was %s)",
                     volume, zone.upper(), name, self._volumes[zone].get(name))
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

    def _cancel_restore(self, zone: str, reason: str | None = None) -> None:
        self._superseded_inputs.pop(zone, None)
        pending = self._restores.pop(zone, None)
        if pending is not None and pending.timer is not None:
            pending.timer.cancel()
        if pending is not None and reason is not None:
            LOGGER.debug("Volume restore for %s input %s ended: %s", zone.upper(), pending.target, reason)

    def _cancel_restores(self, reason: str | None = None) -> None:
        for zone in list(self._restores):
            self._cancel_restore(zone, reason)

    def _cancel_input_status_query(self, zone: str) -> None:
        timer = self._input_status_timers.pop(zone, None)
        if timer is not None:
            timer.cancel()

    def _cancel_input_status_queries(self) -> None:
        for zone in list(self._input_status_timers):
            self._cancel_input_status_query(zone)

    def _schedule_input_status_query(self, zone: str) -> None:
        self._cancel_input_status_query(zone)
        timer: threading.Timer
        timer = threading.Timer(
            INPUT_STATUS_TIMEOUT_SECONDS, lambda: self._query_input_status(zone, timer)
        )
        timer.daemon = True
        self._input_status_timers[zone] = timer
        timer.start()

    def _query_input_status(self, zone: str, timer: threading.Timer) -> None:
        with self._condition:
            if self._input_status_timers.get(zone) is not timer or self._stop.is_set():
                return
            self._input_status_timers.pop(zone)
        query = "SI?" if zone == "z1" else f"{zone.upper()}?"
        LOGGER.info("No input status received from AVR for %s within %ss; querying once",
                    zone.upper(), INPUT_STATUS_TIMEOUT_SECONDS)
        self.send(query)

    def _schedule_restore(self, zone: str, pending: _VolumeRestore, delay: float) -> None:
        if pending.timer is not None:
            pending.timer.cancel()
        pending.timer = threading.Timer(delay, self._restore_volume, args=(zone, pending))
        pending.timer.daemon = True
        pending.timer.start()

    def _track_command(self, command: str, suppress_power_restore: bool) -> None:
        if command == "PWSTANDBY":
            self._cancel_restores("standby command sent")
            self._cancel_input_status_queries()
            self._unconfirmed_inputs.clear()
            return
        head, value = command[:2], command[2:]
        zone = "z1" if head in {"SI", "ZM", "MV"} else head.lower()
        if zone not in ZONES or "?" in value:
            return
        if value == "OFF" and head in {"ZM", "Z2", "Z3"}:
            self._cancel_restore(zone, "power off command sent")
            self._cancel_input_status_query(zone)
            self._unconfirmed_inputs.pop(zone, None)
            return
        if head == "MV" or (head in {"Z2", "Z3"} and
                            (value.isdigit() or value in {"UP", "DOWN"})):
            self._cancel_restore(zone, f"manual volume command {command!r} sent")
            return
        power_on = value == "ON" and head in {"ZM", "Z2", "Z3"}
        if power_on and suppress_power_restore:
            self._cancel_restore(zone, "power-on helper will select the input")
            self._schedule_input_status_query(zone)
            LOGGER.debug("Power-on helper started for %s; waiting for its input selection before restoring volume",
                         zone.upper())
            # Guard power-on transition observations until the helper selects
            # its destination, without scheduling an outgoing-input restore.
            self._restores[zone] = _VolumeRestore(None, "")
            return
        if not power_on and head not in {"SI", "Z2", "Z3"}:
            return
        try:
            target = (self._unconfirmed_inputs.get(
                          zone, self._zones[zone].input or self._power_off_inputs.get(zone))
                      if power_on else normalize_input(value))
        except ValueError:
            return
        if not power_on and head in {"Z2", "Z3"} and (
                value.startswith(_IGNORED_ZONE_EVENTS) or value.startswith("MU")):
            return
        self._schedule_input_status_query(zone)
        superseded = set(self._superseded_inputs.get(zone, set()))
        unconfirmed = self._unconfirmed_inputs.get(zone)
        if unconfirmed is not None:
            superseded.add(unconfirmed)
        self._cancel_restore(zone, f"superseded by {command!r}")
        superseded.discard(target)
        if superseded:
            self._superseded_inputs[zone] = superseded
        if not power_on:
            self._unconfirmed_inputs[zone] = target
        pending = _VolumeRestore(target, command)
        self._restores[zone] = pending
        if target is None:
            LOGGER.info("Volume restore pending for %s after %r; input not known yet", zone.upper(), command)
        else:
            LOGGER.info("Volume restore pending for %s input %s after %r (saved volume: %s)",
                        zone.upper(), target, command, self._volumes[zone].get(target, "none"))
        LOGGER.debug("Waiting up to %ss for AVR echo %r before restoring %s volume",
                     VOLUME_RESTORE_SECONDS, command, zone.upper())
        self._schedule_restore(zone, pending, VOLUME_RESTORE_SECONDS)

    def _volume_event(self, line: str) -> None:
        if line == "PWSTANDBY":
            self._cancel_restores("AVR reported standby")
            self._cancel_input_status_queries()
            self._unconfirmed_inputs.clear()
            return
        head, value = line[:2], line[2:]
        zone = "z1" if head in {"SI", "ZM", "MV"} else head.lower()
        if zone not in ZONES:
            return
        if value == "OFF" and head in {"ZM", "Z2", "Z3"}:
            self._cancel_restore(zone, "AVR reported zone power off")
            self._cancel_input_status_query(zone)
            self._unconfirmed_inputs.pop(zone, None)
            return
        is_input = head == "SI" or (head in {"Z2", "Z3"} and value
                    and not value.isdigit() and value not in _RESERVED_INPUTS
                    and not value.startswith(_IGNORED_ZONE_EVENTS))
        if is_input:
            self._cancel_input_status_query(zone)
            self._unconfirmed_inputs.pop(zone, None)
        pending = self._restores.get(zone)
        if pending is not None and is_input and pending.target is not None and value != pending.target:
            LOGGER.info("Cancelled volume restore for %s: AVR switched to %s instead of %s",
                        zone.upper(), value, pending.target)
            self._cancel_restore(zone, f"AVR reported different input {value!r}")
            self._unconfirmed_inputs.pop(zone, None)
            pending = None
        if pending is not None:
            if pending.target is None and is_input:
                pending.target = self._zones[zone].input
                LOGGER.debug("Volume restore for %s now targets reported input %s", zone.upper(), pending.target)
            if line == pending.echo:
                LOGGER.debug("AVR echoed %r; %s volume restore is ready", line, zone.upper())
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
                LOGGER.info("No saved volume for %s input %s; remembering current volume %s",
                            zone.upper(), pending.target, volume)
                self._cancel_restore(zone, "first use of input")
                self._remember_volume(zone, volume)
                return
            if pending.restoring != volume:
                LOGGER.debug("Ignoring %s volume %s while restore to %s is pending",
                             zone.upper(), volume, pending.restoring)
                return
            LOGGER.info("AVR confirmed restored volume %s for %s input %s", volume, zone.upper(), pending.target)
            self._cancel_restore(zone, "restore confirmed")
            if self._zones[zone].input != pending.target:
                return
        self._remember_volume(zone, volume)

    def _restore_volume(self, zone: str, pending: _VolumeRestore) -> None:
        # Use the normal command lock so cancellation and restoration cannot
        # race a newer input/manual-volume command onto the wire.
        with self._send_lock:
            with self._condition:
                if self._restores.get(zone) is not pending or self._stop.is_set():
                    LOGGER.debug("Skipping outdated volume restore for %s input %s", zone.upper(), pending.target)
                    return
                pending.ready = True
                if pending.restoring is not None:
                    if pending.attempts >= VOLUME_RESTORE_ATTEMPTS:
                        LOGGER.info("AVR did not confirm restored volume %s for %s input %s after %d attempts",
                                    pending.restoring, zone.upper(), pending.target, pending.attempts)
                        self._cancel_restore(zone, "confirmation timed out")
                        return
                    state = self._zones[zone]
                    if state.power == "off" or (state.input is not None and state.input != pending.target):
                        self._cancel_restore(zone, "zone changed before retry")
                        return
                    LOGGER.info("AVR did not confirm restored volume %s for %s input %s; retrying (%d/%d)",
                                pending.restoring, zone.upper(), pending.target,
                                pending.attempts + 1, VOLUME_RESTORE_ATTEMPTS)
                if pending.target is None:
                    LOGGER.debug("Volume restore for %s waiting for the AVR to report its input", zone.upper())
                    return
                saved = self._volumes[zone].get(pending.target)
                if saved is None:
                    LOGGER.info("No saved volume for %s input %s; leaving volume unchanged",
                                zone.upper(), pending.target)
                    self._cancel_restore(zone, "no saved volume")
                    return
                volume = min(saved, self.max_volume)
                if volume != saved:
                    LOGGER.info("Saved volume %s for %s input %s exceeds MAX_VOLUME; restoring %s",
                                saved, zone.upper(), pending.target, volume)
                sock = self._socket
                if sock is None:
                    LOGGER.info("Cannot restore %s volume for input %s: AVR not connected",
                                zone.upper(), pending.target)
                    self._cancel_restore(zone, "not connected")
                    return
            wait = self._last_send + COMMAND_INTERVAL_SECONDS - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            with self._condition:
                if self._restores.get(zone) is not pending or self._stop.is_set():
                    return
                pending.restoring = volume
                command = build_zone_command(zone, "volume", volume)
                LOGGER.info("Restoring saved volume %s for %s input %s", volume, zone.upper(), pending.target)
                LOGGER.debug("Sending volume restore command to Denon AVR: %r", command)
                try:
                    sock.sendall((command + "\r").encode("ascii"))
                except OSError as err:
                    LOGGER.warning("Volume restore command %r failed: %s", command, err)
                    self._record_command(command, False, "volume restore")
                    self._cancel_restore(zone, "send failed")
                    self._shutdown(sock)
                else:
                    pending.attempts += 1
                    self._record_command(command, True, "volume restore")
                    self._schedule_restore(
                        zone, pending, VOLUME_RESTORE_RETRY_SECONDS * 2 ** (pending.attempts - 1)
                    )
                finally:
                    self._last_send = time.monotonic()

    def _set(self, zone: str, field: str, value: Any) -> bool:
        state = self._zones[zone]
        if getattr(state, field) == value:
            return False
        setattr(state, field, value)
        return True

    def _set_input(self, zone: str, value: str) -> bool:
        self._power_off_inputs.pop(zone, None)
        return self._set(zone, "input", value)

    def _power_off(self, zone: str) -> bool:
        # Forget the input so the AVR's report at the next power-on is treated
        # as newly learned state rather than a change from a stale input.
        previous = self._zones[zone].input
        if previous is not None:
            self._power_off_inputs[zone] = previous
        powered = self._set(zone, "power", "off")
        cleared = self._set(zone, "input", None)
        return powered or cleared

    def _apply(self, line: str) -> bool:
        if line == "PWSTANDBY":
            return any([self._power_off(zone) for zone in ZONES])
        head, value = line[:2], line[2:]
        if head == "ZM" and value == "OFF":
            return self._power_off("z1")
        if head == "ZM" and value == "ON":
            return self._set("z1", "power", "on")
        if head == "SI" and value:
            return self._set_input("z1", value)
        if head == "MV":
            volume = parse_volume(value)
            return volume is not None and self._set("z1", "volume", volume)
        if head == "MU" and value in {"ON", "OFF"}:
            return self._set("z1", "muted", value == "ON")
        if head == "MS" and value:
            return self._set("z1", "sound_mode", value.upper())
        if head in {"Z2", "Z3"} and value:
            zone = head.lower()
            if value == "OFF":
                return self._power_off(zone)
            if value == "ON":
                return self._set(zone, "power", "on")
            if value in {"MUON", "MUOFF"}:
                return self._set(zone, "muted", value == "MUON")
            if value.isdigit():
                volume = parse_volume(value)
                return volume is not None and self._set(zone, "volume", volume)
            if not value.startswith(_IGNORED_ZONE_EVENTS):
                return self._set_input(zone, value)
        return False

    # Commands -------------------------------------------------------------

    def send(self, command: str) -> bool:
        return self._send(command)

    def limit_volume_command(self, command: str) -> str:
        prefix, value = command[:2], command[2:]
        if prefix not in {"MV", "Z2", "Z3"}:
            return command
        volume = parse_volume(value)
        zone = "z1" if prefix == "MV" else prefix.lower()
        if value in {"UP", "DOWN"}:
            with self._condition:
                current = self._zones[zone].volume
            if current is None:
                if value == "DOWN":
                    return command
                raise ValueError("cannot increase volume until the AVR reports its current volume")
            step = 0.5 if zone == "z1" else 1
            volume = max(0, current + (step if value == "UP" else -step))
        elif volume is None:
            return command
        return build_zone_command(zone, "volume", min(volume, self.max_volume))

    def _send(self, command: str, suppress_power_restore: bool = False) -> bool:
        """Send a raw command over the persistent connection."""
        if "\r" in command or "\n" in command:
            raise ValueError("command must not contain line breaks")
        command = self.limit_volume_command(command)
        payload = (command + "\r").encode("ascii")
        if not self.wait_connected(self.timeout):
            LOGGER.warning("Denon Telnet command %r failed: not connected", command)
            self._record_command(command, False, "not connected")
            return False
        with self._send_lock:
            with self._condition:
                sock = self._socket
            if sock is None:
                LOGGER.warning("Denon Telnet command %r failed: not connected", command)
                self._record_command(command, False, "not connected")
                return False
            wait = self._last_send + COMMAND_INTERVAL_SECONDS - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            update = describe_update(command)
            if update is not None and update["event"] == "volume":
                LOGGER.debug("Sending command to Denon AVR: %r (volume=%s, MAX_VOLUME=%s)",
                             command, update["value"], self.max_volume)
            else:
                LOGGER.debug("Sending command to Denon AVR: %r", command)
            try:
                with self._condition:
                    sock.sendall(payload)
                    self._record_command(command, True)
                    self._track_command(command, suppress_power_restore)
            except OSError as err:
                LOGGER.warning("Denon Telnet command %r failed: %s", command, err)
                self._record_command(command, False, str(err))
                with self._condition:
                    self._cancel_restores("command send failed")
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
            self._cancel_restore("z1", "power-on helper started")
        self._send("ZMON", suppress_power_restore=True)
        time.sleep(delay)
        self.set_input(input_name)
