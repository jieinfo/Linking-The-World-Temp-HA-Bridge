"""Privacy-safe diagnostic export and entity regression tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import json
import logging
from types import SimpleNamespace

import pytest

from custom_components.linking_the_world_temp_ha.binary_sensor import (
    ControllerConnectionSensor,
    ProtocolVerifiedSensor,
)
from custom_components.linking_the_world_temp_ha.diagnostics import (
    async_get_config_entry_diagnostics,
    build_anonymous_panel_map,
)
from custom_components.linking_the_world_temp_ha.health import HealthTracker
import custom_components.linking_the_world_temp_ha.hub as hub_module
from custom_components.linking_the_world_temp_ha.hub import LinkingTempHub
from custom_components.linking_the_world_temp_ha.protocol import ThermostatState
from custom_components.linking_the_world_temp_ha.protocol import (
    TcpConnectError,
    tlv,
)
from custom_components.linking_the_world_temp_ha.runtime import (
    ConnectionStage,
    FailureKind,
)
from custom_components.linking_the_world_temp_ha.sensor import (
    DIAGNOSTICS,
    DiagnosticSensor,
)


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


class _CommandClient:
    """Small ready client that records real hub command paths."""

    def __init__(self, *, fail_send: bool = False) -> None:
        self.fail_send = fail_send
        self.commands: list[tuple[bytes, int, int | None]] = []
        self.status_requests = 0

    async def send_command(
        self,
        mac: bytes,
        command: int,
        value: int | None,
        *,
        before_write=None,
    ) -> None:
        if before_write is not None:
            before_write()
        self.commands.append((mac, command, value))
        if self.fail_send:
            raise ConnectionError("send failed")

    async def request_status(self) -> None:
        self.status_requests += 1


class _LifecycleClient(_CommandClient):
    """Client boundary double which drives the hub's normal lifecycle callbacks."""

    reader_alive = True
    reader_error = None

    def __init__(self) -> None:
        super().__init__()
        self.on_frame = None
        self.on_status = None
        self.on_stage = None
        self.on_parser_event = None

    async def connect(self) -> None:
        assert self.on_stage is not None
        for stage in ("connecting", "handshaking", "authenticating", "ready"):
            await self.on_stage(stage)

    async def heartbeat(self) -> None:
        return None

    async def close(self) -> None:
        self.reader_alive = False


class _ReconnectClient(_LifecycleClient):
    """Programmable controller client for a real hub reconnect cycle."""

    def __init__(
        self,
        attempt: int,
        ready: list[asyncio.Event],
        closed: list[asyncio.Event],
    ) -> None:
        super().__init__()
        self.attempt = attempt
        self.ready = ready[attempt - 1]
        self.closed = closed[attempt - 1]

    async def connect(self) -> None:
        assert self.on_stage is not None
        self.reader_alive = True
        await self.on_stage("handshaking")
        await self.on_stage("authenticating")
        await self.on_stage("ready")
        self.ready.set()

    async def heartbeat(self) -> None:
        return None

    async def close(self) -> None:
        self.reader_alive = False
        self.closed.set()


def _ready_command_hub(hass, mock_config_entry) -> LinkingTempHub:
    hub = LinkingTempHub(hass, mock_config_entry, HealthTracker())
    hub.allow_control = True
    hub.connected = True
    hub.protocol_verified = True
    hub.health.mark_stage(ConnectionStage.READY)
    hub.command_min_interval = 0
    hub.command_confirmation_timeout = 1
    hub.thermostats = {
        "aabbccddeeff0011": ThermostatState(
            mac=bytes.fromhex("aabbccddeeff0011"),
            room_id="ROOM-ID-CANARY",
            target_temperature=21,
            current_temperature=25,
            humidity=60,
            available=True,
        )
    }
    hub.room_names["ROOM-ID-CANARY"] = "ROOM-NAME-CANARY"
    return hub


