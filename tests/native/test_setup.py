"""Real Home Assistant integration setup tests."""

import asyncio

import pytest
from homeassistant.config_entries import ConfigEntryState

from custom_components.linking_the_world_temp_ha.protocol import AsyncMoorgenClient
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
