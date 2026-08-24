"""Persistent room-panel lifecycle tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.linking_the_world_temp_ha.panel_registry import PanelRegistry


def utc(value: str) -> datetime:
    """Build a timezone-aware test timestamp."""
    return datetime.fromisoformat(value).replace(tzinfo=UTC)


async def test_load_migrates_v1_panels_without_resetting_identity(hass, hass_storage):
    """A v1 room/panel list becomes v2 records without changing identifiers."""
    entry_id = "panel-migration"
    key = f"linking_the_world_temp_ha.{entry_id}.panels"
    hass_storage[key] = {
        "version": 1,
        "minor_version": 1,
        "key": key,
        "data": {
            "rooms": {"r0100": "主卧"},
            "panels": [
                {"mac": "ff00ffffffff01ff", "room_id": "r0100"},
                {"mac": "not-a-mac", "room_id": "broken"},
            ],
        },
    }

    registry = PanelRegistry(hass, entry_id, clock=lambda: utc("2026-08-24T00:00:00"))
    await registry.async_load()

    record = registry.records["ff00ffffffff01ff"]
    assert registry.room_names == {"r0100": "主卧"}
    assert record.mac_hex == "ff00ffffffff01ff"
    assert record.room_id == "r0100"
    assert record.first_seen_utc.tzinfo is UTC
    assert record.last_report_utc is None
    assert record.monitored_absence_seconds == 0
    assert record.checkpoint_utc is None
    assert not record.available
    assert "not-a-mac" not in registry.records


async def test_observed_absence_only_advances_during_active_valid_status_stream(hass):
    """Downtime, silent traffic, and reconnect gaps never count toward 30 days."""
    now = utc("2026-08-24T00:00:00")
    registry = PanelRegistry(hass, "observed-absence", clock=lambda: now)
    await registry.async_load()
    await registry.async_note_panel_report("ff00ffffffff01ff", "r0100", now)

    # The first verified status opens a monitored interval; it has no past time.
    await registry.async_note_status_stream(now)
    now += timedelta(days=5)
    await registry.async_note_status_stream(now)
    assert registry.records["ff00ffffffff01ff"].monitored_absence_seconds == 5 * 86400

    # Controller/HA downtime and malformed or stalled status traffic pause time.
    now += timedelta(days=7)
    await registry.async_pause_monitoring(now)
    now += timedelta(days=10)
    await registry.async_note_status_stream(now)
    assert registry.records["ff00ffffffff01ff"].monitored_absence_seconds == 5 * 86400

    now += timedelta(days=3)
    await registry.async_note_status_stream(now)
    assert registry.records["ff00ffffffff01ff"].monitored_absence_seconds == 8 * 86400

    # A new valid panel report is the only event that clears its accumulation.
    now += timedelta(hours=1)
    is_new = await registry.async_note_panel_report(
        "ff00ffffffff01ff", "r0100", now
    )
    record = registry.records["ff00ffffffff01ff"]
    assert not is_new
    assert record.monitored_absence_seconds == 0
    assert record.available
    assert record.last_report_utc == now


async def test_pause_preserves_accumulation_and_report_restores_panel(hass):
    """Reconnect marks panels unavailable but cannot erase a prior absence total."""
    now = utc("2026-08-24T00:00:00")
    registry = PanelRegistry(hass, "panel-reconnect", clock=lambda: now)
    await registry.async_load()
    await registry.async_note_panel_report("ff00ffffffff01ff", "r0100", now)
    await registry.async_note_status_stream(now)
    now += timedelta(hours=6)
    await registry.async_note_status_stream(now)

    before_pause = registry.records["ff00ffffffff01ff"].monitored_absence_seconds
    last_report = registry.records["ff00ffffffff01ff"].last_report_utc
    await registry.async_pause_monitoring(now)
    assert not registry.records["ff00ffffffff01ff"].available
    assert registry.records["ff00ffffffff01ff"].last_report_utc == last_report
    assert registry.records["ff00ffffffff01ff"].monitored_absence_seconds == before_pause

    now += timedelta(days=20)
    await registry.async_note_status_stream(now)
    assert registry.records["ff00ffffffff01ff"].monitored_absence_seconds == before_pause


async def test_load_skips_bad_v2_records_without_losing_valid_panel(hass, hass_storage):
    """A damaged record is isolated instead of invalidating the whole registry."""
    entry_id = "v2-records"
    key = f"linking_the_world_temp_ha.{entry_id}.panels"
    hass_storage[key] = {
        "version": 2,
        "minor_version": 1,
        "key": key,
        "data": {
            "rooms": {"r0100": "主卧"},
            "panels": [
                {
                    "mac_hex": "ff00ffffffff01ff",
                    "room_id": "r0100",
                    "first_seen_utc": "2026-08-01T00:00:00+00:00",
                    "last_report_utc": "2026-08-02T00:00:00+00:00",
                    "available": True,
                    "monitored_absence_seconds": 3600,
                    "checkpoint_utc": "2026-08-02T01:00:00+00:00",
                },
                {"mac_hex": "ff00ffffffff02ff", "first_seen_utc": "not-a-date"},
                {
                    "mac_hex": "ff00ffffffff03ff",
                    "room_id": "r0100",
                    "first_seen_utc": "2026-08-01T00:00:00+00:00",
                    "last_report_utc": None,
                    "available": False,
                    "monitored_absence_seconds": True,
                    "checkpoint_utc": None,
                },
                {
                    "mac_hex": "ff00ffffffff04ff",
                    "room_id": "r0100",
                    "first_seen_utc": "2026-08-01T00:00:00+00:00",
                    "last_report_utc": None,
                    "available": False,
                    "monitored_absence_seconds": float("nan"),
                    "checkpoint_utc": None,
                },
            ],
        },
    }

    registry = PanelRegistry(hass, entry_id, clock=lambda: utc("2026-08-24T00:00:00"))
    await registry.async_load()

    assert set(registry.records) == {"ff00ffffffff01ff"}
    assert registry.records["ff00ffffffff01ff"].monitored_absence_seconds == 3600


async def test_flush_writes_v2_utc_records_for_restart(hass, hass_storage):
    """A clean unload can persist a debounced update before HA exits."""
    entry_id = "flush-v2"
    key = f"linking_the_world_temp_ha.{entry_id}.panels"
    now = utc("2026-08-24T00:00:00")
    registry = PanelRegistry(hass, entry_id, clock=lambda: now)
    await registry.async_load()
    await registry.async_note_panel_report("ff00ffffffff01ff", "r0100", now)
    await registry.async_set_room_name("r0100", "客餐厅")
    await registry.async_flush()

    stored = hass_storage[key]
    assert stored["version"] == 2
    assert stored["data"]["panels"][0]["mac_hex"] == "ff00ffffffff01ff"
    assert stored["data"]["panels"][0]["first_seen_utc"].endswith("+00:00")

    restarted = PanelRegistry(hass, entry_id, clock=lambda: now)
    await restarted.async_load()
    assert restarted.room_names == {"r0100": "客餐厅"}
    assert set(restarted.records) == {"ff00ffffffff01ff"}
