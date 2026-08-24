"""Linking The World Temp HA native integration."""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant

from .const import PLATFORMS
from .health import HealthTracker
from .hub import LinkingTempHub
from .repairs import async_delete_connection_issues
from .runtime import LinkingTempConfigEntry, LinkingTempRuntime

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: LinkingTempConfigEntry
) -> bool:
    """Set up one MC7021 controller."""
    health = HealthTracker()
    hub = LinkingTempHub(hass, entry, health)
    runtime = LinkingTempRuntime(hub=hub, health=health)
    await hub.async_start()
    runtime_assigned = False
    try:
        entry.runtime_data = runtime
        runtime_assigned = True
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
        return True
    except BaseException:
        try:
            await hub.async_stop()
        except BaseException:  # pragma: no cover - preserve the setup failure.
            _LOGGER.exception("Could not clean up controller runtime after setup failure")
        if runtime_assigned:
            try:
                entry.runtime_data = None
            except BaseException:  # pragma: no cover - preserve the setup failure.
                _LOGGER.exception("Could not clear failed controller runtime data")
        raise


async def async_unload_entry(
    hass: HomeAssistant, entry: LinkingTempConfigEntry
) -> bool:
    """Unload the controller and all native entities."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    await entry.runtime_data.hub.async_stop()
    return True


async def async_remove_entry(
    hass: HomeAssistant, entry: LinkingTempConfigEntry
) -> None:
    """Clean up Repairs that otherwise outlive a removed config entry."""
    async_delete_connection_issues(hass, entry.entry_id)


async def _async_reload_entry(
    hass: HomeAssistant, entry: LinkingTempConfigEntry
) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
