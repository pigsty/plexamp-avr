from __future__ import annotations

import logging
import socket
import time

LOGGER = logging.getLogger(__name__)


class DenonClient:
    def __init__(self, address: str, port: int = 23, timeout: float = 2.0):
        self.address = address
        self.port = port
        self.timeout = timeout
        LOGGER.info("Denon client initialized with address: %s:%d", self.address, self.port)

    def command(self, value: str, wait_for_response_prefix: str | None = None) -> list[str]:
        replies = []
        try:
            with socket.create_connection((self.address, self.port), self.timeout) as connection:
                connection.settimeout(self.timeout)
                connection.sendall((value + "\r").encode("ascii"))
                
                buffer = ""
                while True:
                    try:
                        chunk = connection.recv(4096).decode("ascii", errors="replace")
                        if not chunk:
                            break
                        buffer += chunk
                        
                        # Process complete lines terminated by \r
                        while "\r" in buffer:
                            line, buffer = buffer.split("\r", 1)
                            line = line.strip()
                            if line:
                                replies.append(line)
                                # Exit immediately if we found the line we were waiting for
                                if wait_for_response_prefix and line.startswith(wait_for_response_prefix):
                                    LOGGER.debug("Received target response %r for %r", line, value)
                                    return replies
                    except socket.timeout:
                        # Fallback timeout if wait_for_response_prefix is never sent by AVR
                        LOGGER.debug("Socket timeout reached while waiting for %r", value)
                        break

        except (socket.timeout, OSError) as err:
            LOGGER.warning("Denon Telnet command %r failed: %s", value, err)

        return replies

    def power_state(self) -> str:
        replies = self.command("ZM?", wait_for_response_prefix="ZM")
        for reply in replies:
            if reply.startswith("ZM"):
                return reply[2:].upper()
        return "UNKNOWN"

    def input_name(self) -> str | None:
        replies = self.command("SI?", wait_for_response_prefix="SI")
        for reply in replies:
            if reply.startswith("SI"):
                return reply[2:]
        return None
    
    def is_on(self) -> bool:
        return self.power_state() == "ON"

    def power_on(self) -> None:
        self.command("ZMON")

    def standby(self) -> None:
        self.command("ZMOFF")

    def set_input(self, input_name: str) -> None:
        self.command("SI" + input_name)

    def set_volume(self, volume_db: float) -> None:
        if volume_db > 98 or volume_db < 0:
            raise ValueError("preset volume must be between 0 and 98")
        value = abs(volume_db)
        command_value = f"{value:.1f}".replace(".", "") if value % 1 else f"{int(value)}"
        self.command("MV" + command_value)

    def power_on_and_configure(self, input_name: str, volume_db: float | None, delay: float) -> None:
        self.power_on()
        time.sleep(delay)
        self.set_input(input_name)
        if volume_db is not None:
            self.set_volume(volume_db)
