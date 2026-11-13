# Changelog

## Unreleased

- Fix: with control enabled, telemetry stopped updating. Each limit write restarted the poll timer, and the 40 s heartbeat is shorter than the 60 s scan interval.
- Fix: a limit write could put older telemetry back for a moment, which made the lifetime energy go down ("not strictly increasing" warnings).
- Fix: a request without an answer is tried once more on a new connection, instead of making every entity unavailable until the next poll.
- Fix: the limit is still refreshed while a telemetry poll fails, so one lost poll cannot let it expire.
- Fix: after an outage the settings are read again and applied only if they changed (before: a full set-up every time). Every setting written at set-up is now checked, not only two of them.
- Fix: a lifetime energy reading of 0, a lower one, or one higher than the inverter can produce is ignored.
- Fix: reconfiguring no longer opens a second connection next to the running one.
- Fix: timeouts were logged with an empty reason.
- Fix: a cancelled request or an unloading entry could leave a half-read answer or a reopened connection behind.

- Per-phase AC current and voltage sensors (A, B, C; disabled by default, B and C are only meaningful on three-phase inverters).
- Production limit in watts (`number`), kept in step with the percent limit using the inverter's 100 % reference power.
- "Output limited" binary sensor, on while the inverter reports it is throttling.
- Read-only diagnostic sensors for the meter-based export control: mode, limit mode and site limit.

## 0.1.0

- First release: telemetry, Dynamic Power Control (production limit), config, reconfigure and options flows, diagnostics.
