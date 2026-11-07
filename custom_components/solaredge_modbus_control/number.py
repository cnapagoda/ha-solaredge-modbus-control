"""The production limit: percent of the inverter's rated power."""
from __future__ import annotations

from homeassistant.components.number import NumberMode, RestoreNumber
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SolarEdgeConfigEntry
from .const import CONF_SERIAL, DOMAIN
from .entity import SolarEdgeEntity

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant, entry: SolarEdgeConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    runtime = entry.runtime_data
    if runtime.controller is not None:
        async_add_entities([ProductionLimit(runtime.coordinator, runtime.controller)])
        return
    # Control was switched off: drop the entity left over from when it was on.
    registry = er.async_get(hass)
    unique_id = f"{entry.data[CONF_SERIAL]}_production_limit"
    if entity_id := registry.async_get_entity_id("number", DOMAIN, unique_id):
        registry.async_remove(entity_id)


class ProductionLimit(SolarEdgeEntity, RestoreNumber):
    """Setting this caps the inverter output; 100 means no limit.

    The value is kept across restarts. The inverter falls back to its own
    fallback limit if Home Assistant stops refreshing it."""

    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 0.1
    _attr_native_unit_of_measurement = "%"
    _attr_mode = NumberMode.BOX
    _attr_icon = "mdi:solar-power-variant"

    def __init__(self, coordinator, controller) -> None:
        super().__init__(coordinator, "production_limit")
        self._controller = controller

    @property
    def available(self) -> bool:
        # The target can be set while the inverter sleeps; it is written once it wakes.
        return True

    @property
    def native_value(self) -> float:
        return self._controller.target

    @property
    def extra_state_attributes(self) -> dict:
        written = self._controller.last_written_at
        return {"last_written": written.isoformat(timespec="seconds") if written else None}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_number_data()) is not None and last.native_value is not None:
            self._controller.target = max(0.0, min(100.0, float(last.native_value)))
        self.async_on_remove(self._controller.add_listener(self.async_write_ha_state))

    async def async_set_native_value(self, value: float) -> None:
        await self._controller.async_set_target(value)
        self.async_write_ha_state()
