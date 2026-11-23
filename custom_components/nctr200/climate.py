"""Boiler climate entity: power, heating mode and room target."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_platform
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoredExtraData, RestoreEntity

from . import NctrConfigEntry
from . import messages as m
from .entity import NctrEntity

PRESET_ROOM = m.MODE_ROOM
PRESET_ONDOL = m.MODE_ONDOL
PRESET_AWAY = m.MODE_AWAY
PRESET_RESERVATION = m.MODE_RESERVATION
PRESET_BATH = m.OVERLAY_BATH
PRESET_HOT_WATER_ONLY = m.OVERLAY_HOT_WATER_ONLY
PRESETS = [
    PRESET_ROOM,
    PRESET_ONDOL,
    PRESET_RESERVATION,
    PRESET_AWAY,
    PRESET_BATH,
    PRESET_HOT_WATER_ONLY,
]

# The climate dialog offers two dropdowns besides the dial, mode and preset: fan
# mode and swing mode. They carry the reservation stop hours and run minutes;
# their names and values are translated to say so.
STOP_HOURS = [str(h) for h in range(m.RESERVATION_STOP_RANGE[0], m.RESERVATION_STOP_RANGE[1] + 1)]
RUN_MINUTES = [str(n) for n in range(m.RESERVATION_RUN_RANGE[0], m.RESERVATION_RUN_RANGE[1] + 1)]

RESERVATION_ATTRIBUTES = ("reservation_stop_hours", "reservation_run_minutes")

SERVICE_SET_RESERVATION = "set_reservation"
SERVICE_SET_SCHEDULE = "set_schedule"


def _text(value: int | None) -> str | None:
    return None if value is None else str(value)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NctrConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([NctrClimate(entry, entry.runtime_data, "boiler")])
    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_SET_RESERVATION,
        {
            vol.Required("stop_hours"): vol.All(
                vol.Coerce(int), vol.Range(*m.RESERVATION_STOP_RANGE)
            ),
            vol.Required("run_minutes"): vol.All(
                vol.Coerce(int), vol.Range(*m.RESERVATION_RUN_RANGE)
            ),
        },
        "async_set_reservation",
    )
    platform.async_register_entity_service(
        SERVICE_SET_SCHEDULE,
        {vol.Required("schedule"): cv.string},
        "async_set_schedule",
    )


class NctrClimate(NctrEntity, ClimateEntity, RestoreEntity):
    """Power on/off (HVAC heat/off), mode as preset, one target dial, and the
    reservation values as the fan and swing dropdowns.

    The dial sets the ondol temperature in ondol mode and the room temperature
    otherwise; setting the room temperature also selects room heating.
    Reservation values changed in reservation mode are sent at once; otherwise
    they are kept for the next time the reservation preset is chosen.
    """

    _attr_name = None
    # Heat means "on": the controller has no separate heating switch. The labels are
    # translated as on/off.
    _attr_hvac_modes = [HVACMode.HEAT, HVACMode.OFF]
    _attr_preset_modes = PRESETS
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.PRESET_MODE
        | ClimateEntityFeature.FAN_MODE
        | ClimateEntityFeature.SWING_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    _attr_native_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = 1
    _attr_fan_modes = STOP_HOURS
    _attr_swing_modes = RUN_MINUTES

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # The controller reports reservation values only when its button is pressed;
        # keep the ones picked here across restarts.
        if (last := await self.async_get_last_extra_data()) is None:
            return
        saved = last.as_dict()
        for index, name in enumerate(RESERVATION_ATTRIBUTES):
            value = saved.get(name)
            if isinstance(value, int):
                self.hub.restore_reservation_value(index, value)

    @property
    def extra_restore_state_data(self) -> RestoredExtraData:
        # Saved apart from the state, so it survives while the entity is unavailable.
        return RestoredExtraData(
            {
                name: self.hub.reservation_value(index)
                for index, name in enumerate(RESERVATION_ATTRIBUTES)
            }
        )

    @property
    def fan_mode(self) -> str | None:
        return _text(self.hub.reservation_value(0))

    @property
    def swing_mode(self) -> str | None:
        return _text(self.hub.reservation_value(1))

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        await self._run(self.hub.async_set_reservation_value(0, int(fan_mode)))

    async def async_set_swing_mode(self, swing_mode: str) -> None:
        await self._run(self.hub.async_set_reservation_value(1, int(swing_mode)))

    @property
    def _ondol_dial(self) -> bool:
        state = self.hub.state
        return state.mode == m.MODE_ONDOL and state.overlay is None

    @property
    def min_temp(self) -> float:
        return (m.ONDOL_RANGE if self._ondol_dial else m.ROOM_RANGE)[0]

    @property
    def max_temp(self) -> float:
        return (m.ONDOL_RANGE if self._ondol_dial else m.ROOM_RANGE)[1]

    @property
    def hvac_mode(self) -> HVACMode | None:
        power = self.hub.state.power_on
        if power is None:
            return None
        return HVACMode.HEAT if power else HVACMode.OFF

    @property
    def preset_mode(self) -> str | None:
        state = self.hub.state
        return state.overlay or state.mode

    @property
    def native_current_temperature(self) -> float | None:
        return self.hub.state.room_current

    @property
    def native_target_temperature(self) -> float | None:
        state = self.hub.state
        return state.ondol_target if self._ondol_dial else state.room_target

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        state = self.hub.state
        return {
            "target_kind": "ondol" if self._ondol_dial else "room",
            "room_target": state.room_target,
            "ondol_target": state.ondol_target,
            "reservation_stop_hours": self.hub.reservation_value(0),
            "reservation_run_minutes": self.hub.reservation_value(1),
            "schedule": state.schedule,
        }

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        await self._run(self.hub.async_set_power(hvac_mode != HVACMode.OFF))

    async def async_turn_on(self) -> None:
        await self._run(self.hub.async_set_power(True))

    async def async_turn_off(self) -> None:
        await self._run(self.hub.async_set_power(False))

    async def async_set_temperature(self, **kwargs: Any) -> None:
        if (temperature := kwargs.get(ATTR_TEMPERATURE)) is None:
            return
        if self._ondol_dial:
            await self._run(self.hub.async_set_ondol_target(round(temperature)))
        else:
            # 0102 also selects room heating mode.
            await self._run(self.hub.async_set_room_target(round(temperature)))

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        hub = self.hub
        actions = {
            PRESET_ROOM: hub.async_select_room,
            PRESET_ONDOL: hub.async_select_ondol,
            PRESET_AWAY: hub.async_select_away,
            PRESET_RESERVATION: hub.async_select_reservation,
            PRESET_BATH: hub.async_select_bath,
            PRESET_HOT_WATER_ONLY: hub.async_select_hot_water_only,
        }
        await self._run(actions[preset_mode]())

    async def async_set_reservation(self, stop_hours: int, run_minutes: int) -> None:
        await self._run(self.hub.async_set_reservation(stop_hours, run_minutes))

    async def async_set_schedule(self, schedule: str) -> None:
        await self._run(self.hub.async_set_schedule(schedule))
