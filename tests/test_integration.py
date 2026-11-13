"""Setup, entities, availability and diagnostics."""
from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from pytest_homeassistant_custom_component.components.diagnostics import get_diagnostics_for_config_entry

from custom_components.solaredge_modbus_control.const import DOMAIN
from custom_components.solaredge_modbus_control.sunspec import (
    REG_FALLBACK_LIMIT,
    REG_MAX_ACTIVE_POWER,
    encode_float32,
)
from tests.conftest import AC, DC, SERIAL, make_entry


async def setup(hass, entry):
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_entities_have_the_expected_ids_and_values(hass, inverter, clock):
    entry = make_entry()
    await setup(hass, entry)
    expected = {
        "ac_power": "2300",
        "ac_energy": "123456.789",
        "ac_current": "10.0",
        "ac_voltage": "240.0",
        "ac_frequency": "50.02",
        "ac_apparent_power": "2350",
        "ac_reactive_power": "-100",
        "ac_power_factor": "98",
        "dc_power": "2400",
        "dc_current": "5.0",
        "dc_voltage": "400.0",
        "heatsink_temperature": "45.0",
        "status": "MPPT (Producing)",
        "status_code": "4",
        "status_vendor_code": "7",
        "dynamic_active_power_limit": "100.0",
        "max_active_power_rated": "5000.0",
        "power_base_100_reference": "5000.0",
        "command_timeout": "120",
        "fallback_active_power_limit": "100.0",
        "ramp_up_rate": "100.0",
        "ramp_down_rate": "100.0",
        "advanced_power_control_enable": "1",
        "enable_dynamic_power_control": "1",
        "active_reactive_preference": "0",
        "ac_current_phase_a": "10.0",
        "ac_current_phase_b": STATE_UNKNOWN,
        "ac_current_phase_c": STATE_UNKNOWN,
        "ac_voltage_phase_a": "240.0",
        "ac_voltage_phase_b": STATE_UNKNOWN,
        "ac_voltage_phase_c": STATE_UNKNOWN,
        "export_control_mode": "1",
        "export_limit_mode": "0",
        "export_site_limit": "3000.0",
    }
    registry = er.async_get(hass)
    for suffix in expected:
        entity_id = f"sensor.solaredge_direct_{suffix}"
        if registry.async_get(entity_id).disabled:  # per-phase sensors are opt-in
            registry.async_update_entity(entity_id, disabled_by=None)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    for suffix, value in expected.items():
        state = hass.states.get(f"sensor.solaredge_direct_{suffix}")
        assert state is not None, suffix
        assert state.state == value, suffix
    assert hass.states.get("number.solaredge_direct_production_limit").state == "100.0"


async def test_device(hass, inverter, clock):
    await setup(hass, make_entry())
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, SERIAL)})
    assert (device.manufacturer, device.model, device.sw_version, device.name) == (
        "SolarEdge",
        "SE5000H-AUS",
        "0004.0021.0002",
        "SolarEdge Direct",
    )


async def test_inverter_asleep_at_setup_then_wakes(hass, inverter, clock):
    inverter.online = False
    entry = make_entry()
    await setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get("sensor.solaredge_direct_ac_power").state == STATE_UNAVAILABLE
    assert hass.states.get("number.solaredge_direct_production_limit").state != STATE_UNAVAILABLE
    inverter.online = True
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
    await hass.async_block_till_done()
    assert hass.states.get("sensor.solaredge_direct_ac_power").state == "2300"


async def test_inverter_going_to_sleep(hass, inverter, clock):
    await setup(hass, make_entry())
    inverter.online = False
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
    await hass.async_block_till_done()
    assert hass.states.get("sensor.solaredge_direct_ac_power").state == STATE_UNAVAILABLE


async def test_not_implemented_values_are_unknown(hass, inverter, clock):
    ac = list(AC)
    ac[13] = -32768  # AC power scale factor: inverter asleep
    inverter.set_ac(ac)
    await setup(hass, make_entry())
    assert hass.states.get("sensor.solaredge_direct_ac_power").state == STATE_UNKNOWN


