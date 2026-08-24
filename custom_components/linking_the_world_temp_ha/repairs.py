"""Actionable Home Assistant Repairs for controller connection problems."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components.repairs import RepairsFlow
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN
from .panel_registry import PanelRecord


def async_delete_entry_issues(hass: HomeAssistant, entry_id: str) -> None:
    """Remove every entry-scoped Repair during config-entry removal."""
    for issue_type in ("login_timeout", "protocol_incompatible"):
        ir.async_delete_issue(hass, DOMAIN, f"{issue_type}_{entry_id}")
    stale_prefix = f"stale_panel_{entry_id}_"
    for domain, issue_id in tuple(ir.async_get(hass).issues):
        if domain == DOMAIN and issue_id.startswith(stale_prefix):
            ir.async_delete_issue(hass, DOMAIN, issue_id)


def _short_mac(mac_hex: str) -> str:
    """Display a panel identity without exposing its full protocol address."""
    return f"{mac_hex[:2]}...{mac_hex[-2:]}"


class RepairManager:
    """Deduplicate and clear connection Repairs for one config entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.consecutive_login_timeouts = 0

    @property
    def _login_timeout_issue_id(self) -> str:
        return f"login_timeout_{self.entry.entry_id}"

    @property
    def _protocol_issue_id(self) -> str:
        return f"protocol_incompatible_{self.entry.entry_id}"

    def _stale_panel_issue_id(self, mac_hex: str) -> str:
        return f"stale_panel_{self.entry.entry_id}_{mac_hex}"

    async def async_set_login_timeout(self, active: bool) -> None:
        """Track ambiguous login timeouts and surface the third occurrence."""
        if not active:
            self.consecutive_login_timeouts = 0
            ir.async_delete_issue(self.hass, DOMAIN, self._login_timeout_issue_id)
            return

        self.consecutive_login_timeouts += 1
        if self.consecutive_login_timeouts < 3:
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self._login_timeout_issue_id,
            data={"entry_id": self.entry.entry_id},
            is_fixable=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key="login_timeout",
        )

    async def async_set_protocol_incompatible(self, active: bool) -> None:
        """Expose unsupported protocol replies as one entry-linked Repair."""
        if not active:
            ir.async_delete_issue(self.hass, DOMAIN, self._protocol_issue_id)
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self._protocol_issue_id,
            data={"entry_id": self.entry.entry_id},
            is_fixable=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key="protocol_incompatible",
        )

    async def async_set_stale_panel(
        self, record: PanelRecord, room_name: str | None
    ) -> None:
        """Create one fixable stale-panel issue without exposing its full MAC."""
        last_report = (
            record.last_report_utc.isoformat()
            if record.last_report_utc is not None
            else "unknown"
        )
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self._stale_panel_issue_id(record.mac_hex),
            data={"entry_id": self.entry.entry_id, "mac_hex": record.mac_hex},
            is_fixable=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key="stale_panel",
            translation_placeholders={
                "room": room_name or record.room_id or "Unknown room",
                "short_mac": _short_mac(record.mac_hex),
                "last_report": last_report,
            },
        )

    async def async_clear_stale_panel(self, mac_hex: str) -> None:
        """Resolve the stale issue only when the named panel reports again."""
        ir.async_delete_issue(self.hass, DOMAIN, self._stale_panel_issue_id(mac_hex))


class ConnectionRepairFlow(RepairsFlow):
    """Let a user start the appropriate entry-linked recovery action."""

    def __init__(self, entry_id: str, issue_id: str) -> None:
        self.entry_id = entry_id
        self._issue_id = issue_id

    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> dict[str, Any]:
        """Show a small, explicit confirmation screen."""
        if user_input is not None:
            entry = self.hass.config_entries.async_get_entry(self.entry_id)
            if entry is None:
                return self.async_abort(reason="entry_not_found")
            if self._issue_id.startswith("login_timeout_"):
                entry.async_start_reauth(self.hass)
            else:
                self.hass.config_entries.async_schedule_reload(self.entry_id)
            return self.async_create_entry(data={})
        return self.async_show_form(step_id="init", data_schema=vol.Schema({}))


class StalePanelRepairFlow(RepairsFlow):
    """Require explicit confirmation before deleting a persisted panel."""

    def __init__(self, entry_id: str, mac_hex: str, issue_id: str) -> None:
        self.entry_id = entry_id
        self.mac_hex = mac_hex
        self._issue_id = issue_id

    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> dict[str, Any]:
        """Route stale-panel Repairs straight to their confirmation step."""
        return await self.async_step_confirm(user_input)

    async def async_step_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> dict[str, Any]:
        """Delete only after the user submits the explicit empty confirmation."""
        if user_input is None:
            return self.async_show_form(step_id="confirm", data_schema=vol.Schema({}))
        entry = self.hass.config_entries.async_get_entry(self.entry_id)
        if entry is None:
            return self.async_abort(reason="entry_not_found")
        runtime = entry.runtime_data
        if runtime is None:
            return self.async_abort(reason="entry_not_loaded")
        await _async_remove_stale_panel(self.hass, entry, self.mac_hex)
        ir.async_delete_issue(self.hass, DOMAIN, self._issue_id)
        return self.async_create_entry(data={})


async def _async_remove_stale_panel(
    hass: HomeAssistant, entry: ConfigEntry, mac_hex: str
) -> None:
    """Remove owned registry records before deleting the source panel record.

    The persistent record is intentionally last: registry failures leave enough
    source state for the user to retry the same Repair safely.
    """
    unique_id_prefix = f"{entry.entry_id}_thermostat_{mac_hex}_"
    panel_identifier = (DOMAIN, f"{entry.entry_id}_{mac_hex}")
    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    owned_entities = [
        entity
        for entity in er.async_entries_for_config_entry(entity_registry, entry.entry_id)
        if entity.unique_id.startswith(unique_id_prefix)
    ]
    for entity in owned_entities:
        entity_registry.async_remove(entity.entity_id)

    device = device_registry.async_get_device(identifiers={panel_identifier})
    if device is not None:
        remaining_entities = er.async_entries_for_device(
            entity_registry, device.id, include_disabled_entities=True
        )
        unrelated_entries = set(device.config_entries) - {entry.entry_id}
        if not remaining_entities and not unrelated_entries:
            device_registry.async_remove_device(device.id)

    await entry.runtime_data.hub.async_forget_panel(mac_hex)
    if entry.state is ConfigEntryState.LOADED:
        # Dynamic entity platforms keep their in-memory entity set. Reloading
        # disposes that set so a future report can recreate the same identities.
        hass.config_entries.async_schedule_reload(entry.entry_id)


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Create the entry-linked fix flow for a known connection issue."""
    if data is None or not isinstance(data.get("entry_id"), str):
        raise ValueError("Connection repair is missing its config entry")
    if issue_id.startswith("stale_panel_"):
        mac_hex = data.get("mac_hex")
        if not isinstance(mac_hex, str):
            raise ValueError("Stale-panel repair is missing its panel identity")
        return StalePanelRepairFlow(data["entry_id"], mac_hex, issue_id)
    if not issue_id.startswith(("login_timeout_", "protocol_incompatible_")):
        raise ValueError(f"Unsupported connection repair: {issue_id}")
    return ConnectionRepairFlow(data["entry_id"], issue_id)
