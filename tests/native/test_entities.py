"""Entity restoration and lifecycle tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import time

import custom_components.linking_the_world_temp_ha.hub as hub_module
from custom_components.linking_the_world_temp_ha.health import HealthTracker
from custom_components.linking_the_world_temp_ha.hub import LinkingTempHub
from custom_components.linking_the_world_temp_ha.panel_registry import PanelRegistry
from custom_components.linking_the_world_temp_ha.protocol import tlv
from custom_components.linking_the_world_temp_ha.protocol import ThermostatState
from custom_components.linking_the_world_temp_ha.runtime import ConnectionStage


async def test_malformed_status_pauses_registry_monitoring_without_erasing_panel(
    hass, mock_config_entry
):
    """A malformed status body cannot turn wall-clock downtime into absence time."""
    hub = LinkingTempHub(hass, mock_config_entry, HealthTracker())
    now = datetime(2026, 8, 24, tzinfo=UTC)
    await hub.panel_registry.async_load()
    await hub.panel_registry.async_note_panel_report(
        "ff00ffffffff01ff", "r0100", now
    )
    await hub.panel_registry.async_note_status_stream(now)
    hub.health.mark_stage(ConnectionStage.READY)
    hub.connected = True

    await hub._async_status_received(b"malformed-status")

    record = hub.panel_registry.records["ff00ffffffff01ff"]
    assert record.last_report_utc == now
    assert record.monitored_absence_seconds == 0
    assert record.checkpoint_utc is None
    assert not record.available


async def test_malformed_transport_frame_pauses_registry_monitoring(
    hass, mock_config_entry
):
    """Decoder recovery events also stop observed-absence accounting."""
    hub = LinkingTempHub(hass, mock_config_entry, HealthTracker())
    now = datetime(2026, 8, 24, tzinfo=UTC)
    await hub.panel_registry.async_load()
    await hub.panel_registry.async_note_panel_report(
        "ff00ffffffff01ff", "r0100", now
    )
    await hub.panel_registry.async_note_status_stream(now)

    await hub._async_parser_event("frames_malformed", 1)

    record = hub.panel_registry.records["ff00ffffffff01ff"]
    assert record.checkpoint_utc is None
    assert not record.available


async def test_truncated_tlv_status_cannot_partially_update_controller_state(
    hass, mock_config_entry
):
    """A valid TLV prefix followed by a truncated tail is one invalid report."""
    hub = LinkingTempHub(hass, mock_config_entry, HealthTracker())
    body = tlv(0x0004, hub.tech_system_mac) + tlv(0x000B, b"\x01")
    body += b"\x0a\x00\x05\x00\x01"

    await hub._async_status_received(body)

    assert hub.state.power is None
    assert not hub.protocol_verified
    assert hub.health.snapshot()["counters"]["ignored_statuses"] == 1


async def test_status_gap_pauses_absence_even_when_other_tcp_traffic_continues(
    hass, mock_config_entry, monkeypatch
):
    """Only valid status reports may keep observed-absence accounting alive."""
    monotonic = [0.0]
    now = [datetime(2026, 8, 24, tzinfo=UTC)]
    monkeypatch.setattr(hub_module.time, "monotonic", lambda: monotonic[0])
    hub = LinkingTempHub(hass, mock_config_entry, HealthTracker())
    hub.controller_silence_timeout = 10
    hub.panel_registry = PanelRegistry(
        hass,
        mock_config_entry.entry_id,
        clock=lambda: now[0],
        monotonic_clock=lambda: monotonic[0],
        status_gap_timeout=10,
    )
    await hub.panel_registry.async_load()
    await hub.panel_registry.async_note_panel_report(
        "ff00ffffffff01ff", "r0100", now[0]
    )
    hub.health.mark_stage(ConnectionStage.READY)
    hub.connected = True
    body = tlv(0x0004, hub.tech_system_mac) + tlv(0x000B, b"\x01")

    await hub._async_status_received(body)
    now[0] += timedelta(days=31)
    monotonic[0] += 31 * 86400
    await hub._async_status_received(body)

    record = hub.panel_registry.records["ff00ffffffff01ff"]
    assert record.monitored_absence_seconds == 0
    assert not record.available


async def test_ready_stage_without_tcp_session_does_not_start_absence_monitoring(
    hass, mock_config_entry
):
    """Observed absence needs both READY and a real controller session."""
    hub = LinkingTempHub(hass, mock_config_entry, HealthTracker())
    now = datetime(2026, 8, 24, tzinfo=UTC)
    await hub.panel_registry.async_load()
    await hub.panel_registry.async_note_panel_report(
        "ff00ffffffff01ff", "r0100", now
    )
    hub.health.mark_stage(ConnectionStage.READY)
    hub.connected = False
    body = tlv(0x0004, hub.tech_system_mac) + tlv(0x000B, b"\x01")

    await hub._async_status_received(body)

    assert hub.panel_registry.records["ff00ffffffff01ff"].checkpoint_utc is None


async def test_short_term_offline_timeout_is_independent_from_absence_counter(
    hass, mock_config_entry, hass_storage
):
    """Entity availability may expire without resetting persistent absence facts."""
    hub = LinkingTempHub(hass, mock_config_entry, HealthTracker())
    hub.thermostat_offline_after = 1
    now = datetime(2026, 8, 24, tzinfo=UTC)
    mac_hex = "ff00ffffffff01ff"
    await hub.panel_registry.async_load()
    await hub.panel_registry.async_note_panel_report(mac_hex, "r0100", now)
    await hub.panel_registry.async_note_status_stream(now)
    await hub.panel_registry.async_note_status_stream(now.replace(hour=1))
    record = hub.panel_registry.records[mac_hex]
    absence_before = record.monitored_absence_seconds
    hub.thermostats[mac_hex] = ThermostatState(
        mac=bytes.fromhex(mac_hex),
        room_id="r0100",
        available=True,
        last_seen=time.monotonic() - 2,
    )

    await hub._async_refresh_thermostat_availability(time.monotonic())

    assert not hub.thermostats[mac_hex].available
    assert not record.available
    assert record.monitored_absence_seconds == absence_before
    assert record.last_report_utc == now
    await hub.panel_registry.async_flush()
    stored = hass_storage[
        f"linking_the_world_temp_ha.{mock_config_entry.entry_id}.panels"
    ]
    assert not stored["data"]["panels"][0]["available"]
