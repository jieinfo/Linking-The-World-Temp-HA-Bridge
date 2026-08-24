"""Connection-stage failure classification tests for the MC7021 client."""

from __future__ import annotations

import asyncio

import pytest

from custom_components.linking_the_world_temp_ha.protocol import (
    AsyncMoorgenClient,
    AuthenticationRejected,
    CannotConnect,
    HandshakeTimeout,
    IncompatibleProtocol,
    LoginTimeout,
    MoorgenConnectionError,
    TcpConnectError,
    YasHcpFrame,
    tlv,
)
from custom_components.linking_the_world_temp_ha.health import HealthTracker
from custom_components.linking_the_world_temp_ha.runtime import (
    ConnectionStage,
    FailureKind,
)
from tests.helpers import FakeControllerBehavior, FakeMC7021Server


pytestmark = pytest.mark.usefixtures("socket_enabled")


def _client(server: FakeMC7021Server) -> AsyncMoorgenClient:
    return AsyncMoorgenClient(
        server.host,
        server.port,
        "admin",
        "password",
    )


def _captured_login_rejection(body: bytes = tlv(0x031C, b"\x01")) -> bytes:
    """Return the captured rejection shape without sequence rewriting by the fake."""
    return YasHcpFrame(2, 5, 0, body).encode()


async def _wait_for_received_frame(
    server: FakeMC7021Server, kind: int, opcode: int
) -> None:
    while not any(
        (frame.kind, frame.opcode) == (kind, opcode)
        for frame in server.received_frames
    ):
        await asyncio.sleep(0)


