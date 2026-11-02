import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from plexamp_avr.config import Config


class ConfigTests(unittest.TestCase):
    def read_config(self, aliases=""):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "plexamp-avr.conf"
        path.write_text(f"[plexamp-avr]\navr_input_aliases = {aliases}\n")
        return Config.from_file(path)

    def test_reads_input_aliases_and_normalizes_names(self):
        config = self.read_config("mplay=Apple TV, BD=Blu-ray")

        self.assertEqual(config.avr_input_aliases, (("MPLAY", "Apple TV"), ("BD", "Blu-ray")))

    def test_input_aliases_default_to_empty(self):
        self.assertEqual(self.read_config().avr_input_aliases, ())

    def test_max_volume_defaults_to_75(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(self.read_config().max_volume, 75)

    def test_max_volume_from_environment(self):
        for value in (0, 65, 98):
            with self.subTest(value=value), patch.dict(os.environ, {"MAX_VOLUME": str(value)}):
                self.assertEqual(self.read_config().max_volume, value)

    def test_invalid_max_volume_is_rejected(self):
        for value in ("-1", "99", "75.5", "nan", "", "loud"):
            with self.subTest(value=value), patch.dict(os.environ, {"MAX_VOLUME": value}):
                with self.assertRaises(ValueError):
                    self.read_config()

    def test_rejects_malformed_input_alias(self):
        with self.assertRaisesRegex(ValueError, "INPUT=Alias"):
            self.read_config("MPLAY")

    def test_legacy_preset_volume_is_ignored(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "plexamp-avr.conf"
        path.write_text("[plexamp-avr]\npreset_volume=obsolete\n")
        config = Config.from_file(path)
        self.assertFalse(hasattr(config, "preset_volume"))


if __name__ == "__main__":
    unittest.main()