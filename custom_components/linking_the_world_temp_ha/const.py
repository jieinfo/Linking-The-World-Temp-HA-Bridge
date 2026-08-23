"""Constants for Linking The World Temp HA."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "linking_the_world_temp_ha"
NAME: Final = "Linking The World Temp HA"

CONF_CLIENT_ID: Final = "client_id"
CONF_TECH_SYSTEM_MAC: Final = "tech_system_mac"
CONF_ALLOW_CONTROL: Final = "allow_control"
CONF_COMMAND_MIN_INTERVAL: Final = "command_min_interval"
CONF_COMMAND_CONFIRMATION_TIMEOUT: Final = "command_confirmation_timeout"
CONF_CONTROLLER_SILENCE_TIMEOUT: Final = "controller_silence_timeout"
CONF_THERMOSTAT_OFFLINE_AFTER: Final = "thermostat_offline_after"

DEFAULT_PORT: Final = 9000
DEFAULT_USERNAME: Final = "admin"
DEFAULT_CLIENT_ID: Final = "ff9549d5891998e5"
DEFAULT_TECH_SYSTEM_MAC: Final = "ff00ffffffff00ff"
DEFAULT_ALLOW_CONTROL: Final = True
DEFAULT_COMMAND_MIN_INTERVAL: Final = 0.5
DEFAULT_COMMAND_CONFIRMATION_TIMEOUT: Final = 8.0
DEFAULT_CONTROLLER_SILENCE_TIMEOUT: Final = 300.0
DEFAULT_THERMOSTAT_OFFLINE_AFTER: Final = 900.0

THERMOSTAT_MIN_TEMPERATURE: Final = 16
THERMOSTAT_MAX_TEMPERATURE: Final = 28

MODE_VALUES: Final = {"cool": 1, "heat": 2, "ventilation": 3, "dehumidify": 4}
MODE_LABELS: Final = {
    "cool": "制冷",
    "heat": "制热",
    "ventilation": "通风",
    "dehumidify": "除湿",
}
MODE_BY_LABEL: Final = {label: mode for mode, label in MODE_LABELS.items()}
SCENE_VALUES: Final = {"away": 0, "home": 1}
SCENE_LABELS: Final = {"away": "离家", "home": "居家"}
SCENE_BY_LABEL: Final = {label: scene for scene, label in SCENE_LABELS.items()}

PLATFORMS: Final = ["binary_sensor", "climate", "select", "sensor", "switch"]
