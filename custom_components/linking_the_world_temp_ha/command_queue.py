"""Command confirmation and latest-value coalescing helpers."""

from __future__ import annotations

from dataclasses import dataclass

STATUS_POLL_INTERVAL = 2.0
MAX_TEMPERATURE_COMMAND_ATTEMPTS = 2


@dataclass
class PendingCommand:
    """A sent command awaiting a matching state report."""

    label: str
    target: str
    expected: dict[str, str]
    sent_at: float
    deadline: float
    next_status_poll_at: float
    mac: bytes
    command: int
    value: int | None
    attempts: int = 1


@dataclass(frozen=True)
class QueuedCommand:
    """The final command retained while another command awaits confirmation."""

    label: str
    target: str
    expected: dict[str, str]
    mac: bytes
    command: int
    value: int | None


def coalesce_latest(
    pending: PendingCommand,
    queued: QueuedCommand | None,
    replacement: QueuedCommand,
) -> QueuedCommand | None:
    """Keep only the final distinct value until the sent command is resolved."""
    if replacement.expected == pending.expected:
        return None
    if queued is not None and replacement.expected == queued.expected:
        return queued
    return replacement


def temperature_retry_is_allowed(pending: PendingCommand) -> bool:
    """Return whether an idempotent thermostat setpoint may be sent once more."""
    return (
        pending.target.startswith("thermostat_")
        and "target_temperature" in pending.expected
        and pending.value is not None
        and pending.attempts < MAX_TEMPERATURE_COMMAND_ATTEMPTS
    )
