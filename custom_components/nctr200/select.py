"""Operating mode and reservation values as dropdowns for dashboard cards.

They mirror the climate entity's preset and its two reservation dropdowns, so an
entities card can show them like other devices' select rows. Hidden by default:
the climate entity already offers them on the device page.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NctrConfigEntry
from .climate import PRESETS, RUN_MINUTES, STOP_HOURS
from .entity import NctrEntity
from .hub import NctrHub


@dataclass(frozen=True, kw_only=True)
class NctrSelectDescription(SelectEntityDescription):
    current: Callable[[NctrHub], str | None]
    choose: Callable[[NctrHub, str], Awaitable[None]]


def _text(value: int | None) -> str | None:
    return None if value is None else str(value)


SELECTS = (
    NctrSelectDescription(
        key="operating_mode",
        options=PRESETS,
        current=lambda hub: hub.state.overlay or hub.state.mode,
        choose=lambda hub, option: hub.async_select_preset(option),
    ),
    NctrSelectDescription(
        key="reservation_stop_time",
        options=STOP_HOURS,
        current=lambda hub: _text(hub.reservation_value(0)),
        choose=lambda hub, option: hub.async_set_reservation_value(0, int(option)),
    ),
    NctrSelectDescription(
        key="reservation_run_time",
        options=RUN_MINUTES,
        current=lambda hub: _text(hub.reservation_value(1)),
        choose=lambda hub, option: hub.async_set_reservation_value(1, int(option)),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NctrConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    hub = entry.runtime_data
    async_add_entities(NctrSelect(entry, hub, description) for description in SELECTS)


class NctrSelect(NctrEntity, SelectEntity):
    entity_description: NctrSelectDescription
    _attr_entity_registry_visible_default = False

    def __init__(
        self, entry: NctrConfigEntry, hub: NctrHub, description: NctrSelectDescription
    ) -> None:
        super().__init__(entry, hub, description.key)
        self.entity_description = description

    @property
    def current_option(self) -> str | None:
        return self.entity_description.current(self.hub)

    async def async_select_option(self, option: str) -> None:
        await self._run(self.entity_description.choose(self.hub, option))
