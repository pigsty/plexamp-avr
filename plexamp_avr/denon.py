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

    def command(self, cmd: str, arg: str = "") -> list[str]:
        full_command = f"{cmd}{arg}"
        is_query = arg == "?"
        LOGGER.debug("Sending command to Denon AVR: %r (is_query=%s)", full_command, is_query)
        
        replies = []
        try:
            with socket.create_connection((self.address, self.port), self.timeout) as connection:
                # Use a shorter timeout for reading responses after sending
                connection.settimeout(self.timeout if is_query else 0.3)
                connection.sendall((full_command + "\r").encode("ascii"))
                
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
                                LOGGER.debug("Received reply from Denon AVR: %r", line)
                                
                                # If it's a query and we got our matching prefix, exit immediately
                                if is_query and line.startswith(cmd):
                                    LOGGER.debug("Received query response %r for %r", line, full_command)
                                    return replies
                    except socket.timeout:
                        # For set commands or missing query responses, exit loop cleanly on timeout
                        break

        except (socket.timeout, OSError) as err:
            LOGGER.warning("Denon Telnet command %r failed: %s", full_command, err)

        return replies

    def power_state(self) -> str:
        replies = self.command("ZM", "?")
        for reply in replies:
            if reply.startswith("ZM"):
                return reply[2:].upper()
        return "UNKNOWN"

    def input_name(self) -> str | None:
        replies = self.command("SI", "?")
        for reply in replies:
            if reply.startswith("SI"):
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
        value = abs(volume)
        arg_value = f"{value:.1f}".replace(".", "") if value % 1 else f"{int(value)}"
        self.command("MV", arg_value)

    def power_on_and_configure(self, input_name: str, volume: float | None, delay: float) -> None:
        self.power_on()
        time.sleep(delay)
        self.set_input(input_name)
        if volume is not None:
            self.set_volume(volume)