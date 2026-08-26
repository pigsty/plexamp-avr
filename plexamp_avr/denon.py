from __future__ import annotations

import logging
import socket
import time

LOGGER = logging.getLogger(__name__)


class DenonClient:
    def __init__(self, address: str, port: int = 23, timeout: float = 5.0):
        self.address = address
        self.port = port
        self.timeout = timeout

    def command(self, value: str) -> list[str]:
        with socket.create_connection((self.address, self.port), self.timeout) as connection:
            connection.settimeout(self.timeout)
            connection.sendall((value + "\r").encode("ascii"))
            data = bytearray()
            try:
                while True:
                    chunk = connection.recv(4096)
                    if not chunk:
                        break
                    data.extend(chunk)
                    if b"\r" in data:
                        break
            except socket.timeout:
                pass
        return [line for line in data.decode("ascii", errors="replace").splitlines() if line]

    def power_state(self) -> str:
        replies = self.command("PW?")
        return next((reply[2:].upper() for reply in replies if reply.startswith("PW")), "UNKNOWN")

    def input_name(self) -> str | None:
        replies = self.command("SI?")
        return next((reply[2:] for reply in replies if reply.startswith("SI")), None)

    def is_on(self) -> bool:
        return self.power_state() == "ON"

    def power_on(self) -> None:
        self.command("PWON")

    def standby(self) -> None:
        self.command("PWSTANDBY")

    def set_input(self, input_name: str) -> None:
        self.command("SI" + input_name)

    def set_volume_db(self, volume_db: float) -> None:
        if volume_db > 0 or volume_db < -80:
            raise ValueError("preset volume must be between -80 and 0 dB")
        value = abs(volume_db)
        command_value = f"{value:.1f}".replace(".", "") if value % 1 else f"{int(value)}"
        self.command("MV" + command_value)

    def power_on_and_configure(self, input_name: str, volume_db: float | None, delay: float) -> None:
        self.power_on()
        time.sleep(delay)
        self.set_input(input_name)
        if volume_db is not None:
            self.set_volume_db(volume_db)
