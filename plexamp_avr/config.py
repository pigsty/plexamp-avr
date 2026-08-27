from __future__ import annotations

import configparser
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    plexamp_host: str = "localhost"
    plexamp_port: int = 32500
    avr_host: str = "192.168.1.100"
    avr_port: int = 23
    avr_input: str = "MEDIA PLAYER"
    off_timer_seconds: int = 900
    preset_volume: float | None = None
    power_on_delay_seconds: float = 5.0
    request_timeout_seconds: float = 30.0

    @classmethod
    def from_file(cls, path: str | Path) -> "Config":
        parser = configparser.ConfigParser()
        if not parser.read(path):
            raise FileNotFoundError(path)
        values = parser["plexamp-avr"]
        preset = values.get("preset_volume", "").strip()
        return cls(
            plexamp_host=values.get("plexamp_host", cls.plexamp_host),
            plexamp_port=values.getint("plexamp_port", cls.plexamp_port),
            avr_host=values.get("avr_host", cls.avr_host),
            avr_port=values.getint("avr_port", cls.avr_port),
            avr_input=values.get("avr_input", cls.avr_input),
            off_timer_seconds=values.getint("off_timer_seconds", cls.off_timer_seconds),
            preset_volume=float(preset) if preset else None,
            power_on_delay_seconds=values.getfloat("power_on_delay_seconds", cls.power_on_delay_seconds),
            request_timeout_seconds=values.getfloat("request_timeout_seconds", cls.request_timeout_seconds),
        )
