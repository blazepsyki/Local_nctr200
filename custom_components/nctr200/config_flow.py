"""Config flow for the NCTR-200 local integration."""

from __future__ import annotations

import ipaddress
import socket
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant, callback

from .const import CONF_ALLOWED_CLIENT, DEFAULT_HOST, DEFAULT_PORT, DOMAIN


def _schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_ALLOWED_CLIENT, default=defaults.get(CONF_ALLOWED_CLIENT, vol.UNDEFINED)
            ): str,
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
    try:
        ipaddress.ip_network(user_input[CONF_ALLOWED_CLIENT].strip(), strict=False)
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
        CONF_ALLOWED_CLIENT: user_input[CONF_ALLOWED_CLIENT].strip(),
        CONF_PORT: user_input[CONF_PORT],
        CONF_HOST: user_input[CONF_HOST].strip(),
    }


class NctrConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up the server address the controller was provisioned with."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = await _validate(self.hass, user_input, check_port=True)
            if not errors:
                return self.async_create_entry(title="NCTR-200", data=_clean(user_input))
        return self.async_show_form(
            step_id="user", data_schema=_schema(user_input or {}), errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: Any) -> NctrOptionsFlow:
        return NctrOptionsFlow()


class NctrOptionsFlow(OptionsFlow):
    """Change the listening port or the allowed controller address."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        current = {**self.config_entry.data, **self.config_entry.options}
        errors: dict[str, str] = {}
        if user_input is not None:
            moved = (user_input[CONF_HOST].strip(), user_input[CONF_PORT]) != (
                current.get(CONF_HOST),
                current.get(CONF_PORT),
            )
            errors = await _validate(self.hass, user_input, check_port=moved)
            if not errors:
                return self.async_create_entry(data=_clean(user_input))
        return self.async_show_form(
            step_id="init", data_schema=_schema(user_input or current), errors=errors
        )
