"""A small asynchronous Modbus TCP client (function codes 3, 6 and 16).

SolarEdge inverters accept a single Modbus TCP connection and their embedded
stack occasionally answers a request twice. Every request here goes through one
lock, is paced, and discards late answers to earlier requests.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import struct
from typing import NoReturn

_LOGGER = logging.getLogger(__name__)

READ_HOLDING = 0x03
WRITE_SINGLE = 0x06
WRITE_MULTIPLE = 0x10
MAX_READ_REGISTERS = 125
# Late answers to earlier requests tolerated before the connection is reset.
MAX_STALE_FRAMES = 3


class ModbusError(Exception):
    """Base class for Modbus failures."""


class ModbusConnectionError(ModbusError):
    """The inverter could not be reached or stopped answering."""


class ModbusProtocolError(ModbusError):
    """The inverter sent something that is not a valid answer."""


class ModbusExceptionResponse(ModbusError):
    """The inverter answered with a Modbus exception (for example illegal address)."""

    def __init__(self, function: int, code: int) -> None:
        super().__init__(f"Modbus exception {code} for function {function}")
        self.function = function
        self.code = code


class ModbusTcpClient:
    """One connection, one request at a time."""

    def __init__(self, host: str, port: int, unit: int = 1, *, timeout: float = 5.0, delay: float = 0.2) -> None:
        self.host = host
        self.port = port
        self.unit = unit
        self.timeout = timeout
        self.delay = delay
        self._lock = asyncio.Lock()
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._transaction = 0
        self._last_io = 0.0

    async def close(self) -> None:
        """Close the connection; the next request opens a new one."""
        writer, self._reader, self._writer = self._writer, None, None
        if writer is not None:
            writer.close()
            with contextlib.suppress(OSError, TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(writer.wait_closed(), 1)

    async def read_holding_registers(self, address: int, count: int) -> list[int]:
        """Read `count` holding registers starting at `address` (protocol address)."""
        if not 1 <= count <= MAX_READ_REGISTERS:
            raise ValueError("count must be 1-125")
        pdu = await self._request(READ_HOLDING, struct.pack(">HH", address, count))
        if len(pdu) != 2 + 2 * count or pdu[1] != 2 * count:
            await self._protocol_error("read answer has the wrong length")
        return list(struct.unpack(f">{count}H", pdu[2:]))

    async def write_register(self, address: int, value: int) -> None:
        """Write one register (function 6)."""
        body = struct.pack(">HH", address, value & 0xFFFF)
        if await self._request(WRITE_SINGLE, body) != bytes([WRITE_SINGLE]) + body:
            await self._protocol_error("write answer does not echo the request")

    async def write_registers(self, address: int, values: list[int]) -> None:
        """Write several consecutive registers (function 16)."""
        body = struct.pack(">HHB", address, len(values), 2 * len(values)) + struct.pack(
            f">{len(values)}H", *(v & 0xFFFF for v in values)
        )
        expected = bytes([WRITE_MULTIPLE]) + struct.pack(">HH", address, len(values))
        if await self._request(WRITE_MULTIPLE, body) != expected:
            await self._protocol_error("write answer does not match the request")

    async def _protocol_error(self, message: str) -> NoReturn:
        await self.close()
        raise ModbusProtocolError(message)

    async def _request(self, function: int, body: bytes) -> bytes:
        loop = asyncio.get_running_loop()
        async with self._lock:
            try:
                await self._ensure_connected()
                wait = self.delay - (loop.time() - self._last_io)
                if wait > 0:
                    await asyncio.sleep(wait)
                self._transaction = (self._transaction + 1) & 0xFFFF
                pdu = bytes([function]) + body
                assert self._writer is not None
                self._writer.write(struct.pack(">HHHB", self._transaction, 0, len(pdu) + 1, self.unit) + pdu)
                await asyncio.wait_for(self._writer.drain(), self.timeout)
                answer = await asyncio.wait_for(self._read_answer(self._transaction), self.timeout)
            except (TimeoutError, OSError, asyncio.IncompleteReadError) as err:
                await self.close()
                raise ModbusConnectionError(f"{self.host}:{self.port}: {err or type(err).__name__}") from err
            except ModbusProtocolError:
                await self.close()
                raise
            finally:
                self._last_io = loop.time()
        if answer[0] == function | 0x80:
            raise ModbusExceptionResponse(function, answer[1] if len(answer) > 1 else 0)
        if answer[0] != function:
            await self._protocol_error(f"unexpected function {answer[0]}")
        return answer

    async def _ensure_connected(self) -> None:
        if self._writer is None or self._writer.is_closing():
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), self.timeout
            )

    async def _read_answer(self, transaction: int) -> bytes:
        assert self._reader is not None
        stale = 0
        while True:
            header = await self._reader.readexactly(7)
            got, protocol, length, unit = struct.unpack(">HHHB", header)
            if protocol != 0 or not 2 <= length <= 254 or unit != self.unit:
                raise ModbusProtocolError("invalid MBAP header")
            pdu = await self._reader.readexactly(length - 1)
            if got == transaction:
                return pdu
            stale += 1
            _LOGGER.debug("Discarding late answer %s (waiting for %s)", got, transaction)
            if stale > MAX_STALE_FRAMES:
                raise ModbusProtocolError("too many late answers")
