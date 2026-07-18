"""Start the standalone bridge with Home Assistant add-on options."""

from __future__ import annotations

import json
import logging
from pathlib import Path
import time

from bridge import Bridge


OPTIONS_PATH = Path("/data/options.json")


def load_options() -> dict:
    options = json.loads(OPTIONS_PATH.read_text(encoding="utf-8"))
    return {
        "moorgen": {
            "host": options["moorgen_host"],
            "port": int(options["moorgen_port"]),
            "username": options["moorgen_username"],
            "password": options["moorgen_password"],
        },
        "mqtt": {
            "host": options["mqtt_host"],
            "port": int(options["mqtt_port"]),
            "username": options.get("mqtt_username", ""),
            "password": options.get("mqtt_password", ""),
            "client_id": options["mqtt_client_id"],
            "topic_prefix": options["mqtt_topic_prefix"],
            "discovery_prefix": options["mqtt_discovery_prefix"],
        },
    }


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    while True:
        bridge = Bridge(load_options())
        try:
            bridge.run()
        except (ConnectionError, OSError, TimeoutError):
            logging.exception("MC7021 session failed; retrying in 15 seconds")
        finally:
            bridge.client.close()
        time.sleep(15)


if __name__ == "__main__":
    main()
