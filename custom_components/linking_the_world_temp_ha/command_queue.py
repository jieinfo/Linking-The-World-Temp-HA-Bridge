"""Command confirmation and latest-value coalescing helpers."""

from __future__ import annotations

from dataclasses import dataclass, replace

SUPERSEDED_DEBOUNCE = 0.75
SUPERSEDED_MAX_WAIT = 3.0
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
    """The latest command requested while another command is pending."""

    label: str
    target: str
    expected: dict[str, str]
    mac: bytes
    command: int
    value: int | None
    first_queued_at: float = 0.0
    last_queued_at: float = 0.0
    promote_at: float = 0.0


def coalesce_latest(
    pending: PendingCommand,
    queued: QueuedCommand | None,
    replacement: QueuedCommand,
    now: float,
) -> QueuedCommand | None:
    """Keep only the latest distinct value and calculate when it may be sent."""
    if replacement.expected == pending.expected:
        return None
    if queued is not None and replacement.expected == queued.expected:
        return queued
    first_queued_at = queued.first_queued_at if queued is not None else now
    return replace(
        replacement,
        first_queued_at=first_queued_at,
        last_queued_at=now,
        promote_at=min(
            now + SUPERSEDED_DEBOUNCE,
            first_queued_at + SUPERSEDED_MAX_WAIT,
        ),
    )


def replacement_is_ready(
    pending: PendingCommand, queued: QueuedCommand, now: float
) -> bool:
    """Return whether the latest value should replace the pending intermediate value."""
    return now >= min(pending.deadline, queued.promote_at)


def temperature_retry_is_allowed(pending: PendingCommand) -> bool:
    """Return whether an idempotent thermostat setpoint may be sent once more."""
    return (
        pending.target.startswith("thermostat_")
        and "target_temperature" in pending.expected
        and pending.value is not None
        and pending.attempts < MAX_TEMPERATURE_COMMAND_ATTEMPTS
    )
