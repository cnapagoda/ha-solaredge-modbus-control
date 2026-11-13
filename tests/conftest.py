"""Fixtures: a fake inverter, so no test talks to real hardware."""
from __future__ import annotations

import asyncio
import contextlib
import struct
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.solaredge_modbus_control.const import DOMAIN
from custom_components.solaredge_modbus_control.modbus import ModbusConnectionError, ModbusExceptionResponse
from custom_components.solaredge_modbus_control.sunspec import (
    REG_ACTIVE_REACTIVE,
    REG_ADV_POWER_CONTROL,
    REG_COMMAND_TIMEOUT,
    REG_DYNAMIC_LIMIT,
    REG_ENABLE_DYNAMIC,
    REG_FALLBACK_LIMIT,
    REG_MAX_ACTIVE_POWER,
    REG_POWER_BASE,
    REG_RAMP_DOWN,
    REG_RAMP_UP,
    encode_float32,
    encode_int32,
    encode_uint32,
)

SERIAL = "7E123456"


def text(value: str, registers: int) -> list[int]:
    raw = value.encode().ljust(registers * 2, b"\x00")
    return list(struct.unpack(f">{registers}H", raw))


def pack(fmt: str, *values: int) -> list[int]:
    raw = struct.pack(fmt, *values)
    return list(struct.unpack(f">{len(raw) // 2}H", raw))


AC = (
    1000, 1000, 65535, 65535, -2, 0, 0, 0, 2400, 65535, 65535, -1,
    2300, 0, 5002, -2, 2350, 0, -100, 0, 98, 0, 123456789, 0,
)
DC = (500, -2, 4000, -1, 2400, 0, 0, 450, 0, 0, -1, 4, 7)


class FakeInverter:
    """An in-memory SolarEdge inverter that stands in for ModbusTcpClient."""

    def __init__(self) -> None:
        self.online = True
        self.illegal: set[int] = set()
        self.read_error: Exception | None = None
        self.write_error: Exception | None = None
        self.writes: list[tuple[str, int, list[int]]] = []
        self.write_attempts = 0
        self.gate: asyncio.Event | None = None  # when set, writes wait for it
        self.reads = 0
        self.closed = False
        self.shut = False
        self.pauses = 0
        self.reads_while_paused = 0
        self._paused = False
        self.memory: dict[int, int] = {}
        self.set(40000, [0x5375, 0x6E53, 1, 65])
        self.set(40004, text("SolarEdge", 16))
        self.set(40020, text("SE5000H-AUS", 16))
        self.set(40044, text("0004.0021.0002", 8))
        self.set(40052, text(SERIAL, 16))
        self.set_ac(AC)
        self.set_dc(DC)
        self.set(REG_MAX_ACTIVE_POWER, encode_float32(5000))
        self.set(REG_ADV_POWER_CONTROL, encode_int32(1))
        self.set(REG_ENABLE_DYNAMIC, [1])
        self.set(REG_ACTIVE_REACTIVE, [0])
        self.set(REG_POWER_BASE, encode_float32(5000))
        self.set(REG_COMMAND_TIMEOUT, encode_uint32(120))
        self.set(REG_FALLBACK_LIMIT, encode_float32(100))
        self.set(REG_RAMP_UP, encode_float32(100))
        self.set(REG_RAMP_DOWN, encode_float32(100))
        self.set(REG_DYNAMIC_LIMIT, encode_float32(100))
        self.set(0xE000, [1])
        self.set(0xE001, [0])
        self.set(0xE002, encode_float32(3000))

    def set(self, address: int, values: list[int]) -> None:
        for offset, value in enumerate(values):
            self.memory[address + offset] = value

    def set_ac(self, values) -> None:
        self.set(40071, pack(">HHHHhHHHHHHhhhHhhhhhhhIh", *values))

    def set_dc(self, values) -> None:
        self.set(40096, pack(">HhHhhhhhhhhHH", *values))

    async def read_holding_registers(self, address: int, count: int) -> list[int]:
        self.reads += 1
        self.reads_while_paused += self._paused
        if not self.online:
            raise ModbusConnectionError("offline")
        if self.read_error is not None:
            raise self.read_error
        if address in self.illegal:
            raise ModbusExceptionResponse(3, 2)
        return [self.memory.get(address + i, 0) for i in range(count)]

    async def write_register(self, address: int, value: int) -> None:
        await self._write("single", address, [value])

    async def write_registers(self, address: int, values: list[int]) -> None:
        await self._write("multiple", address, values)

    async def _write(self, kind: str, address: int, values: list[int]) -> None:
        self.write_attempts += 1
        if self.gate is not None:
            await self.gate.wait()
        if not self.online:
            raise ModbusConnectionError("offline")
        if self.write_error is not None:
            raise self.write_error
        self.writes.append((kind, address, list(values)))
        self.set(address, values)

    async def close(self) -> None:
        self.closed = True

    async def shutdown(self) -> None:
        self.shut = True
        await self.close()

    @contextlib.asynccontextmanager
    async def paused(self):
        self.pauses += 1
        self._paused = True
        try:
            yield
        finally:
            self._paused = False

    def writes_to(self, address: int) -> list[list[int]]:
        return [values for _, a, values in self.writes if a == address]


@pytest.fixture(autouse=True)
def _enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture
def inverter():
    fake = FakeInverter()
    with (
        patch("custom_components.solaredge_modbus_control.ModbusTcpClient", return_value=fake),
        patch("custom_components.solaredge_modbus_control.config_flow.ModbusTcpClient", return_value=fake),
    ):
        yield fake


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock():
    clk = Clock()
    fake_time = SimpleNamespace(monotonic=lambda: clk.now)
    with (
        patch("custom_components.solaredge_modbus_control.power_control.time", fake_time),
        patch("custom_components.solaredge_modbus_control.coordinator.time", fake_time),
    ):
        yield clk


def make_entry(control: bool = True, **options) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="SolarEdge Direct",
        unique_id=SERIAL,
        data={
            "host": "192.0.2.10",
            "port": 1502,
            "slave": 1,
            "manufacturer": "SolarEdge",
            "model": "SE5000H-AUS",
            "version": "0004.0021.0002",
            "serial": SERIAL,
        },
        options={"enable_control": control, **options},
    )
