"""Redacted diagnostics support."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from .const import CONF_CLIENT_ID, CONF_TECH_SYSTEM_MAC
from .hub import LinkingTempHub

TO_REDACT = {
    CONF_HOST,
    CONF_PASSWORD,
    CONF_USERNAME,
    CONF_CLIENT_ID,
    CONF_TECH_SYSTEM_MAC,
}


def _anonymous_thermostats(hub: LinkingTempHub) -> dict[str, dict[str, Any]]:
    """Keep useful panel relationships while excluding household identities."""
    room_labels = {
        room_id: f"room_{index:02d}"
        for index, room_id in enumerate(
            sorted({state.room_id for state in hub.thermostats.values()}), start=1
        )
    }
    return {
        f"panel_{index:02d}": {
            "room": room_labels.get(state.room_id, "room_unknown"),
            "available": state.available,
            "power": state.power,
            "target_temperature": state.target_temperature,
            "current_temperature": state.current_temperature,
            "humidity": state.humidity,
        }
        for index, (_, state) in enumerate(sorted(hub.thermostats.items()), start=1)
    }


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
            "thermostats": _anonymous_thermostats(hub),
            "health": runtime.health.snapshot(),
        },
    }
