# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Read the firmware System group ID at Modbus 44101 (`0xAC45`) as UINT32.
  Group STREAM members by this ID while keeping it out of display names.
- Reuse the existing virtual system and route data/control to the current
  master's connection, updating the stored host/port/unit without recreating
  upper-level entities. Migrate linked temporary systems in place, retaining
  their entity/device identity; consolidate linked duplicate virtual entries
  into the established system, retaining its sensors and moving missing points.
- Prefix entity display names with protocol regions (`D1.`, `S1.`, etc.)
  in English, German, French, Italian, Dutch and Polish.
- Editable product profile table with multiple SN prefixes per model and
  longest-prefix matching for future model extensions.

### Changed
- Name virtual systems `EcoFLow STREAM System`, `EcoFLow STREAM System (2)`,
  and so on, using a saved display number independent of the firmware group ID.
  Migrate default entity ID prefixes to `ecoflow_stream_system_` and
  `ecoflow_stream_system_2_`; preserve custom IDs and internal unique IDs.
  References to migrated entity IDs in automations and dashboards need review.
- Batch adjacent readable registers into FC03 requests of at most 125 words,
  without crossing address gaps or reading write-only points. Keep each block
  together when spreading polling across the configured scan interval.
- Reduce a complete STREAM 5000 / STREAM AC 5000 scan from 93 individual
  requests to 28 block requests with the current register schema. Both the
  shared HA connection and bundled client remember rejected blocks and use
  individual reads for those blocks until the integration is reloaded.

### Fixed
- Load entities for an existing firmware-group virtual system even when no
  physical master is online during startup. Persist master confirmation when
  discovering the system, avoiding an empty device if the role changes before
  entity setup; keep entities unavailable until the master returns.
- Set all eight STREAM power-limit controls (`0x1600`–`0x160E`, step 2) to
  the confirmed range 0–100000 W instead of the UINT32 storage maximum.
  Reject out-of-range, non-finite and fractional integer writes before Modbus I/O.
- Expose Control Point options as semantic keys with translated labels, while
  continuing to write Modbus values 0–4. Its stored HA states are now `none`,
  `battery_point`, `meter_point`, `inverter_point` and `grid_port_point`;
  automations referring to the old numeric states/options must use these keys.
- Correct device error code 10 from offset `0x2020` to `0x201A` (Modbus
  address 48219) and migrate its existing sensor without changing the entity ID.
- Give writable numbers valid numeric bounds, including negative INT16/INT32
  values, and use input boxes for wide ranges so power setpoints can be entered.
- Use the `model` TXT field for mDNS product identification instead of `product`.
- Identify manually configured products by SN prefix (ES21: STREAM 5000,
  ES22: STREAM AC 5000, otherwise PowerOcean) before loading supported registers.
- Correct existing manual entries on reload, and reload entities if the serial
  becomes available only after the initial setup.
- Match manual setup, YAML import and mDNS discovery against both stored serial
  numbers and connection endpoints, including legacy entries with older IDs.
- Persist serial numbers read from Modbus so rediscovery can recognize existing
  entries after a restart or IP address change without replacing entity IDs.
- Recheck discovery confirmation to avoid duplicates added while the form is open.

## [1.2.0] - 2025-10-15

### Added
- Multi-language support: English, German, French, Italian, Dutch, Polish
- Automatic language detection from Home Assistant settings
- STREAM 5000 product support with dedicated registers
- Battery pack registers (SOC, SOH, SN) for up to 12 packs

### Changed
- Entity names now use Home Assistant's native translation system
- Select and enum entities use translated state labels

## [1.1.0] - 2025-09-01

### Added
- mDNS auto-discovery (`_ecoflow._tcp.local.`)
- YAML configuration import
- Product-based register filtering
- Heartbeat keep-alive (every 20s)

## [1.0.0] - 2025-08-01

### Added
- Initial release
- Modbus TCP connection to EcoFlow inverters
- Sensor, Number, Select, Switch, Button entity platforms
- PowerOcean product support
- Vendored pymodbus 3.7.4 (zero external dependencies)
