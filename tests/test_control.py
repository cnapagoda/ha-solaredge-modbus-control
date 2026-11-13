"""Dynamic Power Control: set-up, limit writes, heartbeat and failure handling."""
from __future__ import annotations

import asyncio
from datetime import timedelta
import logging

from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import State
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed, mock_restore_cache_with_extra_data

from custom_components.solaredge_modbus_control.modbus import ModbusConnectionError
from custom_components.solaredge_modbus_control.sunspec import (
    REG_ACTIVE_REACTIVE,
    REG_ADV_POWER_CONTROL,
    REG_COMMAND_TIMEOUT,
    REG_DYNAMIC_LIMIT,
    REG_ENABLE_DYNAMIC,
    REG_FALLBACK_LIMIT,
    REG_POWER_BASE,
    REG_RAMP_DOWN,
    REG_RAMP_UP,
    decode_config,
    encode_float32,
    encode_uint32,
)
from tests.conftest import AC, make_entry

NUMBER = "number.solaredge_direct_production_limit"
RESTORED = {"native_max_value": 100, "native_min_value": 0, "native_step": 0.1, "native_unit_of_measurement": "%"}


async def setup(hass, entry):
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry.runtime_data.controller


async def tick(hass, seconds=10):
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()


def last_limit(inverter):
    return decode_config("f32", inverter.writes_to(REG_DYNAMIC_LIMIT)[-1])


async def test_first_tick_sets_up_dynamic_power_control_then_writes_the_limit(hass, inverter, clock):
    controller = await setup(hass, make_entry())
    assert inverter.writes == []
    await tick(hass)
    assert [(k, a) for k, a, _ in inverter.writes] == [
        ("multiple", REG_ADV_POWER_CONTROL),
        ("single", REG_ACTIVE_REACTIVE),
        ("multiple", REG_POWER_BASE),
        ("multiple", REG_COMMAND_TIMEOUT),
        ("multiple", REG_FALLBACK_LIMIT),
        ("multiple", REG_RAMP_UP),
        ("multiple", REG_RAMP_DOWN),
        ("single", REG_ENABLE_DYNAMIC),
        ("multiple", REG_DYNAMIC_LIMIT),
    ]
    assert decode_config("f32", inverter.writes_to(REG_POWER_BASE)[0]) == 5000
    assert decode_config("u32", inverter.writes_to(REG_COMMAND_TIMEOUT)[0]) == 120
    assert inverter.writes_to(REG_ENABLE_DYNAMIC) == [[1]]
    assert last_limit(inverter) == 100.0
    assert controller.last_written_at is not None


async def test_setting_the_number_writes_at_once(hass, inverter, clock):
    await setup(hass, make_entry())
    await tick(hass)
    await hass.services.async_call("number", "set_value", {"entity_id": NUMBER, "value": 12.5}, blocking=True)
    assert last_limit(inverter) == 12.5
    assert hass.states.get(NUMBER).state == "12.5"
    assert hass.states.get("sensor.solaredge_direct_dynamic_active_power_limit").state == "12.5"
    assert hass.states.get(NUMBER).attributes["last_written"] is not None


async def test_limit_is_clamped(hass, inverter, clock):
    controller = await setup(hass, make_entry())
    await tick(hass)
    await controller.async_set_target(150)
    assert last_limit(inverter) == 100
    await controller.async_set_target(-5)
    assert last_limit(inverter) == 0


async def test_heartbeat_rewrites_an_unchanged_limit_every_third_of_the_timeout(hass, inverter, clock):
    controller = await setup(hass, make_entry())
    assert controller.heartbeat == 40
    await tick(hass)
    writes = len(inverter.writes_to(REG_DYNAMIC_LIMIT))
    clock.advance(20)
    await tick(hass, 20)
    assert len(inverter.writes_to(REG_DYNAMIC_LIMIT)) == writes
    clock.advance(21)
    await tick(hass, 41)
    assert len(inverter.writes_to(REG_DYNAMIC_LIMIT)) == writes + 1


async def test_custom_timeout_sets_the_heartbeat(hass, inverter, clock):
    controller = await setup(hass, make_entry(command_timeout=30, fallback_limit=80))
    assert controller.heartbeat == 10
    await tick(hass)
    assert decode_config("u32", inverter.writes_to(REG_COMMAND_TIMEOUT)[0]) == 30
    assert decode_config("f32", inverter.writes_to(REG_FALLBACK_LIMIT)[0]) == 80


def warnings(caplog, text):
    return [r for r in caplog.records if r.levelno >= logging.WARNING and text in r.getMessage()]


