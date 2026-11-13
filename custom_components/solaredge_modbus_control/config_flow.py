"""Config, reconfigure and options flows."""
from __future__ import annotations

import contextlib
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryState, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT
from homeassistant.core import HomeAssistant, callback
import homeassistant.helpers.config_validation as cv
import voluptuous as vol

from .const import (
    CONF_COMMAND_TIMEOUT,
    CONF_ENABLE_CONTROL,
    CONF_FALLBACK_LIMIT,
    CONF_MANUFACTURER,
    CONF_MODEL,
    CONF_SCAN_INTERVAL,
    CONF_SERIAL,
    CONF_SLAVE,
    CONF_VERSION,
    DEFAULT_COMMAND_TIMEOUT,
    DEFAULT_FALLBACK_LIMIT,
    DEFAULT_NAME,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SLAVE,
    DOMAIN,
)
from .modbus import ModbusError, ModbusTcpClient
from .sunspec import COMMON_ADDRESS, COMMON_COUNT, Identity, parse_identity


async def _identify(
    hass: HomeAssistant, host: str, port: int, slave: int, entry: ConfigEntry | None = None
) -> Identity:
    """Read the inverter's identity.

    The inverter accepts one connection only: `entry`, and any other loaded entry
    on the same address, gives up its connection while this runs."""
    loaded = [e for e in hass.config_entries.async_entries(DOMAIN) if e.state is ConfigEntryState.LOADED]
    async with contextlib.AsyncExitStack() as stack:
        for other in loaded:
            if other is entry or (other.data[CONF_HOST], other.data[CONF_PORT]) == (host, port):
                await stack.enter_async_context(other.runtime_data.client.paused())
        client = ModbusTcpClient(host, port, slave)
        try:
            return parse_identity(await client.read_holding_registers(COMMON_ADDRESS, COMMON_COUNT))
        finally:
            await client.close()


def _connection_schema(host: str = "", port: int = DEFAULT_PORT, slave: int = DEFAULT_SLAVE) -> dict:
    return {
        vol.Required(CONF_HOST, default=host): str,
        vol.Required(CONF_PORT, default=port): cv.port,
        vol.Required(CONF_SLAVE, default=slave): vol.All(int, vol.Range(min=1, max=247)),
    }


class SolarEdgeConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                identity = await _identify(
                    self.hass, user_input[CONF_HOST], user_input[CONF_PORT], user_input[CONF_SLAVE]
                )
            except ModbusError:
                errors["base"] = "cannot_connect"
            except ValueError:
                errors["base"] = "not_sunspec"
            else:
                await self.async_set_unique_id(identity.serial)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=user_input[CONF_NAME],
                    data={
                        CONF_HOST: user_input[CONF_HOST],
                        CONF_PORT: user_input[CONF_PORT],
                        CONF_SLAVE: user_input[CONF_SLAVE],
                        CONF_MANUFACTURER: identity.manufacturer,
                        CONF_MODEL: identity.model,
                        CONF_VERSION: identity.version,
                        CONF_SERIAL: identity.serial,
                    },
                    options={CONF_ENABLE_CONTROL: user_input[CONF_ENABLE_CONTROL]},
                )
        defaults = user_input or {}
        schema = {
            vol.Required(CONF_NAME, default=defaults.get(CONF_NAME, DEFAULT_NAME)): str,
            **_connection_schema(
                defaults.get(CONF_HOST, ""),
                defaults.get(CONF_PORT, DEFAULT_PORT),
                defaults.get(CONF_SLAVE, DEFAULT_SLAVE),
            ),
            vol.Required(CONF_ENABLE_CONTROL, default=defaults.get(CONF_ENABLE_CONTROL, False)): bool,
        }
        return self.async_show_form(step_id="user", data_schema=vol.Schema(schema), errors=errors)

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                identity = await _identify(
                    self.hass, user_input[CONF_HOST], user_input[CONF_PORT], user_input[CONF_SLAVE], entry
                )
            except ModbusError:
                errors["base"] = "cannot_connect"
            except ValueError:
                errors["base"] = "not_sunspec"
            else:
                await self.async_set_unique_id(identity.serial)
                self._abort_if_unique_id_mismatch()
                return self.async_update_reload_and_abort(entry, data_updates=user_input)
        data = user_input or entry.data
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(_connection_schema(data[CONF_HOST], data[CONF_PORT], data[CONF_SLAVE])),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return SolarEdgeOptionsFlow()


class SolarEdgeOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        o = self.config_entry.options
        schema = {
            vol.Required(CONF_SCAN_INTERVAL, default=o.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)): vol.All(
                int, vol.Range(min=10, max=300)
            ),
            vol.Required(CONF_ENABLE_CONTROL, default=o.get(CONF_ENABLE_CONTROL, False)): bool,
            vol.Required(CONF_COMMAND_TIMEOUT, default=o.get(CONF_COMMAND_TIMEOUT, DEFAULT_COMMAND_TIMEOUT)): vol.All(
                int, vol.Range(min=30, max=3600)
            ),
            vol.Required(CONF_FALLBACK_LIMIT, default=o.get(CONF_FALLBACK_LIMIT, DEFAULT_FALLBACK_LIMIT)): vol.All(
                vol.Coerce(float), vol.Range(min=0, max=100)
            ),
        }
        return self.async_show_form(step_id="init", data_schema=vol.Schema(schema))
