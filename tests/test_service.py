import unittest

from plexamp_avr.config import Config
from plexamp_avr.plexamp import PlaybackState
from plexamp_avr.service import AvrController


class FakeClock:
    def __init__(self):
        self.value = 0.0

    def monotonic(self):
        return self.value


class FakePlexamp:
    def __init__(self, states):
        self.states = iter(states)

    def poll_with_retry(self):
        return PlaybackState(next(self.states))


class FakeAvr:
    def __init__(self, on=False, input_name="GAME"):
        self.on = on
        self.current_input = input_name
        self.calls = []

    def is_on(self):
        return self.on

    def input_name(self):
        return self.current_input

    def power_on_and_configure(self, input_name, volume, delay):
        self.calls.append(("on", input_name, volume, delay))
        self.on = True
        self.current_input = input_name

    def standby(self):
        self.calls.append(("standby",))
        self.on = False


class FakeWebhooks:
    def __init__(self):
        self.events = []

    def idle_timer_started(self, state, timeout_seconds):
        self.events.append(("started", state, timeout_seconds))

    def idle_timer_expired(self, state, timeout_seconds):
        self.events.append(("expired", state, timeout_seconds))


class ServiceTests(unittest.TestCase):
    def config(self, **changes):
        values = dict(
            avr_input="PLEX",
            off_timer_seconds=60,
            preset_volume=-35.0,
            power_on_delay_seconds=4.0,
        )
        values.update(changes)
        return Config(**values)

    def test_playback_powers_on_and_sets_preset(self):
        avr = FakeAvr()
        controller = AvrController(FakePlexamp(["playing"]), avr, self.config(), FakeClock())
        controller.step()
        self.assertEqual(avr.calls, [("on", "PLEX", -35.0, 4.0)])

    def test_wrong_input_is_never_stopped(self):
        clock = FakeClock()
        avr = FakeAvr(on=True, input_name="GAME")
        controller = AvrController(FakePlexamp(["playing", "stopped", "stopped"]), avr, self.config(), clock)
        controller.step()  # playing at t=0
        controller.step()  # transitions to stopped at t=0 (idle_start_time = 0.0)
        clock.value = 60.0
        controller.step()  # checked at t=60 (timer expired)
        self.assertEqual(avr.calls, [])

    def test_expected_input_is_stopped_after_timer_when_plexamp_stopped(self):
        clock = FakeClock()
        avr = FakeAvr(on=True, input_name="PLEX")
        controller = AvrController(FakePlexamp(["playing", "stopped", "stopped"]), avr, self.config(), clock)
        controller.step()  # playing at t=0
        controller.step()  # transitions to stopped at t=0 (idle_start_time = 0.0)
        clock.value = 60.0
        controller.step()  # checked at t=60 (timer expired)
        self.assertEqual(avr.calls[-1], ("standby",))

    def test_expected_input_is_stopped_after_timer_when_plexamp_paused(self):
        clock = FakeClock()
        avr = FakeAvr(on=True, input_name="PLEX")
        controller = AvrController(FakePlexamp(["playing", "paused", "paused"]), avr, self.config(), clock)
        controller.step()  # playing at t=0
        controller.step()  # transitions to paused at t=0 (idle_start_time = 0.0)
        clock.value = 60.0
        controller.step()  # checked at t=60 (timer expired)
        self.assertEqual(avr.calls[-1], ("standby",))

    def test_startup_while_paused_starts_timer_and_stops_avr(self):
        clock = FakeClock()
        avr = FakeAvr(on=True, input_name="PLEX")
        controller = AvrController(FakePlexamp(["paused", "paused"]), avr, self.config(), clock)
        controller.step()  # startup while paused at t=0 (idle_start_time = 0.0)
        self.assertEqual(avr.calls, [])
        clock.value = 60.0
        controller.step()  # checked at t=60 (timer expired)
        self.assertEqual(avr.calls, [("standby",)])

    def test_unknown_poll_does_not_stop_avr(self):
        clock = FakeClock()
        avr = FakeAvr(on=True, input_name="PLEX")
        controller = AvrController(FakePlexamp(["playing", "unknown"]), avr, self.config(), clock)
        controller.step()  # playing at t=0
        clock.value = 61.0
        controller.step()  # unknown state at t=61 (ignored)
        self.assertEqual(avr.calls, [])

    def test_resume_does_not_call_expired_webhook(self):
        webhooks = FakeWebhooks()
        avr = FakeAvr(on=True, input_name="PLEX")
        controller = AvrController(
            FakePlexamp(["playing", "paused", "paused", "playing", "playing"]),
            avr, self.config(), FakeClock(), webhooks,
        )
        for _ in range(5):
            controller.step()
        self.assertEqual(webhooks.events, [
            ("started", "paused", 60),
        ])

    def test_webhooks_called_when_idle_timer_expires(self):
        clock = FakeClock()
        webhooks = FakeWebhooks()
        avr = FakeAvr(on=True, input_name="PLEX")
        controller = AvrController(FakePlexamp(["stopped", "stopped", "stopped"]), avr, self.config(), clock, webhooks)
        controller.step()
        clock.value = 60.0
        controller.step()
        controller.step()
        self.assertEqual(webhooks.events, [
            ("started", "stopped", 60),
            ("expired", "stopped", 60),
        ])


if __name__ == "__main__":
    unittest.main()