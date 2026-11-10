# ha EcoFlow official

[![HACS][hacs-badge]][hacs-url]
[![Home Assistant][ha-badge]][ha-url]
[![License][license-badge]][license-url]

Home Assistant custom integration that connects to EcoFlow inverters via **Modbus TCP**, exposing all register points as entities.

## Features

- **Full register coverage** — all documented Modbus points mapped to HA entities
- **Auto-discovery** — devices broadcast via mDNS (`_ecoflow._tcp.local.`), appear in HA automatically
- **Product filtering** — only loads registers supported by your device (STREAM 5000, STREAM AC 5000)
- **Region labels** — entity names start with the protocol region, such as
  `D1. Device serial number (SN)` and `S1. Current system fault count`.
- **Zero dependencies** — pymodbus is vendored; installs offline with no pip requirements
- **Shared Modbus connection** — on HA 2026.9+, reuses the built-in Modbus integration's connection so multiple integrations can share a single TCP link

## Supported Devices

| Device | Registers | Status |
|--------|-----------|--------|
| STREAM 5000 | Full | Supported |
| STREAM AC 5000 | Full | Supported |

## Installation

### HACS (Recommended)

1. Open HACS in Home Assistant
2. Go to **Integrations** → **⋮** menu → **Custom repositories**
3. Add `EcoFlow/modbus-API` with category **Integration**
4. Search for **ha EcoFlow official** and install
5. Restart Home Assistant

### Manual

Copy the `custom_components/ha_ecoflow_official` directory into your Home Assistant `config/custom_components/` folder and restart.

## Configuration

### Auto-Discovery (Recommended)

Devices advertise themselves via mDNS. Home Assistant will show a discovery notification — just click **Configure** and confirm.

The `_ecoflow._tcp.local.` service TXT record uses `model` for the product
model (for example, `STREAM AC 5000`) and `serial` for the device SN.

### UI Manual Setup

**Settings** → **Devices & Services** → **Add Integration** → search **ha EcoFlow official** → enter IP and port. Connectivity is verified on submit.

STREAM devices expose their D-class points on individual devices. S-class points
belong to a separate **EcoFLow STREAM System** device.

### YAML (Advanced)

```yaml
ha_ecoflow_official:
  - host: 192.168.1.10
    port: 502               # optional, default 502
    unit_id: 1              # optional, default 1
    scan_interval: 5        # optional, polling interval in seconds
```

## Entities

| Platform | Condition | Examples |
|----------|-----------|----------|
| Sensor | Read-only registers | System SOC, Grid power, PV voltage, Fault codes |
| Number | Writable numeric registers | Backup reserve ratio, Power limits, Setpoints |
| Select | Enumerated control registers | Control point |
| Switch | Boolean control registers | Bypass AC1/2/3, Force off-grid |

## Language Support

The integration adapts to your Home Assistant language setting. Unsupported languages fall back to English.

| Language | Status |
|----------|--------|
| English | ✅ |
| Deutsch | ✅ |
| Français | ✅ |
| Italiano | ✅ |
| Nederlands | ✅ |
| Polski | ✅ |

## FAQ

### 1. How do Home Assistant and the EcoFlow App work together to control devices?

When no control point is set in Home Assistant, devices are controlled by EcoFlow's
local Energy Management System (EMS). Limits set in Home Assistant work together
with EcoFlow App controls. Combining these controls can enable scheduling behavior
that the App alone does not provide.

Regulation-related features, such as power unlocking and permission to export to
the grid, can only be configured in the EcoFlow App. All Home Assistant control
operates within the system's safety constraints.

### 2. How are multiple devices controlled?

If multiple devices support operating together as a system, Home Assistant controls
the system, which then controls each individual device within it. To control each
device independently, configure each device as a separate system in the EcoFlow App.

### 3. Why can't Home Assistant discover my device?

Confirm that the device firmware has been upgraded to a supported version and
that the **Modbus TCP** switch is enabled in the EcoFlow App. This switch is
**disabled by default**.

### 4. Why do the energy statistics differ from those shown in the EcoFlow App?

The EcoFlow App accumulates energy statistics from the time the devices form a
system. The cumulative energy counters read by Home Assistant are a feature
newly supported after a firmware update. Their statistics therefore have
different starting times.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and contribution guidelines.

## License

This project is licensed under the [MIT License](LICENSE).

The vendored [pymodbus](https://github.com/pymodbus-dev/pymodbus) library is licensed under the [BSD-3-Clause License](custom_components/ha_ecoflow_official/vendor/LICENSE).

---

[hacs-badge]: https://img.shields.io/badge/HACS-Custom-41BDF5.svg
[hacs-url]: https://hacs.xyz
[ha-badge]: https://img.shields.io/badge/HA-2025.1%2B-41BDF5.svg
[ha-url]: https://www.home-assistant.io
[license-badge]: https://img.shields.io/badge/License-MIT-yellow.svg
[license-url]: LICENSE
