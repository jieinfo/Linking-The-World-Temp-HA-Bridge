#!/usr/bin/env python3
"""Experimental local Home Assistant bridge for the Moorgen tech-system controller.

The implementation follows the verified Android App <-> MC7021 YAS HCP capture.
It deliberately acts as a local App client, not an MT8157 gateway, so it never
sends an MT8157 device-online report.
"""

from __future__ import annotations

import argparse
import json
import logging
import socket
import struct
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Queue
from typing import Callable

import paho.mqtt.client as mqtt
import yaml

LOG = logging.getLogger("moorgen_ha_bridge")
# Each YAS HCP payload is carried in the App's ``# + uint16 length`` envelope.
# The captured wire magic is consequently ``dooyashcp``, followed by one '#'.
MAGIC = b"dooyashcp"
VERSION = 1
TRAILER = b"#"
TECH_SYSTEM_MAC = bytes.fromhex("ff00ffffffff00ff")
DEFAULT_CLIENT_ID = "ff9549d5891998e5"

# A valid RSA public key is required by the observed first YAS HCP hello. The
# host did not encrypt subsequent App traffic in the supplied capture, so a
# fixed client key is sufficient for this experimental local bridge.
CLIENT_PUBLIC_KEY = b"""-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQCbubcnMbVxGmjp2Sc22azesb08
T1MlidtdZpEJYG6OL/PMhwV4z+B/Trf1aQ5G560/4Xs9f2Vgox36DSUs6pvYOql+
Fjc/WfyEB80l5op4M7AhblPr171spbbxkF4Gk2S8DWlf0YouBl3XDk0ZaW/6QArD
z/tjVw5AVVI7+stdPQIDAQAB
-----END PUBLIC KEY-----
""".rstrip(b"\n")

COMMAND_POWER_OFF = 1
COMMAND_POWER_ON = 2
COMMAND_MODE = 3
COMMAND_SCENE = 4
COMMAND_WINTER_HUMIDIFIER = 5

MODE_VALUES = {"cool": 1, "heat": 2, "ventilation": 3, "dehumidify": 4}
SCENE_VALUES = {"away": 0, "home": 1}
MODE_NAMES = {value: name for name, value in MODE_VALUES.items()}
SCENE_NAMES = {value: name for name, value in SCENE_VALUES.items()}
CLIMATE_MODE_FOR_SYSTEM_MODE = {
    "cool": "cool",
    "heat": "heat",
    "ventilation": "fan_only",
    "dehumidify": "dry",
}


@dataclass(frozen=True)
class YasHcpFrame:
    kind: int
    opcode: int
    sequence: int
    body: bytes

    def encode(self) -> bytes:
        header = MAGIC + bytes((VERSION, self.kind, self.opcode))
        header += struct.pack("<HH", self.sequence, len(self.body))
        payload = header + self.body + TRAILER
        return b"#" + struct.pack("<H", len(payload)) + payload


