from __future__ import annotations

import configparser
from dataclasses import dataclass
from pathlib import Path

DEFAULT_INPUTS = (
    "PHONO", "CD", "TUNER", "DVD", "BD", "TV", "SAT/CBL", "MPLAY", "GAME",
    "AUX1", "AUX2", "NET", "BT", "USB/IPOD",
)


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
    request_timeout_seconds: float = 5.0
    web_enabled: bool = True
    web_host: str = "0.0.0.0"
    web_port: int = 8080
    avr_inputs: tuple[str, ...] = DEFAULT_INPUTS
    data_dir: str = "/data"

    @classmethod
    def from_file(cls, path: str | Path) -> "Config":
        parser = configparser.ConfigParser()
        if not parser.read(path):
            raise FileNotFoundError(path)
        values = parser["plexamp-avr"]
        preset = values.get("preset_volume", "").strip()
        inputs = tuple(
            name.strip().upper() for name in values.get("avr_inputs", "").split(",") if name.strip()
        )
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
            web_enabled=values.getboolean("web_enabled", cls.web_enabled),
            web_host=values.get("web_host", cls.web_host),
            web_port=values.getint("web_port", cls.web_port),
            avr_inputs=inputs or DEFAULT_INPUTS,
            data_dir=values.get("data_dir", "").strip() or cls.data_dir,
        )
