"""Connection and state coordinator for Linking The World Temp HA."""

from __future__ import annotations

import asyncio
import logging
import statistics
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError

from .command_queue import (
    STATUS_POLL_INTERVAL,
    PendingCommand,
    QueuedCommand,
    coalesce_latest,
    temperature_retry_is_allowed,
)
from .const import (
    CONF_ALLOW_CONTROL,
    CONF_CLIENT_ID,
    CONF_COMMAND_CONFIRMATION_TIMEOUT,
    CONF_COMMAND_MIN_INTERVAL,
    CONF_CONTROLLER_SILENCE_TIMEOUT,
    CONF_TECH_SYSTEM_MAC,
    CONF_THERMOSTAT_OFFLINE_AFTER,
    DEFAULT_ALLOW_CONTROL,
    DEFAULT_CLIENT_ID,
    DEFAULT_COMMAND_CONFIRMATION_TIMEOUT,
    DEFAULT_COMMAND_MIN_INTERVAL,
    DEFAULT_CONTROLLER_SILENCE_TIMEOUT,
    DEFAULT_TECH_SYSTEM_MAC,
    DEFAULT_THERMOSTAT_OFFLINE_AFTER,
    DOMAIN,
    MODE_VALUES,
    SCENE_VALUES,
    THERMOSTAT_MAX_TEMPERATURE,
    THERMOSTAT_MIN_TEMPERATURE,
)
from .protocol import (
    COMMAND_MODE,
    COMMAND_POWER_OFF,
    COMMAND_POWER_ON,
    COMMAND_SCENE,
    COMMAND_WINTER_HUMIDIFIER,
    AsyncMoorgenClient,
    AuthenticationRejected,
    CannotConnect,
    HandshakeTimeout,
    IncompatibleProtocol,
    LoginTimeout,
    MoorgenConnectionError,
    TechSystemState,
    TcpConnectError,
    ThermostatState,
    YasHcpFrame,
    decode_tech_system_status,
    decode_text,
    decode_thermostat_status,
    iter_tlvs,
    parse_device_mac,
    preserve_valid_thermostat_measurements,
)
from .health import HealthTracker
from .panel_registry import PanelRegistry
from .runtime import ConnectionStage, FailureKind
from .repairs import RepairManager
from .thermostat_policy import room_thermostat_block_reason

_LOGGER = logging.getLogger(__name__)

REAUTH_FLOW_WATCH_INTERVAL = 1.0
_FLOW_SOURCE_REAUTH = "reauth"
_FLOW_SOURCE_RECONFIGURE = "reconfigure"


@dataclass
class FilteredMeasurements:
    """Smoothed values intended for automations."""

    temperatures: deque[float]
    humidities: deque[int]
    temperature: float | None = None
    humidity: int | None = None


