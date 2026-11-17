# Contributing to ha EcoFlow official

Thank you for your interest in contributing! This guide will help you get started.

## Prerequisites

- [Home Assistant](https://www.home-assistant.io/) development environment
- Python 3.12+
- Basic understanding of [Modbus TCP](https://en.wikipedia.org/wiki/Modbus)

## Development Setup

1. Fork and clone the repository
2. Copy `custom_components/ha_ecoflow_official` into your HA dev config
3. Start the [Modbus Simulator](https://github.com/EcoFlow/modbus-API) for testing:
   ```bash
   python server.py --modbus-port 502 --web-port 8089 --product "STREAM 5000"
   ```
4. Restart Home Assistant and add the integration

## Project Structure

```
custom_components/ha_ecoflow_official/
├── __init__.py           # Integration setup, YAML import
├── config_flow.py        # UI config flow, mDNS discovery
├── coordinator.py        # Data update coordinator
├── modbus_client.py      # Modbus TCP client wrapper
├── link.py               # Connection probing
├── const.py              # Constants, select/switch definitions
├── registers_schema.py   # Auto-generated register point table
├── sensor.py             # Read-only register entities
├── number.py             # Writable numeric entities
├── select.py             # Enumerated control entities
├── switch.py             # Boolean toggle entities
├── button.py             # One-shot write entities
├── strings.json          # English translation source
├── translations/         # Multi-language translations
└── vendor/               # Vendored pymodbus (do not modify)
```

## Making Changes

### Code Changes

- Follow existing code patterns and naming conventions
- Keep entity implementations simple — one class per platform
- Do not modify files under `vendor/` — these are vendored dependencies

### Register Schema

`registers_schema.py` is auto-generated from the EcoFlow Modbus point table. Do not edit it by hand. If registers need updating, update the source table and regenerate.

### Translations

Translation files are in `translations/`. When adding new entities or changing entity names:

1. Update `strings.json` (English source of truth)
2. Update all translation files in `translations/` (en, de, fr, it, nl, pl)
3. Keep the JSON key structure identical across all files

## Pull Request Process

1. Create a feature branch from `main`
2. Make your changes
3. Test with the Modbus Simulator and at least one real device if possible
4. Ensure the HACS validation workflow passes
5. Submit a pull request with a clear description of the change

## Automated validation

`Validate` runs the official HACS Action and Hassfest on pushes, pull requests,
published releases and a daily schedule. HACS checks are not ignored. `Tests`
runs the existing Python regression suite on Python 3.12, 3.13 and 3.14, plus the
frontend checks on Node.js 22.

Run the offline checks from the repository root:

```sh
python scripts/check_repository.py
python -m unittest discover -s tests -v
node tests/frontend-layout.mjs
```

These commands need no pip or npm packages; the Python tests use framework
substitutes and the vendored Modbus client. They do not replace real-device tests.
Before publishing a release, also run `python scripts/check_repository.py --tag
v1.3.1`, using the version from the manifest. Tag pushes and published releases
automatically run the same version consistency check.

See the [HACS submission checklist](.github/HACS_SUBMISSION.md) for repository
settings, release requirements and the default-list PR process. The workflows
validate changes without automatically publishing or submitting anything.

## Reporting Issues

Use [GitHub Issues](https://github.com/EcoFlow-oasis-official/ha-EcoFlow-official/issues) to report bugs or request features. Include:

- Your Home Assistant version
- Integration version (from `manifest.json`)
- Device model (PowerOcean / STREAM 5000)
- Relevant log entries

## License

By contributing, you agree that your contributions will be licensed under the [MIT License](LICENSE).