@pytest.fixture
def short_protocol_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep deliberately silent loopback peers fast during failure tests."""
    monkeypatch.setattr(
        "custom_components.linking_the_world_temp_ha.protocol.CONNECT_TIMEOUT", 0.01
    )
    monkeypatch.setattr(
        "custom_components.linking_the_world_temp_ha.protocol.HELLO_TIMEOUT", 0.01
    )
    monkeypatch.setattr(
        "custom_components.linking_the_world_temp_ha.protocol.LOGIN_TIMEOUT", 0.01
    )


async def test_protocol_classifies_a_refused_tcp_connection() -> None:
    """Refusing the TCP connection must not be reported as a protocol failure."""
    client = AsyncMoorgenClient("127.0.0.1", 1, "admin", "password")

    with pytest.raises(TcpConnectError) as raised:
        await client.connect()

    assert isinstance(raised.value.__cause__, OSError)


async def test_protocol_classifies_a_missing_hello_reply(
    short_protocol_timeouts: None,
) -> None:
    """A controller that never acknowledges hello must time out at handshake."""
    server = FakeMC7021Server(FakeControllerBehavior(hello_reply=None))
    await server.async_start()
    client = _client(server)

    try:
        with pytest.raises(HandshakeTimeout) as raised:
            await client.connect()
        assert isinstance(raised.value.__cause__, TimeoutError)
    finally:
        await client.close()
        await server.async_stop()


async def test_protocol_classifies_a_malformed_hello_reply() -> None:
    """A framed but malformed hello reply is not a handshake timeout."""
    server = FakeMC7021Server(
        FakeControllerBehavior(hello_reply=YasHcpFrame(1, 0x7F, 0, b""))
    )
    await server.async_start()
    client = _client(server)

    try:
        with pytest.raises(IncompatibleProtocol):
            await client.connect()
    finally:
        await client.close()
        await server.async_stop()


async def test_protocol_classifies_a_missing_login_reply(
    short_protocol_timeouts: None,
) -> None:
    """A controller that omits login acknowledgement must time out at login."""
    server = FakeMC7021Server(FakeControllerBehavior(login_reply=None))
    await server.async_start()
    client = _client(server)

    try:
        with pytest.raises(LoginTimeout) as raised:
            await client.connect()
        assert isinstance(raised.value.__cause__, TimeoutError)
    finally:
        await client.close()
        await server.async_stop()


async def test_protocol_classifies_the_captured_login_rejection() -> None:
    """Only the observed rejection frame represents invalid credentials."""
    server = FakeMC7021Server(
        FakeControllerBehavior(
            login_reply=_captured_login_rejection(),
            close_after_stage="login",
        )
    )
    await server.async_start()
    client = _client(server)

    try:
        with pytest.raises(AuthenticationRejected):
            await client.connect()
    finally:
        await client.close()
        await server.async_stop()


@pytest.mark.parametrize(
    "body",
    [tlv(0x031C, b"\x00"), tlv(0x031C, b"\x01\x00")],
    ids=["wrong_value", "wrong_length"],
)
async def test_protocol_does_not_treat_invalid_rejection_tlvs_as_bad_credentials(
    body: bytes,
) -> None:
    """Only a one-byte rejection TLV with value one represents bad credentials."""
    server = FakeMC7021Server(
        FakeControllerBehavior(login_reply=_captured_login_rejection(body))
    )
    await server.async_start()
    client = _client(server)

    try:
        with pytest.raises(IncompatibleProtocol) as raised:
            await client.connect()
        assert not isinstance(raised.value, AuthenticationRejected)
    finally:
        await client.close()
        await server.async_stop()


async def test_protocol_discards_pre_login_rejection_frames() -> None:
    """A rejection queued beside hello must not be attributed to the new login."""
    server = FakeMC7021Server(
        FakeControllerBehavior(
            hello_reply=(
                YasHcpFrame(1, 3, 0, b"").encode()
                + _captured_login_rejection()
            )
        )
    )
    await server.async_start()
    client = _client(server)

    try:
        await client.connect()
        assert client.is_ready
    finally:
        await client.close()
        await server.async_stop()


async def test_protocol_discards_pre_login_rejection_before_frame_callbacks() -> None:
    """A yielding hello callback cannot move a stale rejection across login."""
    hello_callback_started = asyncio.Event()
    release_hello_callback = asyncio.Event()
    server = FakeMC7021Server(
        FakeControllerBehavior(
            hello_reply=(
                YasHcpFrame(1, 3, 0, b"").encode()
                + _captured_login_rejection()
            )
        )
    )
    await server.async_start()
    client = _client(server)

    async def pause_after_hello(frame: YasHcpFrame) -> None:
        if (frame.kind, frame.opcode) == (1, 3):
            hello_callback_started.set()
            await release_hello_callback.wait()

    client.on_frame = pause_after_hello
    connect_task = asyncio.create_task(client.connect())

    try:
        await asyncio.wait_for(hello_callback_started.wait(), 1)
        await asyncio.wait_for(_wait_for_received_frame(server, 2, 4), 1)
        release_hello_callback.set()
        await connect_task
        assert client.is_ready
    finally:
        release_hello_callback.set()
        await client.close()
        await server.async_stop()


async def test_protocol_rebuilds_the_inbox_before_reconnecting() -> None:
    """Frames from a closed session cannot classify credentials on the next one."""
    server = FakeMC7021Server()
    await server.async_start()
    client = _client(server)
    client._inbox.put_nowait(YasHcpFrame(2, 5, 0, tlv(0x031C, b"\x01")))
    await client.close()

    try:
        await client.connect()
        assert client.is_ready
    finally:
        await client.close()
        await server.async_stop()


async def test_protocol_cancelling_a_waiter_does_not_consume_a_later_frame() -> None:
    """Cancelling a wait must not leave a queue-get task behind to steal a frame."""
    client = AsyncMoorgenClient("127.0.0.1", 1, "admin", "password")
    waiter = asyncio.create_task(client._wait_for_opcodes(1, {3}, 1))
    await asyncio.sleep(0)
    waiter.cancel()

    try:
        with pytest.raises(asyncio.CancelledError):
            await waiter
        frame = YasHcpFrame(1, 3, 0, b"")
        client._inbox.put_nowait(frame)
        assert await client._wait_for_opcodes(1, {3}, 0.01) == frame
    finally:
        await client.close()


async def test_protocol_cancelling_connect_closes_the_session() -> None:
    """Cancelling connect must close its reader, writer, and reader task."""
    server = FakeMC7021Server(FakeControllerBehavior(hello_reply=None))
    await server.async_start()
    client = _client(server)
    connect_task = asyncio.create_task(client.connect())

    try:
        await asyncio.wait_for(server.received_frame_event.wait(), 1)
        connect_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await connect_task
        assert client._reader is None
        assert client._writer is None
        assert not client.reader_alive
    finally:
        await client.close()
        await server.async_stop()


async def test_protocol_accepts_the_captured_login_success() -> None:
    """The observed login-success opcode starts a ready controller session."""
    server = FakeMC7021Server(
        FakeControllerBehavior(login_reply=YasHcpFrame(2, 6, 0, b"").encode())
    )
    await server.async_start()
    client = _client(server)

    try:
        await client.connect()
        assert client.is_ready
    finally:
        await client.close()
        await server.async_stop()


async def test_protocol_does_not_treat_an_unknown_login_reply_as_bad_credentials(
) -> None:
    """An unrecognized login frame signals incompatibility, never bad credentials."""
    server = FakeMC7021Server(
        FakeControllerBehavior(login_reply=YasHcpFrame(2, 5, 0, b"").encode())
    )
    await server.async_start()
    client = _client(server)

    try:
        with pytest.raises(IncompatibleProtocol) as raised:
            await client.connect()
        assert not isinstance(raised.value, AuthenticationRejected)
    finally:
        await client.close()
        await server.async_stop()


async def test_protocol_does_not_treat_login_eof_as_bad_credentials(
    short_protocol_timeouts: None,
) -> None:
    """An EOF before the rejection frame remains a login-stage transport failure."""
    server = FakeMC7021Server(
        FakeControllerBehavior(login_reply=None, close_after_stage="login")
    )
    await server.async_start()
    client = _client(server)

    try:
        with pytest.raises(LoginTimeout) as raised:
            await client.connect()
        assert not isinstance(raised.value, AuthenticationRejected)
        assert isinstance(raised.value.__cause__, ConnectionError)
    finally:
        await client.close()
        await server.async_stop()


def test_protocol_keeps_cannot_connect_as_the_compatibility_alias() -> None:
    """Legacy callers can catch every new connection-stage failure uniformly."""
    assert CannotConnect is MoorgenConnectionError
    assert MoorgenConnectionError.__bases__ == (Exception,)


def test_health_tracker_bounds_and_sanitizes_connection_history() -> None:
    """Diagnostics retain only concise, privacy-safe lifecycle context."""
    health = HealthTracker(history_size=2, latency_size=2)
    health.mark_stage(ConnectionStage.CONNECTING)
    health.mark_stage(ConnectionStage.HANDSHAKING)
    health.mark_stage(ConnectionStage.AUTHENTICATING)
    health.record_failure(
        FailureKind.AUTH_REJECTED,
        "password=secret host=10.10.1.246 mac=ff00ffffffff01ff body="
        "00112233445566778899aabbccddeeff",
    )
    health.record_confirmation_latency(0.1)
    health.record_confirmation_latency(0.2)
    health.record_confirmation_latency(0.3)
    health.increment("commands_sent")

    snapshot = health.snapshot()
    assert snapshot["stage"] == ConnectionStage.AUTHENTICATING.value
    assert snapshot["failure_kind"] == FailureKind.AUTH_REJECTED.value
    assert snapshot["counters"]["commands_sent"] == 1
    assert len(snapshot["stage_history"]) == 2
    assert len(snapshot["confirmation_latencies"]) == 2
    message = snapshot["failure_history"][-1]["message"]
    assert "secret" not in message
    assert "10.10.1.246" not in message
    assert "ff00ffffffff01ff" not in message
    assert "00112233445566778899aabbccddeeff" not in message


@pytest.mark.parametrize(
    ("host", "message"),
    [
        (
            "house-controller.lan",
            "Could not connect to MC7021 at house-controller.lan:9000",
        ),
        (
            "10.10.1.246",
            "Could not connect to MC7021 at 10.10.1.246:9000",
        ),
        (
            "2001:db8:85a3::8a2e:370:7334",
            "Could not connect to MC7021 at [2001:db8:85a3::8a2e:370:7334]:9000",
        ),
    ],
    ids=["dns", "ipv4", "ipv6"],
)
def test_health_tracker_redacts_explicit_controller_endpoints(
    host: str, message: str
) -> None:
    """Configured controller endpoints cannot escape diagnostics history."""
    health = HealthTracker()

    health.record_failure(FailureKind.TCP_TIMEOUT, message, secrets={"host": host})

    recorded = health.snapshot()["failure_history"][-1]["message"]
    assert host not in recorded
    assert "<redacted-host>" in recorded


@pytest.mark.parametrize(
    ("message", "secret"),
    [
        ("Could not connect at house-controller.lan:9000", "house-controller.lan"),
        ("Could not connect at 10.10.1.246:9000", "10.10.1.246"),
        (
            "Could not connect at [2001:db8:85a3::8a2e:370:7334]:9000",
            "2001:db8:85a3::8a2e:370:7334",
        ),
    ],
    ids=["dns", "ipv4", "ipv6"],
)
def test_health_tracker_defensively_redacts_endpoint_messages(
    message: str, secret: str
) -> None:
    """Unexpected transport messages remain safe even without explicit values."""
    health = HealthTracker()

    health.record_failure(FailureKind.TCP_TIMEOUT, message)

    assert secret not in health.snapshot()["failure_history"][-1]["message"]


def test_health_tracker_snapshot_does_not_share_history_records() -> None:
    """Callers must not be able to mutate bounded health history in place."""
    health = HealthTracker()
    health.mark_stage(ConnectionStage.CONNECTING)
    health.record_failure(FailureKind.TCP_TIMEOUT, "controller unavailable")

    snapshot = health.snapshot()
    snapshot["stage_history"][0]["stage"] = "mutated"
    snapshot["failure_history"][0]["message"] = "mutated"

    next_snapshot = health.snapshot()
    assert next_snapshot["stage_history"][0]["stage"] == "connecting"
    assert next_snapshot["failure_history"][0]["message"] == "controller unavailable"
