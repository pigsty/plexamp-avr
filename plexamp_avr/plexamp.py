from __future__ import annotations

import logging
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.error import URLError
from urllib.request import Request, urlopen

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlaybackState:
    state: str

    @property
    def is_playing(self) -> bool:
        return self.state.lower() in {"playing", "buffering"}

    @property
    def is_unknown(self) -> bool:
        return self.state.lower() == "unknown"


class PlexampClient:
    def __init__(self, host: str, port: int, timeout: float = 30.0):
        self.base_url = f"http://{host}:{port}"
        self.timeout = timeout

    def poll(self) -> PlaybackState:
        command_id = int(time.time() * 1000)
        url = (
            f"{self.base_url}/player/timeline/poll?type=music&wait=1"
            f"&includeMetadata=1&commandID={command_id}"
        )
        LOGGER.debug("Polling Plexamp URL: %s", url)

        request = Request(url, headers={"Accept": "text/xml, application/xml"})
        with urlopen(request, timeout=self.timeout) as response:
            status = response.status
            raw_body = response.read().strip()

        LOGGER.debug("Plexamp response status: %d, body: %r", status, raw_body)

        if not raw_body:
            LOGGER.debug("Received empty response body from Plexamp")
            return PlaybackState("stopped")

        return PlaybackState(self._state_from_xml(raw_body))

    @staticmethod
    def _state_from_xml(raw_xml: bytes) -> str:
        try:
            root = ET.fromstring(raw_xml)
            # Find the music timeline element specifically
            for timeline in root.findall("Timeline"):
                if timeline.attrib.get("type") == "music":
                    return timeline.attrib.get("state", "stopped")

            # Fallback to any generic timeline with a state attribute
            timeline = root.find(".//Timeline[@state]")
            if timeline is not None:
                return timeline.attrib.get("state", "stopped")
        except ET.ParseError as err:
            LOGGER.warning("XML parse error from Plexamp: %s", err)

        return "stopped"

    def poll_with_retry(self) -> PlaybackState:
        try:
            return self.poll()
        except (OSError, URLError) as err:
            LOGGER.warning("Plexamp poll network connection failed: %s", err)
            return PlaybackState("unknown")