"""Config flow: start the local server, then point the controller at it."""

from __future__ import annotations

import ipaddress
import socket
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from . import provision
from .addressing import advertised_ip, async_lan
from .const import (
    CONF_ALLOWED_CLIENT,
    CONF_SERVER_IP,
    CONF_SSID,
    DEFAULT_HOST,
    DEFAULT_PORT,
    DOMAIN,
)

UNKNOWN = "?"


def _settings_schema(defaults: dict[str, Any], lan: str | None) -> vol.Schema:
    allowed = defaults.get(CONF_ALLOWED_CLIENT)
    if allowed is None:
        allowed = lan or ""
    return vol.Schema(
        {
            vol.Optional(CONF_ALLOWED_CLIENT, description={"suggested_value": allowed}): str,
            vol.Required(CONF_PORT, default=defaults.get(CONF_PORT, DEFAULT_PORT)): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=65535)
            ),
            vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, DEFAULT_HOST)): str,
        }
    )


def _try_bind(host: str, port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))


async def _validate(
    hass: HomeAssistant, user_input: dict[str, Any], *, check_port: bool
) -> dict[str, str]:
    errors: dict[str, str] = {}
    allowed = user_input.get(CONF_ALLOWED_CLIENT, "").strip()
    if allowed:
        try:
            ipaddress.ip_network(allowed, strict=False)
        except ValueError:
            errors[CONF_ALLOWED_CLIENT] = "invalid_client"
    try:
        ipaddress.IPv4Address(user_input[CONF_HOST].strip())
    except ValueError:
        errors[CONF_HOST] = "invalid_host"
    if not errors and check_port:
        try:
            await hass.async_add_executor_job(
                _try_bind, user_input[CONF_HOST].strip(), user_input[CONF_PORT]
            )
        except OSError:
            errors[CONF_PORT] = "port_in_use"
    return errors


def _clean(user_input: dict[str, Any]) -> dict[str, Any]:
    return {
        CONF_ALLOWED_CLIENT: user_input.get(CONF_ALLOWED_CLIENT, "").strip(),
        CONF_PORT: user_input[CONF_PORT],
        CONF_HOST: user_input[CONF_HOST].strip(),
    }


def _placeholders(server_ip: str | None, port: int) -> dict[str, str]:
    try:
        server = provision.server_url(server_ip) if server_ip else UNKNOWN
    except ValueError:
        server = UNKNOWN
    return {
        "server": server,
        "server_ip": server_ip or UNKNOWN,
        "port": str(port),
    }


class NctrConfigFlow(ConfigFlow, domain=DOMAIN):
    """Start listening first; the controller is provisioned afterwards."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        lan_ip, lan = await async_lan(self.hass)
        if user_input is not None:
            errors = await _validate(self.hass, user_input, check_port=True)
            if not errors:
                return self.async_create_entry(title="NCTR-200", data=_clean(user_input))
        host = (user_input or {}).get(CONF_HOST, DEFAULT_HOST).strip()
        port = (user_input or {}).get(CONF_PORT, DEFAULT_PORT)
        return self.async_show_form(
            step_id="user",
            data_schema=_settings_schema(user_input or {}, lan),
            errors=errors,
            description_placeholders=_placeholders(advertised_ip(host, lan_ip), port),
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: Any) -> NctrOptionsFlow:
        return NctrOptionsFlow()


class NctrOptionsFlow(OptionsFlow):
    """Change the server settings, or provision the controller from this host."""

    def _current(self) -> dict[str, Any]:
        return {**self.config_entry.data, **self.config_entry.options}

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="init", menu_options=["settings", "provision"])

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        current = self._current()
        errors: dict[str, str] = {}
        if user_input is not None:
            moved = (user_input[CONF_HOST].strip(), user_input[CONF_PORT]) != (
                current.get(CONF_HOST),
                current.get(CONF_PORT),
            )
            errors = await _validate(self.hass, user_input, check_port=moved)
            if not errors:
                return self.async_create_entry(data=_clean(user_input))
        _, lan = await async_lan(self.hass)
        return self.async_show_form(
            step_id="settings",
            data_schema=_settings_schema(user_input or current, lan),
            errors=errors,
        )

    async def async_step_provision(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Send the Wi-Fi settings to the controller's setup access point.

        Works only while this host is connected to the controller's krb_set network.
        The Wi-Fi password is used once and not stored.
        """
        current = self._current()
        port = int(current.get(CONF_PORT, DEFAULT_PORT))
        lan_ip, _ = await async_lan(self.hass)
        server_ip = advertised_ip(str(current.get(CONF_HOST, DEFAULT_HOST)), lan_ip)
        errors: dict[str, str] = {}
        if user_input is not None:
            server_ip = user_input[CONF_SERVER_IP].strip()
            try:
                plan = provision.Plan(
                    user_input[CONF_SSID].strip(),
                    user_input.get(CONF_PASSWORD, ""),
                    server_ip,
                    port,
                )
            except ValueError:
                errors["base"] = "invalid_provision"
            else:
                source = await self.hass.async_add_executor_job(provision.setup_source_ip)
                if source is None:
                    errors["base"] = "not_on_setup_ap"
                else:
                    try:
                        result = await self.hass.async_add_executor_job(provision.send, plan)
                    except OSError:
                        errors["base"] = "provision_failed"
                    else:
                        if result.rejected:
                            errors["base"] = "provision_rejected"
                        else:
                            return self.async_abort(
                                reason="provision_sent",
                                description_placeholders=_placeholders(server_ip, port),
                            )
        defaults = user_input or {}
        schema = vol.Schema(
            {
                vol.Required(CONF_SSID, default=defaults.get(CONF_SSID, "")): str,
                vol.Optional(CONF_PASSWORD): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.PASSWORD)
                ),
                vol.Required(CONF_SERVER_IP, default=server_ip or ""): str,
            }
        )
        return self.async_show_form(
            step_id="provision",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                **_placeholders(server_ip, port),
                "setup_ssid": provision.SETUP_SSID,
            },
        )
