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
# The lifetime energy cannot rise faster than this multiple of the rated power allows (plus a little slack, kWh).
ENERGY_MARGIN = 2.0
ENERGY_SLACK = 0.01
# A lifetime energy reading that looks wrong is believed once it has been read this many times in a row.
ENERGY_CONFIRM_POLLS = 5


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
        self._energy: tuple[float, float] | None = None
        self._energy_rejected = 0

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            data: dict[str, Any] = decode_ac(await self.client.read_holding_registers(AC_ADDRESS, AC_COUNT))
            data.update(decode_dc(await self.client.read_holding_registers(DC_ADDRESS, DC_COUNT)))
            # After an outage the inverter may have restarted with other settings: read them again.
            if (
                self._offline
                or not self._config_values
                or time.monotonic() - self._config_read_at >= CONFIG_POLL_SECONDS
            ):
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
        data["ac_energy"] = self._plausible_energy(data["ac_energy"], rated)
        if self._offline:
            self._offline = False
            if self.controller is not None:
                self.controller.inverter_back_online()
        return data

    def _plausible_energy(self, energy: float | None, rated: float | None) -> float | None:
        """Keep the last lifetime energy when a reading goes down or jumps further than the inverter can produce."""
        if energy is None:
            return None
        now = time.monotonic()
        if self._energy is not None:
            # Time is counted from the last rise, so a counter that moves in steps is not held back.
            last, rose_at = self._energy
            if energy == last:
                self._energy_rejected = 0
                return energy
            rise = rated / 1000 * (now - rose_at) / 3600 * ENERGY_MARGIN + ENERGY_SLACK if rated else float("inf")
            if not last <= energy <= last + rise:
                self._energy_rejected += 1
                if self._energy_rejected < ENERGY_CONFIRM_POLLS:
                    if self._energy_rejected == 1:
                        _LOGGER.warning("Ignoring lifetime energy %s kWh (last reading %s kWh)", energy, last)
                    return last
                _LOGGER.warning("Lifetime energy stays at %s kWh (was %s kWh): accepting it", energy, last)
        self._energy = (energy, now)
        self._energy_rejected = 0
        return energy

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
                value = decode_config(kind, registers)
                values[key] = round(value, 3) if kind == "f32" else value
            except ModbusExceptionResponse:
                values[key] = None
        self._config_values = values
        self._config_read_at = time.monotonic()
