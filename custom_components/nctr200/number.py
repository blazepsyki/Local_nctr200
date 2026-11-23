"""Water temperature targets and reservation values."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
    RestoreNumber,
)
from homeassistant.const import UnitOfTemperature, UnitOfTime
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


@dataclass(frozen=True, kw_only=True)
class NctrReservationDescription(NumberEntityDescription):
    index: int  # 0 stop hours, 1 run minutes


RESERVATION_NUMBERS = (
    NctrReservationDescription(
        key="reservation_stop_hours",
        index=0,
        native_min_value=m.RESERVATION_STOP_RANGE[0],
        native_max_value=m.RESERVATION_STOP_RANGE[1],
        native_unit_of_measurement=UnitOfTime.HOURS,
    ),
    NctrReservationDescription(
        key="reservation_run_minutes",
        index=1,
        native_min_value=m.RESERVATION_RUN_RANGE[0],
        native_max_value=m.RESERVATION_RUN_RANGE[1],
        native_unit_of_measurement=UnitOfTime.MINUTES,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NctrConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    hub = entry.runtime_data
    async_add_entities(
        [
            *(NctrNumber(entry, hub, description) for description in NUMBERS),
            *(NctrReservationNumber(entry, hub, d) for d in RESERVATION_NUMBERS),
        ]
    )


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


class NctrReservationNumber(NctrEntity, RestoreNumber):
    """Reservation stop hours or run minutes.

    The controller reports these only when its reservation button is pressed, so the
    value picked here is kept across restarts. In reservation mode a change is sent
    at once; otherwise it is used when the reservation preset is selected.
    """

    entity_description: NctrReservationDescription
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX

    def __init__(
        self, entry: NctrConfigEntry, hub: NctrHub, description: NctrReservationDescription
    ) -> None:
        super().__init__(entry, hub, description.key)
        self.entity_description = description

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if last is not None and last.native_value is not None:
            self.hub.restore_reservation_value(
                self.entity_description.index, round(last.native_value)
            )

    @property
    def native_value(self) -> float | None:
        return self.hub.reservation_value(self.entity_description.index)

    async def async_set_native_value(self, value: float) -> None:
        index = self.entity_description.index
        await self._run(self.hub.async_set_reservation_value(index, round(value)))
