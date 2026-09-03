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

    def test_malformed_payload_records_structure_and_next_frame_recovery(self) -> None:
        """Diagnostics describe framing failures without retaining payload bytes."""
        decoder = protocol.YasHcpDecoder()
        malformed_frame = protocol.YasHcpFrame(5, 12, 41, b"x" * 23).encode()
        declared_length = struct.unpack_from("<H", malformed_frame, 1)[0] - 1
        malformed_frame = (
            malformed_frame[:1]
            + struct.pack("<H", declared_length)
            + malformed_frame[3:]
        )
        recovered_frame = protocol.YasHcpFrame(5, 12, 42, b"ok")

        self.assertEqual(
            decoder.feed(malformed_frame + recovered_frame.encode()),
            [recovered_frame],
        )

        diagnostics = decoder.parser_anomalies
        self.assertEqual(len(diagnostics), 1)
        anomaly = diagnostics[0]
        self.assertEqual(anomaly["reason"], "invalid_envelope")
        self.assertEqual(anomaly["outer_length_declared"], 39)
        self.assertEqual(anomaly["payload_bytes_consumed"], 39)
        self.assertEqual(anomaly["body_length_declared"], 23)
        self.assertEqual(anomaly["payload_length_expected"], 40)
        self.assertTrue(anomaly["magic_header_valid"])
        self.assertFalse(anomaly["trailer_present"])
        self.assertEqual(
            (anomaly["kind"], anomaly["opcode"], anomaly["sequence"]),
            (5, 12, 41),
        )
        self.assertEqual(anomaly["recovery"], "next_valid_frame")
        self.assertTrue(anomaly["immediate_recovery"])
        self.assertEqual(
            (
                anomaly["recovery_kind"],
                anomaly["recovery_opcode"],
                anomaly["recovery_sequence"],
            ),
            (5, 12, 42),
        )
        self.assertNotIn("raw", anomaly)
        self.assertNotIn("body", anomaly)

    def test_malformed_payload_recovery_reports_intervening_anomaly(self) -> None:
        """A second bad frame prevents the first anomaly being called immediate."""
        decoder = protocol.YasHcpDecoder()

        def without_trailer(frame: protocol.YasHcpFrame) -> bytes:
            encoded = frame.encode()
            declared = struct.unpack_from("<H", encoded, 1)[0] - 1
            return encoded[:1] + struct.pack("<H", declared) + encoded[3:]

        decoder.feed(without_trailer(protocol.YasHcpFrame(5, 12, 1, b"a")))
        decoder.feed(without_trailer(protocol.YasHcpFrame(5, 12, 2, b"b")))
        decoder.feed(protocol.YasHcpFrame(5, 12, 3, b"ok").encode())

        first, second = decoder.parser_anomalies
        self.assertEqual(first["recovery"], "after_additional_anomalies")
        self.assertFalse(first["immediate_recovery"])
        self.assertEqual(first["additional_anomalies_before_recovery"], 1)
        self.assertEqual(second["recovery"], "next_valid_frame")
        self.assertTrue(second["immediate_recovery"])


class StatusTests(unittest.TestCase):
    def test_total_control_state_has_unknown_defaults_for_full_status_block(self) -> None:
        state = protocol.TechSystemState()

        self.assertEqual(
            vars(state),
            {
                "power": None,
                "mode": None,
                "scene": None,
                "winter_humidifier": None,
                "energy_saving": None,
                "temperature": None,
                "humidity": None,
                "pm25": None,
                "co2": None,
                "system_fault_code": None,
                "filter_fault_code": None,
            },
        )

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
