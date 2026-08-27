"""Operating policy shared by room thermostat entities and commands."""

from __future__ import annotations

ROOM_THERMOSTAT_ACTIVE_MODES = frozenset(("cool", "heat"))


def can_operate_room_thermostat(
    system_power: str | None, system_mode: str | None
) -> bool:
    """Return whether the original controller allows room panels to run."""
    return (
        system_power in ("ON", "OFF")
        and system_mode in ROOM_THERMOSTAT_ACTIVE_MODES
    )


def room_thermostat_block_reason(
    system_power: str | None, system_mode: str | None
) -> str | None:
    """Return a user-facing reason when a room panel cannot be enabled."""
    if system_power not in ("ON", "OFF"):
        return "科技系统总开关状态尚未确认"
    if system_mode == "ventilation":
        return "当前为通风模式，房间温控面板由科技系统总控强制关闭"
    if system_mode == "dehumidify":
        return "当前为除湿模式，房间温控面板由科技系统总控强制关闭"
    if system_mode not in ROOM_THERMOSTAT_ACTIVE_MODES:
        return "当前运行模式不支持开启房间温控面板"
    return None
