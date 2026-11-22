"""A real Home Assistant core for integration tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio
from homeassistant import bootstrap, loader
from homeassistant.config_entries import ConfigEntries
from homeassistant.core import HomeAssistant
from homeassistant.core_config import DATA_CUSTOMIZE
from homeassistant.helpers.entity_values import EntityValues


@pytest_asyncio.fixture
async def hass(tmp_path: Path) -> AsyncIterator[HomeAssistant]:
    instance = HomeAssistant(str(tmp_path))
    await instance.config.async_set_time_zone("Asia/Seoul")
    loader.async_setup(instance)
    instance.data[DATA_CUSTOMIZE] = EntityValues({})
    instance.config_entries = ConfigEntries(instance, {})
    assert await bootstrap.async_load_base_functionality(instance)
    # Let the loader find custom_components/ in this repository.
    instance.data.pop(loader.DATA_CUSTOM_COMPONENTS, None)
    await instance.async_start()
    try:
        yield instance
    finally:
        await instance.async_stop(force=True)
