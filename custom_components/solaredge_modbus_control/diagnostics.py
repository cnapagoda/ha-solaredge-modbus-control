"""Diagnostics."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant

from . import SolarEdgeConfigEntry
from .const import CONF_SERIAL

TO_REDACT = {CONF_HOST, CONF_SERIAL, "unique_id", "title"}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: SolarEdgeConfigEntry) -> dict[str, Any]:
    runtime = entry.runtime_data
    controller = runtime.controller
    return {
        "entry": async_redact_data(entry.as_dict(), TO_REDACT),
        "data": runtime.coordinator.data,
        "control": None
        if controller is None
        else {
            "target": controller.target,
            "command_timeout": controller.command_timeout,
            "fallback_limit": controller.fallback_limit,
            "last_written": controller.last_written_at.isoformat() if controller.last_written_at else None,
        },
    }
