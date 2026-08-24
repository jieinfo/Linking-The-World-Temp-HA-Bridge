"""Config flow for Linking The World Temp HA."""

from __future__ import annotations

import ipaddress
import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.helpers import config_validation as cv

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
    DEFAULT_PORT,
    DEFAULT_TECH_SYSTEM_MAC,
    DEFAULT_THERMOSTAT_OFFLINE_AFTER,
    DEFAULT_USERNAME,
    DOMAIN,
)
from .protocol import (
    AsyncMoorgenClient,
    AuthenticationRejected,
    CannotConnect,
    HandshakeTimeout,
    IncompatibleProtocol,
    LoginTimeout,
    TcpConnectError,
    parse_device_mac,
)

_LOGGER = logging.getLogger(__name__)


def _validate_host(host: str) -> str:
    host = host.strip()
    if not host:
        raise vol.Invalid("host is empty")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if any(char.isspace() for char in host) or "." not in host:
            raise vol.Invalid("invalid host")
    return host


def _validate_client_id(value: str) -> str:
    value = value.strip().lower()
    if len(value) != 16 or any(char not in "0123456789abcdef" for char in value):
        raise vol.Invalid("client_id must contain 16 hexadecimal characters")
    return value


def _validate_mac(value: str) -> str:
    return parse_device_mac(value).hex()


