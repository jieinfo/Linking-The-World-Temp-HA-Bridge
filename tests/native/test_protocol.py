"""Pure protocol regression tests without requiring Home Assistant at runtime."""

from __future__ import annotations

import importlib
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGE_NAME = "custom_components.linking_the_world_temp_ha"
if PACKAGE_NAME not in sys.modules:
    package = types.ModuleType(PACKAGE_NAME)
    package.__path__ = [str(ROOT / "custom_components" / "linking_the_world_temp_ha")]
    sys.modules[PACKAGE_NAME] = package

protocol = importlib.import_module(f"{PACKAGE_NAME}.protocol")


class DecoderTests(unittest.TestCase):
    def test_fragmented_frame_round_trip(self) -> None:
        frame = protocol.YasHcpFrame(5, 12, 42, b"payload")
        encoded = frame.encode()
        decoder = protocol.YasHcpDecoder()
        self.assertEqual(decoder.feed(encoded[:7]), [])
        self.assertEqual(decoder.feed(encoded[7:]), [frame])

    def test_stream_resynchronizes_after_noise(self) -> None:
        frame = protocol.YasHcpFrame(1, 3, 0, b"")
        decoder = protocol.YasHcpDecoder()
        self.assertEqual(decoder.feed(b"noise#bad" + frame.encode()), [frame])


class StatusTests(unittest.TestCase):
    def test_truncated_status_tlv_is_rejected_without_partial_decoding(self) -> None:
        mac = bytes.fromhex("ff00ffffffff00ff")
        body = protocol.tlv(0x0004, mac) + protocol.tlv(0x000B, b"\x01")
        body += b"\x0a\x00\x05\x00\x01"

        self.assertFalse(protocol.is_complete_tlv_body(body))
        self.assertEqual(protocol.decode_tech_system_status(body, mac), {})

    def test_total_control_status(self) -> None:
        mac = bytes.fromhex("ff00ffffffff00ff")
        body = protocol.tlv(0x0004, mac)
        body += protocol.tlv(0x000B, b"\x01")
        body += protocol.tlv(0x000A, bytes((1, 1, 0)))
        self.assertEqual(
            protocol.decode_tech_system_status(body, mac),
            {
                "power": "ON",
                "mode": "cool",
                "scene": "home",
                "winter_humidifier": "OFF",
            },
        )

    def test_room_panel_status(self) -> None:
        total_mac = bytes.fromhex("ff00ffffffff00ff")
        panel_mac = bytes.fromhex("ff00ffffffff01ff")
        body = protocol.tlv(0x0004, panel_mac)
        body += protocol.tlv(0x0075, total_mac)
        body += protocol.tlv(0x0030, b"r1100")
        body += protocol.tlv(0x000A, bytes((44, 0x1A, 0x01, 63, 0)))
        body += protocol.tlv(0x000B, b"\x01")
        state = protocol.decode_thermostat_status(body, total_mac)
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state.mac, panel_mac)
        self.assertEqual(state.target_temperature, 22)
        self.assertEqual(state.current_temperature, 28.2)
        self.assertEqual(state.humidity, 63)
        self.assertEqual(state.power, "ON")

    def test_placeholder_measurements_preserve_the_last_valid_values(self) -> None:
        total_mac = bytes.fromhex("ff00ffffffff00ff")
        panel_mac = bytes.fromhex("ff00ffffffff01ff")
        body = protocol.tlv(0x0004, panel_mac)
        body += protocol.tlv(0x0075, total_mac)
        body += protocol.tlv(0x0030, b"r1100")
        body += protocol.tlv(0x000A, bytes((46, 0xE8, 0x03, 100, 0)))
        body += protocol.tlv(0x000B, b"\x01")
        current = protocol.decode_thermostat_status(body, total_mac)
        assert current is not None
        previous = protocol.ThermostatState(
            mac=panel_mac,
            room_id="r1100",
            target_temperature=24,
            current_temperature=29.5,
            power="ON",
            humidity=62,
            available=True,
        )

        self.assertFalse(
            protocol.preserve_valid_thermostat_measurements(current, previous)
        )
        self.assertEqual(current.target_temperature, 23)
        self.assertEqual(current.power, "ON")
        self.assertEqual(current.current_temperature, 29.5)
        self.assertEqual(current.humidity, 62)

    def test_invalid_first_measurement_remains_unknown(self) -> None:
        current = protocol.ThermostatState(
            mac=bytes.fromhex("ff00ffffffff01ff"),
            room_id="r1100",
            current_temperature=100,
            humidity=100,
            available=True,
        )
        self.assertFalse(
            protocol.preserve_valid_thermostat_measurements(current, None)
        )
        self.assertIsNone(current.current_temperature)
        self.assertIsNone(current.humidity)


if __name__ == "__main__":
    unittest.main()
