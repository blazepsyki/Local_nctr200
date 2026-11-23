"""Hot water temperature.

The ondol temperature is the climate dial in ondol mode, and the reservation
values are the climate entity's two dropdowns.
"""

from __future__ import annotations

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NctrConfigEntry
from . import messages as m
from .entity import NctrEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NctrConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([NctrHotWaterNumber(entry, entry.runtime_data, "hot_water_target")])


class NctrHotWaterNumber(NctrEntity, NumberEntity):
    _attr_device_class = NumberDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_native_min_value = m.HOT_WATER_RANGE[0]
    _attr_native_max_value = m.HOT_WATER_RANGE[1]
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX

    @property
    def native_value(self) -> float | None:
        return self.hub.state.hot_water_target

    async def async_set_native_value(self, value: float) -> None:
        await self._run(self.hub.async_set_hot_water_target(round(value)))