async def test_a_failed_poll_does_not_stop_the_heartbeat(hass, inverter, clock):
    controller = await setup(hass, make_entry())
    await tick(hass)
    inverter.writes.clear()
    inverter.read_error = ModbusConnectionError("no answer")
    clock.advance(61)
    await tick(hass, 61)
    assert not controller.coordinator.last_update_success
    assert len(inverter.writes_to(REG_DYNAMIC_LIMIT)) == 1
    await tick(hass, 71)
    assert len(inverter.writes_to(REG_DYNAMIC_LIMIT)) == 1
    clock.advance(41)
    await tick(hass, 81)
    assert len(inverter.writes_to(REG_DYNAMIC_LIMIT)) == 2
    # A new target does not wait for the heartbeat.
    await controller.async_set_target(30)
    assert last_limit(inverter) == 30
    assert inverter.writes_to(REG_ENABLE_DYNAMIC) == []


async def test_unreachable_inverter_is_tried_once_per_heartbeat_and_not_set_up_again(hass, inverter, clock, caplog):
    await setup(hass, make_entry())
    await tick(hass)
    inverter.writes.clear()
    inverter.write_attempts = 0
    inverter.online = False
    clock.advance(61)
    await tick(hass, 61)
    await tick(hass, 71)
    await tick(hass, 81)
    assert inverter.write_attempts == 1
    assert inverter.writes == []
    assert not warnings(caplog, "Could not write the power limit")
    inverter.online = True
    clock.advance(61)
    await tick(hass, 122)
    await tick(hass, 132)
    assert inverter.writes_to(REG_DYNAMIC_LIMIT)
    assert inverter.writes_to(REG_ENABLE_DYNAMIC) == []
    assert "Power limit writes working again" not in caplog.text


async def test_settings_lost_during_an_outage_are_applied_again(hass, inverter, clock):
    await setup(hass, make_entry())
    await tick(hass)
    inverter.writes.clear()
    inverter.online = False
    clock.advance(61)
    await tick(hass, 61)
    inverter.set(REG_ENABLE_DYNAMIC, [0])
    inverter.set(REG_FALLBACK_LIMIT, encode_float32(0))
    inverter.online = True
    clock.advance(61)
    await tick(hass, 122)
    await tick(hass, 132)
    assert inverter.writes_to(REG_ENABLE_DYNAMIC) == [[1]]
    assert decode_config("f32", inverter.writes_to(REG_FALLBACK_LIMIT)[0]) == 100
    assert inverter.writes_to(REG_DYNAMIC_LIMIT)


async def test_set_up_is_not_tried_on_every_tick_while_polls_fail(hass, inverter, clock, caplog):
    controller = await setup(hass, make_entry())
    inverter.online = False
    await controller.coordinator.async_refresh()
    for _ in range(3):
        await controller.async_write()
    assert inverter.write_attempts == 1
    assert not warnings(caplog, "Could not set up dynamic power control")
    assert controller.last_written_at is None


async def test_a_poll_that_finishes_during_a_write_is_kept(hass, inverter, clock):
    controller = await setup(hass, make_entry())
    await tick(hass)
    inverter.gate = asyncio.Event()
    first = hass.async_create_task(controller.async_set_target(40))
    second = hass.async_create_task(controller.async_set_target(60))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    ac = list(AC)
    ac[12] = 1234
    inverter.set_ac(ac)
    await controller.coordinator.async_refresh()
    inverter.gate.set()
    await first
    await second
    await hass.async_block_till_done()
    # The writes waited their turn, one after the other, and the newer telemetry survived them.
    assert [decode_config("f32", w) for w in inverter.writes_to(REG_DYNAMIC_LIMIT)[-2:]] == [40, 60]
    assert controller.coordinator.data["ac_power"] == 1234
    assert controller.coordinator.data["dynamic_limit"] == 60
    assert hass.states.get("sensor.solaredge_direct_ac_power").state == "1234"


async def test_a_write_cut_short_by_unloading_is_not_reported(hass, inverter, clock, caplog):
    entry = make_entry()
    controller = await setup(hass, entry)
    await tick(hass)
    inverter.gate = asyncio.Event()
    write = hass.async_create_task(controller.async_set_target(40))
    await asyncio.sleep(0)
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert inverter.shut
    inverter.write_error = ModbusConnectionError("the client is shut down")
    inverter.gate.set()
    await write
    assert not warnings(caplog, "Could not write the power limit")


async def test_failed_limit_write_is_retried_and_logged_once(hass, inverter, clock, caplog):
    controller = await setup(hass, make_entry())
    await tick(hass)
    inverter.write_error = ModbusConnectionError("boom")
    await controller.async_set_target(20)
    await controller.async_write()
    assert caplog.text.count("Could not write the power limit") == 1
    inverter.write_error = None
    await controller.async_write()
    assert last_limit(inverter) == 20
    assert "Power limit writes working again" in caplog.text


async def test_failed_setup_is_retried(hass, inverter, clock, caplog):
    controller = await setup(hass, make_entry())
    inverter.write_error = ModbusConnectionError("boom")
    await controller.async_write()
    await controller.async_write()
    assert caplog.text.count("Could not set up dynamic power control") == 1
    assert inverter.writes == []
    inverter.write_error = None
    await controller.async_write()
    assert inverter.writes_to(REG_ENABLE_DYNAMIC) == [[1]]


