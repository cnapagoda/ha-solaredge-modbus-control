"""Inverter sensors."""
from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    EntityCategory,
    UnitOfApparentPower,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfPower,
    UnitOfReactivePower,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SolarEdgeConfigEntry
from .entity import SolarEdgeEntity

PARALLEL_UPDATES = 0

_M = SensorStateClass.MEASUREMENT
_DIAG = EntityCategory.DIAGNOSTIC


@dataclass(frozen=True, kw_only=True)
class SolarEdgeSensorDescription(SensorEntityDescription):
    """A sensor whose value is `data[key]`."""


def _s(key: str, device_class=None, unit=None, state_class=_M, *, category=None, enabled=True, icon=None):
    return SolarEdgeSensorDescription(
        key=key,
        translation_key=key,
        device_class=device_class,
        native_unit_of_measurement=unit,
        state_class=state_class,
        entity_category=category,
        entity_registry_enabled_default=enabled,
        icon=icon,
    )


SENSORS: tuple[SolarEdgeSensorDescription, ...] = (
    _s("ac_power", SensorDeviceClass.POWER, UnitOfPower.WATT),
    _s("ac_energy", SensorDeviceClass.ENERGY, UnitOfEnergy.KILO_WATT_HOUR, SensorStateClass.TOTAL_INCREASING),
    _s("ac_current", SensorDeviceClass.CURRENT, UnitOfElectricCurrent.AMPERE),
    _s("ac_voltage", SensorDeviceClass.VOLTAGE, UnitOfElectricPotential.VOLT),
    _s("ac_frequency", SensorDeviceClass.FREQUENCY, UnitOfFrequency.HERTZ),
    _s("ac_apparent_power", SensorDeviceClass.APPARENT_POWER, UnitOfApparentPower.VOLT_AMPERE),
    _s("ac_reactive_power", SensorDeviceClass.REACTIVE_POWER, UnitOfReactivePower.VOLT_AMPERE_REACTIVE),
    _s("ac_power_factor", SensorDeviceClass.POWER_FACTOR, "%"),
    _s("dc_power", SensorDeviceClass.POWER, UnitOfPower.WATT),
    _s("dc_current", SensorDeviceClass.CURRENT, UnitOfElectricCurrent.AMPERE),
    _s("dc_voltage", SensorDeviceClass.VOLTAGE, UnitOfElectricPotential.VOLT),
    _s("heatsink_temperature", SensorDeviceClass.TEMPERATURE, UnitOfTemperature.CELSIUS),
    _s("status", state_class=None, icon="mdi:solar-power"),
    _s("status_code", state_class=None, category=_DIAG),
    _s("status_vendor_code", state_class=None, category=_DIAG),
    _s("dynamic_limit", unit="%", category=_DIAG, icon="mdi:solar-power-variant"),
    _s("max_active_power", SensorDeviceClass.POWER, UnitOfPower.WATT, category=_DIAG),
    _s("power_base", SensorDeviceClass.POWER, UnitOfPower.WATT, category=_DIAG),
    _s("command_timeout", SensorDeviceClass.DURATION, UnitOfTime.SECONDS, category=_DIAG),
    _s("fallback_limit", unit="%", category=_DIAG),
    _s("ramp_up_rate", unit="%/min", category=_DIAG),
    _s("ramp_down_rate", unit="%/min", category=_DIAG),
    _s("advanced_power_control", state_class=None, category=_DIAG),
    _s("dynamic_power_control", state_class=None, category=_DIAG),
    _s("active_reactive_preference", state_class=None, category=_DIAG),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: SolarEdgeConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data.coordinator
    async_add_entities(SolarEdgeSensor(coordinator, description) for description in SENSORS)


class SolarEdgeSensor(SolarEdgeEntity, SensorEntity):
    entity_description: SolarEdgeSensorDescription

    def __init__(self, coordinator, description: SolarEdgeSensorDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self):
        return self.coordinator.data.get(self.entity_description.key)
