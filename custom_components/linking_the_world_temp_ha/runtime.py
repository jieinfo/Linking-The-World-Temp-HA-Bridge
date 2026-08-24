"""Typed runtime state for Linking The World Temp HA."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, TypeAlias

from homeassistant.config_entries import ConfigEntry

if TYPE_CHECKING:
    from .health import HealthTracker
    from .hub import LinkingTempHub
    from .panel_registry import PanelRegistry


class ConnectionStage(StrEnum):
    """Observable controller connection lifecycle stages."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    HANDSHAKING = "handshaking"
    AUTHENTICATING = "authenticating"
    READY = "ready"


class FailureKind(StrEnum):
    """Privacy-safe controller failure classifications."""

    NONE = "none"
    TCP_REFUSED = "tcp_refused"
    TCP_TIMEOUT = "tcp_timeout"
    HANDSHAKE = "handshake_failed"
    LOGIN_TIMEOUT = "login_timeout"
    AUTH_REJECTED = "authentication_rejected"
    PROTOCOL = "protocol_incompatible"
    STATUS_SILENCE = "status_silence"


@dataclass(slots=True)
class LinkingTempRuntime:
    """Objects shared by every platform for one config entry."""

    hub: LinkingTempHub
    health: HealthTracker
    panel_registry: PanelRegistry


LinkingTempConfigEntry: TypeAlias = ConfigEntry[LinkingTempRuntime]
