"""Privacy-safe diagnostic export and entity regression tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace

from custom_components.linking_the_world_temp_ha.binary_sensor import (
    ControllerConnectionSensor,
    ProtocolVerifiedSensor,
)
from custom_components.linking_the_world_temp_ha.diagnostics import (
    async_get_config_entry_diagnostics,
    build_anonymous_panel_map,
)
from custom_components.linking_the_world_temp_ha.health import HealthTracker
from custom_components.linking_the_world_temp_ha.hub import LinkingTempHub
from custom_components.linking_the_world_temp_ha.protocol import ThermostatState
from custom_components.linking_the_world_temp_ha.runtime import (
    ConnectionStage,
    FailureKind,
)
from custom_components.linking_the_world_temp_ha.sensor import DIAGNOSTICS, DiagnosticSensor


async def _hub_with_two_panels(hass, mock_config_entry) -> LinkingTempHub:
    """Create a runtime surface with deliberately private panel identities."""
    hub = LinkingTempHub(hass, mock_config_entry, HealthTracker())
    now = datetime.now(UTC)
    await hub.panel_registry.async_load()
    await hub.panel_registry.async_note_panel_report(
        "aabbccddeeff0011", "ROOM-ID-CANARY", now - timedelta(seconds=12)
    )
    await hub.panel_registry.async_note_panel_report(
        "ff00ffffffff01ff", "ROOM-ID-SECOND", now - timedelta(seconds=4)
    )
    await hub.panel_registry.async_set_room_name("ROOM-ID-CANARY", "ROOM-NAME-CANARY")
    await hub.panel_registry.async_set_room_name("ROOM-ID-SECOND", "ROOM-NAME-SECOND")
    hub.thermostats = {
        "aabbccddeeff0011": ThermostatState(
            mac=bytes.fromhex("aabbccddeeff0011"),
            room_id="ROOM-ID-CANARY",
            power="ON",
            target_temperature=22,
            current_temperature=25.4,
            humidity=64,
            available=True,
        ),
        "ff00ffffffff01ff": ThermostatState(
            mac=bytes.fromhex("ff00ffffffff01ff"),
            room_id="ROOM-ID-SECOND",
            power="OFF",
            target_temperature=20,
            current_temperature=24.7,
            humidity=58,
            available=False,
        ),
    }
    return hub


async def test_diagnostics_redact_all_secret_canaries_and_anonymize_panels(
    hass, mock_config_entry
) -> None:
    """No configured or runtime household identity may reach the JSON export."""
    canaries = {
        "host": "HOST-CANARY.internal",
        "username": "USERNAME-CANARY",
        "password": "PASSWORD-CANARY",
        "client_id": "CLIENT-ID-CANARY",
        "tech_system_mac": "1122334455667788",
        "panel_mac": "aabbccddeeff0011",
        "room_id": "ROOM-ID-CANARY",
        "room_name": "ROOM-NAME-CANARY",
        "public_key": "PUBLIC-KEY-CANARY",
        "token": "TOKEN-CANARY",
        "raw_body": "RAW-BODY-CANARY",
    }
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry,
        data={
            **mock_config_entry.data,
            "host": canaries["host"],
            "username": canaries["username"],
            "password": canaries["password"],
            "client_id": canaries["client_id"],
            "tech_system_mac": canaries["tech_system_mac"],
        },
    )
    hub = await _hub_with_two_panels(hass, mock_config_entry)
    hub.controller_public_key = canaries["public_key"]
    hub.session_token = canaries["token"]
    hub.last_connection_error = f"error host={canaries['host']} body={canaries['raw_body']}"
    hub.last_command_status = f"waiting:{canaries['room_name']} mac={canaries['panel_mac']}"
    hub.health.mark_stage(ConnectionStage.READY)
    hub.health.record_failure(
        FailureKind.TCP_TIMEOUT,
        " ".join(f"{name}={value}" for name, value in canaries.items()),
        secrets=canaries,
    )
    mock_config_entry.runtime_data = SimpleNamespace(hub=hub, health=hub.health)

    result = await async_get_config_entry_diagnostics(hass, mock_config_entry)
    serialized = json.dumps(result, ensure_ascii=False, sort_keys=True)

    for secret in canaries.values():
        assert secret not in serialized
    assert set(result["runtime"]["panels"]) == {"panel_01", "panel_02"}
    assert {panel["room"] for panel in result["runtime"]["panels"].values()} == {
        "room_01",
        "room_02",
    }


async def test_diagnostics_export_metrics_and_panel_observation_details(
    hass, mock_config_entry
) -> None:
    """The support snapshot contains useful counters without object dumps."""
    hub = await _hub_with_two_panels(hass, mock_config_entry)
    hub.health.mark_stage(ConnectionStage.READY)
    hub.health.record_failure(FailureKind.STATUS_SILENCE, "status stream silent")
    for counter in (
        "connection_attempts",
        "connection_successes",
        "reconnects",
        "frames_malformed",
        "invalid_measurements",
        "commands_sent",
        "commands_confirmed",
        "commands_retried",
        "commands_timed_out",
    ):
        hub.health.increment(counter)
    hub.health.record_confirmation_latency(0.42)
    hub._pending = {"system": object()}
    hub._queued = {"thermostat": object()}
    mock_config_entry.runtime_data = SimpleNamespace(hub=hub, health=hub.health)

    result = await async_get_config_entry_diagnostics(hass, mock_config_entry)
    runtime = result["runtime"]
    health = runtime["health"]

    assert health["stage"] == "ready"
    assert health["failure_kind"] == "status_silence"
    assert health["counters"]["commands_retried"] == 1
    assert health["confirmation_latency_summary"]["mean"] == 0.42
    assert runtime["command_queue"] == {"pending": 1, "queued": 1}
    for panel in runtime["panels"].values():
        assert isinstance(panel["last_report_age_seconds"], float)
        assert panel["last_report_age_seconds"] >= 0
        assert isinstance(panel["observed_absence_seconds"], float)
    assert "__dict__" not in json.dumps(result)


async def test_anonymous_panel_labels_are_stable_within_one_export(
    hass, mock_config_entry
) -> None:
    """Sorted panel/room identities produce deterministic export-local labels."""
    hub = await _hub_with_two_panels(hass, mock_config_entry)

    first = build_anonymous_panel_map(hub.panel_registry.records)
    second = build_anonymous_panel_map(hub.panel_registry.records)

    assert first == second
    assert first == {
        "aabbccddeeff0011": "panel_01",
        "ff00ffffffff01ff": "panel_02",
    }


async def test_diagnostic_entities_remain_available_and_use_safe_stable_states(
    hass, mock_config_entry
) -> None:
    """Offline controllers still expose diagnostic categories without raw details."""
    hub = LinkingTempHub(hass, mock_config_entry, HealthTracker())
    hub.last_connection_error = "host=HOST-CANARY.internal password=PASSWORD-CANARY"
    hub.last_command_status = "timeout:ROOM-NAME-CANARY mac=aabbccddeeff0011"
    hub.health.mark_stage(ConnectionStage.AUTHENTICATING)
    hub.health.record_failure(FailureKind.AUTH_REJECTED, "credentials rejected")

    descriptions = {description.key: description for description in DIAGNOSTICS}
    assert DiagnosticSensor(hub, descriptions["connection_stage"]).native_value == "authenticating"
    assert DiagnosticSensor(hub, descriptions["connection_error"]).native_value == "authentication_rejected"
    assert DiagnosticSensor(hub, descriptions["last_command"]).native_value == "timeout"
    assert DiagnosticSensor(hub, descriptions["connection_stage"]).available
    assert DiagnosticSensor(hub, descriptions["connection_error"]).available
    assert ControllerConnectionSensor(hub).available
    assert ProtocolVerifiedSensor(hub).available
    assert not ControllerConnectionSensor(hub).is_on
    assert not ProtocolVerifiedSensor(hub).is_on
