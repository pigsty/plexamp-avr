import os
import unittest
from unittest.mock import patch

from plexamp_avr.cli import _configure_logging


class LoggingConfigurationTests(unittest.TestCase):
    @patch("plexamp_avr.cli.logging.basicConfig")
    def test_defaults_to_info(self, basic_config):
        with patch.dict(os.environ, {}, clear=True):
            _configure_logging(False)

        self.assertEqual(basic_config.call_args.kwargs["level"], "INFO")

    @patch("plexamp_avr.cli.logging.basicConfig")
    def test_uses_log_level_environment_variable(self, basic_config):
        with patch.dict(os.environ, {"LOG_LEVEL": "warning"}):
            _configure_logging(False)

        self.assertEqual(basic_config.call_args.kwargs["level"], "WARNING")

    @patch("plexamp_avr.cli.logging.basicConfig")
    def test_debug_flag_overrides_log_level_environment_variable(self, basic_config):
        with patch.dict(os.environ, {"LOG_LEVEL": "error"}):
            _configure_logging(True)

        self.assertEqual(basic_config.call_args.kwargs["level"], "DEBUG")


if __name__ == "__main__":
    unittest.main()