"""Polling coordinator: reads the inverter and holds the latest values."""
from __future__ import annotations

from datetime import timedelta
import logging
import time
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import CONF_SCAN_INTERVAL, CONFIG_POLL_SECONDS, DEFAULT_SCAN_INTERVAL, DOMAIN
from .modbus import ModbusError, ModbusExceptionResponse, ModbusTcpClient
from .sunspec import (
    AC_ADDRESS,
    AC_COUNT,
    CONFIG_REGISTERS,
    DC_ADDRESS,
    DC_COUNT,
    KIND_REGISTERS,
    REG_DYNAMIC_LIMIT,
    decode_ac,
    decode_config,
    decode_dc,
)

if TYPE_CHECKING:
    from .power_control import PowerLimitController

_LOGGER = logging.getLogger(__name__)

# An AC power reading above this multiple of the rated power is a corrupted frame.
OVER_CAPACITY = 1.2


class SolarEdgeCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Reads telemetry every scan interval and the control registers every few minutes."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: ModbusTcpClient) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)),
        )
        self.client = client
        self.controller: PowerLimitController | None = None
        self._config_values: dict[str, Any] = {}
        self._config_read_at = 0.0
        self._offline = False

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            data: dict[str, Any] = decode_ac(await self.client.read_holding_registers(AC_ADDRESS, AC_COUNT))
            data.update(decode_dc(await self.client.read_holding_registers(DC_ADDRESS, DC_COUNT)))
            if time.monotonic() - self._config_read_at >= CONFIG_POLL_SECONDS or not self._config_values:
                await self._read_config()
            data["dynamic_limit"] = await self._read_dynamic_limit()
        except ModbusError as err:
            self._offline = True
            raise UpdateFailed(str(err)) from err
        data.update(self._config_values)
        data["config_read_at"] = self._config_read_at
        rated = self._config_values.get("max_active_power")
        power = data["ac_power"]
        if rated and power is not None and abs(power) > rated * OVER_CAPACITY:
            _LOGGER.warning("Ignoring AC power %s W (rated %s W): corrupted frame", power, rated)
            data["ac_power"] = (self.data or {}).get("ac_power")
        if self._offline:
            self._offline = False
            if self.controller is not None:
                self.controller.inverter_back_online()
        return data

    async def _read_dynamic_limit(self) -> float | None:
        try:
            return round(decode_config("f32", await self.client.read_holding_registers(REG_DYNAMIC_LIMIT, 2)), 2)
        except ModbusExceptionResponse:
            return None

    async def _read_config(self) -> None:
        values: dict[str, Any] = {}
        for key, (address, kind) in CONFIG_REGISTERS.items():
            try:
                registers = await self.client.read_holding_registers(address, KIND_REGISTERS[kind])
                values[key] = decode_config(kind, registers)
            except ModbusExceptionResponse:
                values[key] = None
        self._config_values = values
        self._config_read_at = time.monotonic()
