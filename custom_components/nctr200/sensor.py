"""Read-only values from the controller."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NctrConfigEntry
from . import messages as m
from .entity import NctrEntity
from .hub import DeviceState, NctrHub

MODES = [
    m.MODE_ROOM,
    m.MODE_ONDOL,
    m.MODE_RESERVATION,
    m.MODE_AWAY,
    m.OVERLAY_BATH,
    m.OVERLAY_HOT_WATER_ONLY,
    "off",
]


def _mode(state: DeviceState) -> str | None:
    if state.power_on is False:
        return "off"
    return state.overlay or state.mode


@dataclass(frozen=True, kw_only=True)
class NctrSensorDescription(SensorEntityDescription):
    value: Callable[[DeviceState], int | str | datetime | None]


SENSORS = (
    NctrSensorDescription(
        key="room_current",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        value=lambda s: s.room_current,
    ),
    NctrSensorDescription(
        key="heating_water_current",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        value=lambda s: s.heating_water_current,
    ),
    NctrSensorDescription(
        key="mode",
        device_class=SensorDeviceClass.ENUM,
        options=MODES,
        value=_mode,
    ),
    NctrSensorDescription(
        key="burner_level",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        state_class=SensorStateClass.MEASUREMENT,
        value=lambda s: s.burner_level,
    ),
    NctrSensorDescription(
        key="status_flags",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value=lambda s: None if s.flags is None else f"{s.flags:02X}",
    ),
    NctrSensorDescription(
        key="last_report",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda s: s.last_report,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NctrConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    hub = entry.runtime_data
    async_add_entities(NctrSensor(entry, hub, description) for description in SENSORS)


class NctrSensor(NctrEntity, SensorEntity):
    entity_description: NctrSensorDescription

    def __init__(
        self, entry: NctrConfigEntry, hub: NctrHub, description: NctrSensorDescription
    ) -> None:
        super().__init__(entry, hub, description.key)
        self.entity_description = description
        # The time of the last report stays meaningful while the device is offline.
        self._requires_ready = description.key != "last_report"

    @property
    def native_value(self) -> int | str | datetime | None:
        return self.entity_description.value(self.hub.state)
