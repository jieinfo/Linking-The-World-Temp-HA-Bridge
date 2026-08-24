"""Entity restoration and lifecycle tests."""

from __future__ import annotations

from datetime import UTC, datetime
import time

from custom_components.linking_the_world_temp_ha.health import HealthTracker
from custom_components.linking_the_world_temp_ha.hub import LinkingTempHub
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
    hass, mock_config_entry
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

    hub._refresh_thermostat_availability(time.monotonic())

    assert not hub.thermostats[mac_hex].available
    assert record.available
    assert record.monitored_absence_seconds == absence_before
    assert record.last_report_utc == now
