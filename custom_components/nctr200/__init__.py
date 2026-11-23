"""Local control of the NCTR-200 boiler controller without the manufacturer server."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .addressing import advertised_ip, async_lan
from .const import CONF_ALLOWED_CLIENT, CONF_NODE, DEFAULT_HOST, DEFAULT_PORT, DOMAIN
from .hub import NctrHub

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.CLIMATE,
    Platform.NUMBER,
    Platform.SENSOR,
]

type NctrConfigEntry = ConfigEntry[NctrHub]


def _settings(entry: ConfigEntry) -> dict[str, object]:
    return {**entry.data, **entry.options}


# Number entities replaced by the climate dial (ondol) and its two dropdowns
# (reservation), or dropped (heating water).
RETIRED_NUMBERS = (
    "heating_water_target",
    "ondol_target",
    "reservation_stop_hours",
    "reservation_run_minutes",
)


@callback
def _remove_retired_entities(hass: HomeAssistant, entry: ConfigEntry) -> None:
    registry = er.async_get(hass)
    for key in RETIRED_NUMBERS:
        unique_id = f"{entry.entry_id}_{key}"
        if entity_id := registry.async_get_entity_id(Platform.NUMBER, DOMAIN, unique_id):
            registry.async_remove(entity_id)


async def async_setup_entry(hass: HomeAssistant, entry: NctrConfigEntry) -> bool:
    """Start the TCP server the controller connects to."""
    settings = _settings(entry)
    node_hex = entry.data.get(CONF_NODE)

    @callback
    def save_node(node: bytes) -> None:
        hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_NODE: node.hex()})

    host = str(settings.get(CONF_HOST, DEFAULT_HOST))
    lan_ip, _ = await async_lan(hass)
    hub = NctrHub(
        host,
        int(settings.get(CONF_PORT, DEFAULT_PORT)),  # type: ignore[call-overload]
        str(settings.get(CONF_ALLOWED_CLIENT) or "") or None,
        now=dt_util.now,
        node=bytes.fromhex(node_hex) if node_hex else None,
        on_node=save_node,
        advertised_ip=advertised_ip(host, lan_ip),
    )
    try:
        await hub.async_start()
    except OSError as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="port_unavailable",
            translation_placeholders={"port": str(hub.port), "error": str(err)},
        ) from err
    entry.runtime_data = hub
    _remove_retired_entities(hass, entry)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    options = dict(entry.options)

    async def reload_on_options(hass: HomeAssistant, entry: NctrConfigEntry) -> None:
        # Saving the learned controller ID also fires this listener; only option
        # changes need a restart of the server.
        if dict(entry.options) != options:
            await hass.config_entries.async_reload(entry.entry_id)

    entry.async_on_unload(entry.add_update_listener(reload_on_options))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: NctrConfigEntry) -> bool:
    """Stop the server and unload entities."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_stop()
    return unloaded
