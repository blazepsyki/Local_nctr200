"""Diagnostics download."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import NctrConfigEntry
from .const import CONF_NODE


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: NctrConfigEntry
) -> dict[str, Any]:
    return {
        "entry": async_redact_data({**entry.data, **entry.options}, {CONF_NODE}),
        "hub": entry.runtime_data.diagnostics(),
    }
