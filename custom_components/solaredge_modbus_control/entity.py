"""Base entity."""
from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_MANUFACTURER, CONF_MODEL, CONF_SERIAL, CONF_VERSION, DOMAIN
from .coordinator import SolarEdgeCoordinator


class SolarEdgeEntity(CoordinatorEntity[SolarEdgeCoordinator]):
    """One inverter is one device; entity names are relative to it."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: SolarEdgeCoordinator, key: str) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._attr_translation_key = key
        self._attr_unique_id = f"{entry.data[CONF_SERIAL]}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.data[CONF_SERIAL])},
            name=entry.title,
            manufacturer=entry.data.get(CONF_MANUFACTURER),
            model=entry.data.get(CONF_MODEL),
            sw_version=entry.data.get(CONF_VERSION),
            serial_number=entry.data[CONF_SERIAL],
        )
