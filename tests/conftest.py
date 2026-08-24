"""Fixtures for native Home Assistant integration tests."""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.linking_the_world_temp_ha.const import (
    CONF_ALLOW_CONTROL,
    CONF_CLIENT_ID,
    CONF_COMMAND_CONFIRMATION_TIMEOUT,
    CONF_COMMAND_MIN_INTERVAL,
    CONF_CONTROLLER_SILENCE_TIMEOUT,
    CONF_TECH_SYSTEM_MAC,
    CONF_THERMOSTAT_OFFLINE_AFTER,
    DEFAULT_ALLOW_CONTROL,
    DEFAULT_CLIENT_ID,
    DEFAULT_COMMAND_CONFIRMATION_TIMEOUT,
    DEFAULT_COMMAND_MIN_INTERVAL,
    DEFAULT_CONTROLLER_SILENCE_TIMEOUT,
    DEFAULT_TECH_SYSTEM_MAC,
    DEFAULT_THERMOSTAT_OFFLINE_AFTER,
    DOMAIN,
)
from custom_components.linking_the_world_temp_ha.runtime import ConnectionStage
from tests.helpers import FakeMC7021Server


SETUP_READY_TIMEOUT = 1


@pytest_asyncio.fixture
async def fake_controller(socket_enabled) -> FakeMC7021Server:
    """Run one deterministic local MC7021 controller server."""
    server = FakeMC7021Server()
    await server.async_start()
    yield server
    await server.async_stop()


@pytest.fixture
def mock_config_entry(fake_controller: FakeMC7021Server) -> MockConfigEntry:
    """Build the default native integration configuration entry."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Linking The World Temp HA",
        data={
            CONF_HOST: fake_controller.host,
            CONF_PORT: fake_controller.port,
            CONF_USERNAME: "admin",
            CONF_PASSWORD: "secret",
            CONF_CLIENT_ID: DEFAULT_CLIENT_ID,
            CONF_TECH_SYSTEM_MAC: DEFAULT_TECH_SYSTEM_MAC,
        },
        options={
            CONF_ALLOW_CONTROL: DEFAULT_ALLOW_CONTROL,
            CONF_COMMAND_MIN_INTERVAL: DEFAULT_COMMAND_MIN_INTERVAL,
            CONF_COMMAND_CONFIRMATION_TIMEOUT: DEFAULT_COMMAND_CONFIRMATION_TIMEOUT,
            CONF_CONTROLLER_SILENCE_TIMEOUT: DEFAULT_CONTROLLER_SILENCE_TIMEOUT,
            CONF_THERMOSTAT_OFFLINE_AFTER: DEFAULT_THERMOSTAT_OFFLINE_AFTER,
        },
    )


@pytest_asyncio.fixture
async def setup_integration(
    hass, mock_config_entry: MockConfigEntry, fake_controller: FakeMC7021Server
):
    """Load the entry after its fake session and runtime are ready for pushes."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    runtime = mock_config_entry.runtime_data
    await fake_controller.async_wait_for_handshake()
    ready = asyncio.Event()

    def mark_ready() -> None:
        if runtime.hub.health.stage is ConnectionStage.READY:
            ready.set()

    remove_listener = runtime.hub.async_add_listener(mark_ready)
    mark_ready()
    try:
        await asyncio.wait_for(ready.wait(), timeout=SETUP_READY_TIMEOUT)
    finally:
        remove_listener()
    return runtime
