"""Dynamic Power Control: keeps the inverter's active power limit refreshed.

The inverter reverts to its fallback limit when the limit register is not
rewritten within the command timeout, so the limit is written again every third
of the timeout. A failed telemetry poll does not stop that: the write is still
tried once per heartbeat, so a single lost poll cannot let the limit expire.
"""
from __future__ import annotations

import asyncio
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
# Read-backs of 32-bit float settings are compared with this tolerance.
SETTING_EPSILON = 0.01


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
        self._attempt_monotonic = 0.0
        self._lock = asyncio.Lock()
        self._stopped = False
        self._unsub: CALLBACK_TYPE | None = None
        self._listeners: list[Callable[[], None]] = []

    @property
    def heartbeat(self) -> float:
        return max(10.0, self.command_timeout / 3)

    def start(self) -> None:
        self._stopped = False
        self._unsub = async_track_time_interval(self.hass, self._tick, timedelta(seconds=CONTROL_TICK_SECONDS))

    def stop(self) -> None:
        self._stopped = True
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    @callback
    def inverter_back_online(self) -> None:
        """The inverter rebooted or reappeared: write the limit again at the next tick.

        Its settings were read again in the same poll; they are applied again only if they changed."""
        self._last_written = None

    async def async_set_target(self, value: float) -> None:
        """Change the target and write it straight away."""
        self.target = max(0.0, min(100.0, float(value)))
        await self.async_write(now=True)

    async def _tick(self, _now: datetime) -> None:
        await self.async_write()

    async def async_write(self, *, now: bool = False) -> None:
        """Write the limit if it changed or the heartbeat is due."""
        async with self._lock:
            await self._write(now)

    async def _write(self, now: bool) -> None:
        data = self.coordinator.data
        if self._stopped or not data:
            return
        polling = self.coordinator.last_update_success
        if not polling:
            # The last poll failed, yet the inverter may still listen: keep the limit alive, but with
            # one attempt per heartbeat, as each attempt can take as long as two request timeouts.
            if not now and time.monotonic() - self._attempt_monotonic < self.heartbeat:
                return
            self._attempt_monotonic = time.monotonic()
        if not self._needs_init and (differing := self._config_mismatch(data)):
            _LOGGER.warning(
                "Inverter power control settings differ from the configured ones (%s); applying them again",
                ", ".join(f"{key} is {value}" for key, value in differing.items()),
            )
            self._needs_init = True
        if self._needs_init and not await self._initialise(data, polling):
            return
        target = self.target
        changed = self._last_written is None or abs(target - self._last_written) > TARGET_EPSILON
        due = time.monotonic() - self._written_monotonic >= self.heartbeat
        if not (changed or due):
            return
        try:
            await self.coordinator.client.write_registers(REG_DYNAMIC_LIMIT, encode_float32(target))
        except ModbusError as err:
            self._last_written = None
            self._failed("Could not write the power limit: %s", err, polling)
            return
        if self._write_failed:
            _LOGGER.info("Power limit writes working again")
            self._write_failed = False
        self._last_written = target
        self._written_monotonic = time.monotonic()
        self.last_written_at = dt_util.utcnow()
        # Merged into the latest data: a poll may have finished while the write was waiting its turn.
        # Not async_set_updated_data: that restarts the poll timer, and a heartbeat shorter than
        # the scan interval would then stop telemetry polling altogether.
        self.coordinator.data = {**(self.coordinator.data or data), "dynamic_limit": round(target, 2)}
        self.coordinator.async_update_listeners()
        for listener in self._listeners:
            listener()

    def _failed(self, message: str, err: ModbusError, polling: bool) -> None:
        """Log a failed write once; quietly when it was stopped, or the failed poll already reported the outage."""
        self._attempt_monotonic = time.monotonic()
        if self._stopped or not polling:
            _LOGGER.debug(message, err)
            return
        if not self._write_failed:
            _LOGGER.warning(message, err)
        self._write_failed = True

    def _settings(self, rated: float) -> dict[str, float]:
        """What set-up writes, by read-back key."""
        return {
            "advanced_power_control": 1,
            "active_reactive_preference": 0,
            "power_base": rated,
            "command_timeout": self.command_timeout,
            "fallback_limit": self.fallback_limit,
            "ramp_up_rate": 100.0,
            "ramp_down_rate": 100.0,
            "dynamic_power_control": 1,
        }

    def _config_mismatch(self, data: dict) -> dict[str, float]:
        """The settings that a read-back made after our last set-up shows with other values."""
        if data.get("config_read_at", 0.0) <= self._init_monotonic or not (rated := data.get("max_active_power")):
            return {}
        return {
            key: data[key]
            for key, wanted in self._settings(rated).items()
            if data.get(key) is not None and abs(data[key] - wanted) > SETTING_EPSILON
        }

    async def _initialise(self, data: dict, polling: bool) -> bool:
        rated = data.get("max_active_power")
        if not rated:
            if not self._write_failed:
                _LOGGER.error("Rated power unknown; not enabling dynamic power control")
            self._write_failed = True
            return False
        client = self.coordinator.client
        wanted = self._settings(rated)
        try:
            await client.write_registers(REG_ADV_POWER_CONTROL, encode_int32(wanted["advanced_power_control"]))
            await client.write_register(REG_ACTIVE_REACTIVE, wanted["active_reactive_preference"])
            await client.write_registers(REG_POWER_BASE, encode_float32(wanted["power_base"]))
            await client.write_registers(REG_COMMAND_TIMEOUT, encode_uint32(wanted["command_timeout"]))
            await client.write_registers(REG_FALLBACK_LIMIT, encode_float32(wanted["fallback_limit"]))
            await client.write_registers(REG_RAMP_UP, encode_float32(wanted["ramp_up_rate"]))
            await client.write_registers(REG_RAMP_DOWN, encode_float32(wanted["ramp_down_rate"]))
            await client.write_register(REG_ENABLE_DYNAMIC, wanted["dynamic_power_control"])
        except ModbusError as err:
            self._failed("Could not set up dynamic power control: %s", err, polling)
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
