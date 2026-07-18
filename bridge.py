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
import secrets
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
MAGIC = b"yashcp"
VERSION = 1
# The capture consistently terminates YAS HCP frames with '#' plus six bytes.
# Zero is the most common value in the App capture and is accepted by the
# protocol decoder in the current MC7021 sample.
TRAILER = b"\x23" + b"\x00" * 6
TECH_SYSTEM_MAC = bytes.fromhex("ff00ffffffff00ff")

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


@dataclass(frozen=True)
class YasHcpFrame:
    kind: int
    opcode: int
    sequence: int
    body: bytes

    def encode(self) -> bytes:
        header = MAGIC + bytes((VERSION, self.kind, self.opcode))
        header += struct.pack("<HH", self.sequence, len(self.body))
        return header + self.body + TRAILER


class YasHcpDecoder:
    """Incremental decoder for the YAS HCP framing observed in the PCAP files."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[YasHcpFrame]:
        self._buffer.extend(data)
        output: list[YasHcpFrame] = []
        while True:
            start = self._buffer.find(MAGIC)
            if start < 0:
                self._buffer[:] = self._buffer[-(len(MAGIC) - 1) :]
                return output
            if start:
                del self._buffer[:start]
            if len(self._buffer) < 13:
                return output
            body_length = struct.unpack_from("<H", self._buffer, 11)[0]
            frame_length = 13 + body_length + len(TRAILER)
            if len(self._buffer) < frame_length:
                return output
            raw = bytes(self._buffer[:frame_length])
            del self._buffer[:frame_length]
            output.append(
                YasHcpFrame(
                    kind=raw[7],
                    opcode=raw[8],
                    sequence=struct.unpack_from("<H", raw, 9)[0],
                    body=raw[13 : 13 + body_length],
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


def tlv(tag: int, value: bytes) -> bytes:
    return struct.pack("<HH", tag, len(value)) + value


class MoorgenClient:
    def __init__(self, host: str, port: int, username: str, password: str) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self._socket: socket.socket | None = None
        self._decoder = YasHcpDecoder()
        self._sequence = 0
        self._client_id = ""
        self._write_lock = threading.Lock()
        self._ready = threading.Event()
        self._closed = threading.Event()
        self._inbox: Queue[YasHcpFrame] = Queue()
        self._reader: threading.Thread | None = None
        self.on_status: Callable[[bytes], None] | None = None

    def connect(self) -> None:
        self._closed.clear()
        self._socket = socket.create_connection((self.host, self.port), timeout=8)
        self._socket.settimeout(1)
        self._reader = threading.Thread(target=self._read_loop, name="mc7021-reader", daemon=True)
        self._reader.start()
        self._send_hello()
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
        if not self._ready.is_set():
            raise RuntimeError("MC7021 session is not ready")
        body = tlv(0x0010, b"\x01")
        body += tlv(0x0004, TECH_SYSTEM_MAC)
        body += tlv(0x0009, bytes((command,)))
        if value is not None:
            body += tlv(0x000A, bytes((value,)))
        self._send(4, 9, body)

    def heartbeat(self) -> None:
        if self._ready.is_set():
            self._send(6, 0x0E, b"")

    def _send_hello(self) -> None:
        self._client_id = secrets.token_hex(8)
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
        self.client = MoorgenClient(host["host"], int(host.get("port", 9000)), host["username"], host["password"])
        self.client.on_status = self._status_received
        mqtt_config = config["mqtt"]
        self.topic_prefix = mqtt_config.get("topic_prefix", "moorgen/tech_system")
        self.discovery_prefix = mqtt_config.get("discovery_prefix", "homeassistant")
        self.mqtt = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=mqtt_config.get("client_id", "moorgen-ha-bridge"))
        if mqtt_config.get("username"):
            self.mqtt.username_pw_set(mqtt_config["username"], mqtt_config.get("password"))
        self.mqtt.on_connect = self._mqtt_connected
        self.mqtt.on_message = self._mqtt_message
        self.mqtt.on_disconnect = self._mqtt_disconnected
        self.mqtt_config = mqtt_config
        self._stop = threading.Event()
        # State starts unknown because the controller's packed 14-byte status
        # report is not fully mapped. Send an OFF command once after startup to
        # establish the safe state needed for mode changes.
        self.state = TechSystemState()

    def run(self) -> None:
        self.client.connect()
        self.mqtt.connect(self.mqtt_config["host"], int(self.mqtt_config.get("port", 1883)), 60)
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
            raise RuntimeError(f"MQTT connection failed: {reason_code}")
        client.subscribe(f"{self.topic_prefix}/+/set")
        self._publish_discovery()
        client.publish(f"{self.topic_prefix}/availability", "online", retain=True)
        LOG.info("connected to MQTT broker")

    def _mqtt_disconnected(self, client, userdata, disconnect_flags, reason_code, properties) -> None:
        LOG.warning("MQTT disconnected: %s", reason_code)

    def _mqtt_message(self, client, userdata, message) -> None:
        value = message.payload.decode("utf-8").strip().lower()
        suffix = message.topic.removeprefix(f"{self.topic_prefix}/").removesuffix("/set")
        try:
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

    def _status_received(self, body: bytes) -> None:
        # Preserve every raw host report. The captured 14-byte controller status
        # has not been fully field-mapped yet, so this avoids publishing guesses.
        payload = json.dumps({"raw": body.hex()}, separators=(",", ":"))
        self.mqtt.publish(f"{self.topic_prefix}/status_raw", payload, retain=False)

    def _publish_state(self, name: str, value: str) -> None:
        self.mqtt.publish(f"{self.topic_prefix}/{name}/state", value, retain=True)

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
