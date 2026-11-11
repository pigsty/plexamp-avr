"""Configurable HTTP webhooks triggered by AVR zone events."""
from __future__ import annotations

import json
import logging
import os
import queue
import re
import tempfile
import threading
import time
import uuid
from base64 import b64encode
from collections import deque
from http.client import HTTPException
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import unquote_to_bytes, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from .denon import ZONES, normalize_input, normalize_zone

LOGGER = logging.getLogger(__name__)

EVENTS = ("power", "input", "mute")
METHODS = ("GET", "PUT", "POST")
MAX_NAME_LENGTH = 100
MAX_URL_LENGTH = 2048
MAX_BODY_LENGTH = 16384
MAX_HEADERS = 50
MAX_HEADER_VALUE_LENGTH = 8192
WEBHOOKS_FILE = "webhooks.json"
TAGS_FILE = "input_tags.json"
TAG_PREFIX = "tag:"
MAX_TAG_LENGTH = 32
MAX_TAGS_PER_INPUT = 20
MAX_TAGGED_INPUTS = 100
_TAG_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]*")
COMMAND_EVENT_DEDUPE_SECONDS = 2.0
HISTORY_SIZE = 25
_ID_PATTERN = re.compile(r"[0-9a-f]{1,64}")
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")


def _on_off(value: Any) -> str:
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, str) and value.strip().lower() in {"on", "off"}:
        return value.strip().lower()
    raise ValueError("value must be \"on\" or \"off\"")


def _request_url_and_authorization(url: str) -> tuple[str, str | None]:
    parts = urlsplit(url)
    if parts.username is None:
        return url, None
    username = unquote_to_bytes(parts.username)
    password = unquote_to_bytes(parts.password or "")
    if b":" in username:
        raise ValueError("URL username must not contain a colon")
    netloc = parts.netloc.rsplit("@", 1)[-1]
    request_url = urlunsplit((parts.scheme, netloc, parts.path, parts.query, ""))
    authorization = "Basic " + b64encode(username + b":" + password).decode("ascii")
    return request_url, authorization


def _redact_url(url: str) -> str:
    parts = urlsplit(url)
    if "@" not in parts.netloc:
        return url
    netloc = parts.netloc.rsplit("@", 1)[-1]
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, ""))


def normalize_tag(tag: Any) -> str:
    if not isinstance(tag, str):
        raise ValueError("tags must be strings")
    value = tag.strip().lower()
    if len(value) > MAX_TAG_LENGTH or not _TAG_PATTERN.fullmatch(value):
        raise ValueError(
            f"tags must be 1-{MAX_TAG_LENGTH} characters of letters, digits, '-' or '_'"
        )
    return value


def validate_tags(data: Any) -> dict[str, list[str]]:
    """Validate and normalize an {input: [tag, ...]} mapping; empty entries are dropped."""
    if not isinstance(data, dict) or len(data) > MAX_TAGGED_INPUTS:
        raise ValueError("tags must be an object mapping inputs to lists of tags")
    result: dict[str, list[str]] = {}
    for input_name, tags in data.items():
        name = normalize_input(input_name)
        if not isinstance(tags, list) or len(tags) > MAX_TAGS_PER_INPUT:
            raise ValueError(f"tags for {name} must be a list of at most {MAX_TAGS_PER_INPUT} tags")
        normalized = sorted({normalize_tag(tag) for tag in tags})
        if normalized:
            result[name] = normalized
    return result


