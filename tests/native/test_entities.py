"""Entity restoration, lifecycle, and real-HA entity tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import time

import pytest
from homeassistant.components import climate, select, switch
from homeassistant.const import ATTR_TEMPERATURE
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

import custom_components.linking_the_world_temp_ha.hub as hub_module
from custom_components.linking_the_world_temp_ha.health import HealthTracker
from custom_components.linking_the_world_temp_ha.hub import LinkingTempHub
from custom_components.linking_the_world_temp_ha.panel_registry import PanelRegistry
from custom_components.linking_the_world_temp_ha.protocol import (
    ThermostatState,
    YasHcpFrame,
    parse_tlvs,
    tlv,
)
from custom_components.linking_the_world_temp_ha.runtime import ConnectionStage
from custom_components.linking_the_world_temp_ha.switch import (
    SystemPowerSwitch,
    WinterHumidifierSwitch,
)


async def _wait_for(predicate, *, timeout: float = 1) -> None:
    """Wait for a push-driven Home Assistant state without fixed sleeps."""
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0)


def _system_status(hub: LinkingTempHub, *, power: bool, mode: int = 1, scene: int = 1, humidifier: bool = False) -> bytes:
    """Build one verified total-control report using the captured TLV shape."""
    return (
        tlv(0x0004, hub.tech_system_mac)
        + tlv(0x000B, bytes((power,)))
        + tlv(0x000A, bytes((mode, scene, humidifier)))
    )


def _thermostat_status(
    hub: LinkingTempHub,
    mac_hex: str,
    *,
    room_id: str = "r0100",
    target: int = 22,
    current: float = 24.6,
    humidity: int = 58,
    power: bool = True,
) -> bytes:
    """Build one verified room-panel status report."""
    current_tenths = round(current * 10)
    return (
        tlv(0x0004, bytes.fromhex(mac_hex))
        + tlv(0x0075, hub.tech_system_mac)
        + tlv(0x0030, room_id.encode())
        + tlv(
            0x000A,
            bytes((target * 2, current_tenths & 0xFF, current_tenths >> 8, humidity, 0)),
        )
        + tlv(0x000B, bytes((power,)))
    )


def _entity_id(hass, entry_id: str, platform: str, unique_key: str) -> str:
    """Resolve an entity by the integration's stable public unique ID."""
    entity_id = er.async_get(hass).async_get_entity_id(
        platform,
        "linking_the_world_temp_ha",
        f"{entry_id}_{unique_key}",
    )
    assert entity_id is not None
    return entity_id


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_system_switch_service_round_trips_through_controller_push_ack(
    hass, setup_integration, fake_controller
):
    """A switch service call stays pending only until a real push acknowledgement."""
    runtime = setup_integration
    hub = runtime.hub
    await fake_controller.async_send_status(_system_status(hub, power=False))
    await _wait_for(lambda: hub.available and hub.state.power == "OFF")

    async def acknowledge_command(frame) -> None:
        fields = parse_tlvs(frame.body)
        if fields.get(0x0004) != hub.tech_system_mac:
            return
        if fields.get(0x0009) == b"\x02":
            await fake_controller.async_send_status(_system_status(hub, power=True))

    # The fake controller deliberately exposes a command hook so real HA service
    # tests can use deterministic controller-originated status acknowledgements.
    fake_controller.on_command = acknowledge_command
    entity_id = _entity_id(hass, hub.entry.entry_id, "switch", "system_power")
    await hass.services.async_call(
        switch.DOMAIN, switch.SERVICE_TURN_ON, {"entity_id": entity_id}, blocking=True
    )

    await _wait_for(lambda: hub.state.power == "ON")
    assert hub.last_command_status.startswith("confirmed:")
    assert not hub._pending


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_system_mode_scene_and_humidifier_follow_controller_push_state(
    hass, setup_integration, fake_controller
):
    """System controls use HA services and reflect controller-originated updates."""
    hub = setup_integration.hub
    controller = {"power": False, "mode": 1, "scene": 1, "humidifier": False}

    def status() -> bytes:
        return _system_status(
            hub,
            power=controller["power"],
            mode=controller["mode"],
            scene=controller["scene"],
            humidifier=controller["humidifier"],
        )

    async def acknowledge_command(frame) -> None:
        fields = parse_tlvs(frame.body)
        if fields.get(0x0004) != hub.tech_system_mac:
            return
        command = fields.get(0x0009, b"\x00")[0]
        value = fields.get(0x000A, b"\x00")[0]
        if command == 2:
            controller["power"] = True
        elif command == 1:
            controller["power"] = False
        elif command == 3:
            controller["mode"] = value
        elif command == 4:
            controller["scene"] = value
        elif command == 5:
            controller["humidifier"] = bool(value)
        await fake_controller.async_send_status(status())

    fake_controller.on_command = acknowledge_command
    await fake_controller.async_send_status(status())
    await _wait_for(lambda: hub.available)

    mode_entity = _entity_id(hass, hub.entry.entry_id, "select", "system_mode")
    scene_entity = _entity_id(hass, hub.entry.entry_id, "select", "system_scene")
    humidifier_entity = _entity_id(
        hass, hub.entry.entry_id, "switch", "winter_humidifier"
    )
    assert hass.states.get(humidifier_entity).state == "unavailable"

    await hass.services.async_call(
        select.DOMAIN,
        select.SERVICE_SELECT_OPTION,
        {"entity_id": mode_entity, "option": "制热"},
        blocking=True,
    )
    await _wait_for(lambda: hub.state.mode == "heat")
    await hass.async_block_till_done()
    assert hass.states.get(mode_entity).state == "制热"
    assert hass.states.get(humidifier_entity).state == "off"

    await hass.services.async_call(
        select.DOMAIN,
        select.SERVICE_SELECT_OPTION,
        {"entity_id": scene_entity, "option": "离家"},
        blocking=True,
    )
    await _wait_for(lambda: hub.state.scene == "away")
    await hass.async_block_till_done()
    assert hass.states.get(scene_entity).state == "离家"

    await hass.services.async_call(
        switch.DOMAIN,
        switch.SERVICE_TURN_ON,
        {"entity_id": humidifier_entity},
        blocking=True,
    )
    await _wait_for(lambda: hub.state.winter_humidifier == "ON")
    await hass.async_block_till_done()
    assert hass.states.get(humidifier_entity).state == "on"

    await hass.services.async_call(
        switch.DOMAIN,
        switch.SERVICE_TURN_OFF,
        {"entity_id": humidifier_entity},
        blocking=True,
    )
    await _wait_for(lambda: hub.state.winter_humidifier == "OFF")
    system_entity = _entity_id(hass, hub.entry.entry_id, "switch", "system_power")
    await hass.services.async_call(
        switch.DOMAIN,
        switch.SERVICE_TURN_OFF,
        {"entity_id": system_entity},
        blocking=True,
    )
    await _wait_for(lambda: hub.state.power == "OFF")


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_dynamic_climate_and_filtered_sensors_follow_app_pushes(
    hass, setup_integration, fake_controller
):
    """A controller-discovered panel adds native entities and filters valid samples."""
    hub = setup_integration.hub
    mac_hex = "ff00ffffffff01ff"
    await fake_controller.async_send_status(_system_status(hub, power=True, mode=1))
    await fake_controller.async_send_status(
        _thermostat_status(hub, mac_hex, current=20.1, humidity=50)
    )
    await fake_controller.async_send_status(
        _thermostat_status(hub, mac_hex, current=25.2, humidity=60)
    )
    await fake_controller.async_send_status(
        _thermostat_status(hub, mac_hex, current=24.6, humidity=58)
    )
    await _wait_for(lambda: mac_hex in hub.thermostats and hub.available)
    await hass.async_block_till_done()

    climate_entity = _entity_id(
        hass, hub.entry.entry_id, "climate", f"thermostat_{mac_hex}_climate"
    )
    temperature_entity = _entity_id(
        hass,
        hub.entry.entry_id,
        "sensor",
        f"thermostat_{mac_hex}_automation_temperature",
    )
    humidity_entity = _entity_id(
        hass,
        hub.entry.entry_id,
        "sensor",
        f"thermostat_{mac_hex}_automation_humidity",
    )
    state = hass.states.get(climate_entity)
    assert state is not None
    assert state.attributes["min_temp"] == 16
    assert state.attributes["max_temp"] == 28
    assert state.attributes["target_temp_step"] == 1
    assert state.state == "cool"
    assert float(hass.states.get(temperature_entity).state) == 24.6
    assert int(hass.states.get(humidity_entity).state) == 58

    # This is an App-originated push: no HA service call is involved.
    await fake_controller.async_send_status(
        _thermostat_status(hub, mac_hex, target=23, current=26.4, humidity=62)
    )
    await _wait_for(lambda: hub.thermostats[mac_hex].target_temperature == 23)
    await hass.async_block_till_done()
    assert float(hass.states.get(climate_entity).attributes["temperature"]) == 23
    assert float(hass.states.get(climate_entity).attributes["current_temperature"]) == 26.4
    assert int(hass.states.get(climate_entity).attributes["current_humidity"]) == 62

    # The source system forces individual panels off in ventilation/dehumidify.
    await fake_controller.async_send_status(_system_status(hub, power=True, mode=3))
    await _wait_for(lambda: hub.state.mode == "ventilation")
    await hass.async_block_till_done()
    state = hass.states.get(climate_entity)
    assert state.state == "off"
    assert state.attributes["hvac_modes"] == ["off"]


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_fragmented_status_stress_and_rapid_setpoints_recover_to_latest_value(
    hass, setup_integration, fake_controller
):
    """A long fragmented stream and 50 updates never desynchronize the session."""
    hub = setup_integration.hub
    mac_hex = "ff00ffffffff01ff"
    await fake_controller.async_send_status(_system_status(hub, power=True, mode=1))
    await fake_controller.async_send_status(_thermostat_status(hub, mac_hex))
    await _wait_for(lambda: hub.available and mac_hex in hub.thermostats)
    await hass.async_block_till_done()
    climate_entity = _entity_id(
        hass, hub.entry.entry_id, "climate", f"thermostat_{mac_hex}_climate"
    )

    decoded_before = hub.health.snapshot()["counters"]["frames_decoded"]
    frames = [
        YasHcpFrame(
            5,
            0x0C,
            sequence,
            _thermostat_status(
                hub,
                mac_hex,
                target=20 + sequence % 8,
                current=20 + sequence / 10,
                humidity=40 + sequence % 40,
            ),
        )
        for sequence in range(100)
    ]
    await fake_controller.async_send_frames(*frames, fragment_size=7)
    await _wait_for(
        lambda: hub.health.snapshot()["counters"]["frames_decoded"]
        >= decoded_before + 100
    )
    await hass.async_block_till_done()
    thermostat = hub.thermostats[mac_hex]
    assert thermostat.current_temperature == 29.9
    assert thermostat.humidity == 59
    assert thermostat.current_temperature != 100
    assert thermostat.humidity != 100

    received_setpoints: list[int] = []

    async def acknowledge_latest_setpoint(frame) -> None:
        fields = parse_tlvs(frame.body)
        if fields.get(0x0004) != bytes.fromhex(mac_hex):
            return
        if fields.get(0x0009) != b"\x03":
            return
        received_setpoints.append(fields[0x000A][0] // 2)
        # Let the first request time out. The hub must then release the pending
        # slot and dispatch the newest coalesced temperature without blocking.
        if len(received_setpoints) == 2:
            await fake_controller.async_send_status(
                _thermostat_status(hub, mac_hex, target=received_setpoints[-1])
            )

    fake_controller.on_command = acknowledge_latest_setpoint
    for temperature in range(16, 29):
        for _ in range(4):
            await hass.services.async_call(
                climate.DOMAIN,
                climate.SERVICE_SET_TEMPERATURE,
                {"entity_id": climate_entity, ATTR_TEMPERATURE: temperature},
                blocking=True,
            )
    target = f"thermostat_{mac_hex}"
    assert hub._pending[target].expected == {"target_temperature": "16"}
    assert hub._queued[target].expected == {"target_temperature": "28"}

    hub._pending[target].deadline = 0
    await hub._async_expire_pending(time.monotonic())
    await hub._async_dispatch_queued()
    await _wait_for(
        lambda: not hub._pending
        and not hub._queued
        and hub.thermostats[mac_hex].target_temperature == 28
    )
    assert received_setpoints == [16, 28]
    # Repeating the in-flight value cancels a stale replacement rather than
    # counting as a new coalesced value; all later rapid values are retained.
    assert hub.health.snapshot()["counters"]["commands_coalesced"] >= 48
    assert hub.health.snapshot()["counters"]["commands_timed_out"] >= 1
    assert not hub._pending
    assert not hub._queued


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_thermostat_timeout_retries_once_and_accepts_the_late_ack(
    hass, setup_integration, fake_controller
):
    """A dropped first setpoint is retried once and cannot poison later commands."""
    hub = setup_integration.hub
    hub.command_min_interval = 0
    mac_hex = "ff00ffffffff02ff"
    await fake_controller.async_send_status(_system_status(hub, power=True, mode=1))
    await fake_controller.async_send_status(_thermostat_status(hub, mac_hex))
    await _wait_for(lambda: hub.available and mac_hex in hub.thermostats)
    await hass.async_block_till_done()
    climate_entity = _entity_id(
        hass, hub.entry.entry_id, "climate", f"thermostat_{mac_hex}_climate"
    )
    attempts = 0

    async def acknowledge_retry(frame) -> None:
        nonlocal attempts
        fields = parse_tlvs(frame.body)
        if fields.get(0x0004) != bytes.fromhex(mac_hex):
            return
        if fields.get(0x0009) != b"\x03":
            return
        attempts += 1
        if attempts == 2:
            await fake_controller.async_send_status(
                _thermostat_status(hub, mac_hex, target=fields[0x000A][0] // 2)
            )

    fake_controller.on_command = acknowledge_retry
    await hass.services.async_call(
        climate.DOMAIN,
        climate.SERVICE_SET_TEMPERATURE,
        {"entity_id": climate_entity, ATTR_TEMPERATURE: 22},
        blocking=True,
    )
    target = f"thermostat_{mac_hex}"
    hub._pending[target].deadline = 0
    await hub._async_expire_pending(time.monotonic())
    await _wait_for(lambda: not hub._pending and hub.thermostats[mac_hex].target_temperature == 22)

    assert attempts == 2
    counters = hub.health.snapshot()["counters"]
    assert counters["commands_retried"] == 1
    assert counters["commands_confirmed"] >= 1


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_control_guards_reject_invalid_or_unsafe_operations(
    setup_integration, fake_controller
):
    """Native controls retain the controller's safety gates before writing TCP."""
    hub = setup_integration.hub
    mac_hex = "ff00ffffffff03ff"
    await fake_controller.async_send_status(_system_status(hub, power=True, mode=1))
    await fake_controller.async_send_status(_thermostat_status(hub, mac_hex))
    await _wait_for(lambda: hub.available and mac_hex in hub.thermostats)

    with pytest.raises(HomeAssistantError, match="请先关闭"):
        await hub.async_set_mode("heat")
    with pytest.raises(HomeAssistantError, match="不支持的场景"):
        await hub.async_set_scene("party")
    with pytest.raises(HomeAssistantError, match="冬季加湿"):
        await hub.async_set_winter_humidifier(True)
    with pytest.raises(HomeAssistantError, match="整数"):
        await hub.async_set_thermostat_temperature(mac_hex, 22.5)
    with pytest.raises(HomeAssistantError, match="整数"):
        await hub.async_set_thermostat_temperature(mac_hex, 29)
    with pytest.raises(HomeAssistantError, match="尚未被主机发现"):
        await hub.async_set_thermostat_temperature("ff00ffffffff09ff", 22)

    hub.allow_control = False
    assert hub.control_permission == "read_only"
    with pytest.raises(HomeAssistantError, match="只读模式"):
        await hub.async_set_system_power(False)


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_switches_report_unknown_before_the_first_controller_status(
    setup_integration,
) -> None:
    """Startup never invents an on/off value before MC7021 sends a state frame."""
    hub = setup_integration.hub
    assert SystemPowerSwitch(hub).is_on is None
    assert WinterHumidifierSwitch(hub).is_on is None


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
