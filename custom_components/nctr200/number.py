"""Water temperature targets."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NctrConfigEntry
from . import messages as m
from .entity import NctrEntity
from .hub import DeviceState, NctrHub


@dataclass(frozen=True, kw_only=True)
class NctrNumberDescription(NumberEntityDescription):
    value: Callable[[DeviceState], int | None]
    setter: Callable[[NctrHub, int], Awaitable[None]]


NUMBERS = (
    NctrNumberDescription(
        key="ondol_target",
        native_min_value=m.ONDOL_RANGE[0],
        native_max_value=m.ONDOL_RANGE[1],
        value=lambda s: s.ondol_target,
        setter=lambda hub, v: hub.async_set_ondol_target(v),
    ),
    NctrNumberDescription(
        key="hot_water_target",
        native_min_value=m.HOT_WATER_RANGE[0],
        native_max_value=m.HOT_WATER_RANGE[1],
        value=lambda s: s.hot_water_target,
        setter=lambda hub, v: hub.async_set_hot_water_target(v),
    ),
    # The controller has not been seen reporting this value; it shows the last
    # value set here or reported by a 0103 event.
    NctrNumberDescription(
        key="heating_water_target",
        native_min_value=m.HEATING_WATER_RANGE[0],
        native_max_value=m.HEATING_WATER_RANGE[1],
        entity_registry_enabled_default=False,
        value=lambda s: s.heating_water_target,
        setter=lambda hub, v: hub.async_set_heating_water_target(v),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NctrConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    hub = entry.runtime_data
    async_add_entities(NctrNumber(entry, hub, description) for description in NUMBERS)


class NctrNumber(NctrEntity, NumberEntity):
    entity_description: NctrNumberDescription
    _attr_device_class = NumberDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX

    def __init__(
        self, entry: NctrConfigEntry, hub: NctrHub, description: NctrNumberDescription
    ) -> None:
        super().__init__(entry, hub, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float | None:
        return self.entity_description.value(self.hub.state)

    async def async_set_native_value(self, value: float) -> None:
        await self._run(self.entity_description.setter(self.hub, round(value)))
