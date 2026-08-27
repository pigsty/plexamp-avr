from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Protocol

from .config import Config
from .denon import DenonClient
from .plexamp import PlexampClient

LOGGER = logging.getLogger(__name__)


class Clock(Protocol):
    def monotonic(self) -> float: ...


@dataclass
class SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()


class AvrController:
    def __init__(self, plexamp: PlexampClient, avr: DenonClient, config: Config, clock: Clock | None = None):
        self.plexamp = plexamp
        self.avr = avr
        self.config = config
        self.clock = clock or SystemClock()
        self.last_playing_at: float | None = None

    def step(self) -> None:
        state = self.plexamp.poll_with_retry()
        now = self.clock.monotonic()
      
        if state.is_unknown:
            return
        if state.is_playing:
            self.last_playing_at = now
            if not self.avr.is_on():
                LOGGER.info("Playback detected and AVR is off; powering AVR on and setting volume")
                self.avr.power_on_and_configure(
                    self.config.avr_input,
                    self.config.preset_volume,
                    self.config.power_on_delay_seconds,
                )
            return

        if self.last_playing_at is None:
            return
        if now - self.last_playing_at < self.config.off_timer_seconds:
            return
        if self.avr.is_on():
            if self.avr.input_name() == self.config.avr_input:
                LOGGER.info("Playback idle for %d seconds; putting AVR into standby", self.config.off_timer_seconds)
                self.avr.standby()
            else:
                LOGGER.info(
                    "Playback idle for %d seconds, but AVR input is %r; leaving it on",
                    self.config.off_timer_seconds,
                    self.avr.input_name(),
                )
        else:
            LOGGER.info("Playback idle for %d seconds, but AVR is already off", self.config.off_timer_seconds) 
        self.last_playing_at = None

    def run(self) -> None:
        while True:
            self.step()


def build_controller(config: Config) -> AvrController:
    return AvrController(
        PlexampClient(config.plexamp_host, config.plexamp_port, config.request_timeout_seconds),
        DenonClient(config.avr_host, config.avr_port, config.request_timeout_seconds),
        config,
    )
