"""Shared MC7021 controller test doubles."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import TypeAlias

from custom_components.linking_the_world_temp_ha.protocol import (
    YasHcpDecoder,
    YasHcpFrame,
    iter_tlvs,
    tlv,
)

FrameReply: TypeAlias = YasHcpFrame | bytes | None
CommandHandler: TypeAlias = Callable[[YasHcpFrame], Awaitable[None] | None]
CLIENT_CLOSE_TIMEOUT = 0.25


def _hello_reply() -> YasHcpFrame:
    return YasHcpFrame(1, 3, 0, b"")


def _login_reply() -> YasHcpFrame:
    return YasHcpFrame(2, 6, 0, b"")


@dataclass(slots=True)
class FakeControllerBehavior:
    """Program the fake controller's deterministic connection behavior."""

    hello_reply: FrameReply = field(default_factory=_hello_reply)
    login_reply: FrameReply = field(default_factory=_login_reply)
    close_after_stage: str | None = None
    stage_delays: dict[str, float] = field(default_factory=dict)
    fragment_size: int | None = None

    def delay_for(self, stage: str) -> float:
        """Return the configured delay for one protocol stage."""
        return self.stage_delays.get(stage, 0.0)


class FakeMC7021Server:
    """Programmable async MC7021 TCP server for integration tests."""

    def __init__(self, behavior: FakeControllerBehavior | None = None) -> None:
        self.behavior = behavior or FakeControllerBehavior()
        self.host = "127.0.0.1"
        self.port = 0
        self.received_frames: list[YasHcpFrame] = []
        self.received_frame_event = asyncio.Event()
        self._server: asyncio.AbstractServer | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._client_tasks: set[asyncio.Task[None]] = set()
        self._sequence = 0
        self.on_command: CommandHandler | None = None

    async def async_start(self) -> None:
        """Start accepting a single local controller connection."""
        self._server = await asyncio.start_server(
            self._async_handle_client, self.host, 0
        )
        socket = self._server.sockets[0]
        self.host, self.port = socket.getsockname()[:2]

    async def async_stop(self) -> None:
        """Close the connection and stop the local TCP listener."""
        await self.async_close_client()
        if self._server is not None:
            server = self._server
            server.close()
            client_tasks = tuple(self._client_tasks)
            for task in client_tasks:
                task.cancel()
            if client_tasks:
                await asyncio.gather(*client_tasks, return_exceptions=True)
            try:
                await asyncio.wait_for(server.wait_closed(), CLIENT_CLOSE_TIMEOUT)
            except asyncio.TimeoutError:
                # Python 3.12 can keep Server.wait_closed() blocked after an
                # accepted transport has been cancelled by a HA entry unload.
                # The handler tasks above are already awaited, so this is only
                # the interpreter's server bookkeeping and is safe to bound.
                pass
            self._server = None

    async def async_send_status(self, body: bytes) -> None:
        """Send a valid MC7021 status frame to the connected client."""
        frame = YasHcpFrame(5, 0x0C, self._next_sequence(), body)
        await self.async_send_frames(frame)

    async def async_send_malformed(self, data: bytes) -> None:
        """Send raw malformed transport bytes to exercise decoder recovery."""
        await self._async_write(data)

    async def async_send_frames(
        self, *frames: YasHcpFrame, fragment_size: int | None = None
    ) -> None:
        """Send one or more frames, optionally fragmented across writes."""
        payload = b"".join(frame.encode() for frame in frames)
        await self._async_write(
            payload,
            self.behavior.fragment_size if fragment_size is None else fragment_size,
        )

    async def async_close_client(self) -> None:
        """Close the active client connection when a test needs an EOF."""
        writer = self._writer
        self._writer = None
        if writer is not None:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), CLIENT_CLOSE_TIMEOUT)
            except (asyncio.TimeoutError, OSError):
                # A Home Assistant config-entry removal may already have closed
                # the peer. Do not let the fixture teardown wait forever for a
                # TCP FIN that the test runtime will never deliver.
                writer.transport.abort()

    async def _async_handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._client_tasks.add(task)
        self._writer = writer
        decoder = YasHcpDecoder()
        if self.behavior.close_after_stage == "connect":
            await self.async_close_client()
            return
        try:
            while data := await reader.read(4096):
                for frame in decoder.feed(data):
                    self.received_frames.append(frame)
                    self.received_frame_event.set()
                    await self._async_handle_frame(frame)
        except ConnectionError:
            # Home Assistant may reset a short-lived validation connection as
            # soon as a config-flow handshake succeeds. That is a normal peer
            # shutdown for this programmable test controller.
            pass
        finally:
            if self._writer is writer:
                self._writer = None
            self._client_tasks.discard(task)

    async def _async_handle_frame(self, frame: YasHcpFrame) -> None:
        if (frame.kind, frame.opcode) == (1, 1):
            await self._async_respond("hello", frame, self.behavior.hello_reply)
        elif (frame.kind, frame.opcode) == (2, 4):
            await self._async_respond("login", frame, self.behavior.login_reply)
        elif (frame.kind, frame.opcode) == (3, 7):
            category = next(
                (value for tag, value in iter_tlvs(frame.body) if tag == 0x000F),
                b"",
            )
            await self._async_respond(
                "status",
                frame,
                YasHcpFrame(5, 0x0C, frame.sequence, tlv(0x000F, category)),
            )
        elif (frame.kind, frame.opcode) == (4, 9) and self.on_command is not None:
            result = self.on_command(frame)
            if result is not None:
                await result

    async def _async_respond(
        self, stage: str, request: YasHcpFrame, reply: FrameReply
    ) -> None:
        delay = self.behavior.delay_for(stage)
        if delay:
            await asyncio.sleep(delay)
        try:
            if isinstance(reply, YasHcpFrame):
                await self.async_send_frames(replace(reply, sequence=request.sequence))
            elif isinstance(reply, bytes):
                await self._async_write(reply)
        except RuntimeError as error:
            if error.args != ("No MC7021 client is connected",):
                raise
            return
        if self.behavior.close_after_stage == stage:
            await self.async_close_client()

    async def _async_write(
        self, payload: bytes, fragment_size: int | None = None
    ) -> None:
        if self._writer is None:
            raise RuntimeError("No MC7021 client is connected")
        if fragment_size is not None and fragment_size <= 0:
            raise ValueError("fragment_size must be positive")
        if fragment_size is None:
            self._writer.write(payload)
            await self._writer.drain()
            return
        for offset in range(0, len(payload), fragment_size):
            self._writer.write(payload[offset : offset + fragment_size])
            await self._writer.drain()

    def _next_sequence(self) -> int:
        sequence = self._sequence
        self._sequence = (self._sequence + 1) & 0xFFFF
        return sequence
