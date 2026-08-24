"""Actionable connection Repair lifecycle tests."""

from __future__ import annotations

import pytest
from homeassistant.helpers import issue_registry as ir

from custom_components.linking_the_world_temp_ha.const import DOMAIN
from custom_components.linking_the_world_temp_ha.repairs import (
    RepairManager,
    async_create_fix_flow,
)
from custom_components.linking_the_world_temp_ha.protocol import (
    AuthenticationRejected,
    TcpConnectError,
)
from custom_components.linking_the_world_temp_ha.hub import LinkingTempHub
from custom_components.linking_the_world_temp_ha.health import HealthTracker


def _issue(hass, issue_id: str):
    return ir.async_get(hass).async_get_issue(DOMAIN, issue_id)


async def test_login_timeout_repair_starts_after_three_consecutive_failures(
    hass, mock_config_entry
) -> None:
    """Ambiguous login timeouts become actionable only after three attempts."""
    manager = RepairManager(hass, mock_config_entry)
    issue_id = f"login_timeout_{mock_config_entry.entry_id}"

    await manager.async_set_login_timeout(True)
    await manager.async_set_login_timeout(True)
    assert _issue(hass, issue_id) is None

    await manager.async_set_login_timeout(True)
    issue = _issue(hass, issue_id)
    assert issue is not None
    assert issue.is_fixable
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.data == {"entry_id": mock_config_entry.entry_id}

    await manager.async_set_login_timeout(True)
    assert len(
        [item for item in ir.async_get(hass).issues if item[1] == issue_id]
    ) == 1


async def test_successful_login_clears_login_and_protocol_repairs(
    hass, mock_config_entry
) -> None:
    """A verified authenticated session resolves the two connection Repairs."""
    manager = RepairManager(hass, mock_config_entry)
    login_issue_id = f"login_timeout_{mock_config_entry.entry_id}"
    protocol_issue_id = f"protocol_incompatible_{mock_config_entry.entry_id}"

    for _ in range(3):
        await manager.async_set_login_timeout(True)
    await manager.async_set_protocol_incompatible(True)
    assert _issue(hass, login_issue_id) is not None
    assert _issue(hass, protocol_issue_id) is not None

    await manager.async_set_login_timeout(False)
    await manager.async_set_protocol_incompatible(False)
    assert _issue(hass, login_issue_id) is None
    assert _issue(hass, protocol_issue_id) is None
    assert manager.consecutive_login_timeouts == 0


async def test_tcp_failure_does_not_create_connection_repairs(
    hass, mock_config_entry
) -> None:
    """Network failures remain reconnect/diagnostic events, not credential Repairs."""
    manager = RepairManager(hass, mock_config_entry)

    assert _issue(hass, f"login_timeout_{mock_config_entry.entry_id}") is None
    assert _issue(hass, f"protocol_incompatible_{mock_config_entry.entry_id}") is None


async def test_login_timeout_repair_opens_entry_linked_reauth(
    hass, mock_config_entry, monkeypatch
) -> None:
    """The fix button directs users to reauthenticate this exact config entry."""
    manager = RepairManager(hass, mock_config_entry)
    for _ in range(3):
        await manager.async_set_login_timeout(True)
    reauth_calls: list[object] = []

    def start_reauth(hass_arg) -> None:
        reauth_calls.append(hass_arg)

    mock_config_entry.add_to_hass(hass)
    monkeypatch.setattr(mock_config_entry, "async_start_reauth", start_reauth)
    flow = await async_create_fix_flow(
        hass,
        f"login_timeout_{mock_config_entry.entry_id}",
        {"entry_id": mock_config_entry.entry_id},
    )
    flow.hass = hass
    flow.handler = DOMAIN

    result = await flow.async_step_init({})
    assert result["type"] == "create_entry"
    assert reauth_calls == [hass]


async def test_runtime_authentication_rejection_starts_reauth_and_stops_retrying(
    hass, mock_config_entry, monkeypatch
) -> None:
    """Known-invalid credentials pause reconnects until the entry is reloaded."""
    import custom_components.linking_the_world_temp_ha.hub as hub_module

    calls = 0
    reauth_calls: list[object] = []

    class RejectingClient:
        def __init__(self, *_args) -> None:
            nonlocal calls
            calls += 1
            self.reader_error = None
            self.on_frame = None
            self.on_status = None
            self.on_stage = None
            self.on_parser_event = None

        async def connect(self) -> None:
            raise AuthenticationRejected("MC7021 rejected the supplied credentials")

        async def close(self) -> None:
            return None

    def start_reauth(hass_arg) -> None:
        reauth_calls.append(hass_arg)

    monkeypatch.setattr(hub_module, "AsyncMoorgenClient", RejectingClient)
    monkeypatch.setattr(mock_config_entry, "async_start_reauth", start_reauth)
    hub = LinkingTempHub(hass, mock_config_entry, HealthTracker())
    await hub._async_run()

    assert calls == 1
    assert reauth_calls == [hass]
    assert hub._client is None
    assert hub._reauth_required
    assert not hub.connected


async def test_runtime_tcp_failure_does_not_create_login_or_protocol_repairs(
    hass, mock_config_entry
) -> None:
    """A network failure must not be misrepresented as credentials or protocol work."""
    hub = LinkingTempHub(hass, mock_config_entry, HealthTracker())
    hub._record_connection_failure(TcpConnectError("connection refused"))
    await hass.async_block_till_done()

    assert _issue(hass, f"login_timeout_{mock_config_entry.entry_id}") is None
    assert _issue(hass, f"protocol_incompatible_{mock_config_entry.entry_id}") is None