async def test_queued_mode_is_revalidated_after_power_changes(
    hass, mock_config_entry
) -> None:
    """A queued mode command cannot cross the controller's power interlock."""
    hub = _ready_command_hub(hass, mock_config_entry)
    client = _CommandClient()
    hub._client = client  # type: ignore[assignment]
    hub.state.power = "OFF"
    hub.state.mode = "cool"

    await hub.async_set_scene("away")
    await hub.async_set_mode("heat")
    hub.state.power = "ON"
    hub._confirm_pending("system", {"scene": "away"})
    await hub._async_dispatch_queued()

    assert [command[1:] for command in client.commands] == [(4, 0)]
    assert "system" not in hub._pending
    assert "system" not in hub._queued
    assert hub.health.snapshot()["counters"]["commands_blocked"] == 1


async def test_queued_humidifier_is_revalidated_after_mode_changes(
    hass, mock_config_entry
) -> None:
    """A queued humidifier command cannot leave heat mode before dispatch."""
    hub = _ready_command_hub(hass, mock_config_entry)
    client = _CommandClient()
    hub._client = client  # type: ignore[assignment]
    hub.state.power = "OFF"
    hub.state.mode = "heat"

    await hub.async_set_scene("away")
    await hub.async_set_winter_humidifier(True)
    hub.state.mode = "cool"
    hub._confirm_pending("system", {"scene": "away"})
    await hub._async_dispatch_queued()

    assert [command[1:] for command in client.commands] == [(4, 0)]
    assert "system" not in hub._pending
    assert "system" not in hub._queued
    assert hub.health.snapshot()["counters"]["commands_blocked"] == 1


async def test_total_control_queue_drops_reversal_to_verified_state(
    hass, mock_config_entry
) -> None:
    """A latest intent equal to verified state removes its stale queued opposite."""
    hub = _ready_command_hub(hass, mock_config_entry)
    client = _CommandClient()
    hub._client = client  # type: ignore[assignment]
    hub.state.power = "OFF"

    await hub.async_set_scene("away")
    await hub.async_set_system_power(True)
    await hub.async_set_system_power(False)

    assert "system" not in hub._queued
    hub._confirm_pending("system", {"scene": "away"})
    await hub._async_dispatch_queued()
    assert [command[1:] for command in client.commands] == [(4, 0)]


async def test_production_command_logs_never_expose_panel_identity(
    hass, mock_config_entry, caplog: pytest.LogCaptureFixture
) -> None:
    """Timeout, retry, queue, and send-failure logs retain only safe context."""
    hub = _ready_command_hub(hass, mock_config_entry)
    client = _CommandClient()
    hub._client = client  # type: ignore[assignment]
    caplog.set_level(
        logging.DEBUG, logger="custom_components.linking_the_world_temp_ha"
    )
    invalid_measurement = (
        tlv(0x0004, bytes.fromhex("aabbccddeeff0011"))
        + tlv(0x0075, hub.tech_system_mac)
        + tlv(0x0030, b"ROOM-ID-CANARY")
        + tlv(0x000A, bytes((44, 0xE8, 0x03, 100, 0)))
        + tlv(0x000B, b"\x01")
    )
    await hub._async_status_received(invalid_measurement)

    await hub.async_set_thermostat_temperature("aabbccddeeff0011", 22)
    await hub.async_set_thermostat_temperature("aabbccddeeff0011", 23)
    pending = hub._pending["thermostat_aabbccddeeff0011"]
    pending.deadline = 0
    await hub._async_expire_pending(1)
    await hub._async_dispatch_queued()
    pending = hub._pending["thermostat_aabbccddeeff0011"]
    pending.deadline = 0
    await hub._async_expire_pending(2)
    pending.deadline = 0
    await hub._async_expire_pending(3)

    failing_hub = _ready_command_hub(hass, mock_config_entry)
    failing_hub._client = _CommandClient(fail_send=True)  # type: ignore[assignment]
    with pytest.raises(ConnectionError):
        await failing_hub.async_set_thermostat_temperature("aabbccddeeff0011", 24)

    production_logs = "\n".join(record.getMessage() for record in caplog.records)
    for canary in ("ROOM-NAME-CANARY", "ROOM-ID-CANARY", "aabbccddeeff0011"):
        assert canary not in production_logs
    assert "MC7021 command send failed: target_type=thermostat" in production_logs


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
    hub.last_connection_error = (
        f"error host={canaries['host']} body={canaries['raw_body']}"
    )
    hub.last_command_status = (
        f"waiting:{canaries['room_name']} mac={canaries['panel_mac']}"
    )
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


