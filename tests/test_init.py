"""Config flow, entry setup and entities on a real Home Assistant core."""

from __future__ import annotations

import asyncio
import ipaddress
import socket

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PORT, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.translation import async_get_translations

import custom_components.nctr200 as integration
from custom_components.nctr200 import config_flow, provision
from custom_components.nctr200 import messages as m
from custom_components.nctr200.const import CONF_ALLOWED_CLIENT, CONF_NODE, DOMAIN
from custom_components.nctr200.diagnostics import async_get_config_entry_diagnostics
from custom_components.nctr200.hub import NctrHub, NotReadyError

from .test_hub import NODE, Device

CLIMATE = "climate.nctr_200"


LAN = ("192.168.1.8", "192.168.1.0/24")


@pytest.fixture(autouse=True)
def fixed_lan(monkeypatch: pytest.MonkeyPatch) -> None:
    async def lan(hass: HomeAssistant) -> tuple[str, str]:
        return LAN

    monkeypatch.setattr(integration, "async_lan", lan)
    monkeypatch.setattr(config_flow, "async_lan", lan)


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


async def test_form_suggests_lan_and_shows_server(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["description_placeholders"]["server"] == "http://nas.192.168.1.8.nip.io"
    assert result["description_placeholders"]["port"] == "20000"
    field = next(k for k in result["data_schema"].schema if k == CONF_ALLOWED_CLIENT)
    assert field.description == {"suggested_value": LAN[1]}

    # The allow-list is optional; leaving it out accepts any client.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PORT: free_port(), CONF_HOST: "127.0.0.1"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_ALLOWED_CLIENT] == ""
    await hass.async_block_till_done()
    hub: NctrHub = hass.config_entries.async_entries(DOMAIN)[0].runtime_data
    assert hub.allowed is None
    # Bound to 127.0.0.1, that is the address to provision.
    assert hub.provision_target()["server_url"] == "http://nas.127.0.0.1.nip.io"
    attributes = hass.states.get("binary_sensor.nctr_200_connection").attributes
    assert attributes["provision_server"] == "http://nas.127.0.0.1.nip.io"


class FakeSetupAp(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.received: list[bytes] = []

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self.received.append(data)


async def test_options_provision(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    await set_up(hass)
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    transport, ap = await asyncio.get_running_loop().create_datagram_endpoint(
        FakeSetupAp, local_addr=("127.0.0.1", 0)
    )
    monkeypatch.setattr(provision, "AP_ADDRESS", transport.get_extra_info("sockname"))
    monkeypatch.setattr(provision, "REPLY_TIMEOUT", 0.3)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "provision"}
    )
    assert result["step_id"] == "provision"
    user = {"ssid": "home", "password": "secret", "server_ip": "192.168.1.8"}

    # Not on the krb_set network: nothing is sent.
    result = await hass.config_entries.options.async_configure(result["flow_id"], user)
    assert result["errors"] == {"base": "not_on_setup_ap"}

    monkeypatch.setattr(provision, "SETUP_NETWORK", ipaddress.ip_network("127.0.0.0/8"))
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**user, "ssid": "a,b"}
    )
    assert result["errors"] == {"base": "invalid_provision"}
    assert ap.received == []

    result = await hass.config_entries.options.async_configure(result["flow_id"], user)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "provision_sent"
    expected = provision.Plan("home", "secret", "192.168.1.8", entry.data[CONF_PORT])
    assert ap.received == [expected.payload()]
    # The password is not kept in the entry.
    assert "secret" not in repr(entry.data) + repr(entry.options)
    transport.close()


async def test_options_settings(hass: HomeAssistant) -> None:
    hub, _ = await set_up(hass)
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "settings"}
    )
    assert result["step_id"] == "settings"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_ALLOWED_CLIENT: "10.0.0.0/8", CONF_PORT: hub.port, CONF_HOST: "127.0.0.1"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert str(entry.runtime_data.allowed) == "10.0.0.0/8"


async def answer(  # type: ignore[no-untyped-def]
    hass: HomeAssistant, device: Device, domain: str, service: str, data: dict
):
    """Call a service, ACK the command it sends, and return that command."""
    call = hass.async_create_task(hass.services.async_call(domain, service, data, blocking=True))
    command = await device.receive()
    await device.send(command.mid, command.token)
    await call
    return command


async def test_hvac_modes_are_labelled_on_off(hass: HomeAssistant) -> None:
    translations = await async_get_translations(hass, "ko", "entity", {DOMAIN})
    prefix = f"component.{DOMAIN}.entity.climate.boiler.state"
    assert translations[f"{prefix}.heat"] == "켜짐"
    assert translations[f"{prefix}.off"] == "꺼짐"


async def test_retired_numbers_are_removed(hass: HomeAssistant) -> None:
    await set_up(hass)
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    registry = er.async_get(hass)
    old = registry.async_get_or_create(
        "number", DOMAIN, f"{entry.entry_id}_ondol_target", config_entry=entry
    )
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert registry.async_get(old.entity_id) is None
    assert registry.async_get_entity_id("number", DOMAIN, f"{entry.entry_id}_hot_water_target")


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
    assert hass.states.get("number.nctr_200_hot_water_target").state == "60"
    # Replaced by the climate dial and dropdowns, or dropped.
    assert hass.states.get("number.nctr_200_ondol_target") is None
    assert climate.attributes["fan_modes"][0] == "1"
    assert climate.attributes["swing_modes"][-1] == "90"
    assert len(climate.attributes["swing_modes"]) == 81
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
    climate = hass.states.get(CLIMATE)
    assert climate.attributes["preset_mode"] == "ondol"
    # In ondol mode the dial is the ondol temperature.
    assert (climate.attributes["min_temp"], climate.attributes["max_temp"]) == (45, 85)
    assert climate.attributes["temperature"] == 45

    command = await answer(
        hass, device, "climate", "set_temperature", {"entity_id": CLIMATE, "temperature": 60}
    )
    assert (command.mid, command.body) == (m.MID_ONDOL, bytes((0, 0, 0, 0, 60, 0)))
    assert hass.states.get(CLIMATE).attributes["temperature"] == 60
    assert hass.states.get(CLIMATE).attributes["ondol_target"] == 60

    # Reservation needs its values first; picking them outside reservation mode sends
    # nothing.
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            "climate",
            "set_preset_mode",
            {"entity_id": CLIMATE, "preset_mode": "reservation"},
            blocking=True,
        )
    for service, field, value in (
        ("set_fan_mode", "fan_mode", "4"),
        ("set_swing_mode", "swing_mode", "20"),
    ):
        await hass.services.async_call(
            "climate", service, {"entity_id": CLIMATE, field: value}, blocking=True
        )
    climate = hass.states.get(CLIMATE)
    assert (climate.attributes["fan_mode"], climate.attributes["swing_mode"]) == ("4", "20")
    command = await answer(
        hass,
        device,
        "climate",
        "set_preset_mode",
        {"entity_id": CLIMATE, "preset_mode": "reservation"},
    )
    assert (command.mid, command.body) == (m.MID_RESERVATION, bytes((1, 0, 0, 0, 4, 20)))
    assert hass.states.get(CLIMATE).attributes["preset_mode"] == "reservation"
    # In reservation mode a change is sent at once.
    command = await answer(
        hass,
        device,
        "climate",
        "set_swing_mode",
        {"entity_id": CLIMATE, "swing_mode": "30"},
    )
    assert command.body == bytes((1, 0, 0, 0, 4, 30))
    assert hass.states.get(CLIMATE).attributes["swing_mode"] == "30"

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
