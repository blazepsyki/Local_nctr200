"""Base entity for the NCTR-200 local integration."""

from __future__ import annotations

from collections.abc import Awaitable

from homeassistant.config_entries import ConfigEntry
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN, MANUFACTURER, MODEL
from .hub import CommandTimeoutError, NctrHub, NotReadyError


class NctrEntity(Entity):
    """Entity backed by the shared hub. Updated by push, never polled."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    # Unavailable unless the controller is connected with a fresh state report.
    _requires_ready = True

    def __init__(self, entry: ConfigEntry, hub: NctrHub, key: str) -> None:
        self.hub = hub
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=MODEL,
            manufacturer=MANUFACTURER,
            model=MODEL,
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.hub.add_listener(self.async_write_ha_state))

    @property
    def available(self) -> bool:
        return self.hub.ready if self._requires_ready else True

    async def _run(self, command: Awaitable[None]) -> None:
        """Send a command and turn hub errors into Home Assistant errors."""
        try:
            await command
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err
        except NotReadyError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="not_ready",
                translation_placeholders={"error": str(err)},
            ) from err
        except CommandTimeoutError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="no_ack",
                translation_placeholders={"error": str(err)},
            ) from err