async def test_corrupted_power_frame_is_ignored(hass, inverter, clock, caplog):
    entry = make_entry()
    await setup(hass, entry)
    ac = list(AC)
    ac[12] = 28542  # 28.5 kW on a 5 kW inverter
    inverter.set_ac(ac)
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get("sensor.solaredge_direct_ac_power").state == "2300"
    assert "corrupted frame" in caplog.text


ENERGY = "sensor.solaredge_direct_ac_energy"


async def poll_energy(hass, inverter, clock, entry, watt_hours, *, seconds=60):
    ac = list(AC)
    ac[22] = watt_hours
    inverter.set_ac(ac)
    clock.advance(seconds)
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()
    return hass.states.get(ENERGY).state


async def test_lifetime_energy_follows_normal_production(hass, inverter, clock):
    entry = make_entry()
    await setup(hass, entry)
    assert await poll_energy(hass, inverter, clock, entry, AC[22] + 80) == "123456.869"
    # A night without contact, then a full day's production in one step.
    inverter.online = False
    await poll_energy(hass, inverter, clock, entry, AC[22], seconds=36000)
    inverter.online = True
    assert await poll_energy(hass, inverter, clock, entry, AC[22] + 30000) == "123486.789"


async def test_lifetime_energy_does_not_go_down_or_jump(hass, inverter, clock, caplog):
    entry = make_entry()
    await setup(hass, entry)
    assert await poll_energy(hass, inverter, clock, entry, AC[22] - 5000) == "123456.789"
    assert await poll_energy(hass, inverter, clock, entry, AC[22] + 1_000_000) == "123456.789"
    assert await poll_energy(hass, inverter, clock, entry, 0) == STATE_UNKNOWN
    assert await poll_energy(hass, inverter, clock, entry, AC[22] + 100) == "123456.889"
    assert caplog.text.count("Ignoring lifetime energy") == 1


async def test_lifetime_energy_that_stays_different_is_accepted(hass, inverter, clock, caplog):
    entry = make_entry()
    await setup(hass, entry)
    for _ in range(4):
        assert await poll_energy(hass, inverter, clock, entry, 1000) == "123456.789"
    assert await poll_energy(hass, inverter, clock, entry, 1000) == "1.0"
    assert caplog.text.count("Ignoring lifetime energy") == 1
    assert "accepting it" in caplog.text


async def test_lifetime_energy_without_a_rated_power_only_refuses_to_go_down(hass, inverter, clock):
    inverter.illegal = {REG_MAX_ACTIVE_POWER}
    entry = make_entry(control=False)
    await setup(hass, entry)
    assert await poll_energy(hass, inverter, clock, entry, AC[22] + 1_000_000) == "124456.789"
    assert await poll_energy(hass, inverter, clock, entry, AC[22]) == "124456.789"


async def test_settings_are_read_again_after_an_outage_and_floats_are_rounded(hass, inverter, clock):
    entry = make_entry(control=False)
    await setup(hass, entry)
    inverter.online = False
    await entry.runtime_data.coordinator.async_refresh()
    inverter.set(REG_FALLBACK_LIMIT, encode_float32(33.3))
    inverter.online = True
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get("sensor.solaredge_direct_fallback_active_power_limit").state == "33.3"


async def test_register_the_inverter_refuses_is_unknown(hass, inverter, clock):
    inverter.illegal = {REG_MAX_ACTIVE_POWER, 0xF322}
    await setup(hass, make_entry())
    assert hass.states.get("sensor.solaredge_direct_max_active_power_rated").state == STATE_UNKNOWN
    assert hass.states.get("sensor.solaredge_direct_dynamic_active_power_limit").state == STATE_UNKNOWN
    assert hass.states.get("sensor.solaredge_direct_ac_power").state == "2300"


async def test_config_registers_are_read_every_five_minutes(hass, inverter, clock):
    await setup(hass, make_entry(control=False))
    inverter.set(REG_MAX_ACTIVE_POWER, encode_float32(6000))
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
    await hass.async_block_till_done()
    assert hass.states.get("sensor.solaredge_direct_max_active_power_rated").state == "5000.0"
    clock.advance(301)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=122))
    await hass.async_block_till_done()
    assert hass.states.get("sensor.solaredge_direct_max_active_power_rated").state == "6000.0"


