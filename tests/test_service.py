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

    def power_on_and_configure(self, input_name, delay):
        self.calls.append(("on", input_name, delay))
        self.on = True
        self.current_input = input_name

    def standby(self):
        self.calls.append(("standby",))
        self.on = False


class ServiceTests(unittest.TestCase):
    def config(self, **changes):
        values = dict(
            avr_input="PLEX",
            off_timer_seconds=60,
            power_on_delay_seconds=4.0,
        )
        values.update(changes)
        return Config(**values)

    def test_playback_powers_on_and_selects_input(self):
        avr = FakeAvr()
        controller = AvrController(FakePlexamp(["playing"]), avr, self.config(), FakeClock())
        controller.step()
        self.assertEqual(avr.calls, [("on", "PLEX", 4.0)])

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

    def test_playback_snapshot_counts_down_and_cancels_on_buffering(self):
        clock = FakeClock()
        controller = AvrController(FakePlexamp(["paused", "paused", "buffering"]), FakeAvr(), self.config(), clock)
        self.assertEqual(controller.playback_snapshot(), {"state": "unknown", "idle_remaining_seconds": None})
        updates = []
        controller.add_listener(lambda: updates.append(controller.playback_snapshot()))
        controller.step()
        self.assertEqual(updates[-1], {"state": "paused", "idle_remaining_seconds": 60.0})
        clock.value = 27.5
        self.assertEqual(controller.playback_snapshot()["idle_remaining_seconds"], 32.5)
        controller.step()
        self.assertEqual(len(updates), 1, "unchanged polls must not restart the timer or broadcast")
        controller.step()
        self.assertEqual(updates[-1], {"state": "buffering", "idle_remaining_seconds": None})

    def test_expired_timer_is_cleared_and_not_restarted(self):
        for input_name in ("PLEX", "GAME"):
            with self.subTest(input_name=input_name):
                clock = FakeClock()
                controller = AvrController(
                    FakePlexamp(["stopped", "stopped", "stopped"]),
                    FakeAvr(on=True, input_name=input_name), self.config(), clock,
                )
                controller.step()
                clock.value = 61
                self.assertEqual(controller.playback_snapshot()["idle_remaining_seconds"], 0)
                controller.step()
                self.assertEqual(controller.playback_snapshot(), {"state": "stopped", "idle_remaining_seconds": None})
                controller.step()
                self.assertIsNone(controller.playback_snapshot()["idle_remaining_seconds"])

    def test_unknown_poll_reports_unknown_without_changing_automation_timer(self):
        clock = FakeClock()
        avr = FakeAvr(on=True, input_name="PLEX")
        controller = AvrController(FakePlexamp(["paused", "unknown", "paused"]), avr, self.config(), clock)
        controller.step()
        clock.value = 61
        controller.step()
        self.assertEqual(controller.playback_snapshot(), {"state": "unknown", "idle_remaining_seconds": 0})
        self.assertEqual(avr.calls, [])
        controller.step()
        self.assertIsNone(controller.playback_snapshot()["idle_remaining_seconds"])
        self.assertEqual(avr.calls, [("standby",)])

    def test_status_is_published_before_power_on_delay(self):
        avr = FakeAvr()
        controller = AvrController(FakePlexamp(["playing"]), avr, self.config())
        snapshots = []
        avr.power_on_and_configure = lambda *args: snapshots.append(controller.playback_snapshot())
        controller.step()
        self.assertEqual(snapshots, [{"state": "playing", "idle_remaining_seconds": None}])


if __name__ == "__main__":
    unittest.main()