async def test_diagnostics_metrics_follow_real_runtime_event_paths(
    hass, mock_config_entry, monkeypatch
) -> None:
    """Diagnostics counts events at production paths rather than test seeding."""
    hub = _ready_command_hub(hass, mock_config_entry)
    await hub.panel_registry.async_load()
    lifecycle_client = _LifecycleClient()
    monkeypatch.setattr(
        hub_module, "AsyncMoorgenClient", lambda *_args: lifecycle_client
    )

    async def stop_after_connect(_client) -> None:
        hub._stop.set()

    hub._async_session_loop = stop_after_connect  # type: ignore[method-assign]
    await hub._async_run()
    assert lifecycle_client.on_parser_event is not None
    await lifecycle_client.on_parser_event("frames_malformed", 1)
    await lifecycle_client.on_parser_event("frames_resynchronized", 1)

    hub._stop.clear()
    hub.connected = True
    hub.protocol_verified = True
    hub.health.mark_stage(ConnectionStage.READY)
    invalid_measurement = (
        tlv(0x0004, bytes.fromhex("aabbccddeeff0011"))
        + tlv(0x0075, hub.tech_system_mac)
        + tlv(0x0030, b"ROOM-ID-CANARY")
        + tlv(0x000A, bytes((44, 0xE8, 0x03, 100, 0)))
        + tlv(0x000B, b"\x01")
    )
    await hub._async_status_received(invalid_measurement)

    command_client = _CommandClient()
    hub._client = command_client  # type: ignore[assignment]
    await hub.async_set_system_power(True)
    hub._confirm_pending("system", {"power": "ON"})
    await hub.async_set_thermostat_temperature("aabbccddeeff0011", 22)
    await hub.async_set_thermostat_temperature("aabbccddeeff0011", 23)
    pending = hub._pending["thermostat_aabbccddeeff0011"]
    pending.deadline = 0
    await hub._async_expire_pending(1)
    await hub._async_dispatch_queued()
    pending = hub._pending["thermostat_aabbccddeeff0011"]
    pending.deadline = 0
    await hub._async_expire_pending(2)
    pending.deadline = 0
    await hub._async_expire_pending(3)

    hub._record_connection_failure(TcpConnectError("connection refused"))
    mock_config_entry.runtime_data = SimpleNamespace(hub=hub, health=hub.health)
    result = await async_get_config_entry_diagnostics(hass, mock_config_entry)

    runtime = result["runtime"]
    health = runtime["health"]

    assert health["stage"] == "ready"
    assert health["failure_kind"] == "tcp_timeout"
    assert health["counters"]["connection_attempts"] == 1
    assert health["counters"]["connection_successes"] == 1
    assert health["counters"]["reconnects"] == 0
    assert health["counters"]["disconnects"] == 1
    assert health["counters"]["handshake_successes"] == 1
    assert health["counters"]["login_successes"] == 1
    assert health["counters"]["frames_malformed"] == 1
    assert health["counters"]["frames_resynchronized"] == 1
    assert health["counters"]["invalid_measurements"] == 1
    assert health["counters"]["commands_sent"] == 4
    assert health["counters"]["commands_confirmed"] == 1
    assert health["counters"]["commands_retried"] == 1
    assert health["counters"]["commands_coalesced"] == 1
    assert health["counters"]["commands_timed_out"] == 3
    assert health["confirmation_latency_summary"]["count"] == 1
    assert runtime["command_queue"] == {"pending": 0, "queued": 0}
    for panel in runtime["panels"].values():
        assert isinstance(panel["last_report_age_seconds"], float)
        assert panel["last_report_age_seconds"] >= 0
        assert isinstance(panel["observed_absence_seconds"], float)
    assert "__dict__" not in json.dumps(result)