class YasHcpDecoder:
    """Incremental decoder for the YAS HCP framing observed in the PCAP files."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[YasHcpFrame]:
        self._buffer.extend(data)
        output: list[YasHcpFrame] = []
        while True:
            start = self._buffer.find(b"#")
            if start < 0:
                self._buffer.clear()
                return output
            if start:
                del self._buffer[:start]
            if len(self._buffer) < 3:
                return output
            payload_length = struct.unpack_from("<H", self._buffer, 1)[0]
            frame_length = 3 + payload_length
            if len(self._buffer) < frame_length:
                return output
            raw = bytes(self._buffer[3:frame_length])
            del self._buffer[:frame_length]
            if not raw.startswith(MAGIC) or not raw.endswith(TRAILER) or len(raw) < len(MAGIC) + 8:
                LOG.warning("discarded malformed YAS HCP payload: %s", raw.hex())
                continue
            body_length = struct.unpack_from("<H", raw, len(MAGIC) + 5)[0]
            expected_length = len(MAGIC) + 7 + body_length + len(TRAILER)
            if len(raw) != expected_length:
                LOG.warning("discarded YAS HCP payload with invalid length: %s", raw.hex())
                continue
            output.append(
                YasHcpFrame(
                    kind=raw[len(MAGIC) + 1],
                    opcode=raw[len(MAGIC) + 2],
                    sequence=struct.unpack_from("<H", raw, len(MAGIC) + 3)[0],
                    body=raw[len(MAGIC) + 7 : len(MAGIC) + 7 + body_length],
                )
            )


@dataclass
class TechSystemState:
    power: str | None = None
    mode: str | None = None
    scene: str | None = None
    winter_humidifier: str | None = None

    @property
    def can_change_mode(self) -> bool:
        return self.power == "OFF"

    @property
    def show_winter_humidifier(self) -> bool:
        return self.mode == "heat"


@dataclass
class ThermostatState:
    mac: bytes
    room_id: str
    target_temperature: float
    current_temperature: float
    power: str
    humidity: int


def tlv(tag: int, value: bytes) -> bytes:
    return struct.pack("<HH", tag, len(value)) + value


def iter_tlvs(data: bytes):
    """Parse the flat little-endian tag/length/value records used by status events."""
    offset = 0
    while offset + 4 <= len(data):
        tag, length = struct.unpack_from("<HH", data, offset)
        offset += 4
        if offset + length > len(data):
            break
        yield tag, data[offset : offset + length]
        offset += length


def parse_tlvs(data: bytes) -> dict[int, bytes]:
    return dict(iter_tlvs(data))
    return output


def decode_tech_system_status(body: bytes) -> dict[str, str]:
    """Return the verified fields from an MC7021 technology-system status event."""
    fields = parse_tlvs(body)
    if fields.get(0x0004) != TECH_SYSTEM_MAC:
        return {}

    state: dict[str, str] = {}
    power = fields.get(0x000B)
    if power:
        state["power"] = "ON" if power[0] else "OFF"

    packed = fields.get(0x000A)
    if packed:
        mode = MODE_NAMES.get(packed[0])
        if mode:
            state["mode"] = mode
        if len(packed) > 1:
            scene = SCENE_NAMES.get(packed[1])
            if scene:
                state["scene"] = scene
        if len(packed) > 2:
            state["winter_humidifier"] = "ON" if packed[2] else "OFF"
    return state


def decode_thermostat_status(body: bytes) -> ThermostatState | None:
    """Decode a child thermostat report verified against the supplied App PCAP."""
    fields = parse_tlvs(body)
    mac = fields.get(0x0004)
    packed = fields.get(0x000A)
    power = fields.get(0x000B)
    if (
        not mac
        or fields.get(0x0075) != TECH_SYSTEM_MAC
        or len(packed or b"") != 5
        or not power
    ):
        return None
    room_id = fields.get(0x0030, b"").decode("utf-8", errors="replace")
    return ThermostatState(
        mac=mac,
        room_id=room_id,
        target_temperature=packed[0] / 2,
        # Bytes 1-2 are the little-endian room temperature in tenths of a
        # degree. The App may round it for display, but HA keeps the decimal.
        current_temperature=int.from_bytes(packed[1:3], "little") / 10,
        power="ON" if power[0] else "OFF",
        humidity=packed[3],
    )


class MoorgenClient:
    def __init__(self, host: str, port: int, username: str, password: str, client_id: str = DEFAULT_CLIENT_ID) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self._socket: socket.socket | None = None
        self._decoder = YasHcpDecoder()
        self._sequence = 0
        if len(client_id) != 16 or any(char not in "0123456789abcdefABCDEF" for char in client_id):
            raise ValueError("moorgen client_id must be 16 hexadecimal characters")
        self._client_id = client_id.lower()
        self._write_lock = threading.Lock()
        self._ready = threading.Event()
        self._closed = threading.Event()
        self._inbox: Queue[YasHcpFrame] = Queue()
        self._reader: threading.Thread | None = None
        self.on_status: Callable[[bytes], None] | None = None
        self.on_frame: Callable[[YasHcpFrame], None] | None = None

    def connect(self) -> None:
        self._closed.clear()
        self._socket = socket.create_connection((self.host, self.port), timeout=8)
        self._socket.settimeout(1)
        LOG.info("MC7021 TCP connected local=%s remote=%s", self._socket.getsockname(), self._socket.getpeername())
        self._reader = threading.Thread(target=self._read_loop, name="mc7021-reader", daemon=True)
        self._reader.start()
        self._send_hello()
        LOG.info("MC7021 hello sent; waiting for response")
        self._wait_for(1, 3, timeout=8)
        self._send_login()
        self._wait_for(2, 6, timeout=8)
        self._send_initial_queries()
        self._ready.set()
        LOG.info("logged in to MC7021 at %s:%s", self.host, self.port)

    def close(self) -> None:
        self._closed.set()
        self._ready.clear()
        if self._socket:
            try:
                self._socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self._socket.close()
        self._socket = None

    def send_command(self, command: int, value: int | None = None) -> None:
        self.send_command_to(TECH_SYSTEM_MAC, command, value)

    def send_command_to(self, mac: bytes, command: int, value: int | None = None) -> None:
        if not self._ready.is_set():
            raise RuntimeError("MC7021 session is not ready")
        body = tlv(0x0010, b"\x01")
        body += tlv(0x0004, mac)
        body += tlv(0x0009, bytes((command,)))
        if value is not None:
            body += tlv(0x000A, bytes((value,)))
        self._send(4, 9, body)

    def heartbeat(self) -> None:
        if self._ready.is_set():
            self._send(6, 0x0E, b"")

    def _send_hello(self) -> None:
        body = bytes.fromhex("12020f01") + CLIENT_PUBLIC_KEY
        body += bytes.fromhex("13021000") + self._client_id.encode("ascii")
        self._send(1, 1, body)

    def _send_login(self) -> None:
        body = tlv(0x000C, self.username.encode("utf-8"))
        body += tlv(0x000D, self.password.encode("utf-8"))
        self._send(2, 4, body)

    def _send_initial_queries(self) -> None:
        # These are the same data categories requested by the App after login.
        for category in (0x0B, 0x1F, 0x01, 0x11, 0x09, 0x0D, 0x03, 0x07, 0x1B):
            body = tlv(0x000F, bytes((category,)))
            self._send(3, 7, body)
        body = tlv(0x000F, b"\x17") + tlv(0x0077, self._client_id.encode("ascii"))
        self._send(3, 7, body)
        self._send(3, 7, tlv(0x000F, b"\x21"))

    def _send(self, kind: int, opcode: int, body: bytes) -> None:
        if not self._socket:
            raise RuntimeError("not connected")
        with self._write_lock:
            frame = YasHcpFrame(kind, opcode, self._sequence, body)
            self._sequence = (self._sequence + 1) & 0xFFFF
            self._socket.sendall(frame.encode())
            LOG.debug("sent kind=%02x opcode=%02x seq=%d body=%s", kind, opcode, frame.sequence, body.hex())

    def _wait_for(self, kind: int, opcode: int, timeout: float) -> YasHcpFrame:
        deadline = time.monotonic() + timeout
        deferred: list[YasHcpFrame] = []
        try:
            while time.monotonic() < deadline:
                try:
                    frame = self._inbox.get(timeout=max(0.1, deadline - time.monotonic()))
                except Empty:
                    continue
                if frame.kind == kind and frame.opcode == opcode:
                    return frame
                deferred.append(frame)
        finally:
            for frame in deferred:
                self._inbox.put(frame)
        raise TimeoutError(f"MC7021 did not return kind={kind:#x}, opcode={opcode:#x}")

    def _read_loop(self) -> None:
        assert self._socket is not None
        try:
            while not self._closed.is_set():
                try:
                    data = self._socket.recv(4096)
                except socket.timeout:
                    continue
                if not data:
                    raise ConnectionError("MC7021 closed the socket")
                for frame in self._decoder.feed(data):
                    LOG.debug("received kind=%02x opcode=%02x body=%s", frame.kind, frame.opcode, frame.body.hex())
                    if self.on_frame:
                        self.on_frame(frame)
                    if frame.kind == 5 and frame.opcode == 0x0C and self.on_status:
                        self.on_status(frame.body)
                    else:
                        self._inbox.put(frame)
        except Exception as error:
            if not self._closed.is_set():
                LOG.warning("MC7021 reader stopped: %s", error)
        finally:
            self._ready.clear()


class Bridge:
    def __init__(self, config: dict) -> None:
        self.config = config
        host = config["moorgen"]
        self.client = MoorgenClient(
            host["host"],
            int(host.get("port", 9000)),
            host["username"],
            host["password"],
            host.get("client_id", DEFAULT_CLIENT_ID),
        )
        self.client.on_status = self._status_received
        self.client.on_frame = self._frame_received
        mqtt_config = config["mqtt"]
        self.topic_prefix = mqtt_config.get("topic_prefix", "moorgen/tech_system")
        self.discovery_prefix = mqtt_config.get("discovery_prefix", "homeassistant")
        self.mqtt = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=mqtt_config.get("client_id", "moorgen-ha-bridge"))
        if mqtt_config.get("username"):
            self.mqtt.username_pw_set(mqtt_config["username"], mqtt_config.get("password"))
        self.mqtt.on_connect = self._mqtt_connected
        self.mqtt.on_connect_fail = self._mqtt_connect_failed
        self.mqtt.on_message = self._mqtt_message
        self.mqtt.on_disconnect = self._mqtt_disconnected
        self.mqtt.reconnect_delay_set(min_delay=1, max_delay=30)
        self.mqtt_config = mqtt_config
        self._stop = threading.Event()
        # State starts unknown because the controller's packed 14-byte status
        # report is not fully mapped. Send an OFF command once after startup to
        # establish the safe state needed for mode changes.
        self.state = TechSystemState()
        self.room_names: dict[str, str] = {}
        self.thermostats: dict[str, ThermostatState] = {}

    def run(self) -> None:
        self.client.connect()
        # MQTT is an independent dependency. connect_async plus loop_start
        # keeps the MC7021 session alive while the broker is unavailable and
        # lets Paho reconnect automatically when it returns.
        self.mqtt.connect_async(self.mqtt_config["host"], int(self.mqtt_config.get("port", 1883)), 60)
        self.mqtt.loop_start()
        heartbeat_at = 0.0
        try:
            while not self._stop.wait(1):
                if time.monotonic() >= heartbeat_at:
                    self.client.heartbeat()
                    heartbeat_at = time.monotonic() + 15
        finally:
            self.mqtt.loop_stop()
            self.mqtt.disconnect()
            self.client.close()

    def _mqtt_connected(self, client, userdata, flags, reason_code, properties) -> None:
        if reason_code.is_failure:
            LOG.error("MQTT broker rejected the connection: %s", reason_code)
            return
        client.subscribe(f"{self.topic_prefix}/#")
        self._publish_discovery()
        for thermostat in self.thermostats.values():
            self._publish_thermostat_discovery(thermostat)
            self._publish_thermostat_state(thermostat)
        client.publish(f"{self.topic_prefix}/availability", "online", retain=True)
        LOG.info("connected to MQTT broker")

    def _mqtt_connect_failed(self, client, userdata) -> None:
        LOG.warning("MQTT connection failed; retrying automatically")

    def _mqtt_disconnected(self, client, userdata, disconnect_flags, reason_code, properties) -> None:
        if not self._stop.is_set():
            LOG.warning("MQTT disconnected: %s; reconnecting automatically", reason_code)

    def _mqtt_message(self, client, userdata, message) -> None:
        value = message.payload.decode("utf-8").strip().lower()
        relative_topic = message.topic.removeprefix(f"{self.topic_prefix}/")
        if not relative_topic.endswith("/set"):
            return
        parts = relative_topic.split("/")
        try:
            if len(parts) == 4 and parts[0] == "thermostat" and parts[3] == "set":
                try:
                    self._thermostat_command(parts[1], parts[2], value)
                except (ValueError, RuntimeError, OSError) as error:
                    LOG.error("thermostat command %s=%s failed: %s", parts[2], value, error)
                return
            suffix = relative_topic.removesuffix("/set")
            if suffix == "power":
                self.client.send_command(COMMAND_POWER_ON if value == "on" else COMMAND_POWER_OFF)
                self.state.power = "ON" if value == "on" else "OFF"
                self._publish_state("power", self.state.power)
                self._refresh_conditional_entities()
            elif suffix == "mode":
                if not self.state.can_change_mode:
                    raise RuntimeError("mode changes are only allowed while the tech system is off")
                self.client.send_command(COMMAND_MODE, MODE_VALUES[value])
                self.state.mode = value
                self._publish_state("mode", value)
                self._refresh_conditional_entities()
            elif suffix == "scene":
                self.client.send_command(COMMAND_SCENE, SCENE_VALUES[value])
                self.state.scene = value
                self._publish_state("scene", value)
            elif suffix == "winter_humidifier":
                if not self.state.show_winter_humidifier:
                    raise RuntimeError("winter humidifier is only available in heat mode")
                self.client.send_command(COMMAND_WINTER_HUMIDIFIER, 1 if value == "on" else 0)
                self.state.winter_humidifier = "ON" if value == "on" else "OFF"
                self._publish_state("winter_humidifier", self.state.winter_humidifier)
            else:
                LOG.warning("ignored MQTT command topic: %s", message.topic)
        except (KeyError, RuntimeError, OSError) as error:
            LOG.error("command %s=%s failed: %s", suffix, value, error)

    def _thermostat_command(self, mac_hex: str, setting: str, value: str) -> None:
        thermostat = self.thermostats.get(mac_hex)
        if not thermostat:
            raise RuntimeError(f"unknown thermostat: {mac_hex}")
        if setting == "temperature":
            temperature = float(value)
            raw_value = round(temperature * 2)
            if not 10 <= raw_value <= 80:
                raise RuntimeError("temperature must be between 5 and 40 degrees")
            self.client.send_command_to(thermostat.mac, COMMAND_MODE, raw_value)
            thermostat.target_temperature = raw_value / 2
        elif setting in ("power", "mode"):
            if setting == "mode" and value not in ("off", self._thermostat_active_hvac_mode()):
                raise RuntimeError(f"unsupported thermostat HVAC mode: {value}")
            if setting == "power" and value not in ("off", "on"):
                raise RuntimeError(f"unsupported thermostat power state: {value}")
            enabled = value != "off"
            self.client.send_command_to(
                thermostat.mac,
                COMMAND_POWER_ON if enabled else COMMAND_POWER_OFF,
            )
            thermostat.power = "ON" if enabled else "OFF"
        else:
            raise RuntimeError(f"unsupported thermostat setting: {setting}")
        self._publish_thermostat_state(thermostat)

    def _frame_received(self, frame: YasHcpFrame) -> None:
        if frame.kind != 3 or frame.opcode != 8:
            return
        room_id = ""
        changed = False
        for tag, value in iter_tlvs(frame.body):
            if tag == 0x0030:
                room_id = value.decode("utf-8", errors="replace")
            elif tag == 0x0036 and room_id:
                name = value.decode("utf-8", errors="replace")
                if self.room_names.get(room_id) != name:
                    self.room_names[room_id] = name
                    changed = True
        if changed:
            for thermostat in self.thermostats.values():
                self._publish_thermostat_discovery(thermostat)

    def _status_received(self, body: bytes) -> None:
        payload = json.dumps({"raw": body.hex()}, separators=(",", ":"))
        self.mqtt.publish(f"{self.topic_prefix}/status_raw", payload, retain=False)
        state = decode_tech_system_status(body)
        if state:
            for name, value in state.items():
                setattr(self.state, name, value)
                self._publish_state(name, value)
            self._refresh_conditional_entities()
            self._refresh_thermostat_climate_modes()

        thermostat = decode_thermostat_status(body)
        if thermostat:
            self.thermostats[thermostat.mac.hex()] = thermostat
            self._publish_thermostat_discovery(thermostat)
            self._publish_thermostat_state(thermostat)

    def _publish_state(self, name: str, value: str) -> None:
        self.mqtt.publish(f"{self.topic_prefix}/{name}/state", value, retain=True)

    def _thermostat_name(self, thermostat: ThermostatState) -> str:
        room_name = self.room_names.get(thermostat.room_id, thermostat.room_id or thermostat.mac.hex())
        return f"{room_name} 温控面板"

    def _thermostat_topic(self, thermostat: ThermostatState) -> str:
        return f"{self.topic_prefix}/thermostat/{thermostat.mac.hex()}"

    def _thermostat_active_hvac_mode(self) -> str:
        # Child panels only enable a room. The central technology system owns
        # the actual HVAC mode, so reflect that mode on each child Climate card.
        return CLIMATE_MODE_FOR_SYSTEM_MODE.get(self.state.mode or "", "heat")

    def _refresh_thermostat_climate_modes(self) -> None:
        for thermostat in self.thermostats.values():
            self._publish_thermostat_discovery(thermostat)
            self._publish_thermostat_state(thermostat)

    def _publish_thermostat_state(self, thermostat: ThermostatState) -> None:
        topic = self._thermostat_topic(thermostat)
        self.mqtt.publish(f"{topic}/power/state", thermostat.power, retain=True)
        self.mqtt.publish(
            f"{topic}/mode/state",
            self._thermostat_active_hvac_mode() if thermostat.power == "ON" else "off",
            retain=True,
        )
        self.mqtt.publish(f"{topic}/temperature/state", f"{thermostat.target_temperature:g}", retain=True)
        self.mqtt.publish(f"{topic}/current_temperature", f"{thermostat.current_temperature:g}", retain=True)
        self.mqtt.publish(f"{topic}/humidity", str(thermostat.humidity), retain=True)

    def _publish_thermostat_discovery(self, thermostat: ThermostatState) -> None:
        mac_hex = thermostat.mac.hex()
        topic = self._thermostat_topic(thermostat)
        device = {"identifiers": [f"moorgen_thermostat_{mac_hex}"], "name": self._thermostat_name(thermostat)}
        # The panel has only enable/disable, but Climate provides the compact
        # thermostat card the user expects. Map Climate's heat/off modes to it.
        self._discovery("climate", f"thermostat_{mac_hex}", {
            "name": "温控器",
            "unique_id": f"moorgen_thermostat_{mac_hex}",
            "mode_command_topic": f"{topic}/mode/set",
            "mode_state_topic": f"{topic}/mode/state",
            "modes": ["off", self._thermostat_active_hvac_mode()],
            "temperature_command_topic": f"{topic}/temperature/set",
            "temperature_state_topic": f"{topic}/temperature/state",
            "current_temperature_topic": f"{topic}/current_temperature",
            "current_humidity_topic": f"{topic}/humidity",
            "min_temp": 5,
            "max_temp": 40,
            "temp_step": 0.5,
            "precision": 0.1,
            "temperature_unit": "C",
            "device": device,
        })
        # Remove the short-lived split controls introduced in v0.1.8.
        for component, object_id in (
            ("switch", f"thermostat_{mac_hex}_power"),
            ("number", f"thermostat_{mac_hex}_target_temperature"),
            ("sensor", f"thermostat_{mac_hex}_temperature"),
            ("sensor", f"thermostat_{mac_hex}_humidity"),
        ):
            self.mqtt.publish(
                f"{self.discovery_prefix}/{component}/moorgen_tech_system/{object_id}/config",
                b"",
                retain=True,
            )

    def _publish_discovery(self) -> None:
        common = {
            "availability_topic": f"{self.topic_prefix}/availability",
            "unique_id": "moorgen_tech_system",
            "device": {"identifiers": ["moorgen_mc7021_tech_system"], "name": "摩根科技系统总控"},
        }
        self._discovery("switch", "power", {
            **common,
            "name": "科技系统总开关",
            "unique_id": "moorgen_tech_system_power",
            "command_topic": f"{self.topic_prefix}/power/set",
            "state_topic": f"{self.topic_prefix}/power/state",
            "payload_on": "ON", "payload_off": "OFF",
        })
        self._discovery("select", "mode", {
            **common,
            "name": "科技系统模式",
            "unique_id": "moorgen_tech_system_mode",
            "command_topic": f"{self.topic_prefix}/mode/set",
            "state_topic": f"{self.topic_prefix}/mode/state",
            "availability_topic": f"{self.topic_prefix}/mode/availability",
            "options": list(MODE_VALUES),
        })
        self._discovery("select", "scene", {
            **common,
            "name": "科技系统场景",
            "unique_id": "moorgen_tech_system_scene",
            "command_topic": f"{self.topic_prefix}/scene/set",
            "state_topic": f"{self.topic_prefix}/scene/state",
            "options": list(SCENE_VALUES),
        })
        self._refresh_conditional_entities()

    def _refresh_conditional_entities(self) -> None:
        mode_availability = "online" if self.state.can_change_mode else "offline"
        self.mqtt.publish(f"{self.topic_prefix}/mode/availability", mode_availability, retain=True)
        winter_topic = f"{self.discovery_prefix}/switch/moorgen_tech_system/winter_humidifier/config"
        if not self.state.show_winter_humidifier:
            # An empty retained MQTT discovery payload removes the entity from HA.
            self.mqtt.publish(winter_topic, b"", retain=True)
            return
        common = {
            "availability_topic": f"{self.topic_prefix}/availability",
            "unique_id": "moorgen_tech_system",
            "device": {"identifiers": ["moorgen_mc7021_tech_system"], "name": "摩根科技系统总控"},
        }
        self._discovery("switch", "winter_humidifier", {
            **common,
            "name": "冬季加湿",
            "unique_id": "moorgen_tech_system_winter_humidifier",
            "command_topic": f"{self.topic_prefix}/winter_humidifier/set",
            "state_topic": f"{self.topic_prefix}/winter_humidifier/state",
            "payload_on": "ON", "payload_off": "OFF",
        })

    def _discovery(self, component: str, object_id: str, payload: dict) -> None:
        topic = f"{self.discovery_prefix}/{component}/moorgen_tech_system/{object_id}/config"
        self.mqtt.publish(topic, json.dumps(payload, ensure_ascii=False), retain=True)


def load_config(path: Path) -> dict:
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    for section, keys in (("moorgen", ("host", "username", "password")), ("mqtt", ("host",))):
        if section not in config or any(not config[section].get(key) for key in keys):
            raise ValueError(f"config.{section} is missing required values")
    return config


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.yaml", type=Path)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    Bridge(load_config(args.config)).run()


if __name__ == "__main__":
    main()
