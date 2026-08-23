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


if __name__ == "__main__":
    unittest.main()
