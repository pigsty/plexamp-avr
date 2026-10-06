import tempfile
import unittest
from pathlib import Path

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

    def test_rejects_malformed_input_alias(self):
        with self.assertRaisesRegex(ValueError, "INPUT=Alias"):
            self.read_config("MPLAY")


if __name__ == "__main__":
    unittest.main()