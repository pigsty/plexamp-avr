import json
import threading
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from plexamp_avr.denon import DenonClient


class VolumePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix=".volume-test-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.path = self.directory / "volumes.json"

    def test_persistence_preserves_each_zone_and_input_on_restart(self):
        client = DenonClient("unused", data_dir=self.directory)
        for line in ("SICD", "MV455", "SITV", "MV20", "Z2CD", "Z230", "Z3CD", "Z340"):
            client._handle_line(line)
        other = DenonClient("unused", data_dir=self.directory)
        self.assertEqual(other._volumes, {
            "z1": {"CD": 45.5, "TV": 20}, "z2": {"CD": 30}, "z3": {"CD": 40},
        })
        self.assertEqual(other.snapshot()["zones"]["z1"]["input"], None)
        self.assertEqual(list(self.directory.iterdir()), [self.path])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_predictable_staging_symlink_is_not_followed(self):
        victim = self.directory / "unrelated"
        victim.write_text("do not overwrite")
        predictable = self.directory / "volumes.json.new"
        predictable.symlink_to(victim)
        client = DenonClient("unused", data_dir=self.directory)
        client._handle_line("SICD")
        client._handle_line("MV30")
        self.assertEqual(victim.read_text(), "do not overwrite")
        self.assertEqual(json.loads(self.path.read_text())["z1"]["CD"], 30)

    def test_concurrent_clients_use_distinct_staging_files(self):
        clients = [DenonClient("unused", data_dir=self.directory) for _ in range(2)]
        for client in clients:
            client._handle_line("SICD")
        barrier = threading.Barrier(2)
        original = tempfile.mkstemp
        paths = []

        def create_staging(*args, **kwargs):
            descriptor, name = original(*args, **kwargs)
            paths.append(name)
            barrier.wait(timeout=5)
            return descriptor, name

        with patch("plexamp_avr.denon.tempfile.mkstemp", side_effect=create_staging):
            threads = [threading.Thread(target=client._handle_line, args=(f"MV{30 + index}",))
                       for index, client in enumerate(clients)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(len(set(paths)), 2)
        self.assertIn(json.loads(self.path.read_text())["z1"]["CD"], (30, 31))
        self.assertEqual(list(self.directory.iterdir()), [self.path])

    def test_invalid_disk_entries_are_ignored(self):
        self.path.write_text(json.dumps({
            "z1": {"CD": 45.5, "ON": 20, "a\rcommand": 20, "TV": True,
                   "DVD": "50", "GAME": -1, "TUNER": 99, "NET": float("nan")},
            "z2": {"CD": 30.5, "TV": 30}, "z3": [],
            "z4": {"CD": 10},
        }))
        client = DenonClient("unused", data_dir=self.directory)
        self.assertEqual(client._volumes, {
            "z1": {"CD": 45.5}, "z2": {"TV": 30}, "z3": {},
        })

    def test_missing_corrupt_or_wrong_shape_file_is_safe(self):
        for content in (None, "{bad json", "[]", "null"):
            if content is not None:
                self.path.write_text(content)
            client = DenonClient("unused", data_dir=self.directory)
            self.assertEqual(client._volumes, {"z1": {}, "z2": {}, "z3": {}})

    def test_io_failure_keeps_in_memory_values_and_existing_disk_file(self):
        client = DenonClient("unused", data_dir=self.directory)
        client._handle_line("SICD")
        client._handle_line("MV30")
        previous = self.path.read_text()
        with patch("plexamp_avr.denon.os.replace", side_effect=OSError("read only")):
            client._handle_line("MV40")
        self.assertEqual(client._volumes["z1"]["CD"], 40)
        self.assertEqual(self.path.read_text(), previous)
        self.assertEqual(list(self.directory.iterdir()), [self.path])
        with patch("pathlib.Path.read_text", side_effect=PermissionError):
            other = DenonClient("unused", data_dir=self.directory)
        self.assertEqual(other._volumes["z1"], {})

    def test_memory_only_never_writes(self):
        client = DenonClient("unused")
        with patch("pathlib.Path.open", side_effect=AssertionError("unexpected disk access")):
            client._handle_line("SICD")
            client._handle_line("MV40")
        self.assertEqual(client._volumes["z1"]["CD"], 40)

    def test_concurrent_event_updates_produce_valid_complete_json(self):
        client = DenonClient("unused", data_dir=self.directory)
        for line in ("SICD", "Z2CD", "Z3CD"):
            client._handle_line(line)
        threads = [threading.Thread(target=client._handle_line, args=(line,))
                   for line in ("MV30", "Z240", "Z350")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(json.loads(self.path.read_text()), {
            "z1": {"CD": 30}, "z2": {"CD": 40}, "z3": {"CD": 50},
        })
