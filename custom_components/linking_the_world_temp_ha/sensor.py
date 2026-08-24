"""Diagnostic and automation sensors."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .diagnostics import _command_state, _protocol_status
from .entity import LinkingTempEntity, LinkingThermostatEntity
from .hub import LinkingTempHub


@dataclass(frozen=True)
class DiagnosticDescription:
    key: str
    translation_key: str
    value_fn: Callable[[LinkingTempHub], Any]


DIAGNOSTICS = (
    DiagnosticDescription(
        "connection_stage", "connection_stage", lambda hub: hub.health.stage.value
    ),
    DiagnosticDescription(
        "connection_error",
        "connection_error",
        lambda hub: hub.health.failure_kind.value,
    ),
    DiagnosticDescription(
        "protocol_status", "protocol_status", lambda hub: _protocol_status(hub.protocol_status)
    ),
    DiagnosticDescription(
        "control_permission", "control_permission", lambda hub: hub.control_permission
    ),
    DiagnosticDescription(
        "last_command", "last_command", lambda hub: _command_state(hub.last_command_status)
    ),
    DiagnosticDescription(
        "panel_count", "panel_count", lambda hub: len(hub.thermostats)
    ),
)


class DiagnosticSensor(LinkingTempEntity, SensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hub: LinkingTempHub, description: DiagnosticDescription) -> None:
        super().__init__(hub, description.key)
        self.description = description
        self._attr_translation_key = description.translation_key

    @property
    def native_value(self) -> Any:
        return self.description.value_fn(self.hub)

    @property
    def available(self) -> bool:
        return True


class AutomationTemperatureSensor(LinkingThermostatEntity, SensorEntity):
    _attr_translation_key = "automation_temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, hub: LinkingTempHub, mac_hex: str) -> None:
        super().__init__(hub, mac_hex, "automation_temperature")

    @property
    def native_value(self) -> float | None:
        values = self.hub.filtered.get(self.mac_hex)
        return values.temperature if values else None


class AutomationHumiditySensor(LinkingThermostatEntity, SensorEntity):
    _attr_translation_key = "automation_humidity"
    _attr_device_class = SensorDeviceClass.HUMIDITY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, hub: LinkingTempHub, mac_hex: str) -> None:
        super().__init__(hub, mac_hex, "automation_humidity")

    @property
    def native_value(self) -> int | None:
        values = self.hub.filtered.get(self.mac_hex)
        return values.humidity if values else None


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    hub: LinkingTempHub = entry.runtime_data.hub
    async_add_entities(
        [DiagnosticSensor(hub, description) for description in DIAGNOSTICS]
    )
    added: set[str] = set()

    @callback
    def add_new_entities() -> None:
        new_macs = [mac for mac in hub.thermostats if mac not in added]
        if not new_macs:
            return
        added.update(new_macs)
        entities: list[SensorEntity] = []
        for mac in new_macs:
            entities.extend(
                [
                    AutomationTemperatureSensor(hub, mac),
                    AutomationHumiditySensor(hub, mac),
                ]
            )
        async_add_entities(entities)

    add_new_entities()
    entry.async_on_unload(hub.async_add_listener(add_new_entities))
