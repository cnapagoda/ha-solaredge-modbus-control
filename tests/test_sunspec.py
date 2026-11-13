"""Register decoding."""
from __future__ import annotations

import pytest

from custom_components.solaredge_modbus_control import sunspec
from tests.conftest import AC, DC, FakeInverter, pack, text


def test_identity():
    inverter = FakeInverter()
    regs = [inverter.memory.get(40000 + i, 0) for i in range(68)]
    assert sunspec.parse_identity(regs) == sunspec.Identity(
        "SolarEdge", "SE5000H-AUS", "0004.0021.0002", "7E123456"
    )


def test_identity_rejects_other_devices():
    with pytest.raises(ValueError):
        sunspec.parse_identity([0] * 68)
    with pytest.raises(ValueError):
        sunspec.parse_identity([0x5375, 0x6E53] + [0] * 66)
    with pytest.raises(ValueError):
        sunspec.parse_identity([0x5375, 0x6E53])


def test_scale():
    assert sunspec.scale(2300, 0) == 2300
    assert sunspec.scale(1000, -2) == 10.0
    assert sunspec.scale(5002, -2) == 50.02
    assert sunspec.scale(123456789, 0, 0.001, 3) == 123456.789
    assert sunspec.scale(12, 1) == 120.0


@pytest.mark.parametrize(
    ("raw", "factor"), [(-32768, 0), (65535, 0), (4294967295, 0), (5, -32768), (5, 11), (5, -11)]
)
def test_scale_not_implemented(raw, factor):
    assert sunspec.scale(raw, factor) is None


def test_decode_ac():
    ac = sunspec.decode_ac(pack(">HHHHhHHHHHHhhhHhhhhhhhIh", *AC))
    assert ac == {
        "ac_current": 10.0,
        "ac_current_a": 10.0,
        "ac_current_b": None,
        "ac_current_c": None,
        "ac_voltage": 240.0,
        "ac_voltage_a": 240.0,
        "ac_voltage_b": None,
        "ac_voltage_c": None,
        "ac_power": 2300,
        "ac_frequency": 50.02,
        "ac_apparent_power": 2350,
        "ac_reactive_power": -100,
        "ac_power_factor": 98,
        "ac_energy": 123456.789,
    }


def test_energy_counter_of_zero_is_not_a_reading():
    ac = sunspec.decode_ac(pack(">HHHHhHHHHHHhhhHhhhhhhhIh", *AC[:22], 0, 0))
    assert ac["ac_energy"] is None


def test_decode_dc():
    dc = sunspec.decode_dc(pack(">HhHhhhhhhhhHH", *DC))
    assert dc == {
        "dc_current": 5.0,
        "dc_voltage": 400.0,
        "dc_power": 2400,
        "heatsink_temperature": 45.0,
        "status_code": 4,
        "status_vendor_code": 7,
        "status": "MPPT (Producing)",
    }
    unknown = sunspec.decode_dc(pack(">HhHhhhhhhhhHH", *DC[:11], 99, 0))
    assert unknown["status"] == "Unknown (99)"


@pytest.mark.parametrize("value", [0.0, 100.0, 12.5, 66.25])
def test_float32_round_trip(value):
    assert sunspec.decode_config("f32", sunspec.encode_float32(value)) == value


def test_integer_round_trip():
    assert sunspec.decode_config("u32", sunspec.encode_uint32(120)) == 120
    assert sunspec.decode_config("i32", sunspec.encode_int32(-5)) == -5
    assert sunspec.decode_config("i16", [0xFFFF]) == -1
    assert sunspec.decode_config("i16", [1]) == 1


def test_word_order_is_low_word_first():
    # 100.0 = 0x42C80000; the low word (0x0000) goes first.
    assert sunspec.encode_float32(100.0) == [0x0000, 0x42C8]


def test_text_stops_at_the_first_nul():
    assert sunspec._text(text("Hello", 8)) == "Hello"
