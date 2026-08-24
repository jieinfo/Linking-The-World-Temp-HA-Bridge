"""Binary sensors for Linking The World Temp HA."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import LinkingTempEntity
from .hub import LinkingTempHub


class ControllerConnectionSensor(LinkingTempEntity, BinarySensorEntity):
    _attr_translation_key = "controller_connection"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hub: LinkingTempHub) -> None:
        super().__init__(hub, "controller_connection")

    @property
    def is_on(self) -> bool:
        return self.hub.available

    @property
    def available(self) -> bool:
        return True


class ProtocolVerifiedSensor(LinkingTempEntity, BinarySensorEntity):
    _attr_translation_key = "protocol_verified"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hub: LinkingTempHub) -> None:
        super().__init__(hub, "protocol_verified")

    @property
    def is_on(self) -> bool:
        return self.hub.protocol_verified

    @property
    def available(self) -> bool:
        return True


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    hub: LinkingTempHub = entry.runtime_data.hub
    async_add_entities([ControllerConnectionSensor(hub), ProtocolVerifiedSensor(hub)])