async def test_unknown_rated_power_blocks_setup(hass, inverter, clock, caplog):
    inverter.illegal = {0xF304}
    controller = await setup(hass, make_entry())
    await controller.async_write()
    await controller.async_write()
    assert caplog.text.count("Rated power unknown") == 1
    assert inverter.writes == []


async def test_settings_changed_behind_our_back_are_applied_again(hass, inverter, clock, caplog):
    controller = await setup(hass, make_entry())
    await tick(hass)
    inverter.writes.clear()
    inverter.set(REG_ENABLE_DYNAMIC, [0])
    clock.advance(301)
    await controller.coordinator.async_refresh()
    await controller.async_write()
    assert "differ from the configured ones" in caplog.text
    assert inverter.writes_to(REG_ENABLE_DYNAMIC) == [[1]]
    # Once applied, the old read-back no longer counts.
    inverter.writes.clear()
    await controller.async_write()
    assert inverter.writes_to(REG_ENABLE_DYNAMIC) == []


async def test_different_timeout_is_applied_again(hass, inverter, clock):
    controller = await setup(hass, make_entry())
    await tick(hass)
    inverter.writes.clear()
    inverter.set(REG_COMMAND_TIMEOUT, encode_uint32(5))
    clock.advance(301)
    await controller.coordinator.async_refresh()
    await controller.async_write()
    assert decode_config("u32", inverter.writes_to(REG_COMMAND_TIMEOUT)[0]) == 120


async def test_every_setting_is_compared_but_a_float_read_back_is_not_a_difference(hass, inverter, clock, caplog):
    controller = await setup(hass, make_entry(fallback_limit=33.3))
    await tick(hass)
    inverter.writes.clear()
    clock.advance(301)
    await controller.coordinator.async_refresh()
    await controller.async_write()
    assert inverter.writes_to(REG_ENABLE_DYNAMIC) == []
    inverter.set(REG_RAMP_UP, encode_float32(5))
    clock.advance(301)
    await controller.coordinator.async_refresh()
    await controller.async_write()
    assert "ramp_up_rate is 5.0" in caplog.text
    assert decode_config("f32", inverter.writes_to(REG_RAMP_UP)[0]) == 100


async def test_unreadable_settings_do_not_trigger_set_up(hass, inverter, clock):
    controller = await setup(hass, make_entry())
    await tick(hass)
    inverter.writes.clear()
    inverter.illegal = {0xF300, 0xF310}
    clock.advance(301)
    await controller.coordinator.async_refresh()
    await controller.async_write()
    assert inverter.writes_to(REG_ENABLE_DYNAMIC) == []


async def test_stopping_stops_the_writes(hass, inverter, clock):
    entry = make_entry()
    controller = await setup(hass, entry)
    await tick(hass)
    await hass.config_entries.async_unload(entry.entry_id)
    inverter.writes.clear()
    clock.advance(100)
    await tick(hass, 100)
    assert inverter.writes == []
    controller.stop()


async def test_limit_is_restored_after_a_restart_and_written_on_the_first_tick(hass, inverter, clock):
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(NUMBER, "35.0"),
                RESTORED | {"native_value": 35.0},
            )
        ],
    )
    controller = await setup(hass, make_entry())
    assert controller.target == 35.0
    await tick(hass)
    assert last_limit(inverter) == 35.0


async def test_number_stays_available_while_the_inverter_sleeps(hass, inverter, clock):
    inverter.online = False
    controller = await setup(hass, make_entry())
    assert hass.states.get(NUMBER).state != STATE_UNAVAILABLE
    await hass.services.async_call("number", "set_value", {"entity_id": NUMBER, "value": 40}, blocking=True)
    assert controller.target == 40
    assert inverter.writes == []
    inverter.online = True
    await controller.coordinator.async_refresh()
    await controller.async_write()
    assert last_limit(inverter) == 40


@pytest.mark.parametrize("restored", ["unknown", None])
async def test_no_usable_restored_value_keeps_the_default(hass, inverter, clock, restored):
    mock_restore_cache_with_extra_data(
        hass,
        [(State(NUMBER, "unknown"), RESTORED | {"native_value": restored})],
    )
    controller = await setup(hass, make_entry())
    assert controller.target == 100.0


async def test_heartbeat_writes_do_not_starve_telemetry_polling(hass, inverter, clock, freezer):
    """Writes every 40 s must not keep pushing back the 60 s telemetry poll."""
    await setup(hass, make_entry())
    ac = list(AC)
    ac[12] = 1234
    inverter.set_ac(ac)
    for _ in range(30):  # 5 minutes in 10 s control ticks
        clock.advance(10)
        freezer.tick(timedelta(seconds=10))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
    assert len(inverter.writes_to(REG_DYNAMIC_LIMIT)) >= 6
    assert hass.states.get("sensor.solaredge_direct_ac_power").state == "1234"
