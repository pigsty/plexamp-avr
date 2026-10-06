from __future__ import annotations

import argparse
import logging
import os

from .config import Config
from .denon import DenonClient
from .service import build_controller
from .web import WebServer
from .webhooks import WebhookDispatcher, WebhookStore


def _configure_logging(debug: bool) -> None:
    level = "DEBUG" if debug else os.getenv("LOG_LEVEL", "info").upper()
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(message)s")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="/etc/plexamp-avr.conf")
    parser.add_argument("--debug", "-d", action="store_true", help="Enable debug logging")
    args = parser.parse_args()
    _configure_logging(args.debug)
    config = Config.from_file(args.config)
    avr = DenonClient(config.avr_host, config.avr_port, config.request_timeout_seconds)
    webhooks = WebhookStore(config.data_dir)
    dispatcher = WebhookDispatcher(webhooks, config.request_timeout_seconds)
    dispatcher.start()
    avr.add_listener(dispatcher.handle_snapshot)
    avr.start()
    if config.web_enabled:
        WebServer(
            avr,
            config.web_host,
            config.web_port,
            (*config.avr_inputs, config.avr_input),
            webhooks,
            dict(config.avr_input_aliases),
        ).start()
    build_controller(config, avr).run()

if __name__ == "__main__":
    main()
