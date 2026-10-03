from __future__ import annotations

import json
import logging
import threading
import time
from urllib.error import URLError
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
            threading.Thread(target=self._post, args=(url, payload), daemon=True).start()
        else:
            self._post(url, payload)

    def _post(self, url: str, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8") if self.method != "GET" else None
        headers = {"Content-Type": "application/json"} if data is not None else {}
        request = Request(url, data=data, headers=headers, method=self.method)
        LOGGER.debug("Calling webhook %s %s with %r", self.method, url, payload)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                LOGGER.info("Webhook %s %s returned %d", self.method, url, response.status)
        except (OSError, URLError, ValueError) as err:
            LOGGER.warning("Webhook %s %s failed: %s", self.method, url, err)
