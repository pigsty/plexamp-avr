from __future__ import annotations

import argparse
import logging

from .config import Config
from .denon import DenonClient
from .service import build_controller
from .web import WebServer


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="/etc/plexamp-avr.conf")
    parser.add_argument("--debug", "-d", action="store_true", help="Enable debug logging")
    args = parser.parse_args()
    if args.debug:
        logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s %(message)s")
    else:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = Config.from_file(args.config)
    avr = DenonClient(config.avr_host, config.avr_port, config.request_timeout_seconds)
    avr.start()
    if config.web_enabled:
        WebServer(avr, config.web_host, config.web_port, (*config.avr_inputs, config.avr_input)).start()
    build_controller(config, avr).run()

if __name__ == "__main__":
    main()
