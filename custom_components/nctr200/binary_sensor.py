"""Controller connection state."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NctrConfigEntry
from .entity import NctrEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NctrConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([NctrConnection(entry, entry.runtime_data, "connection")])


class NctrConnection(NctrEntity, BinarySensorEntity):
    """On while the controller is connected and its state report is fresh."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _requires_ready = False

    @property
    def is_on(self) -> bool:
        return self.hub.ready

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        stats = self.hub.stats
        return {
            "tcp_connected": self.hub.connected,
            "peer": self.hub.peer,
            "connections": stats.connections,
            "resumed_sessions": stats.resumed,
            "silent_closes": stats.silent_closes,
            "last_disconnect": stats.last_disconnect,
        }
