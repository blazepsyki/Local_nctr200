"""Power as a switch for dashboard cards.

The climate entity's on/off does the same; this one is hidden by default.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NctrConfigEntry
from .entity import NctrEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NctrConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([NctrPowerSwitch(entry, entry.runtime_data, "power")])


class NctrPowerSwitch(NctrEntity, SwitchEntity):
    _attr_entity_registry_visible_default = False

    @property
    def is_on(self) -> bool | None:
        return self.hub.state.power_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._run(self.hub.async_set_power(True))

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._run(self.hub.async_set_power(False))
