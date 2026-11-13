"""The Modbus client against a real TCP server on localhost."""
from __future__ import annotations

import asyncio
import struct
from unittest.mock import patch

import pytest

from custom_components.solaredge_modbus_control.modbus import (
    ModbusConnectionError,
    ModbusExceptionResponse,
    ModbusProtocolError,
    ModbusTcpClient,
)


class Server:
    """Answers with whatever `responder(transaction, unit, pdu)` returns (a list of raw frames)."""

    def __init__(self, responder) -> None:
        self.responder = responder
        self.connections = 0
        self.requests: list[bytes] = []

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        self._server.close()
        await self._server.wait_closed()

    async def _handle(self, reader, writer) -> None:
        self.connections += 1
        try:
            while True:
                header = await reader.readexactly(7)
                tid, _, length, unit = struct.unpack(">HHHB", header)
                pdu = await reader.readexactly(length - 1)
                self.requests.append(pdu)
                for frame in self.responder(tid, unit, pdu):
                    writer.write(frame)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()


def frame(tid: int, pdu: bytes, unit: int = 1, protocol: int = 0) -> bytes:
    return struct.pack(">HHHB", tid, protocol, len(pdu) + 1, unit) + pdu


def read_answer(tid, count, values=None):
    values = values or list(range(count))
    return frame(tid, bytes([3, 2 * len(values)]) + struct.pack(f">{len(values)}H", *values))


@pytest.fixture(autouse=True)
def _sockets(socket_enabled):
    """These tests talk to a server on localhost."""


@pytest.fixture
async def serve():
    servers: list[Server] = []

    async def start(responder):
        server = Server(responder)
        port = await server.start()
        servers.append(server)
        client = ModbusTcpClient("127.0.0.1", port, 1, timeout=0.5, delay=0)
        return server, client

    yield start
    for server in servers:
        await server.stop()


async def test_read_holding_registers(serve):
    server, client = await serve(lambda tid, unit, pdu: [read_answer(tid, 3, [10, 20, 65535])])
    assert await client.read_holding_registers(40071, 3) == [10, 20, 65535]
    assert server.requests[0] == struct.pack(">BHH", 3, 40071, 3)
    await client.close()


async def test_reuses_the_connection_and_reconnects_after_close(serve):
    server, client = await serve(lambda tid, unit, pdu: [read_answer(tid, 1)])
    await client.read_holding_registers(1, 1)
    await client.read_holding_registers(1, 1)
    assert server.connections == 1
    await client.close()
    await client.close()
    await client.read_holding_registers(1, 1)
    assert server.connections == 2
    await client.close()


async def test_read_count_is_checked(serve):
    _, client = await serve(lambda tid, unit, pdu: [])
    for count in (0, 126):
        with pytest.raises(ValueError):
            await client.read_holding_registers(1, count)


async def test_write_register(serve):
    server, client = await serve(lambda tid, unit, pdu: [frame(tid, pdu)])
    await client.write_register(0xF300, 1)
    assert server.requests[0] == struct.pack(">BHH", 6, 0xF300, 1)
    await client.close()


async def test_write_registers(serve):
    server, client = await serve(lambda tid, unit, pdu: [frame(tid, pdu[:5])])
    await client.write_registers(0xF322, [0x0000, 0x42C8])
    assert server.requests[0] == struct.pack(">BHHBHH", 16, 0xF322, 2, 4, 0, 0x42C8)
    await client.close()


async def test_exception_response_keeps_the_connection(serve):
    server, client = await serve(lambda tid, unit, pdu: [frame(tid, bytes([0x83, 2]))])
    with pytest.raises(ModbusExceptionResponse) as err:
        await client.read_holding_registers(1, 1)
    assert err.value.code == 2
    with pytest.raises(ModbusExceptionResponse):
        await client.read_holding_registers(1, 1)
    assert server.connections == 1
    await client.close()


async def test_exception_response_without_a_code(serve):
    _, client = await serve(lambda tid, unit, pdu: [frame(tid, bytes([0x83]))])
    with pytest.raises(ModbusExceptionResponse) as err:
        await client.read_holding_registers(1, 1)
    assert err.value.code == 0
    await client.close()


async def test_late_answer_to_an_earlier_request_is_discarded(serve):
    def responder(tid, unit, pdu):
        return [read_answer((tid - 1) & 0xFFFF, 1, [111]), read_answer(tid, 1, [222])]

    _, client = await serve(responder)
    assert await client.read_holding_registers(1, 1) == [222]
    await client.close()


async def test_too_many_late_answers_reset_the_connection(serve):
    server, client = await serve(lambda tid, unit, pdu: [read_answer(tid + 5, 1)] * 5)
    with pytest.raises(ModbusProtocolError):
        await client.read_holding_registers(1, 1)
    await client.close()
    # Tried once more on a new connection.
    assert server.connections == 2


@pytest.mark.parametrize(
    "pdu",
    [
        bytes([3, 4, 0, 1, 0, 2]),  # byte count does not match the request
        bytes([3, 2, 0, 1, 9]),  # wrong length
        bytes([4, 2, 0, 1]),  # other function
    ],
)
async def test_invalid_read_answers(serve, pdu):
    _, client = await serve(lambda tid, unit, p: [frame(tid, pdu)])
    with pytest.raises(ModbusProtocolError):
        await client.read_holding_registers(1, 1)
    await client.close()


