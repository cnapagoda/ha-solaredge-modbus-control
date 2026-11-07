"""The production limit: percent of the inverter's rated power."""
from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode, RestoreNumber
from homeassistant.const import UnitOfPower
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
        async_add_entities(
            [
                ProductionLimit(runtime.coordinator, runtime.controller),
                ProductionLimitPower(runtime.coordinator, runtime.controller),
            ]
        )
        return
    # Control was switched off: drop the entities left over from when it was on.
    registry = er.async_get(hass)
    for key in ("production_limit", "production_limit_power"):
        if entity_id := registry.async_get_entity_id("number", DOMAIN, f"{entry.data[CONF_SERIAL]}_{key}"):
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


class ProductionLimitPower(SolarEdgeEntity, NumberEntity):
    """The same production limit in watts, using the inverter's 100 % reference power."""

    _attr_native_min_value = 0
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_mode = NumberMode.BOX
    _attr_icon = "mdi:solar-power-variant"

    def __init__(self, coordinator, controller) -> None:
        super().__init__(coordinator, "production_limit_power")
        self._controller = controller

    @property
    def _base(self) -> float | None:
        data = self.coordinator.data or {}
        return data.get("power_base") or data.get("max_active_power") or None

    @property
    def available(self) -> bool:
        # Like the percent limit this can be set while the inverter sleeps, once the base power is known.
        return self._base is not None

    @property
    def native_max_value(self) -> float:
        return self._base or 0

    @property
    def native_value(self) -> float | None:
        base = self._base
        return None if base is None else round(self._controller.target * base / 100, 1)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._controller.add_listener(self.async_write_ha_state))

    async def async_set_native_value(self, value: float) -> None:
        await self._controller.async_set_target(round(max(0.0, value) / self._base * 100, 1))
        self.async_write_ha_state()
