"""Pure protocol regression tests without requiring Home Assistant at runtime."""

from __future__ import annotations

import importlib
import json
import struct
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
FIXTURE_PATH = ROOT / "tests" / "fixtures" / "official_panel_frames.json"
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

    def test_decoder_bounds_corrupt_lengths_and_recovers_the_next_frame(self) -> None:
        """Corrupt prefixes and payloads do not poison a later valid status frame."""
        decoder = protocol.YasHcpDecoder()
        excessive = b"#" + struct.pack("<H", protocol.MAX_PAYLOAD_LENGTH + 1)
        excessive += protocol.MAGIC
        malformed = (
            b"#"
            + struct.pack("<H", 12)
            + protocol.MAGIC
            + b"\x01\x05\x0c\x00\x00\x00\x00\x00"
        )
        invalid_length = (
            b"#"
            + struct.pack("<H", 14)
            + protocol.MAGIC
            + b"\x01\x05\x0c\x00\x05\x00\x00\x00"
        )
        valid = protocol.YasHcpFrame(5, 12, 7, b"ok")

        self.assertEqual(
            decoder.feed(excessive + malformed + invalid_length + valid.encode()),
            [valid],
        )
        self.assertGreaterEqual(decoder.frames_malformed, 2)
        self.assertGreater(decoder.bytes_discarded, 0)

    def test_decoder_retains_incomplete_prefix_until_the_rest_arrives(self) -> None:
        decoder = protocol.YasHcpDecoder()
        frame = protocol.YasHcpFrame(1, 3, 1, b"")
        self.assertEqual(decoder.feed(frame.encode()[:5]), [])
        self.assertEqual(decoder.feed(frame.encode()[5:]), [frame])


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
        body += protocol.tlv(
            0x000A,
            bytes.fromhex("0101000149013e003200f0010000"),
        )
        self.assertEqual(
            protocol.decode_tech_system_status(body, mac),
            {
                "power": "ON",
                "mode": "cool",
                "scene": "home",
                "winter_humidifier": "OFF",
                "energy_saving": "ON",
                "temperature": 32.9,
                "humidity": 62,
                "pm25": 5.0,
                "co2": 496,
                "system_fault_code": 0,
                "filter_fault_code": 0,
            },
        )

    def test_total_control_rejects_non_14_byte_status_blocks(self) -> None:
        mac = bytes.fromhex("ff00ffffffff00ff")
        prefix = protocol.tlv(0x0004, mac) + protocol.tlv(0x000B, b"\x01")

        for packed in (b"\x01\x01\x00", bytes(15)):
            with self.subTest(length=len(packed)):
                self.assertEqual(
                    protocol.decode_tech_system_status(
                        prefix + protocol.tlv(0x000A, packed), mac
                    ),
                    {},
                )

    def test_unknown_total_control_enums_do_not_hide_measured_values(self) -> None:
        mac = bytes.fromhex("ff00ffffffff00ff")
        packed = bytes.fromhex("7f7e0201fa0041007b00bc022a07")
        body = protocol.tlv(0x0004, mac)
        body += protocol.tlv(0x000B, b"\x00")
        body += protocol.tlv(0x000A, packed)

        self.assertEqual(
            protocol.decode_tech_system_status(body, mac),
            {
                "power": "OFF",
                "winter_humidifier": "ON",
                "energy_saving": "ON",
                "temperature": 25.0,
                "humidity": 65,
                "pm25": 12.3,
                "co2": 700,
                "system_fault_code": 42,
                "filter_fault_code": 7,
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
        self.assertFalse(protocol.preserve_valid_thermostat_measurements(current, None))
        self.assertIsNone(current.current_temperature)
        self.assertIsNone(current.humidity)

    def test_tlv_and_text_validation_rejects_partial_or_invalid_values(self) -> None:
        self.assertFalse(protocol.is_complete_tlv_body(b"\x01\x00"))
        self.assertEqual(protocol.parse_tlvs(b"\x01\x00\x02\x00x"), {})
        self.assertEqual(list(protocol.iter_tlvs(b"\x01\x00\x04\x00x")), [])
        self.assertEqual(protocol.decode_text(b"\xb2\xe2\xca\xd4"), "测试")
        for value in ("", "0011", "not-a-mac"):
            with self.assertRaises(ValueError):
                protocol.parse_device_mac(value)


class ClientEdgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_initial_queries_are_paced_without_expanding_query_surface(
        self,
    ) -> None:
        client = protocol.AsyncMoorgenClient(
            "192.0.2.1", 9000, "user", "password", "0011223344556677"
        )
        sent: list[tuple[int, int, bytes]] = []
        sleeps: list[float] = []

        async def capture_send(kind: int, opcode: int, body: bytes) -> None:
            sent.append((kind, opcode, body))

        async def capture_sleep(delay: float) -> None:
            sleeps.append(delay)

        client._send = capture_send
        with patch.object(protocol.asyncio, "sleep", capture_sleep):
            await client._send_initial_queries()

        categories = [
            dict(protocol.iter_tlvs(body))[0x000F][0]
            for kind, opcode, body in sent
            if (kind, opcode) == (3, 7)
        ]
        self.assertEqual(
            categories,
            [0x0B, 0x1F, 0x01, 0x11, 0x09, 0x0D, 0x03, 0x07, 0x1B, 0x17],
        )
        self.assertEqual(sleeps, [0.15] * 9)

    def test_client_rejects_invalid_identity_before_opening_a_socket(self) -> None:
        with self.assertRaises(ValueError):
            protocol.AsyncMoorgenClient("127.0.0.1", 9000, "admin", "secret", "bad")

    async def test_client_rejects_commands_without_a_ready_session(self) -> None:
        client = protocol.AsyncMoorgenClient("127.0.0.1", 9000, "admin", "secret")
        with self.assertRaises(ConnectionError):
            await client.send_command(bytes.fromhex("ff00ffffffff01ff"), 3, 44)
        with self.assertRaises(ConnectionError):
            await client._send(4, 9, b"")

    async def test_waiter_reports_timeout_eof_and_unexpected_frame(self) -> None:
        client = protocol.AsyncMoorgenClient("127.0.0.1", 9000, "admin", "secret")
        with self.assertRaises(TimeoutError):
            await client._wait_for(1, 3, 0)
        client._inbox.put_nowait(None)
        with self.assertRaises(ConnectionError):
            await client._wait_for(1, 3, 0.1)
        client._inbox.put_nowait(protocol.YasHcpFrame(1, 4, 0, b""))
        with self.assertRaises(protocol.IncompatibleProtocol):
            await client._wait_for(1, 3, 0.1)


class OfficialPanelCaptureRegressionTests(unittest.TestCase):
    def test_sanitized_official_frames_keep_their_control_semantics(self) -> None:
        fixtures = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        decoder = protocol.YasHcpDecoder()

        for fixture in fixtures["frames"]:
            with self.subTest(name=fixture["name"]):
                frames = decoder.feed(bytes.fromhex(fixture["frame_hex"]))
                self.assertEqual(len(frames), 1)
                frame = frames[0]
                self.assertEqual([frame.kind, frame.opcode], fixture["message"])
                fields = protocol.parse_tlvs(frame.body)
                self.assertEqual(fields[0x0004].hex(), fixture["mac"])
                if "command" in fixture:
                    self.assertEqual(fields[0x0009][0], fixture["command"])
                    self.assertEqual(
                        fields.get(0x000A, b"").hex(), fixture.get("value_hex", "")
                    )
                elif "tech_system_state" in fixture:
                    self.assertEqual(
                        protocol.decode_tech_system_status(
                            frame.body, bytes.fromhex(fixtures["tech_system_mac"])
                        ),
                        fixture["tech_system_state"],
                    )
                else:
                    state = protocol.decode_thermostat_status(
                        frame.body, bytes.fromhex(fixtures["tech_system_mac"])
                    )
                    self.assertIsNotNone(state)
                    assert state is not None
                    self.assertEqual(state.target_temperature, 24)
                    self.assertEqual(state.current_temperature, 31.2)
                    self.assertEqual(state.humidity, 64)
                    self.assertEqual(state.power, "ON")


if __name__ == "__main__":
    unittest.main()
