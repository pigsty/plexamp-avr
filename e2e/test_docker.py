import json
import tempfile
import unittest
from pathlib import Path

from mocks import MockAvr, docker, free_port, mock_plexamp, print_logs, run_container, serve


class DockerTests(unittest.TestCase):
    def test_playback_configures_avr_and_idle_puts_it_in_standby(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        plexamp = mock_plexamp()
        self.addCleanup(serve(plexamp))
        avr = MockAvr()
        self.addCleanup(serve(avr))
        data_dir = Path(temporary.name) / "data"
        data_dir.mkdir()
        (data_dir / "volumes.json").write_text(json.dumps({"z1": {"MEDIA PLAYER": 45}}))
        config = Path(temporary.name) / "plexamp-avr.conf"
        config.write_text(
            "[plexamp-avr]\n"
            "plexamp_host=127.0.0.1\n"
            f"plexamp_port={plexamp.server_address[1]}\n"
            "avr_host=127.0.0.1\n"
            f"avr_port={avr.server_address[1]}\n"
            "avr_input=MEDIA PLAYER\n"
            "power_on_delay_seconds=0\n"
            "off_timer_seconds=1\n"
            "request_timeout_seconds=2\n"
            "web_host=127.0.0.1\n"
            f"web_port={free_port()}\n"
        )
        container = run_container(config, data_dir)
        self.addCleanup(docker, "rm", "--force", container, check=False)
        try:
            for expected in ("ZMON", "SIMEDIA PLAYER", "MV45"):
                self.assertEqual(avr.commands.get(timeout=20), expected)
            plexamp.stopped.set()
            self.assertEqual(avr.commands.get(timeout=20), "ZMOFF")
            self.assertEqual(
                docker("inspect", "--format", "{{.State.Running}}", container).stdout.strip(),
                "true",
            )
            self.assertEqual(avr.connections, 1, "telnet connection should be persistent")
        except Exception:
            print_logs(container)
            raise


if __name__ == "__main__":
    unittest.main()
