"""Switches for Linking The World Temp HA."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import LinkingTempEntity
from .hub import LinkingTempHub


class SystemPowerSwitch(LinkingTempEntity, SwitchEntity):
    _attr_translation_key = "system_power"

    def __init__(self, hub: LinkingTempHub) -> None:
        super().__init__(hub, "system_power")

    @property
    def is_on(self) -> bool | None:
        if self.hub.state.power is None:
            return None
        return self.hub.state.power == "ON"

    async def async_turn_on(self, **kwargs) -> None:
        await self.hub.async_set_system_power(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self.hub.async_set_system_power(False)


class WinterHumidifierSwitch(LinkingTempEntity, SwitchEntity):
    _attr_translation_key = "winter_humidifier"

    def __init__(self, hub: LinkingTempHub) -> None:
        super().__init__(hub, "winter_humidifier")

    @property
    def available(self) -> bool:
        return self.hub.available and self.hub.state.mode == "heat"

    @property
    def is_on(self) -> bool | None:
        if self.hub.state.winter_humidifier is None:
            return None
        return self.hub.state.winter_humidifier == "ON"

    async def async_turn_on(self, **kwargs) -> None:
        await self.hub.async_set_winter_humidifier(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self.hub.async_set_winter_humidifier(False)


class EnergyControlSwitch(LinkingTempEntity, SwitchEntity):
    """Opt-in writable control for the controller-reported energy state."""

    _attr_translation_key = "energy_control"
    _attr_icon = "mdi:leaf"

    def __init__(self, hub: LinkingTempHub) -> None:
        super().__init__(hub, "energy_control")

    @property
    def available(self) -> bool:
        return super().available and self.hub.state.energy_saving is not None

    @property
    def is_on(self) -> bool | None:
        if self.hub.state.energy_saving is None:
            return None
        return self.hub.state.energy_saving == "ON"

    async def async_turn_on(self, **kwargs) -> None:
        await self.hub.async_set_energy_saving(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self.hub.async_set_energy_saving(False)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    hub: LinkingTempHub = entry.runtime_data.hub
    entities = [SystemPowerSwitch(hub), WinterHumidifierSwitch(hub)]
    if hub.enable_experimental_energy_control:
        entities.append(EnergyControlSwitch(hub))
    else:
        registry = er.async_get(hass)
        entity_id = registry.async_get_entity_id(
            "switch", DOMAIN, f"{entry.entry_id}_energy_control"
        )
        if entity_id is not None:
            registry.async_remove(entity_id)
    async_add_entities(entities)
