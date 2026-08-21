"""Start the standalone bridge with Home Assistant add-on options."""

from __future__ import annotations

import json
import logging
from pathlib import Path
import time

from bridge import Bridge, HealthEndpoint, LivenessMonitor


OPTIONS_PATH = Path("/data/options.json")


def load_options() -> dict:
    options = json.loads(OPTIONS_PATH.read_text(encoding="utf-8"))
    return {
        "moorgen": {
            "host": options["moorgen_host"],
            "port": int(options["moorgen_port"]),
            "username": options["moorgen_username"],
            "password": options["moorgen_password"],
            "client_id": options["moorgen_client_id"],
            "tech_system_mac": options.get("moorgen_tech_system_mac", "ff00ffffffff00ff"),
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
        "safety": {
            "allow_control": options.get("allow_control", True),
            "command_min_interval": float(options.get("command_min_interval", 0.5)),
            "thermostat_offline_after": float(options.get("thermostat_offline_after", 900)),
            "require_protocol_verification": options.get("require_protocol_verification", True),
            "controller_silence_timeout": float(options.get("controller_silence_timeout", 300)),
            "command_confirmation_timeout": float(options.get("command_confirmation_timeout", 8)),
        },
        "automation_filter": {
            "enabled": options.get("automation_filter_enabled", True),
            "samples": int(options.get("automation_filter_samples", 3)),
            "temperature_deadband": float(options.get("automation_temperature_deadband", 0.2)),
            "humidity_deadband": int(options.get("automation_humidity_deadband", 2)),
        },
        "diagnostics": {
            "publish_raw_status": options.get("publish_raw_status", False),
        },
    }


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    monitor = LivenessMonitor()
    endpoint = HealthEndpoint(monitor)
    endpoint.start()
    try:
        while True:
            monitor.touch("starting_session")
            bridge: Bridge | None = None
            try:
                bridge = Bridge(load_options(), liveness_monitor=monitor)
                bridge.run()
            except (ConnectionError, OSError, TimeoutError):
                monitor.touch("waiting_to_retry")
                logging.exception("MC7021 session failed; retrying in 15 seconds")
            finally:
                if bridge is not None:
                    bridge.client.close()
            for _ in range(15):
                monitor.touch("waiting_to_retry")
                time.sleep(1)
    finally:
        endpoint.stop()


if __name__ == "__main__":
    main()
