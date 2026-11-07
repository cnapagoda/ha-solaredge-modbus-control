# Contributing

Bug reports, tests on other inverter models and pull requests are welcome.

- **Open an issue first** for anything bigger than a small fix.
- **Safety first.** This integration writes to real inverter control registers. A change that writes needs a clear reason, a guard and a test.
- **Tests for all new code**, coverage stays at 99 % or above, `ruff check .` clean.
- **Strings** go in `strings.json` and `translations/en.json`.
- **A `CHANGELOG.md` line** and a line or two in the README.
- **No secrets**, addresses or serial numbers in code, tests, logs or screenshots.

```bash
pip install -r requirements_test.txt
ruff check .
pytest -q --cov=custom_components.solaredge_modbus_control --cov-report=term-missing
```
