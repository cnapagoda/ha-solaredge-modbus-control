"""SolarEdge Modbus Control: telemetry and Dynamic Power Control over local Modbus TCP."""
from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant

from .const import (
    CONF_COMMAND_TIMEOUT,
    CONF_ENABLE_CONTROL,
    CONF_FALLBACK_LIMIT,
    CONF_SLAVE,
    DEFAULT_COMMAND_TIMEOUT,
    DEFAULT_FALLBACK_LIMIT,
)
from .coordinator import SolarEdgeCoordinator
from .modbus import ModbusTcpClient
from .power_control import PowerLimitController

PLATFORMS = [Platform.BINARY_SENSOR, Platform.NUMBER, Platform.SENSOR]


@dataclass
class RuntimeData:
    client: ModbusTcpClient
    coordinator: SolarEdgeCoordinator
    controller: PowerLimitController | None


type SolarEdgeConfigEntry = ConfigEntry[RuntimeData]


async def async_setup_entry(hass: HomeAssistant, entry: SolarEdgeConfigEntry) -> bool:
    client = ModbusTcpClient(entry.data[CONF_HOST], entry.data[CONF_PORT], entry.data[CONF_SLAVE])
    coordinator = SolarEdgeCoordinator(hass, entry, client)
    controller = None
    if entry.options.get(CONF_ENABLE_CONTROL, entry.data.get(CONF_ENABLE_CONTROL, False)):
        controller = PowerLimitController(
            hass,
            coordinator,
            command_timeout=entry.options.get(CONF_COMMAND_TIMEOUT, DEFAULT_COMMAND_TIMEOUT),
            fallback_limit=entry.options.get(CONF_FALLBACK_LIMIT, DEFAULT_FALLBACK_LIMIT),
        )
        coordinator.controller = controller
    entry.runtime_data = RuntimeData(client, coordinator, controller)

    # The inverter's Modbus server is down at night: set up anyway, entities are unavailable until it answers.
    await coordinator.async_refresh()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    if controller is not None:
        controller.start()
        entry.async_on_unload(controller.stop)
    entry.async_on_unload(entry.add_update_listener(_reload))
    return True


async def _reload(hass: HomeAssistant, entry: SolarEdgeConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: SolarEdgeConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.client.close()
    return unloaded
