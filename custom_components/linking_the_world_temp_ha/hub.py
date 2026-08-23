"""Connection and state coordinator for Linking The World Temp HA."""

from __future__ import annotations

import asyncio
import logging
import statistics
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store

from .command_queue import (
    STATUS_POLL_INTERVAL,
    PendingCommand,
    QueuedCommand,
    coalesce_latest,
    replacement_is_ready,
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
    CannotConnect,
    TechSystemState,
    ThermostatState,
    YasHcpFrame,
    decode_tech_system_status,
    decode_text,
    decode_thermostat_status,
    iter_tlvs,
    parse_device_mac,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class FilteredMeasurements:
    """Smoothed values intended for automations."""

    temperatures: deque[float]
    humidities: deque[int]
    temperature: float | None = None
    humidity: int | None = None


class LinkingTempHub:
    """Own the controller session and expose push state to HA entities."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
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
        self.filtered: dict[str, FilteredMeasurements] = {}
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
        self._store: Store[dict[str, Any]] = Store(
            hass, 1, f"{DOMAIN}.{entry.entry_id}.panels"
        )

    async def async_start(self) -> None:
        """Restore known panels and start the supervised TCP session."""
        await self._async_restore_panels()
        self._runner = self.entry.async_create_background_task(
            self.hass,
            self._async_run(),
            f"{DOMAIN}_{self.entry.entry_id}",
        )

    async def async_stop(self) -> None:
        """Stop the session and mark all state unavailable."""
        self._stop.set()
        if self._runner is not None:
            self._runner.cancel()
            try:
                await self._runner
            except asyncio.CancelledError:
                pass
            self._runner = None
        await self._async_disconnect()

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
        return self.connected and self.protocol_verified

    @property
    def control_permission(self) -> str:
        if not self.allow_control:
            return "read_only"
        if not self.connected:
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
        )

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
                self._client = client
                await client.connect()
                self.connected = True
                self.last_connection_error = "none"
                retry_delay = 5
                self._notify()
                await self._async_session_loop(client)
                if client.reader_error is not None:
                    raise ConnectionError(str(client.reader_error))
                raise ConnectionError("MC7021 TCP reader stopped")
            except asyncio.CancelledError:
                raise
            except (CannotConnect, ConnectionError, OSError, TimeoutError) as error:
                self.last_connection_error = str(error) or error.__class__.__name__
                _LOGGER.warning(
                    "MC7021 session unavailable: %s; retrying in %ss",
                    error,
                    retry_delay,
                )
            finally:
                await self._async_disconnect()
            try:
                await asyncio.wait_for(self._stop.wait(), retry_delay)
            except asyncio.TimeoutError:
                pass
            retry_delay = min(30, retry_delay * 2)

    async def _async_session_loop(self, client: AsyncMoorgenClient) -> None:
        heartbeat_at = 0.0
        availability_at = 0.0
        while not self._stop.is_set() and client.reader_alive:
            now = time.monotonic()
            if now - client.last_received_at >= self.controller_silence_timeout:
                raise ConnectionError(
                    f"MC7021 has been silent for {now - client.last_received_at:.0f} seconds"
                )
            self._promote_superseded(now)
            await self._async_poll_pending_status(now)
            self._expire_pending(now)
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
        self.connected = False
        self.protocol_verified = False
        self.protocol_status = "disconnected"
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
    ) -> None:
        if not self.allow_control:
            raise HomeAssistantError("集成当前处于只读模式")
        if not self.available or self._client is None:
            raise HomeAssistantError("主机尚未连接或协议状态尚未验证")
        async with self._command_lock:
            if target in self._pending:
                if not coalesce:
                    raise HomeAssistantError(
                        f"仍在等待主机确认: {self._pending[target].label}"
                    )
                now = time.monotonic()
                pending = self._pending[target]
                replacement = QueuedCommand(
                    label, target, expected, mac, command, value
                )
                queued = coalesce_latest(
                    pending, self._queued.get(target), replacement, now
                )
                if queued is None:
                    self._queued.pop(target, None)
                    self.last_command_status = f"waiting:{pending.label}"
                else:
                    self._queued[target] = queued
                    pending.next_status_poll_at = min(
                        pending.next_status_poll_at, now + 0.5
                    )
                    self.last_command_status = f"queued:{label}"
                self._notify()
                return
            now = time.monotonic()
            if self._last_command_at is not None:
                remaining = self.command_min_interval - (now - self._last_command_at)
                if remaining > 0:
                    await asyncio.sleep(remaining)
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
                await self._client.send_command(mac, command, value)
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

    def _promote_superseded(self, now: float) -> None:
        """Release superseded intermediate values after a short quiet period."""
        ready = [
            target
            for target, queued in self._queued.items()
            if (pending := self._pending.get(target)) is not None
            and queued.promote_at < pending.deadline
            and replacement_is_ready(pending, queued, now)
        ]
        for target in ready:
            pending = self._pending.pop(target)
            queued = self._queued[target]
            self.last_command_status = f"superseded:{pending.label}"
            _LOGGER.info(
                "Superseded intermediate command; dispatching latest value: "
                "label=%s target=%s previous=%s latest=%s waited=%.1fs",
                pending.label,
                target,
                pending.expected,
                queued.expected,
                now - pending.sent_at,
            )
        if ready:
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
        queued = self._queued.get(target)
        if queued is not None and all(
            actual.get(key) == value for key, value in queued.expected.items()
        ):
            self._pending.pop(target, None)
            self._queued.pop(target, None)
            self.last_command_status = f"confirmed:{queued.label}"
            return
        pending = self._pending.get(target)
        if pending is None or not all(
            actual.get(key) == value for key, value in pending.expected.items()
        ):
            return
        self._pending.pop(target, None)
        self.last_command_status = f"confirmed:{pending.label}"

    def _expire_pending(self, now: float) -> None:
        expired = [
            target
            for target, pending in self._pending.items()
            if now >= pending.deadline
        ]
        for target in expired:
            pending = self._pending.pop(target)
            if target in self._queued:
                self.last_command_status = f"timeout_continuing:{pending.label}"
                _LOGGER.warning(
                    "Command confirmation timed out; continuing latest queued value: "
                    "label=%s target=%s expected=%s waited=%.1fs",
                    pending.label,
                    target,
                    pending.expected,
                    now - pending.sent_at,
                )
            else:
                self.last_command_status = f"timeout:{pending.label}"
                _LOGGER.error(
                    "MC7021 command confirmation timed out: label=%s target=%s "
                    "expected=%s waited=%.1fs",
                    pending.label,
                    target,
                    pending.expected,
                    now - pending.sent_at,
                )
        if expired:
            self._notify()

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
                if self.room_names.get(room_id) != name:
                    self.room_names[room_id] = name
                    changed = True
        if changed:
            await self._async_save_panels()
            self._notify()

    async def _async_status_received(self, body: bytes) -> None:
        changed = False
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
        if thermostat is not None:
            mac_hex = thermostat.mac.hex()
            is_new = mac_hex not in self.thermostats
            self.thermostats[mac_hex] = thermostat
            self._update_filtered(thermostat)
            self._confirm_pending(
                f"thermostat_{mac_hex}",
                {
                    "target_temperature": f"{thermostat.target_temperature:g}",
                    "power": thermostat.power,
                },
            )
            changed = True
            if is_new:
                await self._async_save_panels()
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
        stored = await self._store.async_load() or {}
        self.room_names.update(stored.get("rooms", {}))
        for item in stored.get("panels", []):
            try:
                mac = parse_device_mac(item["mac"])
            except (KeyError, ValueError):
                continue
            self.thermostats[mac.hex()] = ThermostatState(
                mac=mac,
                room_id=item.get("room_id", ""),
                available=False,
            )

    async def _async_save_panels(self) -> None:
        await self._store.async_save(
            {
                "rooms": self.room_names,
                "panels": [
                    {"mac": thermostat.mac.hex(), "room_id": thermostat.room_id}
                    for thermostat in self.thermostats.values()
                ],
            }
        )
