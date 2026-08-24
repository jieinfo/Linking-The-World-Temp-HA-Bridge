"""Actionable Home Assistant Repairs for controller connection problems."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components.repairs import RepairsFlow
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN


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


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Create the entry-linked fix flow for a known connection issue."""
    if data is None or not isinstance(data.get("entry_id"), str):
        raise ValueError("Connection repair is missing its config entry")
    if not issue_id.startswith(("login_timeout_", "protocol_incompatible_")):
        raise ValueError(f"Unsupported connection repair: {issue_id}")
    return ConnectionRepairFlow(data["entry_id"], issue_id)
