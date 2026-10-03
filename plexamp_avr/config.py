from __future__ import annotations

import configparser
from dataclasses import dataclass
from pathlib import Path

WEBHOOK_METHODS = frozenset({"GET", "POST", "PUT", "PATCH"})


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
    idle_timer_start_webhook_url: str | None = None
    idle_timer_expired_webhook_url: str | None = None
    webhook_method: str = "POST"

    def __post_init__(self) -> None:
        if self.webhook_method not in WEBHOOK_METHODS:
            raise ValueError(f"webhook_method must be one of {', '.join(sorted(WEBHOOK_METHODS))}")

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
            idle_timer_start_webhook_url=values.get("idle_timer_start_webhook_url", "", raw=True).strip() or None,
            idle_timer_expired_webhook_url=values.get("idle_timer_expired_webhook_url", "", raw=True).strip() or None,
            webhook_method=values.get("webhook_method", cls.webhook_method).strip().upper() or cls.webhook_method,
        )
