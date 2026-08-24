"""Tests for the native integration config flow."""

from __future__ import annotations

from time import monotonic
import unittest

import pytest

try:
    import voluptuous as vol
    from homeassistant.helpers import config_validation as cv
    from voluptuous_serialize import convert

    from custom_components.linking_the_world_temp_ha.config_flow import (
        _connection_schema,
        _normalize_connection_data,
    )
    from custom_components.linking_the_world_temp_ha.const import DOMAIN
    from custom_components.linking_the_world_temp_ha.protocol import (
        AuthenticationRejected,
        HandshakeTimeout,
        IncompatibleProtocol,
        LoginTimeout,
        TcpConnectError,
    )
    from tests.helpers import FakeControllerBehavior, FakeMC7021Server
except ImportError:
    convert = None


@unittest.skipUnless(convert is not None, "Home Assistant test runtime is unavailable")
class ConfigFlowTest(unittest.TestCase):
    """Verify frontend serialization and submitted-value validation."""

    def test_connection_schema_can_be_serialized_for_frontend(self) -> None:
        """The config form must be serializable by Home Assistant's frontend API."""
        self.assertTrue(
            convert(_connection_schema(), custom_serializer=cv.custom_serializer)
        )

    def test_connection_data_is_normalized_after_submission(self) -> None:
        """Strict validation remains in place after using serializable form fields."""
        normalized = _normalize_connection_data(
            {
                "host": " 192.168.10.246 ",
                "port": 9000,
                "username": "admin",
                "password": "secret",
                "client_id": "FF9549D5891998E5",
                "tech_system_mac": "FF:00:FF:FF:FF:FF:00:FF",
            }
        )

        self.assertEqual(normalized["host"], "192.168.10.246")
        self.assertEqual(normalized["client_id"], "ff9549d5891998e5")
        self.assertEqual(normalized["tech_system_mac"], "ff00ffffffff00ff")

    def test_invalid_connection_data_is_rejected(self) -> None:
        """Submitted connection values still receive strict validation."""
        valid = {
            "host": "192.168.10.246",
            "port": 9000,
            "username": "admin",
            "password": "secret",
            "client_id": "ff9549d5891998e5",
            "tech_system_mac": "ff00ffffffff00ff",
        }
        for field, value in (
            ("host", "not a host"),
            ("client_id", "invalid"),
            ("tech_system_mac", "invalid"),
        ):
            with self.subTest(field=field):
                data = dict(valid)
                data[field] = value
                with self.assertRaises((ValueError, vol.Invalid)):
                    _normalize_connection_data(data)


if __name__ == "__main__":
    unittest.main()


pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


def _connection_data(host: str = "192.168.10.246") -> dict[str, object]:
    return {
        "host": host,
        "port": 9000,
        "username": "admin",
        "password": "secret",
        "client_id": "ff9549d5891998e5",
        "tech_system_mac": "ff00ffffffff00ff",
    }


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (TcpConnectError("refused"), "cannot_connect"),
        (HandshakeTimeout("hello timed out"), "handshake_failed"),
        (LoginTimeout("login timed out"), "login_timeout"),
        (AuthenticationRejected("rejected"), "invalid_auth"),
        (IncompatibleProtocol("unknown reply"), "protocol_incompatible"),
    ],
)
async def test_user_flow_maps_typed_connection_errors(
    hass, monkeypatch, error, expected
) -> None:
    """Every expected transport failure has a stable localized flow error."""
    import custom_components.linking_the_world_temp_ha.config_flow as config_flow

    async def fail_validation(_data) -> None:
        raise error

    monkeypatch.setattr(config_flow, "_async_validate_connection", fail_validation)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}, data=_connection_data()
    )

    assert result["type"] == "form"
    assert result["errors"] == {"base": expected}


async def test_user_flow_rejects_captured_wrong_password_immediately(
    hass, monkeypatch
) -> None:
    """An explicit controller rejection must not wait for the login timeout."""
    import custom_components.linking_the_world_temp_ha.config_flow as config_flow

    async def reject_password(_data) -> None:
        raise AuthenticationRejected("MC7021 rejected the supplied credentials")

    monkeypatch.setattr(config_flow, "_async_validate_connection", reject_password)
    started = monotonic()
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}, data=_connection_data()
    )

    assert monotonic() - started < 0.5
    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_auth"}


