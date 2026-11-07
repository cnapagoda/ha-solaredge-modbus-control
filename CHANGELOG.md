# Changelog

## Unreleased

- Per-phase AC current and voltage sensors (A, B, C; disabled by default, B and C are only meaningful on three-phase inverters).
- Production limit in watts (`number`), kept in step with the percent limit using the inverter's 100 % reference power.
- "Output limited" binary sensor, on while the inverter reports it is throttling.
- Read-only diagnostic sensors for the meter-based export control: mode, limit mode and site limit.

## 0.1.0

- First release: telemetry, Dynamic Power Control (production limit), config, reconfigure and options flows, diagnostics.
