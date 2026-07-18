import struct
import unittest

from bridge import (
    CLIENT_PUBLIC_KEY,
    COMMAND_MODE,
    COMMAND_POWER_ON,
    COMMAND_SCENE,
    COMMAND_WINTER_HUMIDIFIER,
    MODE_VALUES,
    SCENE_VALUES,
    TECH_SYSTEM_MAC,
    TechSystemState,
    YasHcpDecoder,
    YasHcpFrame,
    tlv,
)


class ProtocolTests(unittest.TestCase):
    def test_yashcp_round_trip(self):
        source = YasHcpFrame(4, 9, 37, b"example").encode()
        decoded = YasHcpDecoder().feed(source)
        self.assertEqual(decoded, [YasHcpFrame(4, 9, 37, b"example")])

    def test_captured_tech_system_command_shape(self):
        body = tlv(0x0010, b"\x01") + tlv(0x0004, TECH_SYSTEM_MAC)
        body += tlv(0x0009, bytes((COMMAND_MODE,))) + tlv(0x000A, bytes((MODE_VALUES["heat"],)))
        self.assertEqual(
            body.hex(),
            "100001000104000800ff00ffffffff00ff09000100030a00010002",
        )

    def test_captured_command_values(self):
        self.assertEqual(MODE_VALUES["cool"], 1)
        self.assertEqual(COMMAND_POWER_ON, 2)
        self.assertEqual(SCENE_VALUES["away"], 0)
        self.assertEqual(COMMAND_SCENE, 4)
        self.assertEqual(COMMAND_WINTER_HUMIDIFIER, 5)

    def test_captured_hello_body_length(self):
        body = bytes.fromhex("12020f01") + CLIENT_PUBLIC_KEY
        body += bytes.fromhex("13021000") + b"ff9549d5891998e5"
        self.assertEqual(len(body), 295)

    def test_tech_system_interlocks(self):
        state = TechSystemState()
        self.assertFalse(state.can_change_mode)
        self.assertFalse(state.show_winter_humidifier)
        state.power = "OFF"
        state.mode = "heat"
        self.assertTrue(state.can_change_mode)
        self.assertTrue(state.show_winter_humidifier)
        state.power = "ON"
        state.mode = "cool"
        self.assertFalse(state.can_change_mode)
        self.assertFalse(state.show_winter_humidifier)


if __name__ == "__main__":
    unittest.main()