async def test_user_flow_uses_the_captured_rejection_without_waiting_for_timeout(
    hass, socket_enabled,
) -> None:
    """The packet-proven kind=2/opcode=5 rejection maps straight to invalid_auth."""
    from custom_components.linking_the_world_temp_ha.protocol import YasHcpFrame, tlv

    server = FakeMC7021Server(
        FakeControllerBehavior(
            login_reply=YasHcpFrame(2, 5, 0, tlv(0x031C, b"\x01")).encode(),
            close_after_stage="login",
        )
    )
    await server.async_start()
    try:
        data = _connection_data(server.host)
        data["port"] = server.port
        started = monotonic()
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}, data=data
        )
        assert monotonic() - started < 0.5
        assert result["type"] == "form"
        assert result["errors"] == {"base": "invalid_auth"}
    finally:
        await server.async_stop()


async def test_reauth_only_requests_credentials_and_updates_same_entry(
    hass, mock_config_entry, monkeypatch
) -> None:
    """Reauth validates credentials then reloads the existing entry in place."""
    import custom_components.linking_the_world_temp_ha.config_flow as config_flow

    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry,
        unique_id=f"{mock_config_entry.data['host']}:{mock_config_entry.data['port']}",
    )
    calls: list[dict[str, object]] = []

    async def validate(data) -> None:
        calls.append(dict(data))

    monkeypatch.setattr(config_flow, "_async_validate_connection", validate)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "reauth", "entry_id": mock_config_entry.entry_id},
        data=dict(mock_config_entry.data),
    )

    assert result["type"] == "form"
    assert result["step_id"] == "reauth_confirm"
    assert set(result["data_schema"].schema) == {"username", "password"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={"username": "new-admin", "password": "new-secret"},
    )

    assert result["type"] == "abort"
    assert result["reason"] == "reauth_successful"
    assert mock_config_entry.data["username"] == "new-admin"
    assert mock_config_entry.data["password"] == "new-secret"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1
    assert calls == [
        {
            **dict(mock_config_entry.data),
            "username": "new-admin",
            "password": "new-secret",
        }
    ]


async def test_reauth_rejection_keeps_existing_credentials_and_shows_error(
    hass, mock_config_entry, monkeypatch
) -> None:
    """A failed reauth form stays editable without mutating the config entry."""
    import custom_components.linking_the_world_temp_ha.config_flow as config_flow

    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry,
        unique_id=f"{mock_config_entry.data['host']}:{mock_config_entry.data['port']}",
    )
    original = dict(mock_config_entry.data)

    async def reject(_data) -> None:
        raise AuthenticationRejected("rejected")

    monkeypatch.setattr(config_flow, "_async_validate_connection", reject)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "reauth", "entry_id": mock_config_entry.entry_id},
        data=original,
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"username": "wrong", "password": "wrong"}
    )

    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_auth"}
    assert dict(mock_config_entry.data) == original

    defaults = {
        key.schema: key.default()
        for key in result["data_schema"].schema
    }
    assert defaults == {"username": "wrong", "password": "wrong"}


async def test_reauth_updates_once_and_uses_the_entry_update_listener(
    hass, mock_config_entry, monkeypatch
) -> None:
    """A real reauth flow updates once; the registered listener owns reload."""
    import custom_components.linking_the_world_temp_ha.config_flow as config_flow

    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry,
        unique_id=f"{mock_config_entry.data['host']}:{mock_config_entry.data['port']}",
    )
    updates: list[object] = []

    async def update_listener(_hass, entry) -> None:
        updates.append(entry)

    mock_config_entry.add_update_listener(update_listener)

    async def validate(_data) -> None:
        return None

    monkeypatch.setattr(config_flow, "_async_validate_connection", validate)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "reauth", "entry_id": mock_config_entry.entry_id},
        data=dict(mock_config_entry.data),
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={"username": "new-admin", "password": "new-secret"},
    )
    await hass.async_block_till_done()

    assert result["type"] == "abort"
    assert result["reason"] == "reauth_successful"
    assert updates == [mock_config_entry]