class LinkingTempHub:
    """Own the controller session and expose push state to HA entities."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, health: HealthTracker
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.host = entry.data["host"]
        self.port = int(entry.data["port"])
        self.username = entry.data["username"]
        self.password = entry.data["password"]
        self.client_id = entry.data.get(CONF_CLIENT_ID, DEFAULT_CLIENT_ID)
        self.tech_system_mac = parse_device_mac(
            entry.data.get(CONF_TECH_SYSTEM_MAC, DEFAULT_TECH_SYSTEM_MAC)
        )
        options = entry.options
        self.allow_control = bool(
            options.get(CONF_ALLOW_CONTROL, DEFAULT_ALLOW_CONTROL)
        )
        self.command_min_interval = float(
            options.get(CONF_COMMAND_MIN_INTERVAL, DEFAULT_COMMAND_MIN_INTERVAL)
        )
        self.command_confirmation_timeout = float(
            options.get(
                CONF_COMMAND_CONFIRMATION_TIMEOUT,
                DEFAULT_COMMAND_CONFIRMATION_TIMEOUT,
            )
        )
        self.controller_silence_timeout = float(
            options.get(
                CONF_CONTROLLER_SILENCE_TIMEOUT, DEFAULT_CONTROLLER_SILENCE_TIMEOUT
            )
        )
        self.thermostat_offline_after = float(
            options.get(CONF_THERMOSTAT_OFFLINE_AFTER, DEFAULT_THERMOSTAT_OFFLINE_AFTER)
        )

        self.state = TechSystemState()
        self.thermostats: dict[str, ThermostatState] = {}
        self.room_names: dict[str, str] = {}
        self.panel_registry = PanelRegistry(hass, entry.entry_id)
        self.filtered: dict[str, FilteredMeasurements] = {}
        self.health = health
        self.repairs = RepairManager(hass, entry)
        self.connected = False
        self.protocol_verified = False
        self.protocol_status = "waiting"
        self.last_connection_error = "starting"
        self.last_command_status = "idle"

        self._client: AsyncMoorgenClient | None = None
        self._listeners: set[Callable[[], None]] = set()
        self._runner: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._command_lock = asyncio.Lock()
        self._last_command_at: float | None = None
        self._pending: dict[str, PendingCommand] = {}
        self._queued: dict[str, QueuedCommand] = {}
        self._has_attempted_connection = False
        self._session_authenticated = False
        self._reauth_required = False
        self._reauth_watcher: asyncio.Task[None] | None = None

    async def async_start(self) -> None:
        """Restore known panels and start the supervised TCP session."""
        await self._async_restore_panels()
        await self.panel_registry.async_pause_monitoring(datetime.now(UTC))
        self._runner = self.entry.async_create_background_task(
            self.hass,
            self._async_run(),
            f"{DOMAIN}_{self.entry.entry_id}",
        )

    async def async_stop(self) -> None:
        """Stop the session and mark all state unavailable."""
        self._stop.set()
        watcher = self._reauth_watcher
        self._reauth_watcher = None
        if watcher is not None and watcher is not asyncio.current_task():
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
        if self._runner is not None:
            self._runner.cancel()
            try:
                await self._runner
            except asyncio.CancelledError:
                pass
            self._runner = None
        await self._async_disconnect()
        await self.panel_registry.async_flush()

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.add(listener)

        @callback
        def remove_listener() -> None:
            self._listeners.discard(listener)

        return remove_listener

    @callback
    def _notify(self) -> None:
        for listener in tuple(self._listeners):
            listener()

    @property
    def available(self) -> bool:
        stage = getattr(getattr(self, "health", None), "stage", None)
        return (
            (stage is None or stage is ConnectionStage.READY)
            and self.connected
            and self.protocol_verified
        )

    @property
    def control_permission(self) -> str:
        if not self.allow_control:
            return "read_only"
        if (
            getattr(getattr(self, "health", None), "stage", None)
            is ConnectionStage.DISCONNECTED
        ):
            return "disconnected"
        if not self.protocol_verified:
            return "waiting_for_protocol"
        return "ready"

    def thermostat_name(self, thermostat: ThermostatState) -> str:
        room_name = self.room_names.get(thermostat.room_id, thermostat.room_id)
        return f"{room_name or thermostat.mac.hex()} 温控面板"

    async def async_set_system_power(self, enabled: bool) -> None:
        await self._async_send_tracked(
            "system",
            "总控开关",
            {"power": "ON" if enabled else "OFF"},
            self.tech_system_mac,
            COMMAND_POWER_ON if enabled else COMMAND_POWER_OFF,
        )

    async def async_set_mode(self, mode: str) -> None:
        if not self.state.can_change_mode:
            raise HomeAssistantError("请先关闭科技系统，再切换运行模式")
        if mode not in MODE_VALUES:
            raise HomeAssistantError(f"不支持的运行模式: {mode}")
        await self._async_send_tracked(
            "system",
            "总控模式",
            {"mode": mode},
            self.tech_system_mac,
            COMMAND_MODE,
            MODE_VALUES[mode],
        )

    async def async_set_scene(self, scene: str) -> None:
        if scene not in SCENE_VALUES:
            raise HomeAssistantError(f"不支持的场景: {scene}")
        await self._async_send_tracked(
            "system",
            "总控场景",
            {"scene": scene},
            self.tech_system_mac,
            COMMAND_SCENE,
            SCENE_VALUES[scene],
        )

    async def async_set_winter_humidifier(self, enabled: bool) -> None:
        if self.state.mode != "heat":
            raise HomeAssistantError("冬季加湿仅能在制热模式下使用")
        await self._async_send_tracked(
            "system",
            "冬季加湿",
            {"winter_humidifier": "ON" if enabled else "OFF"},
            self.tech_system_mac,
            COMMAND_WINTER_HUMIDIFIER,
            1 if enabled else 0,
        )

    async def async_set_thermostat_power(self, mac_hex: str, enabled: bool) -> None:
        thermostat = self._require_thermostat(mac_hex)
        await self._async_send_tracked(
            f"thermostat_{mac_hex}",
            f"{self.thermostat_name(thermostat)} 开关",
            {"power": "ON" if enabled else "OFF"},
            thermostat.mac,
            COMMAND_POWER_ON if enabled else COMMAND_POWER_OFF,
            send_guard=(
                self._room_thermostat_block_reason
                if enabled
                else None
            ),
        )

    def _room_thermostat_block_reason(self) -> str | None:
        """Return why a room panel cannot be enabled at send time."""
        pending_system = self._pending.get("system")
        if pending_system is not None and pending_system.expected.get("power") == "OFF":
            return "科技系统总开关正在关闭，房间温控面板不能开启"
        return room_thermostat_block_reason(self.state.power, self.state.mode)

    async def async_set_thermostat_temperature(
        self, mac_hex: str, temperature: float
    ) -> None:
        thermostat = self._require_thermostat(mac_hex)
        temperature = float(temperature)
        if not temperature.is_integer() or not (
            THERMOSTAT_MIN_TEMPERATURE <= temperature <= THERMOSTAT_MAX_TEMPERATURE
        ):
            raise HomeAssistantError(
                f"温度必须是 {THERMOSTAT_MIN_TEMPERATURE} 至 {THERMOSTAT_MAX_TEMPERATURE}°C 的整数"
            )
        await self._async_send_tracked(
            f"thermostat_{mac_hex}",
            f"{self.thermostat_name(thermostat)} 设定温度",
            {"target_temperature": f"{int(temperature)}"},
            thermostat.mac,
            COMMAND_MODE,
            int(temperature) * 2,
            coalesce=True,
        )

    def _require_thermostat(self, mac_hex: str) -> ThermostatState:
        if thermostat := self.thermostats.get(mac_hex):
            return thermostat
        raise HomeAssistantError("该房间温控面板尚未被主机发现")

    async def _async_run(self) -> None:
        retry_delay = 5
        while not self._stop.is_set():
            try:
                self.health.increment("connection_attempts")
                if self._has_attempted_connection:
                    self.health.increment("reconnects")
                self._has_attempted_connection = True
                self._mark_stage(ConnectionStage.CONNECTING)
                self.last_connection_error = "connecting"
                self._notify()
                client = AsyncMoorgenClient(
                    self.host,
                    self.port,
                    self.username,
                    self.password,
                    self.client_id,
                )
                client.on_frame = self._async_frame_received
                client.on_status = self._async_status_received
                client.on_stage = self._async_protocol_stage_received
                client.on_parser_event = self._async_parser_event
                self._session_authenticated = False
                self._reauth_required = False
                self._client = client
                await client.connect()
                self.connected = True
                self.health.increment("connection_successes")
                self.health.clear_failure()
                await self.repairs.async_set_login_timeout(False)
                await self.repairs.async_set_protocol_incompatible(False)
                self.last_connection_error = "none"
                retry_delay = 5
                self._notify()
                await self._async_session_loop(client)
                if client.reader_error is not None:
                    raise ConnectionError(str(client.reader_error))
                raise ConnectionError("MC7021 TCP reader stopped")
            except asyncio.CancelledError:
                raise
            except AuthenticationRejected as error:
                self._record_connection_failure(error)
                self.last_connection_error = str(error) or error.__class__.__name__
                self._reauth_required = True
                _LOGGER.warning(
                    "MC7021 rejected the configured credentials; reauthentication is required"
                )
                self._async_request_reauth()
            except (CannotConnect, ConnectionError, OSError, TimeoutError) as error:
                self._record_connection_failure(error)
                self.last_connection_error = str(error) or error.__class__.__name__
                _LOGGER.warning(
                    "MC7021 session unavailable: %s; retrying in %ss",
                    error,
                    retry_delay,
                )
            finally:
                await self._async_disconnect()
            if self._reauth_required:
                return
            try:
                await asyncio.wait_for(self._stop.wait(), retry_delay)
            except asyncio.TimeoutError:
                pass
            retry_delay = min(30, retry_delay * 2)

    def _async_request_reauth(self) -> None:
        """Start reauth now, or wait quietly for a conflicting flow to finish."""
        flows = tuple(
            self.entry.async_get_active_flows(
                self.hass, {_FLOW_SOURCE_REAUTH, _FLOW_SOURCE_RECONFIGURE}
            )
        )
        if any(
            flow["context"].get("source") == _FLOW_SOURCE_REAUTH for flow in flows
        ):
            return
        if not flows:
            self.entry.async_start_reauth(self.hass)
            return
        if self._reauth_watcher is None or self._reauth_watcher.done():
            self._reauth_watcher = self.entry.async_create_background_task(
                self.hass,
                self._async_wait_for_reauth_slot(),
                f"{DOMAIN}_{self.entry.entry_id}_reauth_waiter",
            )

    async def _async_wait_for_reauth_slot(self) -> None:
        """Wait at a low rate for an existing reconfigure flow to end.

        Config-entry flows expose no completion callback.  This task only exists
        while a rejected session is paused, performs no network I/O, and is
        owned by the entry so unload reliably cancels it.
        """
        try:
            while self._reauth_required and not self._stop.is_set():
                flows = tuple(
                    self.entry.async_get_active_flows(
                        self.hass, {_FLOW_SOURCE_REAUTH, _FLOW_SOURCE_RECONFIGURE}
                    )
                )
                if any(
                    flow["context"].get("source") == _FLOW_SOURCE_REAUTH
                    for flow in flows
                ):
                    return
                if not flows:
                    self.entry.async_start_reauth(self.hass)
                    await asyncio.sleep(0)
                    if any(
                        flow["context"].get("source") == _FLOW_SOURCE_REAUTH
                        for flow in self.entry.async_get_active_flows(
                            self.hass,
                            {_FLOW_SOURCE_REAUTH, _FLOW_SOURCE_RECONFIGURE},
                        )
                    ):
                        return
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), REAUTH_FLOW_WATCH_INTERVAL
                    )
                except asyncio.TimeoutError:
                    pass
        finally:
            self._reauth_watcher = None

    async def _async_protocol_stage_received(self, stage: str) -> None:
        """Reflect protocol lifecycle callbacks in the shared health runtime."""
        connection_stage = ConnectionStage(stage)
        if connection_stage is ConnectionStage.AUTHENTICATING:
            self.health.increment("handshake_successes")
        elif connection_stage is ConnectionStage.READY:
            self.connected = True
            self._session_authenticated = True
            self.health.increment("login_successes")
        self._mark_stage(connection_stage)
        self._notify()

    async def _async_parser_event(self, name: str, count: int) -> None:
        """Record decoder counters only; raw protocol data stays in the client."""
        self.health.increment(name, count)
        if name == "frames_malformed":
            await self.panel_registry.async_pause_monitoring(datetime.now(UTC))

    def _increment_health(self, name: str, count: int = 1) -> None:
        """Keep focused compatibility tests independent from runtime construction."""
        if health := getattr(self, "health", None):
            health.increment(name, count)

    def _mark_stage(self, stage: ConnectionStage) -> None:
        """Update the lifecycle before entities are notified of a disconnect."""
        if self.health.stage is stage:
            return
        self.health.mark_stage(stage)
        if stage is ConnectionStage.DISCONNECTED:
            self.connected = False

    def _record_connection_failure(self, error: BaseException) -> None:
        """Map typed connection failures to stable, user-safe health categories."""
        kind = FailureKind.TCP_TIMEOUT
        if isinstance(error, TcpConnectError):
            if isinstance(error.__cause__, ConnectionRefusedError):
                kind = FailureKind.TCP_REFUSED
            else:
                kind = FailureKind.TCP_TIMEOUT
        elif isinstance(error, HandshakeTimeout):
            kind = FailureKind.HANDSHAKE
            self.health.increment("handshake_failures")
        elif isinstance(error, LoginTimeout):
            kind = FailureKind.LOGIN_TIMEOUT
            self.health.increment("login_failures")
            if repairs := getattr(self, "repairs", None):
                self._schedule_repair(repairs.async_set_login_timeout(True))
        elif isinstance(error, AuthenticationRejected):
            kind = FailureKind.AUTH_REJECTED
            self.health.increment("login_failures")
        elif isinstance(error, IncompatibleProtocol):
            kind = FailureKind.PROTOCOL
            if self.health.stage is ConnectionStage.HANDSHAKING:
                self.health.increment("handshake_failures")
            elif self.health.stage is ConnectionStage.AUTHENTICATING:
                self.health.increment("login_failures")
            if repairs := getattr(self, "repairs", None):
                self._schedule_repair(repairs.async_set_protocol_incompatible(True))
        elif "silent" in str(error).lower():
            kind = FailureKind.STATUS_SILENCE
        self.health.record_failure(
            kind,
            error,
            secrets={
                "host": self.host,
                "username": self.username,
                "password": self.password,
                "client_id": self.client_id,
            },
        )

    def _schedule_repair(self, coroutine: Any) -> None:
        """Run issue updates without blocking the reconnect failure path."""
        if not hasattr(self, "hass") or not hasattr(self, "entry"):
            coroutine.close()
            return
        self.entry.async_create_background_task(
            self.hass, coroutine, "linking-temp-connection-repair"
        )

    async def _async_session_loop(self, client: AsyncMoorgenClient) -> None:
        heartbeat_at = 0.0
        availability_at = 0.0
        while not self._stop.is_set() and client.reader_alive:
            now = time.monotonic()
            if now - client.last_received_at >= self.controller_silence_timeout:
                raise ConnectionError(
                    f"MC7021 has been silent for {now - client.last_received_at:.0f} seconds"
                )
            await self._async_poll_pending_status(now)
            await self._async_expire_pending(now)
            await self._async_dispatch_queued()
            if now >= heartbeat_at:
                await client.heartbeat()
                heartbeat_at = now + 15
            if now >= availability_at:
                self._refresh_thermostat_availability(now)
                availability_at = now + 15
            try:
                await asyncio.wait_for(
                    self._stop.wait(), 0.25 if self._queued else 1
                )
            except asyncio.TimeoutError:
                pass

    async def _async_disconnect(self) -> None:
        client = self._client
        self._client = None
        was_authenticated = getattr(self, "_session_authenticated", False)
        self._session_authenticated = False
        self._mark_stage(ConnectionStage.DISCONNECTED)
        self.protocol_verified = False
        self.protocol_status = "disconnected"
        if was_authenticated:
            self.health.increment("disconnects")
        if panel_registry := getattr(self, "panel_registry", None):
            await panel_registry.async_pause_monitoring(datetime.now(UTC))
        for thermostat in self.thermostats.values():
            thermostat.available = False
        self.filtered.clear()
        self._pending.clear()
        self._queued.clear()
        if client is not None:
            await client.close()
        self._notify()

    async def _async_send_tracked(
        self,
        target: str,
        label: str,
        expected: dict[str, str],
        mac: bytes,
        command: int,
        value: int | None = None,
        *,
        coalesce: bool = False,
        send_guard: Callable[[], str | None] | None = None,
    ) -> None:
        if not self.allow_control:
            self.health.increment("commands_blocked")
            raise HomeAssistantError("集成当前处于只读模式")
        if not self.available or self._client is None:
            self.health.increment("commands_blocked")
            raise HomeAssistantError("主机尚未连接或协议状态尚未验证")

        def validate_send_guard() -> None:
            if send_guard is not None and (reason := send_guard()):
                self._increment_health("commands_blocked")
                raise HomeAssistantError(reason)

        async with self._command_lock:
            if target in self._pending:
                if not coalesce:
                    self.health.increment("commands_blocked")
                    raise HomeAssistantError(
                        f"仍在等待主机确认: {self._pending[target].label}"
                    )
                pending = self._pending[target]
                replacement = QueuedCommand(
                    label, target, expected, mac, command, value
                )
                queued = coalesce_latest(
                    pending, self._queued.get(target), replacement
                )
                if queued is None:
                    self._queued.pop(target, None)
                    self.last_command_status = f"waiting:{pending.label}"
                else:
                    self._queued[target] = queued
                    self.last_command_status = f"queued:{label}"
                    self.health.increment("commands_coalesced")
                self._notify()
                return
            now = time.monotonic()
            if self._last_command_at is not None:
                remaining = self.command_min_interval - (now - self._last_command_at)
                if remaining > 0:
                    await asyncio.sleep(remaining)
            validate_send_guard()
            now = time.monotonic()
            pending = PendingCommand(
                label,
                target,
                expected,
                now,
                now + self.command_confirmation_timeout,
                now
                + min(
                    STATUS_POLL_INTERVAL,
                    max(0.5, self.command_confirmation_timeout / 2),
                ),
                mac,
                command,
                value,
            )
            self._pending[target] = pending
            self.last_command_status = f"waiting:{label}"
            self._notify()
            try:
                if send_guard is None:
                    await self._client.send_command(mac, command, value)
                else:
                    await self._client.send_command(
                        mac,
                        command,
                        value,
                        before_write=validate_send_guard,
                    )
                self.health.increment("commands_sent")
                self._last_command_at = time.monotonic()
                await self._client.request_status()
            except Exception:
                self._pending.pop(target, None)
                self.last_command_status = f"failed:{label}"
                self._notify()
                raise

    async def _async_dispatch_queued(self) -> None:
        ready = [target for target in self._queued if target not in self._pending]
        if not ready:
            return
        queued = self._queued.pop(ready[0])
        try:
            await self._async_send_tracked(
                queued.target,
                queued.label,
                queued.expected,
                queued.mac,
                queued.command,
                queued.value,
                coalesce=True,
            )
        except HomeAssistantError as error:
            self.last_command_status = f"failed:{queued.label}"
            _LOGGER.warning("Queued command %s was not sent: %s", queued.label, error)
            self._notify()

    async def _async_poll_pending_status(self, now: float) -> None:
        """Request a fresh status report while commands await confirmation."""
        due = [
            pending
            for pending in self._pending.values()
            if now >= pending.next_status_poll_at and now < pending.deadline
        ]
        if not due or self._client is None:
            return
        await self._client.request_status()
        for pending in due:
            pending.next_status_poll_at = now + STATUS_POLL_INTERVAL
        _LOGGER.debug(
            "Requested MC7021 status for pending confirmations: targets=%s",
            [pending.target for pending in due],
        )

    def _confirm_pending(self, target: str, actual: dict[str, str | None]) -> None:
        pending = self._pending.get(target)
        if pending is None or not all(
            actual.get(key) == value for key, value in pending.expected.items()
        ):
            return
        self._pending.pop(target, None)
        self.last_command_status = f"confirmed:{pending.label}"
        self.health.increment("commands_confirmed")
        self.health.record_confirmation_latency(time.monotonic() - pending.sent_at)

    async def _async_expire_pending(self, now: float) -> None:
        """Advance queued commands, retry setpoints once, or report final failure."""
        expired = [
            target
            for target, pending in self._pending.items()
            if now >= pending.deadline
        ]
        for target in expired:
            async with self._command_lock:
                pending = self._pending.get(target)
                if pending is None or now < pending.deadline:
                    continue
                if target in self._queued:
                    self._pending.pop(target, None)
                    self.health.increment("commands_timed_out")
                    self.last_command_status = f"timeout_continuing:{pending.label}"
                    _LOGGER.warning(
                        "Command confirmation timed out; continuing latest queued "
                        "value: label=%s target=%s expected=%s waited=%.1fs",
                        pending.label,
                        target,
                        pending.expected,
                        now - pending.sent_at,
                    )
                elif temperature_retry_is_allowed(pending):
                    self.health.increment("commands_timed_out")
                    await self._async_retry_temperature_command(pending)
                else:
                    self._pending.pop(target, None)
                    self.health.increment("commands_timed_out")
                    self.last_command_status = f"timeout:{pending.label}"
                    _LOGGER.error(
                        "MC7021 command confirmation timed out: label=%s target=%s "
                        "expected=%s attempts=%d waited=%.1fs",
                        pending.label,
                        target,
                        pending.expected,
                        pending.attempts,
                        now - pending.sent_at,
                    )
        if expired:
            self._notify()

    async def _async_retry_temperature_command(
        self, pending: PendingCommand
    ) -> None:
        """Retry one unconfirmed thermostat setpoint without reporting failure yet."""
        client = self._client
        if client is None or not self.available:
            raise ConnectionError("MC7021 session unavailable during command retry")
        now = time.monotonic()
        if self._last_command_at is not None:
            remaining = self.command_min_interval - (now - self._last_command_at)
            if remaining > 0:
                await asyncio.sleep(remaining)
        if self._pending.get(pending.target) is not pending:
            return
        retry_at = time.monotonic()
        pending.attempts += 1
        self.health.increment("commands_retried")
        pending.deadline = retry_at + self.command_confirmation_timeout
        pending.next_status_poll_at = retry_at + min(
            STATUS_POLL_INTERVAL,
            max(0.5, self.command_confirmation_timeout / 2),
        )
        self.last_command_status = f"retrying:{pending.label}"
        _LOGGER.warning(
            "Thermostat command confirmation delayed; retrying once: "
            "label=%s target=%s expected=%s attempt=%d",
            pending.label,
            pending.target,
            pending.expected,
            pending.attempts,
        )
        try:
            await client.send_command(pending.mac, pending.command, pending.value)
            self.health.increment("commands_sent")
            self._last_command_at = time.monotonic()
            await client.request_status()
        except Exception:
            if self._pending.get(pending.target) is pending:
                self._pending.pop(pending.target, None)
                self.last_command_status = f"failed:{pending.label}"
            raise

    async def _async_frame_received(self, frame: YasHcpFrame) -> None:
        if frame.kind != 3 or frame.opcode != 8:
            return
        room_id = ""
        changed = False
        for tag, value in iter_tlvs(frame.body):
            if tag == 0x0030:
                room_id = decode_text(value)
            elif tag == 0x0036 and room_id:
                name = decode_text(value)
                if await self.panel_registry.async_set_room_name(room_id, name):
                    self.room_names[room_id] = name
                    changed = True
        if changed:
            self._notify()

    async def _async_status_received(self, body: bytes) -> None:
        changed = False
        now_utc = datetime.now(UTC)
        total = decode_tech_system_status(body, self.tech_system_mac)
        if total:
            self.protocol_verified = True
            self.protocol_status = "verified"
            for name, value in total.items():
                if getattr(self.state, name) != value:
                    setattr(self.state, name, value)
                    changed = True
            self._confirm_pending(
                "system",
                {
                    "power": self.state.power,
                    "mode": self.state.mode,
                    "scene": self.state.scene,
                    "winter_humidifier": self.state.winter_humidifier,
                },
            )
        thermostat = decode_thermostat_status(body, self.tech_system_mac)
        valid_status = bool(total) or thermostat is not None
        if (
            valid_status
            and self.connected
            and self.health.stage is ConnectionStage.READY
        ):
            await self.panel_registry.async_note_status_stream(now_utc)
        elif not valid_status:
            await self.panel_registry.async_pause_monitoring(now_utc)
        if thermostat is not None:
            mac_hex = thermostat.mac.hex()
            previous = self.thermostats.get(mac_hex)
            await self.panel_registry.async_note_panel_report(
                mac_hex, thermostat.room_id, now_utc
            )
            reported_temperature = thermostat.current_temperature
            reported_humidity = thermostat.humidity
            measurements_valid = preserve_valid_thermostat_measurements(
                thermostat, previous
            )
            if not measurements_valid:
                self.health.increment("invalid_measurements")
                _LOGGER.debug(
                    "Ignored implausible thermostat measurements: mac=%s "
                    "temperature=%s humidity=%s",
                    mac_hex,
                    reported_temperature,
                    reported_humidity,
                )
            self.thermostats[mac_hex] = thermostat
            if measurements_valid:
                self._update_filtered(thermostat)
            self._confirm_pending(
                f"thermostat_{mac_hex}",
                {
                    "target_temperature": f"{thermostat.target_temperature:g}",
                    "power": thermostat.power,
                },
            )
            changed = True
        if not total and thermostat is None:
            self.health.increment("ignored_statuses")
        if total or changed:
            self._notify()

    def _update_filtered(self, thermostat: ThermostatState) -> None:
        if thermostat.current_temperature is None or thermostat.humidity is None:
            return
        mac_hex = thermostat.mac.hex()
        values = self.filtered.get(mac_hex)
        if values is None:
            values = FilteredMeasurements(deque(maxlen=3), deque(maxlen=3))
            self.filtered[mac_hex] = values
        values.temperatures.append(thermostat.current_temperature)
        values.humidities.append(thermostat.humidity)
        temperature = round(statistics.median(values.temperatures), 1)
        humidity = round(statistics.median(values.humidities))
        if values.temperature is None or abs(temperature - values.temperature) >= 0.2:
            values.temperature = temperature
        if values.humidity is None or abs(humidity - values.humidity) >= 2:
            values.humidity = humidity

    def _refresh_thermostat_availability(self, now: float) -> None:
        if self.thermostat_offline_after == 0:
            return
        changed = False
        for thermostat in self.thermostats.values():
            if (
                thermostat.available
                and now - thermostat.last_seen >= self.thermostat_offline_after
            ):
                thermostat.available = False
                changed = True
        if changed:
            self._notify()

    async def _async_restore_panels(self) -> None:
        await self.panel_registry.async_load()
        self.room_names.update(self.panel_registry.room_names)
        for record in self.panel_registry.records.values():
            mac = parse_device_mac(record.mac_hex)
            self.thermostats[mac.hex()] = ThermostatState(
                mac=mac,
                room_id=record.room_id,
                available=False,
            )
