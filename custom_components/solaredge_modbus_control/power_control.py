"""Dynamic Power Control: keeps the inverter's active power limit refreshed.

The inverter reverts to its fallback limit when the limit register is not
rewritten within the command timeout, so the limit is written again every third
of the timeout, and nothing is written while the inverter is unreachable.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
import logging
import time

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import CONTROL_TICK_SECONDS
from .coordinator import SolarEdgeCoordinator
from .modbus import ModbusError
from .sunspec import (
    REG_ACTIVE_REACTIVE,
    REG_ADV_POWER_CONTROL,
    REG_COMMAND_TIMEOUT,
    REG_DYNAMIC_LIMIT,
    REG_ENABLE_DYNAMIC,
    REG_FALLBACK_LIMIT,
    REG_POWER_BASE,
    REG_RAMP_DOWN,
    REG_RAMP_UP,
    encode_float32,
    encode_int32,
    encode_uint32,
)

_LOGGER = logging.getLogger(__name__)
TARGET_EPSILON = 0.01


class PowerLimitController:
    """Owns the target limit (percent of rated power) and writes it to the inverter."""

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: SolarEdgeCoordinator,
        *,
        command_timeout: int,
        fallback_limit: float,
        target: float = 100.0,
    ) -> None:
        self.hass = hass
        self.coordinator = coordinator
        self.command_timeout = command_timeout
        self.fallback_limit = fallback_limit
        self.target = target
        self.last_written_at: datetime | None = None
        self._last_written: float | None = None
        self._written_monotonic = 0.0
        self._needs_init = True
        self._init_monotonic = 0.0
        self._write_failed = False
        self._unsub: CALLBACK_TYPE | None = None
        self._listeners: list[Callable[[], None]] = []

    @property
    def heartbeat(self) -> float:
        return max(10.0, self.command_timeout / 3)

    def start(self) -> None:
        self._unsub = async_track_time_interval(self.hass, self._tick, timedelta(seconds=CONTROL_TICK_SECONDS))

    def stop(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    @callback
    def inverter_back_online(self) -> None:
        """The inverter rebooted or reappeared: set it up and write the limit again."""
        self._needs_init = True
        self._last_written = None

    async def async_set_target(self, value: float) -> None:
        """Change the target and write it straight away."""
        self.target = max(0.0, min(100.0, float(value)))
        await self.async_write()

    async def _tick(self, _now: datetime) -> None:
        await self.async_write()

    async def async_write(self) -> None:
        """Write the limit if it changed or the heartbeat is due."""
        data = self.coordinator.data
        if not self.coordinator.last_update_success or not data:
            return
        if not self._needs_init and self._config_mismatch(data):
            _LOGGER.warning("Inverter power control settings differ from the configured ones; applying them again")
            self._needs_init = True
        if self._needs_init and not await self._initialise(data):
            return
        changed = self._last_written is None or abs(self.target - self._last_written) > TARGET_EPSILON
        due = time.monotonic() - self._written_monotonic >= self.heartbeat
        if not (changed or due):
            return
        try:
            await self.coordinator.client.write_registers(REG_DYNAMIC_LIMIT, encode_float32(self.target))
        except ModbusError as err:
            self._last_written = None
            if not self._write_failed:
                _LOGGER.warning("Could not write the power limit: %s", err)
            self._write_failed = True
            return
        if self._write_failed:
            _LOGGER.info("Power limit writes working again")
            self._write_failed = False
        self._last_written = self.target
        self._written_monotonic = time.monotonic()
        self.last_written_at = dt_util.utcnow()
        # Not async_set_updated_data: that restarts the poll timer, and a heartbeat shorter than
        # the scan interval would then stop telemetry polling altogether.
        self.coordinator.data = {**data, "dynamic_limit": round(self.target, 2)}
        self.coordinator.async_update_listeners()
        for listener in self._listeners:
            listener()

    def _config_mismatch(self, data: dict) -> bool:
        """True when a settings read-back made after our last set-up shows different values."""
        if data.get("config_read_at", 0.0) <= self._init_monotonic:
            return False
        enabled = data.get("dynamic_power_control")
        timeout = data.get("command_timeout")
        return (enabled is not None and enabled != 1) or (timeout is not None and timeout != self.command_timeout)

    async def _initialise(self, data: dict) -> bool:
        rated = data.get("max_active_power")
        if not rated:
            if not self._write_failed:
                _LOGGER.error("Rated power unknown; not enabling dynamic power control")
            self._write_failed = True
            return False
        client = self.coordinator.client
        try:
            await client.write_registers(REG_ADV_POWER_CONTROL, encode_int32(1))
            await client.write_register(REG_ACTIVE_REACTIVE, 0)
            await client.write_registers(REG_POWER_BASE, encode_float32(rated))
            await client.write_registers(REG_COMMAND_TIMEOUT, encode_uint32(self.command_timeout))
            await client.write_registers(REG_FALLBACK_LIMIT, encode_float32(self.fallback_limit))
            await client.write_registers(REG_RAMP_UP, encode_float32(100.0))
            await client.write_registers(REG_RAMP_DOWN, encode_float32(100.0))
            await client.write_register(REG_ENABLE_DYNAMIC, 1)
        except ModbusError as err:
            if not self._write_failed:
                _LOGGER.warning("Could not set up dynamic power control: %s", err)
            self._write_failed = True
            return False
        _LOGGER.info(
            "Dynamic power control enabled (power base %s W, timeout %s s, fallback %s %%)",
            rated,
            self.command_timeout,
            self.fallback_limit,
        )
        self._needs_init = False
        self._init_monotonic = time.monotonic()
        return True

    @callback
    def add_listener(self, listener: Callable[[], None]) -> CALLBACK_TYPE:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)
