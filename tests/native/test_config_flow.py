"""Tests for the native integration config flow."""

from __future__ import annotations

import unittest

try:
    from homeassistant.helpers import config_validation as cv
    from voluptuous_serialize import convert

    from custom_components.linking_the_world_temp_ha.config_flow import (
        _connection_schema,
        _normalize_connection_data,
    )
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
                with self.assertRaises(ValueError):
                    _normalize_connection_data(data)


if __name__ == "__main__":
    unittest.main()
