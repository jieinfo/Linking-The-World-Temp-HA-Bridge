"""Mode and scene selects for Linking The World Temp HA."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import MODE_BY_LABEL, MODE_LABELS, SCENE_BY_LABEL, SCENE_LABELS
from .entity import LinkingTempEntity
from .hub import LinkingTempHub


class SystemModeSelect(LinkingTempEntity, SelectEntity):
    _attr_translation_key = "system_mode"

    def __init__(self, hub: LinkingTempHub) -> None:
        super().__init__(hub, "system_mode")

    @property
    def options(self) -> list[str]:
        """Keep the entity online while hiding actions the controller rejects."""
        if not self.hub.can_change_system_mode and (current := self.current_option):
            return [current]
        return list(MODE_BY_LABEL)

    @property
    def current_option(self) -> str | None:
        return MODE_LABELS.get(self.hub.state.mode)

    @property
    def extra_state_attributes(self) -> dict[str, str | bool]:
        return {
            "can_change_mode": self.hub.can_change_system_mode,
            "提示": "请先关闭科技系统再切换模式"
            if not self.hub.can_change_system_mode
            else "可以切换模式",
        }

    async def async_select_option(self, option: str) -> None:
        await self.hub.async_set_mode(MODE_BY_LABEL[option])


class SystemSceneSelect(LinkingTempEntity, SelectEntity):
    _attr_translation_key = "system_scene"

    def __init__(self, hub: LinkingTempHub) -> None:
        super().__init__(hub, "system_scene")
        self._attr_options = list(SCENE_BY_LABEL)

    @property
    def current_option(self) -> str | None:
        return SCENE_LABELS.get(self.hub.state.scene)

    async def async_select_option(self, option: str) -> None:
        await self.hub.async_set_scene(SCENE_BY_LABEL[option])


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    hub: LinkingTempHub = entry.runtime_data.hub
    async_add_entities([SystemModeSelect(hub), SystemSceneSelect(hub)])