async def test_invalid_write_answers(serve):
    _, client = await serve(lambda tid, unit, pdu: [frame(tid, bytes([pdu[0], 0, 0, 0, 0]))])
    with pytest.raises(ModbusProtocolError):
        await client.write_register(1, 1)
    with pytest.raises(ModbusProtocolError):
        await client.write_registers(1, [1, 2])
    await client.close()


@pytest.mark.parametrize(("unit", "protocol"), [(1, 1), (9, 0)])
async def test_invalid_header(serve, unit, protocol):
    _, client = await serve(lambda tid, _, pdu: [frame(tid, b"\x03\x02\x00\x01", unit=unit, protocol=protocol)])
    with pytest.raises(ModbusProtocolError):
        await client.read_holding_registers(1, 1)
    await client.close()


async def test_no_answer_times_out(serve):
    server, client = await serve(lambda tid, unit, pdu: [])
    with pytest.raises(ModbusConnectionError, match="no answer within 0.5 s"):
        await client.read_holding_registers(1, 1)
    await client.close()
    assert server.connections == 2


async def test_no_retry_when_switched_off(serve):
    server, client = await serve(lambda tid, unit, pdu: [])
    client.retries = 0
    with pytest.raises(ModbusConnectionError):
        await client.read_holding_registers(1, 1)
    await client.close()
    assert server.connections == 1


async def test_a_lost_answer_is_asked_for_again_on_a_new_connection(serve):
    server, client = await serve(lambda tid, unit, pdu: [read_answer(tid, 1, [7])] if server.requests[1:] else [])
    assert await client.read_holding_registers(1, 1) == [7]
    assert server.connections == 2
    assert len(server.requests) == 2
    await client.close()


async def test_a_garbled_answer_is_asked_for_again_on_a_new_connection(serve):
    server, client = await serve(lambda tid, unit, pdu: [frame(tid, pdu if server.requests[1:] else pdu[:2])])
    await client.write_register(0xF300, 1)
    assert server.connections == 2
    await client.close()


async def test_shut_down_client_does_not_connect_again(serve):
    server, client = await serve(lambda tid, unit, pdu: [read_answer(tid, 1)])
    await client.read_holding_registers(1, 1)
    await client.shutdown()
    with pytest.raises(ModbusConnectionError, match="shut down"):
        await client.read_holding_registers(1, 1)
    assert server.connections == 1


async def test_shutting_down_ends_a_request_in_flight(serve):
    server, client = await serve(lambda tid, unit, pdu: [])
    request = asyncio.create_task(client.read_holding_registers(1, 1))
    await asyncio.sleep(0.05)
    await client.shutdown()
    with pytest.raises(ModbusConnectionError):
        await request
    assert server.connections == 1


async def test_shutting_down_while_connecting_drops_the_new_connection(serve):
    server, client = await serve(lambda tid, unit, pdu: [read_answer(tid, 1)])
    connect = asyncio.open_connection

    async def slow_connect(host, port):
        streams = await connect(host, port)
        await client.shutdown()
        return streams

    with (
        patch("custom_components.solaredge_modbus_control.modbus.asyncio.open_connection", slow_connect),
        pytest.raises(ModbusConnectionError, match="shut down"),
    ):
        await client.read_holding_registers(1, 1)
    assert client._writer is None
    assert server.requests == []


async def test_paused_client_gives_up_the_connection_and_requests_wait(serve):
    server, client = await serve(lambda tid, unit, pdu: [read_answer(tid, 1, [5])])
    await client.read_holding_registers(1, 1)
    async with client.paused():
        assert client._writer is None
        waiting = asyncio.create_task(client.read_holding_registers(1, 1))
        await asyncio.sleep(0.05)
        assert not waiting.done()
        assert len(server.requests) == 1
    assert await waiting == [5]
    assert server.connections == 2
    await client.close()


async def test_cancelled_request_does_not_leave_its_answer_for_the_next_one(serve):
    held: list[bytes] = []

    def responder(tid, unit, pdu):
        if not held:
            held.append(read_answer(tid, 1, [111]))
            return []
        return [read_answer(tid, 1, [222])]

    server, client = await serve(responder)
    request = asyncio.create_task(client.read_holding_registers(1, 1))
    await asyncio.sleep(0.05)
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert client._writer is None
    assert await client.read_holding_registers(1, 1) == [222]
    assert server.connections == 2
    await client.close()


async def test_connection_refused():
    client = ModbusTcpClient("127.0.0.1", 1, 1, timeout=0.5, delay=0)
    with pytest.raises(ModbusConnectionError):
        await client.read_holding_registers(1, 1)


async def test_server_closing_the_connection(serve):
    async def hang_up(reader, writer):
        writer.close()

    server = await asyncio.start_server(hang_up, "127.0.0.1", 0)
    client = ModbusTcpClient("127.0.0.1", server.sockets[0].getsockname()[1], 1, timeout=0.5, delay=0)
    with pytest.raises(ModbusConnectionError):
        await client.read_holding_registers(1, 1)
    server.close()
    await server.wait_closed()


async def test_requests_are_paced(serve):
    _, client = await serve(lambda tid, unit, pdu: [read_answer(tid, 1)])
    client.delay = 0.1
    loop = asyncio.get_running_loop()
    await client.read_holding_registers(1, 1)
    start = loop.time()
    await client.read_holding_registers(1, 1)
    assert loop.time() - start >= 0.09
    await client.close()
