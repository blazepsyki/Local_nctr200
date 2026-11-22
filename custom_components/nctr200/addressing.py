"""Which address the controller should be provisioned with."""

from __future__ import annotations

import ipaddress

from homeassistant.components import network
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

ANY_HOST = ("", "0.0.0.0")


async def async_lan(hass: HomeAssistant) -> tuple[str | None, str | None]:
    """Home Assistant's own LAN address and its network, e.g. 192.168.0.0/24."""
    try:
        source = await network.async_get_source_ip(hass)
    except HomeAssistantError:
        return None, None
    for adapter in await network.async_get_adapters(hass):
        for ipv4 in adapter["ipv4"]:
            if ipv4["address"] == source:
                lan = ipaddress.ip_network(f"{source}/{ipv4['network_prefix']}", strict=False)
                return source, str(lan)
    return source, None


def advertised_ip(host: str, lan_ip: str | None) -> str | None:
    """The address to put into the controller: the bind address, else the LAN address."""
    return lan_ip if host in ANY_HOST else host
