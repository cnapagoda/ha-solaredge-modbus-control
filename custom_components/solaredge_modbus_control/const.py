"""Constants for the SolarEdge Modbus Control integration."""
from __future__ import annotations

DOMAIN = "solaredge_modbus_control"

CONF_SLAVE = "slave"
CONF_ENABLE_CONTROL = "enable_control"
CONF_MANUFACTURER = "manufacturer"
CONF_MODEL = "model"
CONF_VERSION = "version"
CONF_SERIAL = "serial"

CONF_SCAN_INTERVAL = "scan_interval"
CONF_COMMAND_TIMEOUT = "command_timeout"
CONF_FALLBACK_LIMIT = "fallback_limit"

DEFAULT_NAME = "SolarEdge"
DEFAULT_PORT = 1502
DEFAULT_SLAVE = 1
DEFAULT_SCAN_INTERVAL = 60
DEFAULT_COMMAND_TIMEOUT = 120
DEFAULT_FALLBACK_LIMIT = 100.0

# How often the (slow-changing) control registers are read back.
CONFIG_POLL_SECONDS = 300
# How often the power limit controller wakes up to check what to write.
CONTROL_TICK_SECONDS = 10
