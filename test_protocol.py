import struct
import time
import unittest
import json
from unittest.mock import MagicMock

from bridge import (
    CLIENT_PUBLIC_KEY,
    DEFAULT_CLIENT_ID,
    COMMAND_MODE,
    COMMAND_POWER_ON,
    COMMAND_SCENE,
    COMMAND_WINTER_HUMIDIFIER,
    CLIMATE_MODE_FOR_SYSTEM_MODE,
    Bridge,
    decode_text,
    decode_thermostat_status,
    decode_tech_system_status,
    MODE_VALUES,
    SCENE_VALUES,
    TECH_SYSTEM_MAC,
    TechSystemState,
    ThermostatState,
    parse_device_mac,
    YasHcpDecoder,
    YasHcpFrame,
    tlv,
)


class ProtocolTests(unittest.TestCase):
    def test_yashcp_round_trip(self):
        source = YasHcpFrame(4, 9, 37, b"example").encode()
        decoded = YasHcpDecoder().feed(source)
        self.assertEqual(decoded, [YasHcpFrame(4, 9, 37, b"example")])

    def test_yashcp_decoder_recovers_from_a_stray_trailing_delimiter(self):
        source = YasHcpFrame(5, 0x0C, 18, b"status").encode()
        decoder = YasHcpDecoder()
        # MC7021 frames end with '#' and the next frame also starts with '#'.
        # This reproduces an interrupted read that leaves the old trailer in
        # front of a complete subsequent frame.
        decoded = decoder.feed(b"#" + source)
        self.assertEqual(decoded, [YasHcpFrame(5, 0x0C, 18, b"status")])

    def test_yashcp_decoder_keeps_a_partially_received_magic_prefix(self):
        source = YasHcpFrame(5, 0x0C, 19, b"status").encode()
        decoder = YasHcpDecoder()
        self.assertEqual(decoder.feed(source[:7]), [])
        self.assertEqual(decoder.feed(source[7:]), [YasHcpFrame(5, 0x0C, 19, b"status")])

    def test_captured_wire_envelope(self):
        frame = YasHcpFrame(1, 3, 0, b"x").encode()
        self.assertEqual(frame, b"#\x12\x00dooyashcp\x01\x01\x03\x00\x00\x01\x00x#")

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

    def test_child_climate_modes_follow_the_technology_system(self):
        self.assertEqual(CLIMATE_MODE_FOR_SYSTEM_MODE["cool"], "cool")
        self.assertEqual(CLIMATE_MODE_FOR_SYSTEM_MODE["heat"], "heat")
        self.assertEqual(CLIMATE_MODE_FOR_SYSTEM_MODE["ventilation"], "fan_only")
        self.assertEqual(CLIMATE_MODE_FOR_SYSTEM_MODE["dehumidify"], "dry")

    def test_total_control_selects_use_chinese_labels_but_accept_legacy_english(self):
        config = {
            "moorgen": {"host": "192.0.2.1", "username": "Test", "password": ""},
            "mqtt": {"host": "broker", "client_id": "test"},
        }
        bridge = Bridge(config)
        self.assertEqual(bridge._normalize_mode("制冷"), "cool")
        self.assertEqual(bridge._normalize_mode("heat"), "heat")
        self.assertEqual(bridge._normalize_scene("居家"), "home")
        self.assertEqual(bridge._normalize_scene("away"), "away")
        bridge.mqtt.publish = MagicMock()
        bridge._publish_state("mode", "dehumidify")
        bridge._publish_state("scene", "home")
        self.assertEqual(bridge.mqtt.publish.call_args_list[0].args[1], "除湿")
        self.assertEqual(bridge.mqtt.publish.call_args_list[1].args[1], "居家")

    def test_mode_select_stays_available_and_explains_when_locked(self):
        config = {
            "moorgen": {"host": "192.0.2.1", "username": "Test", "password": ""},
            "mqtt": {"host": "broker", "client_id": "test"},
        }
        bridge = Bridge(config)
        bridge.mqtt.publish = MagicMock()
        bridge.state.power = "ON"
        bridge.state.mode = "cool"
        bridge._publish_discovery()

        mode_topic = "homeassistant/select/moorgen_tech_system/mode/config"
        mode_payload = next(
            json.loads(call.args[1])
            for call in bridge.mqtt.publish.call_args_list
            if call.args[0] == mode_topic
        )
        self.assertEqual(mode_payload["availability_topic"], "moorgen/tech_system/availability")
        self.assertEqual(mode_payload["options"], ["制冷"])
        self.assertIn(
            ("moorgen/tech_system/mode_switch_condition/state", "请先关闭科技系统"),
            [(call.args[0], call.args[1]) for call in bridge.mqtt.publish.call_args_list],
        )

        bridge.mqtt.publish.reset_mock()
        bridge.state.power = "OFF"
        bridge._refresh_conditional_entities()
        mode_payload = next(
            json.loads(call.args[1])
            for call in bridge.mqtt.publish.call_args_list
            if call.args[0] == mode_topic
        )
        self.assertEqual(mode_payload["options"], ["制冷", "制热", "通风", "除湿"])
        self.assertIn(
            ("moorgen/tech_system/mode_switch_condition/state", "可以切换模式"),
            [(call.args[0], call.args[1]) for call in bridge.mqtt.publish.call_args_list],
        )

    def test_configurable_total_control_mac_and_text_fallback(self):
        custom_mac = bytes.fromhex("0102030405060708")
        body = tlv(0x0004, custom_mac) + tlv(0x000B, b"\x01") + tlv(0x000A, b"\x02")
        self.assertEqual(decode_tech_system_status(body, custom_mac)["mode"], "heat")
        self.assertEqual(decode_tech_system_status(body), {})
        self.assertEqual(parse_device_mac("01:02:03:04:05:06:07:08"), custom_mac)
        self.assertEqual(decode_text("温控面板".encode("gb18030")), "温控面板")
        with self.assertRaises(ValueError):
            parse_device_mac("not-a-mac")

    def test_read_only_rate_limit_and_stale_panel_protection(self):
        config = {
            "moorgen": {"host": "192.0.2.1", "port": 9000, "username": "Test", "password": "", "client_id": DEFAULT_CLIENT_ID},
            "mqtt": {"host": "broker", "port": 1883, "client_id": "test"},
            "safety": {
                "allow_control": False,
                "command_min_interval": 1,
                "thermostat_offline_after": 1,
                "require_protocol_verification": False,
            },
        }
        bridge = Bridge(config)
        bridge.client.send_command_to = MagicMock()
        with self.assertRaises(RuntimeError):
            bridge._send_host_command(COMMAND_POWER_ON)

        bridge.allow_control = True
        bridge._send_host_command(COMMAND_POWER_ON)
        with self.assertRaises(RuntimeError):
            bridge._send_host_command(COMMAND_POWER_ON)

        thermostat = ThermostatState(bytes.fromhex("ff00ffffffff01ff"), "r1100", 20, 28, "OFF", 60)
        thermostat.last_seen = -100
        bridge.thermostats[thermostat.mac.hex()] = thermostat
        bridge.mqtt.publish = MagicMock()
        bridge._refresh_thermostat_availability()
        self.assertFalse(thermostat.available)
        self.assertIn("availability", bridge.mqtt.publish.call_args.args[0])

    def test_child_target_temperature_requires_whole_degrees(self):
        config = {
            "moorgen": {"host": "192.0.2.1", "port": 9000, "username": "Test", "password": "", "client_id": DEFAULT_CLIENT_ID},
            "mqtt": {"host": "broker", "port": 1883, "client_id": "test"},
            "safety": {"command_min_interval": 0, "require_protocol_verification": False},
        }
        bridge = Bridge(config)
        thermostat = ThermostatState(bytes.fromhex("ff00ffffffff01ff"), "r1100", 20, 28, "OFF", 60)
        bridge.thermostats[thermostat.mac.hex()] = thermostat
        bridge.client.send_command_to = MagicMock()
        bridge._publish_thermostat_state = MagicMock()

        bridge._thermostat_command(thermostat.mac.hex(), "temperature", "21")
        bridge.client.send_command_to.assert_called_once_with(thermostat.mac, COMMAND_MODE, 42)
        self.assertEqual(thermostat.target_temperature, 20)
        self.assertIn(f"thermostat_{thermostat.mac.hex()}", bridge.pending_commands)
        with self.assertRaises(RuntimeError):
            bridge._thermostat_command(thermostat.mac.hex(), "temperature", "21.5")
        with self.assertRaises(RuntimeError):
            bridge._thermostat_command(thermostat.mac.hex(), "temperature", "15")
        with self.assertRaises(RuntimeError):
            bridge._thermostat_command(thermostat.mac.hex(), "temperature", "29")

    def test_protocol_gate_and_confirmation_keep_only_controller_confirmed_state(self):
        config = {
            "moorgen": {"host": "192.0.2.1", "username": "Test", "password": ""},
            "mqtt": {"host": "broker", "client_id": "test"},
            "safety": {"command_min_interval": 0, "command_confirmation_timeout": 1},
        }
        bridge = Bridge(config)
        bridge.client.send_command_to = MagicMock()
        bridge.mqtt.publish = MagicMock()
        with self.assertRaises(RuntimeError):
            bridge._send_host_command(COMMAND_POWER_ON)

        bridge.protocol_verified = True
        bridge._send_host_command(COMMAND_POWER_ON)
        bridge._track_pending_command("system", "总控开关", {"power": "ON"})
        self.assertIsNone(bridge.state.power)
        bridge._confirm_pending_command("system", {"power": "OFF"})
        self.assertIn("system", bridge.pending_commands)
        bridge._confirm_pending_command("system", {"power": "ON"})
        self.assertNotIn("system", bridge.pending_commands)
        self.assertIn("已确认", bridge.last_command_status)

    def test_tracked_command_registers_before_an_immediate_status_reply(self):
        config = {
            "moorgen": {"host": "192.0.2.1", "username": "Test", "password": ""},
            "mqtt": {"host": "broker", "client_id": "test"},
            "safety": {"command_min_interval": 0},
        }
        bridge = Bridge(config)
        bridge.protocol_verified = True
        bridge.mqtt.publish = MagicMock()
        bridge.client.request_tech_system_status = MagicMock()

        def reply_immediately(*_args):
            bridge._confirm_pending_command("system", {"power": "ON"})

        bridge.client.send_command_to = MagicMock(side_effect=reply_immediately)
        bridge._send_and_track_command(
            "system", "总控开关", {"power": "ON"}, bridge.tech_system_mac, COMMAND_POWER_ON
        )

        self.assertNotIn("system", bridge.pending_commands)
        self.assertEqual(bridge.last_command_status, "已确认: 总控开关")
        bridge.client.request_tech_system_status.assert_called_once_with()

    def test_failed_tracked_command_does_not_leave_a_pending_lock(self):
        config = {
            "moorgen": {"host": "192.0.2.1", "username": "Test", "password": ""},
            "mqtt": {"host": "broker", "client_id": "test"},
            "safety": {"command_min_interval": 0},
        }
        bridge = Bridge(config)
        bridge.protocol_verified = True
        bridge.mqtt.publish = MagicMock()
        bridge.client.send_command_to = MagicMock(side_effect=OSError("socket closed"))

        with self.assertRaises(OSError):
            bridge._send_and_track_command(
                "system", "总控开关", {"power": "ON"}, bridge.tech_system_mac, COMMAND_POWER_ON
            )

        self.assertNotIn("system", bridge.pending_commands)

    def test_controller_health_detects_reader_failure_and_silence(self):
        config = {
            "moorgen": {"host": "192.0.2.1", "username": "Test", "password": ""},
            "mqtt": {"host": "broker", "client_id": "test"},
            "safety": {"controller_silence_timeout": 1},
        }
        bridge = Bridge(config)
        bridge.client = MagicMock(is_ready=False, reader_alive=False, last_received_at=0)
        with self.assertRaises(ConnectionError):
            bridge._assert_controller_healthy()
        bridge.client.is_ready = True
        bridge.client.reader_alive = True
        bridge.client.last_received_at = -10
        with self.assertRaises(ConnectionError):
            bridge._assert_controller_healthy()
        bridge.client.last_received_at = time.monotonic()
        bridge._assert_controller_healthy()

    def test_captured_hello_body_length(self):
        body = bytes.fromhex("12020f01") + CLIENT_PUBLIC_KEY
        body += bytes.fromhex("13021000") + DEFAULT_CLIENT_ID.encode("ascii")
        self.assertEqual(len(body), 295)

    def test_captured_technology_system_status(self):
        body = bytes.fromhex(
            "1b00010003110001000104000800ff00ffffffff00ff6000010001"
            "98000100219c00010000720001000086000100015f000100000102"
            "01000275000800ff00ffffffff00ff310002002058300005007230"
            "31303006000c00e4b889e68192e680bbe68ea70b000100010a000e"
            "000201010035014100a000ee010000"
        )
        self.assertEqual(
            decode_tech_system_status(body),
            {"power": "ON", "mode": "heat", "scene": "home", "winter_humidifier": "ON"},
        )

    def test_captured_child_thermostat_status(self):
        body = bytes.fromhex(
            "1b00010003110001000104000800ff00ffffffff01ff6000010001"
            "98000100219c00010000720001000086000100015f000100010102"
            "01000275000800ff00ffffffff00ff310002001d5830000500723131"
            "303006000c00e6b8a9e68ea7e99da2e69dbf0b000100010a000500"
            "3823014300"
        )
        thermostat = decode_thermostat_status(body)
        self.assertIsNotNone(thermostat)
        self.assertEqual(thermostat.mac.hex(), "ff00ffffffff01ff")
        self.assertEqual(thermostat.room_id, "r1100")
        self.assertEqual(thermostat.target_temperature, 28)
        self.assertEqual(thermostat.current_temperature, 29.1)
        self.assertEqual(thermostat.power, "ON")
        self.assertEqual(thermostat.humidity, 67)

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
