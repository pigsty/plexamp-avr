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
        self.previous_state: str | None = None
        self.idle_start_time: float | None = None

    def step(self) -> None:
        current_state = self.plexamp.poll_with_retry()
        now = self.clock.monotonic()

        # 1. Ignore unknown poll errors
        if current_state.is_unknown:
            return

        state_name = current_state.state.lower()

        # Handle Initial Startup while Paused/Stopped
        if self.previous_state is None and not current_state.is_playing:
            self.idle_start_time = now

        # 2. Transition: Transitioning TO Playing
        if current_state.is_playing:
            self.idle_start_time = None
            if self.previous_state != state_name:
                if not self.avr.is_on():
                    LOGGER.info("Playback started; powering AVR on")
                    self.avr.power_on_and_configure(
                        self.config.avr_input,
                        self.config.preset_volume,
                        self.config.power_on_delay_seconds,
                    )
                else:
                    LOGGER.info("Playback started; AVR already on, no action taken")

        # 3. Transition: Transitioning FROM Playing TO Idle (Paused/Stopped)
        elif self.previous_state and self.previous_state in {"playing", "buffering"}:
            LOGGER.info("Playback changed to %s; starting %ds idle timer", state_name, self.config.off_timer_seconds)
            self.idle_start_time = now

        # 4. Sustained Idle Timer Expiration Check
        if self.idle_start_time is not None and (now - self.idle_start_time) >= self.config.off_timer_seconds:
            if self.avr.is_on():
                current_input = self.avr.input_name()
                if current_input == self.config.avr_input:
                    LOGGER.info("Idle timeout reached (%ds); setting AVR to standby", self.config.off_timer_seconds)
                    self.avr.standby()
                else:
                    LOGGER.info("Idle timeout reached, but AVR input is %r; leaving on", current_input)
            else:
                LOGGER.info("Idle timeout reached, but AVR is already off; no action taken")
            
            # Reset timer after firing so it only fires once per idle session
            self.idle_start_time = None

        # Record current state for the next step comparison
        self.previous_state = state_name

    def run(self) -> None:
        while True:
            self.step()


def build_controller(config: Config, avr: DenonClient | None = None) -> AvrController:
    return AvrController(
        PlexampClient(config.plexamp_host, config.plexamp_port, config.request_timeout_seconds),
        avr or DenonClient(config.avr_host, config.avr_port, config.request_timeout_seconds),
        config,
    )