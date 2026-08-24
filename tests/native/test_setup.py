"""Real Home Assistant integration setup tests."""

import asyncio

import pytest
from homeassistant.config_entries import ConfigEntryState

from custom_components.linking_the_world_temp_ha.binary_sensor import (
    ControllerConnectionSensor,
)
from custom_components.linking_the_world_temp_ha.const import DOMAIN
from custom_components.linking_the_world_temp_ha.protocol import (
    AsyncMoorgenClient,
    YasHcpDecoder,
    YasHcpFrame,
    tlv,
)
from custom_components.linking_the_world_temp_ha.runtime import (
    ConnectionStage,
    LinkingTempRuntime,
)
from tests.helpers import FakeControllerBehavior, FakeMC7021Server

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


async def test_setup_uses_real_home_assistant(
    hass, mock_config_entry, fake_controller
):
    """Load the integration through Home Assistant's config-entry lifecycle."""
    mock_config_entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED


async def test_setup_uses_typed_runtime_data_and_never_stores_hub_in_hass_data(
    hass, mock_config_entry, fake_controller
):
    """Platforms receive the entry runtime rather than a global hub lookup."""
    mock_config_entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    runtime = mock_config_entry.runtime_data
    assert isinstance(runtime, LinkingTempRuntime)
    assert runtime.hub.entry is mock_config_entry
    assert runtime.health is runtime.hub.health
    assert mock_config_entry.entry_id not in hass.data.get(DOMAIN, {})


async def test_connection_sensor_waits_for_ready_status_stream(
    hass, setup_integration, fake_controller
):
    """A completed TCP/login exchange alone is not controller availability."""
    runtime = setup_integration
    sensor = ControllerConnectionSensor(runtime.hub)

    async with asyncio.timeout(1):
        while runtime.health.stage is not ConnectionStage.READY:
            await asyncio.sleep(0)
    assert not runtime.hub.available
    assert not sensor.is_on

    await fake_controller.async_send_status(
        tlv(0x0004, runtime.hub.tech_system_mac)
        + tlv(0x000B, b"\x00")
        + tlv(0x000A, b"\x01\x01\x00")
    )
    async with asyncio.timeout(1):
        while not runtime.hub.available:
            await asyncio.sleep(0)

    assert sensor.is_on


async def test_connection_stage_transitions_follow_protocol_setup(
    hass, setup_integration
):
    """The lifecycle is observable in protocol order for one successful session."""
    runtime = setup_integration
    async with asyncio.timeout(1):
        while runtime.health.stage is not ConnectionStage.READY:
            await asyncio.sleep(0)

    stages = runtime.health.snapshot()["stage_history"]
    assert [item["stage"] for item in stages][-4:] == [
        ConnectionStage.CONNECTING.value,
        ConnectionStage.HANDSHAKING.value,
        ConnectionStage.AUTHENTICATING.value,
        ConnectionStage.READY.value,
    ]


async def test_fake_controller_handles_fragmented_malformed_and_status_frames(
    socket_enabled,
):
    """Exercise the transport conditions shared by native integration tests."""
    received_statuses: list[bytes] = []
    received_status = asyncio.Event()
    fake_controller = FakeMC7021Server(
        FakeControllerBehavior(fragment_size=3)
    )
    await fake_controller.async_start()
    client = AsyncMoorgenClient(
        fake_controller.host,
        fake_controller.port,
        "admin",
        "secret",
    )

    async def record_status(body: bytes) -> None:
        received_statuses.append(body)
        received_status.set()

    client.on_status = record_status
    try:
        await client.connect()
        received_statuses.clear()
        received_status.clear()
        await fake_controller.async_send_malformed(b"not a YAS HCP frame")
        await fake_controller.async_send_status(b"status body")
        await asyncio.wait_for(received_status.wait(), timeout=1)
    finally:
        await client.close()
        await fake_controller.async_stop()

    assert b"status body" in received_statuses
    assert [(frame.kind, frame.opcode) for frame in fake_controller.received_frames][
        :2
    ] == [(1, 1), (2, 4)]


async def test_fake_controller_delays_a_configured_stage_response(socket_enabled):
    """Hold a hello reply until the configured server-side delay expires."""
    fake_controller = FakeMC7021Server(
        FakeControllerBehavior(stage_delays={"hello": 0.05})
    )
    await fake_controller.async_start()
    reader, writer = await asyncio.open_connection(
        fake_controller.host, fake_controller.port
    )
    hello = YasHcpFrame(1, 1, 7, b"")
    response = YasHcpFrame(1, 3, 7, b"")
    response_task = asyncio.create_task(reader.readexactly(len(response.encode())))
    try:
        writer.write(hello.encode())
        await writer.drain()
        await asyncio.wait_for(fake_controller.received_frame_event.wait(), timeout=1)

        assert not response_task.done()
        assert YasHcpDecoder().feed(await response_task) == [response]
    finally:
        response_task.cancel()
        await asyncio.gather(response_task, return_exceptions=True)
        writer.close()
        await writer.wait_closed()
        await fake_controller.async_stop()


async def test_fake_controller_closes_after_a_configured_stage(socket_enabled):
    """Return EOF after the configured stage without a test-level delay."""
    fake_controller = FakeMC7021Server(
        FakeControllerBehavior(hello_reply=None, close_after_stage="hello")
    )
    await fake_controller.async_start()
    reader, writer = await asyncio.open_connection(
        fake_controller.host, fake_controller.port
    )
    try:
        writer.write(YasHcpFrame(1, 1, 0, b"").encode())
        await writer.drain()

        assert await asyncio.wait_for(reader.read(), timeout=1) == b""
    finally:
        writer.close()
        await writer.wait_closed()
        await fake_controller.async_stop()


async def test_fake_controller_writes_multiple_status_frames_together(
    socket_enabled,
):
    """Deliver concatenated frames through the production client decoder."""
    statuses: list[bytes] = []
    received_statuses = asyncio.Event()
    fake_controller = FakeMC7021Server()
    await fake_controller.async_start()
    client = AsyncMoorgenClient(
        fake_controller.host,
        fake_controller.port,
        "admin",
        "secret",
    )

    async def record_status(body: bytes) -> None:
        if body in {b"first", b"second"}:
            statuses.append(body)
        if len(statuses) == 2:
            received_statuses.set()

    client.on_status = record_status
    try:
        await client.connect()
        await fake_controller.async_send_frames(
            YasHcpFrame(5, 0x0C, 10, b"first"),
            YasHcpFrame(5, 0x0C, 11, b"second"),
        )
        await asyncio.wait_for(received_statuses.wait(), timeout=1)
    finally:
        await client.close()
        await fake_controller.async_stop()

    assert statuses == [b"first", b"second"]
