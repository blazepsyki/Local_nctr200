"""Config flow, entry setup and entities on a real Home Assistant core."""

from __future__ import annotations

import asyncio
import socket

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PORT, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.nctr200 import messages as m
from custom_components.nctr200.const import CONF_ALLOWED_CLIENT, CONF_NODE, DOMAIN
from custom_components.nctr200.diagnostics import async_get_config_entry_diagnostics
from custom_components.nctr200.hub import NctrHub, NotReadyError

from .test_hub import NODE, Device

CLIMATE = "climate.nctr_200"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def start_flow(hass: HomeAssistant, **overrides: object) -> dict:  # type: ignore[type-arg]
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] is FlowResultType.FORM
    data = {CONF_ALLOWED_CLIENT: "127.0.0.1", CONF_PORT: free_port(), CONF_HOST: "127.0.0.1"}
    data.update(overrides)
    return await hass.config_entries.flow.async_configure(result["flow_id"], data)  # type: ignore[return-value]


async def set_up(hass: HomeAssistant) -> tuple[NctrHub, Device]:
    result = await start_flow(hass)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.state is ConfigEntryState.LOADED
    hub: NctrHub = entry.runtime_data
    reader, writer = await asyncio.open_connection("127.0.0.1", hub.bound_port)
    return hub, Device(reader, writer)


async def test_flow_validates_input(hass: HomeAssistant) -> None:
    result = await start_flow(hass, **{CONF_ALLOWED_CLIENT: "nope", CONF_HOST: "x"})
    assert result["errors"] == {CONF_ALLOWED_CLIENT: "invalid_client", CONF_HOST: "invalid_host"}
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        result = await start_flow(hass, **{CONF_PORT: busy.getsockname()[1]})
    assert result["errors"] == {CONF_PORT: "port_in_use"}


async def test_entities_follow_the_controller(hass: HomeAssistant) -> None:
    hub, device = await set_up(hass)
    assert hass.states.get(CLIMATE).state == STATE_UNAVAILABLE
    assert hass.states.get("binary_sensor.nctr_200_connection").state == "off"

    await device.handshake()
    await hass.async_block_till_done()
    climate = hass.states.get(CLIMATE)
    assert climate.state == "heat"
    assert climate.attributes["current_temperature"] == 23
    assert climate.attributes["temperature"] == 25
    assert climate.attributes["preset_mode"] == "room"
    assert hass.states.get("binary_sensor.nctr_200_connection").state == "on"
    assert hass.states.get("number.nctr_200_ondol_target").state == "45"
    assert hass.states.get("sensor.nctr_200_mode").state == "room"
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.data[CONF_NODE] == NODE.hex()
    assert entry.state is ConfigEntryState.LOADED

    call = hass.async_create_task(
        hass.services.async_call(
            "climate", "set_temperature", {"entity_id": CLIMATE, "temperature": 27}, blocking=True
        )
    )
    command = await device.receive()
    assert (command.mid, command.body) == (m.MID_ROOM, bytes((1, 0, 0, 0, 27, 0)))
    await device.send(command.mid, command.token)
    await call
    assert hass.states.get(CLIMATE).attributes["temperature"] == 27

    call = hass.async_create_task(
        hass.services.async_call(
            "climate",
            "set_preset_mode",
            {"entity_id": CLIMATE, "preset_mode": "ondol"},
            blocking=True,
        )
    )
    command = await device.receive()
    assert (command.mid, command.body) == (m.MID_ONDOL, bytes((0, 0, 0, 0, 45, 0)))
    await device.send(command.mid, command.token)
    await call
    assert hass.states.get(CLIMATE).attributes["preset_mode"] == "ondol"

    call = hass.async_create_task(
        hass.services.async_call(
            DOMAIN,
            "set_reservation",
            {"entity_id": CLIMATE, "stop_hours": 3, "run_minutes": 20},
            blocking=True,
        )
    )
    command = await device.receive()
    assert command.body == bytes((1, 0, 0, 0, 3, 20))
    await device.send(command.mid, command.token)
    await call
    assert hass.states.get(CLIMATE).attributes["reservation_stop_hours"] == 3

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["entry"][CONF_NODE] == "**REDACTED**"
    assert diagnostics["hub"]["ready"] is True

    device.writer.close()
    await asyncio.sleep(0.1)
    await hass.async_block_till_done()
    assert hass.states.get(CLIMATE).state == STATE_UNAVAILABLE
    # Home Assistant skips unavailable entities in service calls; the hub refuses too.
    with pytest.raises(NotReadyError):
        await hub.async_set_power(False)

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert hub.bound_port is None