async def test_command_confirmation_prefers_push_before_status_query(
    hass, mock_config_entry
) -> None:
    """A normal fast controller push avoids an extra status request."""
    hub = _ready_command_hub(hass, mock_config_entry)
    client = _CommandClient()
    hub._client = client  # type: ignore[assignment]

    await hub.async_set_system_power(True)

    assert client.status_requests == 0
    hub._confirm_pending("system", {"power": "ON"})
    counters = hub.health.snapshot()["counters"]
    assert counters["commands_confirmed_by_push"] == 1
    assert counters["commands_confirmed_after_query"] == 0
    assert counters["status_fallback_queries"] == 0


async def test_command_health_tracks_queue_peak_final_timeouts_and_recovery(
    hass, mock_config_entry
) -> None:
    """Diagnostics distinguish queued load, repeated failures, and recovery."""
    hub = _ready_command_hub(hass, mock_config_entry)
    client = _CommandClient()
    hub._client = client  # type: ignore[assignment]

    await hub.async_set_system_power(True)
    await hub.async_set_scene("away")
    snapshot = hub.health.snapshot()["command_runtime"]
    assert snapshot["current_queue_depth"] == 2
    assert snapshot["peak_queue_depth"] == 2

    for _ in range(3):
        hub._pending["system"].deadline = 0
        await hub._async_expire_pending(1)
        await hub._async_dispatch_queued()
        if "system" not in hub._pending:
            await hub.async_set_system_power(True)

    snapshot = hub.health.snapshot()["command_runtime"]
    assert snapshot["consecutive_timeouts"] == 3
    assert snapshot["recoveries"] == 0

    hub._confirm_pending("system", {"power": "ON"})
    snapshot = hub.health.snapshot()["command_runtime"]
    assert snapshot["consecutive_timeouts"] == 0
    assert snapshot["recoveries"] == 1


async def test_final_command_timeout_logs_escalate_only_after_repetition(
    hass, mock_config_entry, caplog: pytest.LogCaptureFixture
) -> None:
    """One-off controller delays warn; a third consecutive failure is an error."""
    hub = _ready_command_hub(hass, mock_config_entry)
    hub._client = _CommandClient()  # type: ignore[assignment]
    caplog.set_level(logging.INFO, logger="custom_components.linking_the_world_temp_ha")

    for _ in range(3):
        await hub.async_set_system_power(True)
        hub._pending["system"].deadline = 0
        await hub._async_expire_pending(1)

    records = [
        record
        for record in caplog.records
        if record.getMessage().startswith("MC7021 command confirmation timed out")
    ]
    assert [record.levelno for record in records] == [
        logging.WARNING,
        logging.WARNING,
        logging.ERROR,
    ]


async def test_superseded_timeout_is_an_info_recovery_event(
    hass, mock_config_entry, caplog: pytest.LogCaptureFixture
) -> None:
    """A timed-out intermediate value with a queued replacement is not an error."""
    hub = _ready_command_hub(hass, mock_config_entry)
    hub._client = _CommandClient()  # type: ignore[assignment]
    caplog.set_level(logging.INFO, logger="custom_components.linking_the_world_temp_ha")

    await hub.async_set_thermostat_temperature("aabbccddeeff0011", 22)
    await hub.async_set_thermostat_temperature("aabbccddeeff0011", 23)
    hub._pending["thermostat_aabbccddeeff0011"].deadline = 0
    await hub._async_expire_pending(1)

    record = next(
        record
        for record in caplog.records
        if record.getMessage().startswith("Command confirmation timed out; continuing")
    )
    assert record.levelno == logging.INFO


