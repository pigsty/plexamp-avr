from __future__ import annotations

import json
import logging
import queue
import threading
import time
from urllib.request import Request, urlopen

LOGGER = logging.getLogger(__name__)


class WebhookClient:
    def __init__(
        self,
        start_url: str | None = None,
        stop_url: str | None = None,
        method: str = "POST",
        timeout: float = 5.0,
        background: bool = True,
    ):
        self.start_url = start_url or None
        self.stop_url = stop_url or None
        self.method = method.upper()
        self.timeout = timeout
        self.background = background
        self._queue: queue.Queue[tuple[str, dict]] = queue.Queue()
        self._worker: threading.Thread | None = None

    def idle_timer_started(self, state: str, timeout_seconds: int) -> None:
        self._send(self.start_url, {
            "event": "idle_timer_started",
            "state": state,
            "timeout_seconds": timeout_seconds,
        })

    def idle_timer_stopped(self, state: str, reason: str) -> None:
        self._send(self.stop_url, {
            "event": "idle_timer_stopped",
            "state": state,
            "reason": reason,
        })

    def _send(self, url: str | None, payload: dict) -> None:
        if not url:
            return
        payload = {**payload, "timestamp": time.time()}
        if self.background:
            # A single worker keeps events delivered in the order they occurred.
            if self._worker is None:
                self._worker = threading.Thread(target=self._drain, daemon=True)
                self._worker.start()
            self._queue.put((url, payload))
        else:
            self._post(url, payload)

    def _drain(self) -> None:
        while True:
            url, payload = self._queue.get()
            self._post(url, payload)

    def _post(self, url: str, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8") if self.method != "GET" else None
        headers = {"Content-Type": "application/json"} if data is not None else {}
        request = Request(url, data=data, headers=headers, method=self.method)
        LOGGER.debug("Calling webhook %s %s with %r", self.method, url, payload)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                LOGGER.info("Webhook %s %s returned %d", self.method, url, response.status)
        except Exception as err:  # noqa: BLE001 - webhook failures must never stop the service
            LOGGER.warning("Webhook %s %s failed: %s", self.method, url, err)
