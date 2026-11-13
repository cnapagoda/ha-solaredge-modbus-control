"""A small asynchronous Modbus TCP client (function codes 3, 6 and 16).

SolarEdge inverters accept a single Modbus TCP connection and their embedded
stack occasionally answers a request twice. Every request here goes through one
lock, is paced, and discards late answers to earlier requests. A request that
gets no (valid) answer is sent once more on a new connection before it fails.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
import contextlib
import logging
import struct

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

    def __init__(
        self, host: str, port: int, unit: int = 1, *, timeout: float = 5.0, delay: float = 0.2, retries: int = 1
    ) -> None:
        self.host = host
        self.port = port
        self.unit = unit
        self.timeout = timeout
        self.delay = delay
        self.retries = retries
        self._lock = asyncio.Lock()
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._transaction = 0
        self._last_io = 0.0
        self._shut = False

    async def close(self) -> None:
        """Close the connection; the next request opens a new one."""
        writer = self._drop()
        if writer is not None:
            with contextlib.suppress(OSError, TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(writer.wait_closed(), 1)

    async def shutdown(self) -> None:
        """Close for good: later requests fail instead of opening a new connection."""
        self._shut = True
        await self.close()

    @contextlib.asynccontextmanager
    async def paused(self) -> AsyncIterator[None]:
        """Keep the connection closed, so something else can use the inverter's only connection."""
        async with self._lock:
            await self.close()
            yield

    async def read_holding_registers(self, address: int, count: int) -> list[int]:
        """Read `count` holding registers starting at `address` (protocol address)."""
        if not 1 <= count <= MAX_READ_REGISTERS:
            raise ValueError("count must be 1-125")
        pdu = await self._request(
            READ_HOLDING,
            struct.pack(">HH", address, count),
            lambda answer: len(answer) == 2 + 2 * count and answer[1] == 2 * count,
            "read answer has the wrong length",
        )
        return list(struct.unpack(f">{count}H", pdu[2:]))

    async def write_register(self, address: int, value: int) -> None:
        """Write one register (function 6)."""
        body = struct.pack(">HH", address, value & 0xFFFF)
        expected = bytes([WRITE_SINGLE]) + body
        await self._request(
            WRITE_SINGLE, body, lambda answer: answer == expected, "write answer does not echo the request"
        )

    async def write_registers(self, address: int, values: list[int]) -> None:
        """Write several consecutive registers (function 16)."""
        body = struct.pack(">HHB", address, len(values), 2 * len(values)) + struct.pack(
            f">{len(values)}H", *(v & 0xFFFF for v in values)
        )
        expected = bytes([WRITE_MULTIPLE]) + struct.pack(">HH", address, len(values))
        await self._request(
            WRITE_MULTIPLE, body, lambda answer: answer == expected, "write answer does not match the request"
        )

    async def _request(self, function: int, body: bytes, valid: Callable[[bytes], bool], invalid: str) -> bytes:
        """Send one request and return the checked answer; everything happens under the lock."""
        async with self._lock:
            attempt = 0
            while True:
                try:
                    answer = await self._exchange(bytes([function]) + body)
                    if answer[0] == function | 0x80:
                        raise ModbusExceptionResponse(function, answer[1] if len(answer) > 1 else 0)
                    if answer[0] != function:
                        raise ModbusProtocolError(f"unexpected function {answer[0]}")
                    if not valid(answer):
                        raise ModbusProtocolError(invalid)
                    return answer
                except (ModbusConnectionError, ModbusProtocolError) as err:
                    await self.close()
                    if attempt >= self.retries or self._shut:
                        raise
                    attempt += 1
                    _LOGGER.debug("%s; trying once more on a new connection", err)

    async def _exchange(self, pdu: bytes) -> bytes:
        loop = asyncio.get_running_loop()
        try:
            reader, writer = await self._connect()
            wait = self.delay - (loop.time() - self._last_io)
            if wait > 0:
                await asyncio.sleep(wait)
            self._transaction = (self._transaction + 1) & 0xFFFF
            writer.write(struct.pack(">HHHB", self._transaction, 0, len(pdu) + 1, self.unit) + pdu)
            await asyncio.wait_for(writer.drain(), self.timeout)
            return await asyncio.wait_for(self._read_answer(reader, self._transaction), self.timeout)
        except (TimeoutError, OSError, asyncio.IncompleteReadError) as err:
            reason = str(err) or f"no answer within {self.timeout:g} s"
            raise ModbusConnectionError(f"{self.host}:{self.port}: {reason}") from err
        except asyncio.CancelledError:
            # The answer may still arrive, or be half read: this connection cannot be used again.
            self._drop()
            raise
        finally:
            self._last_io = loop.time()

    def _drop(self) -> asyncio.StreamWriter | None:
        writer, self._reader, self._writer = self._writer, None, None
        if writer is not None:
            writer.close()
        return writer

    async def _connect(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        if self._writer is None or self._reader is None or self._writer.is_closing():
            if self._shut:
                raise ModbusConnectionError(f"{self.host}:{self.port}: the client is shut down")
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), self.timeout
            )
            if self._shut:
                self._drop()
                raise ModbusConnectionError(f"{self.host}:{self.port}: the client is shut down")
        return self._reader, self._writer

    async def _read_answer(self, reader: asyncio.StreamReader, transaction: int) -> bytes:
        stale = 0
        while True:
            header = await reader.readexactly(7)
            got, protocol, length, unit = struct.unpack(">HHHB", header)
            if protocol != 0 or not 2 <= length <= 254 or unit != self.unit:
                raise ModbusProtocolError("invalid MBAP header")
            pdu = await reader.readexactly(length - 1)
            if got == transaction:
                return pdu
            stale += 1
            _LOGGER.debug("Discarding late answer %s (waiting for %s)", got, transaction)
            if stale > MAX_STALE_FRAMES:
                raise ModbusProtocolError("too many late answers")
