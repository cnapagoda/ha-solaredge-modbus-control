"""Inverter binary sensors."""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SolarEdgeConfigEntry
from .entity import SolarEdgeEntity
from .sunspec import STATUS_THROTTLED

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: SolarEdgeConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([OutputLimited(entry.runtime_data.coordinator)])


class OutputLimited(SolarEdgeEntity, BinarySensorEntity):
    """On while the inverter reports it is throttling its output."""

    _attr_icon = "mdi:speedometer-slow"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "output_limited")

    @property
    def is_on(self) -> bool | None:
        code = self.coordinator.data.get("status_code")
        return None if code is None else code == STATUS_THROTTLED
