"""Config, reconfigure and options flows."""
from __future__ import annotations

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.setup import async_setup_component

from custom_components.solaredge_modbus_control.const import DOMAIN
from tests.conftest import SERIAL, make_entry

USER = {"name": "SolarEdge Direct", "host": "192.0.2.10", "port": 1502, "slave": 1, "enable_control": True}


async def start(hass):
    return await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})


async def test_user_flow_creates_the_entry(hass, inverter, clock):
    result = await start(hass)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "SolarEdge Direct"
    assert result["result"].unique_id == SERIAL
    assert result["data"]["serial"] == SERIAL
    assert result["data"]["model"] == "SE5000H-AUS"
    assert result["options"] == {"enable_control": True}
    await hass.async_block_till_done()


async def test_user_flow_errors_and_recovers(hass, inverter, clock):
    inverter.online = False
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER)
    assert result["errors"] == {"base": "cannot_connect"}
    inverter.online = True
    inverter.memory[40000] = 0
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER)
    assert result["errors"] == {"base": "not_sunspec"}
    inverter.memory[40000] = 0x5375
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()


async def test_already_configured(hass, inverter, clock):
    make_entry().add_to_hass(hass)
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_adding_an_address_in_use_pauses_the_entry_that_uses_it(hass, inverter, clock):
    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER | {"host": "192.0.2.99"})
    assert result["reason"] == "already_configured"
    assert inverter.pauses == 0
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER | {"slave": 2})
    assert result["reason"] == "already_configured"
    assert inverter.pauses == 1
    assert inverter.reads_while_paused == 1


async def test_reconfigure(hass, inverter, clock):
    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "192.0.2.20", "port": 1503, "slave": 2}
    )
    assert result["reason"] == "reconfigure_successful"
    # The inverter accepts one connection: the running entry let go of it for the check.
    assert inverter.pauses == 1
    assert inverter.reads_while_paused == 1
    assert (entry.data["host"], entry.data["port"], entry.data["slave"]) == ("192.0.2.20", 1503, 2)
    await hass.async_block_till_done()


async def test_reconfigure_errors(hass, inverter, clock):
    entry = make_entry()
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    new = {"host": "192.0.2.20", "port": 1502, "slave": 1}
    inverter.online = False
    result = await hass.config_entries.flow.async_configure(result["flow_id"], new)
    assert result["errors"] == {"base": "cannot_connect"}
    inverter.online = True
    inverter.memory[40001] = 0
    result = await hass.config_entries.flow.async_configure(result["flow_id"], new)
    assert result["errors"] == {"base": "not_sunspec"}
    inverter.memory[40001] = 0x6E53
    inverter.memory[40052] = 0x4142  # a different serial number
    result = await hass.config_entries.flow.async_configure(result["flow_id"], new)
    assert result["reason"] == "unique_id_mismatch"


async def test_options_flow(hass, inverter, clock):
    entry = make_entry()
    entry.add_to_hass(hass)
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    values = {"scan_interval": 30, "enable_control": False, "command_timeout": 90, "fallback_limit": 50.0}
    result = await hass.config_entries.options.async_configure(result["flow_id"], values)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == values
    await hass.async_block_till_done()
