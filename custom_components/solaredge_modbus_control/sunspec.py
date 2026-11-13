"""SolarEdge SunSpec and Power Control register maps and decoding."""
from __future__ import annotations

from dataclasses import dataclass
import struct
from typing import Any

# SunSpec common model (identification), 68 registers from the SunS marker.
COMMON_ADDRESS = 40000
COMMON_COUNT = 68
# Inverter model 101 telemetry, read as two atomic blocks so every value and its
# scale factor come from the same Modbus frame.
AC_ADDRESS = 40071
AC_COUNT = 25
DC_ADDRESS = 40096
DC_COUNT = 13
_AC_FORMAT = ">HHHHhHHHHHHhhhHhhhhhhhIh"
_DC_FORMAT = ">HhHhhhhhhhhHH"

# Power Control (0xF3xx) registers.
REG_ADV_POWER_CONTROL = 0xF142
REG_ENABLE_DYNAMIC = 0xF300
REG_MAX_ACTIVE_POWER = 0xF304
REG_ACTIVE_REACTIVE = 0xF308
REG_POWER_BASE = 0xF30C
REG_COMMAND_TIMEOUT = 0xF310
REG_FALLBACK_LIMIT = 0xF312
REG_RAMP_UP = 0xF318
REG_RAMP_DOWN = 0xF31A
REG_DYNAMIC_LIMIT = 0xF322

# Export control (meter based site export limit), read only here.
REG_EXPORT_MODE = 0xE000
REG_EXPORT_LIMIT_MODE = 0xE001
REG_EXPORT_SITE_LIMIT = 0xE002

# key -> (address, kind); kind: f32 / i32 / u32 (low word first) or i16.
CONFIG_REGISTERS: dict[str, tuple[int, str]] = {
    "max_active_power": (REG_MAX_ACTIVE_POWER, "f32"),
    "advanced_power_control": (REG_ADV_POWER_CONTROL, "i32"),
    "dynamic_power_control": (REG_ENABLE_DYNAMIC, "i16"),
    "active_reactive_preference": (REG_ACTIVE_REACTIVE, "i16"),
    "power_base": (REG_POWER_BASE, "f32"),
    "command_timeout": (REG_COMMAND_TIMEOUT, "u32"),
    "fallback_limit": (REG_FALLBACK_LIMIT, "f32"),
    "ramp_up_rate": (REG_RAMP_UP, "f32"),
    "ramp_down_rate": (REG_RAMP_DOWN, "f32"),
    "export_control_mode": (REG_EXPORT_MODE, "i16"),
    "export_limit_mode": (REG_EXPORT_LIMIT_MODE, "i16"),
    "export_site_limit": (REG_EXPORT_SITE_LIMIT, "f32"),
}
KIND_REGISTERS = {"f32": 2, "i32": 2, "u32": 2, "i16": 1}

STATUS_LABELS = {
    1: "Off",
    2: "Sleeping",
    3: "Starting",
    4: "MPPT (Producing)",
    5: "Throttled",
    6: "Shutting Down",
    7: "Fault",
    8: "Standby",
}

STATUS_THROTTLED = 5

_NOT_IMPLEMENTED = {-32768, 65535, 4294967295}


@dataclass(frozen=True)
class Identity:
    """What the inverter says it is."""

    manufacturer: str
    model: str
    version: str
    serial: str


def _text(registers: list[int]) -> str:
    raw = struct.pack(f">{len(registers)}H", *registers)
    return raw.split(b"\x00", 1)[0].decode("ascii", errors="replace").strip()


def parse_identity(registers: list[int]) -> Identity:
    """Decode the SunSpec common model; ValueError if this is not a SunSpec device."""
    if len(registers) < COMMON_COUNT or registers[0] != 0x5375 or registers[1] != 0x6E53:
        raise ValueError("no SunSpec marker")
    serial = _text(registers[52:68])
    if not serial:
        raise ValueError("no serial number")
    return Identity(_text(registers[4:20]), _text(registers[20:36]), _text(registers[44:52]), serial)


def scale(raw: int, factor: int, multiplier: float = 1.0, digits: int | None = None) -> float | None:
    """raw * 10**factor, or None for SunSpec "not implemented" (inverter asleep)."""
    if factor == -32768 or abs(factor) > 10 or raw in _NOT_IMPLEMENTED:
        return None
    places = max(0, -factor) if digits is None else digits
    value = round(raw * 10.0**factor * multiplier, places)
    return int(value) if places == 0 else value


def _unpack(fmt: str, registers: list[int]) -> tuple[Any, ...]:
    return struct.unpack(fmt, struct.pack(f">{len(registers)}H", *registers))


def decode_ac(registers: list[int]) -> dict[str, float | None]:
    """AC side: current and voltage (total and per phase), power, frequency, apparent/reactive power, factor, energy."""
    v = _unpack(_AC_FORMAT, registers)
    return {
        "ac_current": scale(v[0], v[4]),
        "ac_current_a": scale(v[1], v[4]),
        "ac_current_b": scale(v[2], v[4]),
        "ac_current_c": scale(v[3], v[4]),
        "ac_voltage": scale(v[8], v[11]),
        "ac_voltage_a": scale(v[8], v[11]),
        "ac_voltage_b": scale(v[9], v[11]),
        "ac_voltage_c": scale(v[10], v[11]),
        "ac_power": scale(v[12], v[13]),
        "ac_frequency": scale(v[14], v[15]),
        "ac_apparent_power": scale(v[16], v[17]),
        "ac_reactive_power": scale(v[18], v[19]),
        "ac_power_factor": scale(v[20], v[21]),
        # SunSpec accumulators report 0 for "not implemented".
        "ac_energy": scale(v[22], v[23], 0.001, 3) if v[22] else None,
    }


def decode_dc(registers: list[int]) -> dict[str, float | int | str | None]:
    """DC side, heatsink temperature and operating status."""
    v = _unpack(_DC_FORMAT, registers)
    return {
        "dc_current": scale(v[0], v[1]),
        "dc_voltage": scale(v[2], v[3]),
        "dc_power": scale(v[4], v[5]),
        "heatsink_temperature": scale(v[7], v[10]),
        "status_code": v[11],
        "status_vendor_code": v[12],
        "status": STATUS_LABELS.get(v[11], f"Unknown ({v[11]})"),
    }


def decode_config(kind: str, registers: list[int]) -> float | int:
    """Decode a Power Control register (32-bit values have the low word first)."""
    if kind == "i16":
        return struct.unpack(">h", struct.pack(">H", registers[0]))[0]
    pair = struct.pack(">HH", registers[1], registers[0])
    return struct.unpack({"f32": ">f", "i32": ">i", "u32": ">I"}[kind], pair)[0]


def _words(raw: bytes) -> list[int]:
    return [int.from_bytes(raw[2:4], "big"), int.from_bytes(raw[0:2], "big")]


def encode_float32(value: float) -> list[int]:
    return _words(struct.pack(">f", float(value)))


def encode_uint32(value: int) -> list[int]:
    return _words(struct.pack(">I", int(value)))


def encode_int32(value: int) -> list[int]:
    return _words(struct.pack(">i", int(value)))