async def test_unconfirmed_command_uses_one_shared_status_query_fallback(
    hass, mock_config_entry
) -> None:
    """Only commands which outlive the push grace period trigger a query."""
    hub = _ready_command_hub(hass, mock_config_entry)
    client = _CommandClient()
    hub._client = client  # type: ignore[assignment]

    await hub.async_set_system_power(True)
    pending = hub._pending["system"]
    await hub._async_poll_pending_status(pending.next_status_poll_at - 0.001)
    assert client.status_requests == 0

    await hub._async_poll_pending_status(pending.next_status_poll_at + 0.001)
    assert client.status_requests == 1
    hub._confirm_pending("system", {"power": "ON"})
    counters = hub.health.snapshot()["counters"]
    assert counters["commands_confirmed_by_push"] == 0
    assert counters["commands_confirmed_after_query"] == 1
    assert counters["status_fallback_queries"] == 1


async def test_diagnostics_connection_metrics_follow_real_reconnect_cycle(
    hass, mock_config_entry, monkeypatch
) -> None:
    """A real hub run counts two authenticated sessions and one reconnect."""
    hub = LinkingTempHub(hass, mock_config_entry, HealthTracker())
    await hub.panel_registry.async_load()
    ready = [asyncio.Event(), asyncio.Event()]
    closed = [asyncio.Event(), asyncio.Event()]
    clients: list[_ReconnectClient] = []

    def make_client(*_args) -> _ReconnectClient:
        client = _ReconnectClient(len(clients) + 1, ready, closed)
        clients.append(client)
        return client

    monkeypatch.setattr(hub_module, "AsyncMoorgenClient", make_client)
    real_wait_for = asyncio.wait_for

    async def fast_hub_wait_for(awaitable, timeout):
        """Keep hub retry and session polling deterministic and short."""
        if timeout in {1, 5, 10, 20, 30}:
            timeout = 0.01
        return await real_wait_for(awaitable, timeout)

    monkeypatch.setattr(hub_module.asyncio, "wait_for", fast_hub_wait_for)
    run_task = asyncio.create_task(hub._async_run())
    try:
        await real_wait_for(ready[0].wait(), timeout=1)
        clients[0].reader_alive = False
        await real_wait_for(closed[0].wait(), timeout=1)

        await real_wait_for(ready[1].wait(), timeout=1)
        hub._stop.set()
        await real_wait_for(run_task, timeout=1)
        await real_wait_for(closed[1].wait(), timeout=1)
    finally:
        hub._stop.set()
        if not run_task.done():
            run_task.cancel()
        await asyncio.gather(run_task, return_exceptions=True)

    counters = hub.health.snapshot()["counters"]
    assert counters["connection_attempts"] == 2
    assert counters["connection_successes"] == 2
    assert counters["reconnects"] == 1
    assert counters["disconnects"] == 2
    assert len(clients) == 2
    assert all(client.reader_alive is False for client in clients)
    assert not any(
        task is not asyncio.current_task()
        and not task.done()
        and task.get_coro().__qualname__.endswith("_async_run")
        for task in asyncio.all_tasks()
    )


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
    assert (
        DiagnosticSensor(hub, descriptions["connection_stage"]).native_value
        == "authenticating"
    )
    assert (
        DiagnosticSensor(hub, descriptions["connection_error"]).native_value
        == "authentication_rejected"
    )
    assert DiagnosticSensor(hub, descriptions["last_command"]).native_value == "timeout"
    assert DiagnosticSensor(hub, descriptions["connection_stage"]).available
    assert DiagnosticSensor(hub, descriptions["connection_error"]).available
    assert ControllerConnectionSensor(hub).available
    assert ProtocolVerifiedSensor(hub).available
    assert not ControllerConnectionSensor(hub).is_on
    assert not ProtocolVerifiedSensor(hub).is_on
