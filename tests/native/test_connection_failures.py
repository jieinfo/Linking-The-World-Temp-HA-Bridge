"""Connection-stage failure classification tests for the MC7021 client."""

from __future__ import annotations

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
from tests.helpers import FakeControllerBehavior, FakeMC7021Server


pytestmark = pytest.mark.usefixtures("socket_enabled")


def _client(server: FakeMC7021Server) -> AsyncMoorgenClient:
    return AsyncMoorgenClient(
        server.host,
        server.port,
        "admin",
        "password",
    )


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
            login_reply=YasHcpFrame(2, 5, 0, tlv(0x031C, b"\x01")).encode(),
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
