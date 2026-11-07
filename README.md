# SolarEdge Modbus Control

Home Assistant integration for SolarEdge inverters over local Modbus TCP: live telemetry **and** a production limit you can set from automations, with no cloud, no add-on and no YAML.

- **Telemetry:** AC and DC power, energy, voltage, current, frequency, power factor, heatsink temperature and inverter status. Ready for the Energy dashboard.
- **Production limit:** a `number` entity (0–100 % of rated power) that curtails the inverter, for example when the feed-in price goes negative.
- **Safe by design:** if Home Assistant stops, the inverter returns to its fallback limit (default 100 %) after the command timeout.
- **One connection:** a single paced Modbus client, so reads and writes never collide. SolarEdge inverters accept only one Modbus TCP client at a time.

## Requirements

- A SolarEdge inverter with Modbus TCP enabled (SetApp: *Commissioning → Site Communication → Modbus TCP*). Control needs a recent HD-Wave or SetApp inverter that supports Dynamic Power Control.
- Home Assistant 2025.6 or newer.
- **No other Modbus client** on the inverter. Remove or disable any other SolarEdge Modbus integration, add-on or script first.

## Install

1. In HACS, add this repository as a custom repository (type *Integration*) and install **SolarEdge Modbus Control**, or copy `custom_components/solaredge_modbus_control` into your `custom_components` folder.
2. Restart Home Assistant.
3. *Settings → Devices & services → Add integration → SolarEdge Modbus Control*. Enter the inverter's address (default port 1502) and tick **Allow controlling the production limit** if you want the number entity.

## Entities

| Entity | Notes |
|---|---|
| AC power, AC energy, AC current, AC voltage, AC frequency, AC apparent/reactive power, AC power factor | AC energy is a lifetime `total_increasing` counter in kWh. |
| DC power, DC current, DC voltage, Heatsink temperature, Status | Status is text (MPPT (Producing), Sleeping, Throttled …). |
| Production limit (`number`) | Only with control enabled. Survives restarts. |
| Dynamic active power limit and other control registers | Diagnostic sensors that show what the inverter has accepted. |

The inverter's Modbus server is off at night, so entities are unavailable until sunrise.

## Limiting production

Set `number.<name>_production_limit` to a percentage of rated power. 100 means no limit. Changes are written immediately and then refreshed in the background.

```yaml
action: number.set_value
target:
  entity_id: number.solaredge_production_limit
data:
  value: 20
```

Options (*Configure*): update interval, command timeout (default 120 s) and fallback limit (default 100 %).

## Troubleshooting

- **Cannot connect:** check the address, that Modbus TCP is enabled, and that nothing else is connected to the inverter.
- **Limit has no effect:** download the diagnostics and check that *Enable dynamic power control* is 1 and *Command timeout* matches your option.

## Development

```bash
pip install -r requirements_test.txt
ruff check .
pytest -q --cov=custom_components.solaredge_modbus_control
```

Tests use an in-memory fake inverter; nothing talks to real hardware. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Disclaimer

Not affiliated with SolarEdge. This integration writes to inverter control registers. Use it at your own risk and check your grid operator's rules before curtailing export.

MIT licence.