def validate_webhook(data: Any) -> dict[str, Any]:
    """Validate and normalize a webhook definition (without its id)."""
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    name = data.get("name", "")
    if not isinstance(name, str) or len(name.strip()) > MAX_NAME_LENGTH:
        raise ValueError(f"name must be a string of at most {MAX_NAME_LENGTH} characters")
    display_name = data.get("displayName", "")
    if not isinstance(display_name, str) or len(display_name.strip()) > MAX_NAME_LENGTH:
        raise ValueError(f"displayName must be a string of at most {MAX_NAME_LENGTH} characters")
    enabled = data.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be true or false")
    favorite = data.get("favorite", False)
    if not isinstance(favorite, bool):
        raise ValueError("favorite must be true or false")
    favorite_order = data.get("favoriteOrder", 0)
    if type(favorite_order) is not int or favorite_order < 0:
        raise ValueError("favoriteOrder must be a non-negative integer")
    favorite_width = data.get("favoriteWidth", 1)
    if type(favorite_width) is not int or favorite_width not in (1, 2):
        raise ValueError("favoriteWidth must be 1 or 2")
    zone = normalize_zone(data["zone"]) if isinstance(data.get("zone"), str) else None
    if zone is None:
        raise ValueError(f"zone must be one of {', '.join(ZONES)}")
    event = data.get("event")
    if event not in EVENTS:
        raise ValueError(f"event must be one of {', '.join(EVENTS)}")
    value = data.get("value", "")
    if event == "input":
        if value is None or (isinstance(value, str) and not value.strip()):
            value = ""
        elif isinstance(value, str) and value.strip().lower().startswith(TAG_PREFIX):
            value = TAG_PREFIX + normalize_tag(value.strip()[len(TAG_PREFIX):])
        else:
            value = normalize_input(value)
    else:
        value = _on_off(value)
    method = data.get("method", "POST")
    method = method.strip().upper() if isinstance(method, str) else method
    if method not in METHODS:
        raise ValueError(f"method must be one of {', '.join(METHODS)}")
    url = data.get("url")
    if not isinstance(url, str):
        raise ValueError("url must be a string")
    url = url.strip()
    try:
        parts = urlsplit(url)
        hostname = parts.hostname
        parts.port
    except ValueError as err:
        raise ValueError("url must have a valid host and optional numeric port") from err
    if (
        len(url) > MAX_URL_LENGTH
        or parts.scheme not in {"http", "https"}
        or not hostname
        or "<" in hostname
        or ">" in hostname
        or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in url)
    ):
        raise ValueError("url must be an absolute http:// or https:// URL")
    body = data.get("body", "")
    if body is None:
        body = ""
    if not isinstance(body, str) or len(body) > MAX_BODY_LENGTH:
        raise ValueError(f"body must be a string of at most {MAX_BODY_LENGTH} characters")
    headers = data.get("headers", {})
    if not isinstance(headers, dict) or len(headers) > MAX_HEADERS:
        raise ValueError(f"headers must be an object with at most {MAX_HEADERS} entries")
    normalized_headers = {}
    for header_name, header_value in headers.items():
        if not isinstance(header_name, str) or not _HEADER_NAME.fullmatch(header_name):
            raise ValueError("header names must be valid HTTP field names")
        if (
            not isinstance(header_value, str)
            or len(header_value) > MAX_HEADER_VALUE_LENGTH
            or any((ord(char) < 32 and char != "\t") or ord(char) == 127 for char in header_value)
        ):
            raise ValueError("header values must be strings without control characters")
        try:
            header_value.encode("latin-1")
        except UnicodeEncodeError as err:
            raise ValueError("header values must contain only Latin-1 characters") from err
        normalized_headers[header_name] = header_value
    return {
        "name": name.strip(),
        "displayName": display_name.strip(),
        "enabled": enabled,
        "favorite": favorite,
        "favoriteOrder": favorite_order,
        "favoriteWidth": favorite_width,
        "zone": zone,
        "event": event,
        "value": value,
        "method": method,
        "url": url,
        "body": body,
        "headers": normalized_headers,
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Config files may contain tokens: keep new files private, but preserve
    # the mode of an existing file so it can be relaxed on the host.
    try:
        mode = path.stat().st_mode & 0o777
    except FileNotFoundError:
        mode = 0o600
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), mode)
            json.dump(payload, handle, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def describe_event(zone: str, event: str, value: str) -> str:
    return f"{zone.upper()} {event} {value or 'any'}"


class TagStore:
    """Persists user-defined input tags as JSON in the config store directory."""

    def __init__(self, directory: str | Path):
        self.path = Path(directory) / TAGS_FILE
        self._lock = threading.Lock()
        self._tags: dict[str, list[str]] = self._load()

    def _load(self) -> dict[str, list[str]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return validate_tags(data.get("tags", {}) if isinstance(data, dict) else {})
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as err:
            LOGGER.error("Could not read input tags from %s: %s", self.path, err)
            return {}

    def all(self) -> dict[str, list[str]]:
        with self._lock:
            return {name: list(tags) for name, tags in self._tags.items()}

    def tags_for(self, input_name: str) -> list[str]:
        with self._lock:
            return list(self._tags.get(input_name, []))

    def replace(self, data: Any) -> dict[str, list[str]]:
        tags = validate_tags(data)
        with self._lock:
            _write_json(self.path, {"tags": tags})
            self._tags = tags
        return {name: list(values) for name, values in tags.items()}


class WebhookStore:
    """Persists webhook definitions as JSON in the config store directory."""

    def __init__(self, directory: str | Path):
        self.path = Path(directory) / WEBHOOKS_FILE
        self._lock = threading.Lock()
        self._webhooks: list[dict[str, Any]] = self._load()

    def _load(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            LOGGER.info("No webhooks configured yet (%s)", self.path)
            return []
        except (OSError, ValueError) as err:
            LOGGER.error("Could not read webhooks from %s: %s", self.path, err)
            return []
        webhooks = []
        seen: set[str] = set()
        for item in data.get("webhooks", []) if isinstance(data, dict) else []:
            try:
                webhook = validate_webhook(item)
            except (KeyError, ValueError) as err:
                LOGGER.warning("Ignoring invalid webhook %r in %s: %s", item, self.path, err)
                continue
            webhook_id = item.get("id")
            if not isinstance(webhook_id, str) or not _ID_PATTERN.fullmatch(webhook_id) or webhook_id in seen:
                webhook_id = uuid.uuid4().hex
            seen.add(webhook_id)
            webhooks.append({"id": webhook_id, **webhook})
        LOGGER.info("Loaded %d webhook(s) from %s", len(webhooks), self.path)
        return webhooks

    def _save(self, webhooks: list[dict[str, Any]]) -> None:
        _write_json(self.path, {"webhooks": webhooks})

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(webhook) for webhook in self._webhooks]

    def get(self, webhook_id: str) -> dict[str, Any] | None:
        with self._lock:
            for webhook in self._webhooks:
                if webhook["id"] == webhook_id:
                    return dict(webhook)
        return None

    def create(self, data: Any) -> dict[str, Any]:
        webhook = {"id": uuid.uuid4().hex, **validate_webhook(data)}
        with self._lock:
            webhooks = [*self._webhooks, webhook]
            self._save(webhooks)
            self._webhooks = webhooks
        return dict(webhook)

    def update(self, webhook_id: str, data: Any) -> dict[str, Any] | None:
        webhook = {"id": webhook_id, **validate_webhook(data)}
        with self._lock:
            if not any(item["id"] == webhook_id for item in self._webhooks):
                return None
            webhooks = [webhook if item["id"] == webhook_id else item for item in self._webhooks]
            self._save(webhooks)
            self._webhooks = webhooks
        return dict(webhook)

    def delete(self, webhook_id: str) -> bool:
        with self._lock:
            webhooks = [item for item in self._webhooks if item["id"] != webhook_id]
            if len(webhooks) == len(self._webhooks):
                return False
            self._save(webhooks)
            self._webhooks = webhooks
        return True

    def replace_favorites(self, data: Any) -> list[dict[str, Any]]:
        """Atomically update favorite presentation without changing hook actions."""
        if not isinstance(data, list):
            raise ValueError("favorites must be a list")
        with self._lock:
            by_id = {hook["id"]: hook for hook in self._webhooks}
            updates = {}
            for order, item in enumerate(data):
                if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                    raise ValueError("each favorite must have an id")
                webhook_id = item["id"]
                if webhook_id not in by_id:
                    raise ValueError("unknown webhook; reload favorites and try again")
                if webhook_id in updates:
                    raise ValueError("duplicate favorite id")
                if "displayName" not in item or "favoriteWidth" not in item:
                    raise ValueError("each favorite must have displayName and favoriteWidth")
                updates[webhook_id] = {
                    "id": webhook_id,
                    **validate_webhook({
                        **by_id[webhook_id], "favorite": True, "favoriteOrder": order,
                        "displayName": item["displayName"], "favoriteWidth": item["favoriteWidth"],
                    }),
                }
            webhooks = [
                updates.get(hook["id"], {**hook, "favorite": False})
                for hook in self._webhooks
            ]
            self._save(webhooks)
            self._webhooks = webhooks
            return [dict(hook) for hook in webhooks]


def zone_events(previous: dict[str, Any], current: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Return (zone, event, value) for each power/input/mute change between two snapshots.

    Changes from an unknown (None) value are ignored so that learning the
    initial AVR state at startup does not trigger webhooks.
    """
    events = []
    for zone in ZONES:
        before = previous.get(zone) or {}
        after = current.get(zone) or {}
        for event, field in (("power", "power"), ("input", "input"), ("mute", "muted")):
            old, new = before.get(field), after.get(field)
            if old is None or new is None or old == new:
                continue
            value = ("on" if new else "off") if field == "muted" else str(new)
            events.append((zone, event, value))
    return events


class WebhookDispatcher:
    """Listens for AVR state changes and calls matching webhooks in the background."""

    def __init__(self, store: WebhookStore, timeout: float = 5.0, tags: TagStore | None = None):
        self.store = store
        self.tags = tags
        self.timeout = timeout
        self._previous: dict[str, Any] | None = None
        self._observed_events: dict[tuple[str, str, str], float] = {}
        self._state_lock = threading.Lock()
        self._queue: queue.Queue = queue.Queue(maxsize=100)
        self._thread: threading.Thread | None = None
        self._history: deque[dict[str, Any]] = deque(maxlen=HISTORY_SIZE)
        self._history_lock = threading.Lock()

    def history(self) -> list[dict[str, Any]]:
        """Recent webhook calls with their results, newest first."""
        with self._history_lock:
            return [dict(entry) for entry in reversed(self._history)]

    def _record(self, webhook: dict[str, Any], event: str, url: str, status: int | None, error: str | None) -> None:
        entry = {
            "time": time.time(),
            "name": webhook.get("name") or webhook.get("displayName") or "",
            "id": webhook.get("id"),
            "event": event,
            "method": webhook["method"],
            "url": url,
            "status": status,
            "error": error,
        }
        with self._history_lock:
            self._history.append(entry)

    def start(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, name="webhooks", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        if self._thread is not None:
            self._queue.put(None)
            self._thread.join(timeout=self.timeout + 5)

    def handle_snapshot(self, snapshot: dict[str, Any]) -> None:
        zones = snapshot.get("zones", {})
        now = time.monotonic()
        with self._state_lock:
            previous, self._previous = self._previous, zones
            self._observed_events = {
                event: observed_at
                for event, observed_at in self._observed_events.items()
                if now - observed_at <= COMMAND_EVENT_DEDUPE_SECONDS
            }
            events = zone_events(previous, zones) if previous is not None else []
            for event in events:
                self._observed_events[event] = now
        if previous is None:
            return
        for zone, event, value in events:
            self.trigger(zone, event, value)

    def handle_command(self, zone: str, event: str, value: str) -> None:
        """Fire a webhook for an accepted API command and suppress its AVR echo."""
        signature = (zone, event, value)
        now = time.monotonic()
        with self._state_lock:
            observed_at = self._observed_events.pop(signature, None)
            if observed_at is not None and now - observed_at <= COMMAND_EVENT_DEDUPE_SECONDS:
                return
            if self._previous is None:
                self._previous = {}
            zone_state = dict(self._previous.get(zone) or {})
            field = {"power": "power", "input": "input", "mute": "muted"}[event]
            zone_state[field] = (value == "on") if event == "mute" else value
            self._previous[zone] = zone_state
        self.trigger(zone, event, value)

    def trigger(self, zone: str, event: str, value: str) -> None:
        for webhook in self.store.list():
            if not webhook["enabled"] or webhook["zone"] != zone or webhook["event"] != event:
                continue
            expected = webhook["value"]
            if expected.startswith(TAG_PREFIX):
                if self.tags is None or expected[len(TAG_PREFIX):] not in self.tags.tags_for(value):
                    continue
            elif expected and expected != value:
                continue
            try:
                self._queue.put_nowait((webhook, describe_event(zone, event, value)))
            except queue.Full:
                LOGGER.warning("Webhook queue full; dropping webhook %r", webhook["name"] or webhook["id"])

    def trigger_manual(self, webhook: dict[str, Any]) -> bool:
        """Queue a manual call, independently of automatic event matching."""
        try:
            self._queue.put_nowait((webhook, "manual"))
        except queue.Full:
            return False
        return True

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            self.call(*item)

    def call(self, webhook: dict[str, Any], event: str) -> int | None:
        """Perform the HTTP request for a webhook; returns the response code."""
        name = webhook.get("name") or webhook.get("id")
        method, url = webhook["method"], webhook["url"]
        safe_url = _redact_url(url)
        data = None
        try:
            request_url, authorization = _request_url_and_authorization(url)
            request = Request(request_url, method=method, headers={"User-Agent": "plexamp-avr"})
            if method != "GET" and webhook.get("body"):
                body = webhook["body"]
                data = body.encode("utf-8")
                try:
                    json.loads(body)
                    content_type = "application/json"
                except ValueError:
                    content_type = "text/plain; charset=utf-8"
                request.add_header("Content-Type", content_type)
            if authorization is not None:
                request.add_header("Authorization", authorization)
            for header_name, header_value in webhook.get("headers", {}).items():
                request.add_header(header_name, header_value)
            request.data = data
            with urlopen(request, timeout=self.timeout) as response:
                status = response.status
        except HTTPError as err:
            status = err.code
            err.close()
        except (HTTPException, URLError, OSError, UnicodeError, ValueError) as err:
            reason = getattr(err, "reason", err)
            LOGGER.info("Webhook %r (%s): %s %s -> failed", name, event, method, safe_url)
            LOGGER.warning("Webhook %r (%s): %s %s failed: %s", name, event, method, safe_url, reason)
            self._record(webhook, event, safe_url, None, str(reason))
            return None
        LOGGER.info("Webhook %r (%s): %s %s -> %d", name, event, method, safe_url, status)
        self._record(webhook, event, safe_url, status, None)
        return status