def _normalize_connection_data(data: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize fields after the frontend submits the form."""
    normalized = dict(data)
    normalized[CONF_HOST] = _validate_host(normalized[CONF_HOST])
    normalized[CONF_CLIENT_ID] = _validate_client_id(normalized[CONF_CLIENT_ID])
    normalized[CONF_TECH_SYSTEM_MAC] = _validate_mac(
        normalized[CONF_TECH_SYSTEM_MAC]
    )
    return normalized


def _connection_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_HOST, default=defaults.get(CONF_HOST, "")
            ): cv.string,
            vol.Required(
                CONF_PORT, default=defaults.get(CONF_PORT, DEFAULT_PORT)
            ): vol.All(vol.Coerce(int), vol.Range(min=1, max=65535)),
            vol.Required(
                CONF_USERNAME, default=defaults.get(CONF_USERNAME, DEFAULT_USERNAME)
            ): cv.string,
            vol.Required(
                CONF_PASSWORD, default=defaults.get(CONF_PASSWORD, "")
            ): cv.string,
            vol.Required(
                CONF_CLIENT_ID, default=defaults.get(CONF_CLIENT_ID, DEFAULT_CLIENT_ID)
            ): cv.string,
            vol.Required(
                CONF_TECH_SYSTEM_MAC,
                default=defaults.get(CONF_TECH_SYSTEM_MAC, DEFAULT_TECH_SYSTEM_MAC),
            ): cv.string,
        }
    )


async def _async_validate_connection(data: dict[str, Any]) -> None:
    client = AsyncMoorgenClient(
        data[CONF_HOST],
        data[CONF_PORT],
        data[CONF_USERNAME],
        data[CONF_PASSWORD],
        data[CONF_CLIENT_ID],
    )
    try:
        await client.connect()
    finally:
        await client.close()


def _connection_error_key(error: BaseException) -> str | None:
    """Map expected transport failures to stable config-flow translation keys."""
    if isinstance(error, AuthenticationRejected):
        return "invalid_auth"
    if isinstance(error, LoginTimeout):
        return "login_timeout"
    if isinstance(error, HandshakeTimeout):
        return "handshake_failed"
    if isinstance(error, IncompatibleProtocol):
        return "protocol_incompatible"
    if isinstance(error, (TcpConnectError, CannotConnect)):
        return "cannot_connect"
    return None


def _reauth_schema(defaults: Mapping[str, Any]) -> vol.Schema:
    """Reauthentication deliberately exposes credentials only."""
    return vol.Schema(
        {
            vol.Required(CONF_USERNAME, default=defaults.get(CONF_USERNAME, "")): cv.string,
            vol.Required(CONF_PASSWORD, default=""): cv.string,
        }
    )


class LinkingTempConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle native integration setup."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                normalized = _normalize_connection_data(user_input)
                await _async_validate_connection(normalized)
            except Exception as error:  # noqa: BLE001 - expected errors map below.
                if (error_key := _connection_error_key(error)) is not None:
                    errors["base"] = error_key
                elif isinstance(error, (ValueError, vol.Invalid)):
                    errors["base"] = "invalid_config"
                else:
                    _LOGGER.exception(
                        "Unexpected error while validating the MC7021 connection"
                    )
                    errors["base"] = "unknown"
            else:
                unique_id = f"{normalized[CONF_HOST]}:{normalized[CONF_PORT]}"
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"Linking The World Temp HA ({normalized[CONF_HOST]})",
                    data=normalized,
                )
        return self.async_show_form(
            step_id="user",
            data_schema=_connection_schema(user_input),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change controller addressing or credentials without removing entities."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                normalized = _normalize_connection_data(user_input)
                await _async_validate_connection(normalized)
            except Exception as error:  # noqa: BLE001 - expected errors map below.
                if (error_key := _connection_error_key(error)) is not None:
                    errors["base"] = error_key
                elif isinstance(error, (ValueError, vol.Invalid)):
                    errors["base"] = "invalid_config"
                else:
                    _LOGGER.exception(
                        "Unexpected error while reconfiguring the MC7021 connection"
                    )
                    errors["base"] = "unknown"
            else:
                unique_id = f"{normalized[CONF_HOST]}:{normalized[CONF_PORT]}"
                existing = await self.async_set_unique_id(
                    unique_id, raise_on_progress=False
                )
                if existing is not None and existing.entry_id != entry.entry_id:
                    return self.async_abort(reason="already_configured")
                return self.async_update_reload_and_abort(
                    entry,
                    title=f"Linking The World Temp HA ({normalized[CONF_HOST]})",
                    unique_id=unique_id,
                    data_updates=normalized,
                )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_connection_schema(user_input or dict(entry.data)),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Ask for replacement credentials after an explicit controller rejection."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Validate credentials, then update and reload the original entry."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            candidate = {**dict(entry.data), **user_input}
            try:
                await _async_validate_connection(candidate)
            except Exception as error:  # noqa: BLE001 - expected errors map below.
                if (error_key := _connection_error_key(error)) is not None:
                    errors["base"] = error_key
                else:
                    _LOGGER.exception("Unexpected error while reauthenticating MC7021")
                    errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(entry.unique_id)
                self._abort_if_unique_id_mismatch()
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_USERNAME: user_input[CONF_USERNAME],
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                    },
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=_reauth_schema({**dict(entry.data), **(user_input or {})}),
            errors=errors,
        )

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> LinkingOptionsFlow:
        return LinkingOptionsFlow()


class LinkingOptionsFlow(config_entries.OptionsFlow):
    """Edit runtime and safety settings."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_ALLOW_CONTROL,
                    default=options.get(CONF_ALLOW_CONTROL, DEFAULT_ALLOW_CONTROL),
                ): bool,
                vol.Required(
                    CONF_COMMAND_MIN_INTERVAL,
                    default=options.get(
                        CONF_COMMAND_MIN_INTERVAL, DEFAULT_COMMAND_MIN_INTERVAL
                    ),
                ): vol.All(vol.Coerce(float), vol.Range(min=0, max=30)),
                vol.Required(
                    CONF_COMMAND_CONFIRMATION_TIMEOUT,
                    default=options.get(
                        CONF_COMMAND_CONFIRMATION_TIMEOUT,
                        DEFAULT_COMMAND_CONFIRMATION_TIMEOUT,
                    ),
                ): vol.All(vol.Coerce(float), vol.Range(min=1, max=60)),
                vol.Required(
                    CONF_CONTROLLER_SILENCE_TIMEOUT,
                    default=options.get(
                        CONF_CONTROLLER_SILENCE_TIMEOUT,
                        DEFAULT_CONTROLLER_SILENCE_TIMEOUT,
                    ),
                ): vol.All(vol.Coerce(float), vol.Range(min=30, max=3600)),
                vol.Required(
                    CONF_THERMOSTAT_OFFLINE_AFTER,
                    default=options.get(
                        CONF_THERMOSTAT_OFFLINE_AFTER, DEFAULT_THERMOSTAT_OFFLINE_AFTER
                    ),
                ): vol.All(vol.Coerce(float), vol.Range(min=0, max=86400)),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
