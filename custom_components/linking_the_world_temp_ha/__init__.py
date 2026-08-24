"""Linking The World Temp HA native integration."""

from __future__ import annotations

from homeassistant.core import HomeAssistant

from .const import PLATFORMS
from .health import HealthTracker
from .hub import LinkingTempHub
from .runtime import LinkingTempConfigEntry, LinkingTempRuntime


async def async_setup_entry(
    hass: HomeAssistant, entry: LinkingTempConfigEntry
) -> bool:
    """Set up one MC7021 controller."""
    health = HealthTracker()
    hub = LinkingTempHub(hass, entry, health)
    runtime = LinkingTempRuntime(hub=hub, health=health)
    await hub.async_start()
    entry.runtime_data = runtime
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: LinkingTempConfigEntry
) -> bool:
    """Unload the controller and all native entities."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    await entry.runtime_data.hub.async_stop()
    return True


async def _async_reload_entry(
    hass: HomeAssistant, entry: LinkingTempConfigEntry
) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
