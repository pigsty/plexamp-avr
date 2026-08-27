import unittest

from plexamp_avr.config import Config
from plexamp_avr.plexamp import PlaybackState
from plexamp_avr.service import AvrController


class FakeClock:
    def __init__(self):
        self.value = 0

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


class ServiceTests(unittest.TestCase):
    def config(self, **changes):
        values = dict(avr_input="PLEX", off_timer_seconds=60, preset_volume=35, power_on_delay_seconds=4)
        values.update(changes)
        return Config(**values)

    def test_playback_powers_on_and_sets_preset(self):
        avr = FakeAvr()
        controller = AvrController(FakePlexamp(["playing"]), avr, self.config(), FakeClock())
        controller.step()
        self.assertEqual(avr.calls, [("on", "PLEX", 35, 4)])

    def test_wrong_input_is_never_stopped(self):
        clock = FakeClock()
        avr = FakeAvr(on=True, input_name="GAME")
        controller = AvrController(FakePlexamp(["playing", "stopped"]), avr, self.config(), clock)
        controller.step()
        clock.value = 61
        controller.step()
        self.assertEqual(avr.calls, [])

    def test_expected_input_is_stopped_after_timer(self):
        clock = FakeClock()
        avr = FakeAvr(on=True, input_name="PLEX")
        controller = AvrController(FakePlexamp(["playing", "stopped"]), avr, self.config(), clock)
        controller.step()
        clock.value = 60
        controller.step()
        self.assertEqual(avr.calls[-1], ("standby",))

    def test_unknown_poll_does_not_stop_avr(self):
        clock = FakeClock()
        avr = FakeAvr(on=True, input_name="PLEX")
        controller = AvrController(FakePlexamp(["playing", "unknown"]), avr, self.config(), clock)
        controller.step()
        clock.value = 61
        controller.step()
        self.assertEqual(avr.calls, [])


if __name__ == "__main__":
    unittest.main()
