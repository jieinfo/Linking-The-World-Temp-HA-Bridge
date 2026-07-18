"""Start the standalone bridge with Home Assistant add-on options."""

from __future__ import annotations

import json
from pathlib import Path

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


if __name__ == "__main__":
    Bridge(load_options()).run()
