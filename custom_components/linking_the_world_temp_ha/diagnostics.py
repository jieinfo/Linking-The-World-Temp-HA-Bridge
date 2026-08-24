"""Redacted diagnostics support."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD
from homeassistant.core import HomeAssistant

from .hub import LinkingTempHub

TO_REDACT = {CONF_PASSWORD}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    runtime = entry.runtime_data
    hub: LinkingTempHub = runtime.hub
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "options": dict(entry.options),
        "runtime": {
            "connected": hub.connected,
            "protocol_verified": hub.protocol_verified,
            "protocol_status": hub.protocol_status,
            "last_connection_error": hub.last_connection_error,
            "last_command_status": hub.last_command_status,
            "control_permission": hub.control_permission,
            "system_state": vars(hub.state),
            "thermostat_count": len(hub.thermostats),
            "thermostats": {
                mac: {
                    "room_id": state.room_id,
                    "available": state.available,
                    "power": state.power,
                    "target_temperature": state.target_temperature,
                    "current_temperature": state.current_temperature,
                    "humidity": state.humidity,
                }
                for mac, state in hub.thermostats.items()
            },
            "health": runtime.health.snapshot(),
        },
    }