async def test_read_only_entry_has_no_number_and_never_writes(hass, inverter, clock):
    await setup(hass, make_entry(control=False))
    assert hass.states.get("number.solaredge_direct_production_limit") is None
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
    await hass.async_block_till_done()
    assert inverter.writes == []


async def test_unload(hass, inverter, clock):
    entry = make_entry()
    await setup(hass, entry)
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert inverter.closed
    assert inverter.shut
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_options_change_reloads(hass, inverter, clock):
    entry = make_entry()
    await setup(hass, entry)
    hass.config_entries.async_update_entry(entry, options={"enable_control": False, "scan_interval": 30})
    await hass.async_block_till_done()
    assert hass.states.get("number.solaredge_direct_production_limit") is None
    assert entry.runtime_data.coordinator.update_interval == timedelta(seconds=30)


async def test_diagnostics(hass, hass_client, inverter, clock):
    entry = make_entry()
    await setup(hass, entry)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=10))
    await hass.async_block_till_done()
    diag = await get_diagnostics_for_config_entry(hass, hass_client, entry)
    assert diag["entry"]["data"]["host"] == "**REDACTED**"
    assert diag["entry"]["data"]["serial"] == "**REDACTED**"
    assert diag["data"]["ac_power"] == 2300
    assert diag["control"]["target"] == 100.0
    assert diag["control"]["last_written"] is not None


async def test_diagnostics_without_control(hass, hass_client, inverter, clock):
    entry = make_entry(control=False)
    await setup(hass, entry)
    diag = await get_diagnostics_for_config_entry(hass, hass_client, entry)
    assert diag["control"] is None


async def test_output_limited_follows_the_throttled_status(hass, inverter, clock):
    entry = make_entry()
    await setup(hass, entry)
    assert hass.states.get("binary_sensor.solaredge_direct_output_limited").state == "off"
    dc = list(DC)
    dc[11] = 5
    inverter.set_dc(dc)
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.solaredge_direct_output_limited").state == "on"


async def test_output_limited_is_unavailable_while_asleep(hass, inverter, clock):
    inverter.online = False
    await setup(hass, make_entry())
    assert hass.states.get("binary_sensor.solaredge_direct_output_limited").state == STATE_UNAVAILABLE


async def test_power_limit_in_watts_mirrors_the_percent_limit(hass, inverter, clock):
    await setup(hass, make_entry())
    entity = "number.solaredge_direct_production_limit_power"
    state = hass.states.get(entity)
    assert (state.state, state.attributes["max"], state.attributes["unit_of_measurement"]) == ("5000.0", 5000.0, "W")

    await hass.services.async_call("number", "set_value", {"entity_id": entity, "value": 1500}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get("number.solaredge_direct_production_limit").state == "30.0"
    assert hass.states.get(entity).state == "1500.0"

    await hass.services.async_call(
        "number", "set_value", {"entity_id": "number.solaredge_direct_production_limit", "value": 40}, blocking=True
    )
    await hass.async_block_till_done()
    assert hass.states.get(entity).state == "2000.0"


async def test_power_limit_in_watts_unavailable_until_base_power_is_known(hass, inverter, clock):
    inverter.online = False
    await setup(hass, make_entry())
    assert hass.states.get("number.solaredge_direct_production_limit_power").state == STATE_UNAVAILABLE
    inverter.online = True
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
    await hass.async_block_till_done()
    assert hass.states.get("number.solaredge_direct_production_limit_power").state == "5000.0"


async def test_power_limit_in_watts_removed_when_control_is_off(hass, inverter, clock):
    entry = make_entry()
    await setup(hass, entry)
    hass.config_entries.async_update_entry(entry, options={"enable_control": False})
    await hass.async_block_till_done()
    assert hass.states.get("number.solaredge_direct_production_limit_power") is None
