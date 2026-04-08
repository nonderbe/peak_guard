# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

**Peak Guard** is a Home Assistant custom integration for Belgian electricity customers on Fluvius capacity-based tariffs. It has two operating modes:

1. **Modus 1 – Peak Limitation**: Turns off configured devices when the current quarterly average power threatens to exceed the monthly peak, then restores them once the threat passes. Tracks avoided peaks and calculates cost savings.
2. **Modus 2 – Injection Prevention**: When solar surplus is injected into the grid, turns on consumers (EV charger, boiler, etc.) to shift that energy locally, avoiding poor sell-back rates.

## Validation / CI

There are no local test or lint commands. Validation runs on GitHub Actions via:
- **HACS validation** — checks integration structure, manifest, and metadata
- **hassfest validation** — checks HA integration conformance

Both run automatically on push/PR to `main`. There is no local equivalent beyond running them via Docker or reviewing HA hassfest rules manually.

## Architecture

The integration lives entirely in `custom_components/peak_guard/`.

### Core data flow

1. **Setup** (`__init__.py` → `async_setup_entry`): Loads config, creates `PeakGuardController`, registers REST API endpoints (`/api/peak_guard/cascade`, `/api/peak_guard/force_check`), registers the sidebar panel, and starts the monitoring loop.

2. **Monitoring loop** (`controller.py` → `_monitoring_loop_async`): Runs every 5–60 seconds (configurable). Reads current power and monthly peak from HA sensors, computes quarterly average via `QuarterCalculator`, then drives the **peak cascade** (turn devices off) or **inject cascade** (turn devices on) as needed.

3. **Cascade execution**: Devices in each cascade are stored as `CascadeDevice` dataclasses in priority order. The controller iterates them, calls HA services (`switch.turn_off`, `switch.turn_on`, `number.set_value`), and records events in the appropriate tracker.

4. **Trackers** (`avoided_peak_tracker.py`, and the solar equivalent): Record avoidance/shift events through a 3-phase lifecycle (pending → active → completed), compute kW impact on the quarterly history, and calculate EUR savings using Fluvius 2026 tariffs from `const.py`.

5. **Sensor updates** (`sensor.py`): 16+ read-only sensors are updated each monitoring cycle and also on a 1-minute interval. They expose quarter kW, monthly peak, capacity costs, savings, shifted kWh, etc.

6. **Frontend panel** (`frontend/peak_guard_panel.js`): A ~2000-line custom Web Component (no framework) that polls `/api/peak_guard/cascade` every 15 seconds, shows real-time status (countdown, last loop timestamp), and lets users drag-drop reorder devices and configure EV charger setups.

7. **Persistence** (HA `Store` API): Three stores — cascade config (`peak_guard.cascade`), 30-day quarter history (`peak_guard.quarters`), and savings state (`peak_guard.savings`) — survive restarts.

### Key classes

| Class | File | Role |
|---|---|---|
| `PeakGuardController` | `controller.py` | Core orchestrator; owns cascades, monitoring loop, EV state machines |
| `CascadeDevice` | `controller.py` | Dataclass for a single cascade entry (action type, priority, EV config) |
| `PeakAvoidTracker` | `avoided_peak_tracker.py` | Tracks peak avoidance events and computes kW/EUR impact |
| `QuarterCalculator` | `quarter_calculator.py` | Derives quarterly average power from cumulative kWh sensor |
| `QuarterStore` | `quarter_store.py` | Persists rolling 30-day quarter history |
| `EVRateLimiter` | `controller.py` | Sliding-window rate limiter (max 12 calls / 10 min per EV device) |
| `EVDeviceGuard` | `controller.py` | Per-device state machine for EV charger (idle → waiting_for_stable → charging) |

### EV charger handling

EV chargers are significantly more complex than simple switches:
- Managed via 3 entities: switch (on/off), number (charge current in A), optional SOC-limit number
- Rate-limited to 12 service calls per 10 minutes to avoid hammering Tesla/Easee APIs
- Solar surplus must be stable (±150 W) for 45 seconds before acting (debounce)
- 1 A hysteresis to prevent thrashing
- Wake-up support: detects sleeping EV and calls a wake button before attempting to charge

### Configuration constants (`const.py`)

- `FLUVIUS_REGIO_TARIEVEN`: 2026 capacity tariffs in €/kW/year, keyed by Flemish region name
- `DEFAULT_BUFFER_WATTS = 100` — threshold margin in watts
- `DEFAULT_UPDATE_INTERVAL = 5` — monitoring loop frequency in seconds
- `DEFAULT_POWER_DETECTION_TOLERANCE_PERCENT = 10` — tolerance for "natural stop" detection
- `DEFAULT_SOLAR_NETTO_EUR_PER_KWH = 0.25` — assumed injection savings in €/kWh

## REST API

| Endpoint | Methods | Purpose |
|---|---|---|
| `/api/peak_guard/cascade` | GET, POST | Fetch or update the full cascade configuration |
| `/api/peak_guard/force_check` | POST | Trigger an immediate monitoring cycle |

## No external Python dependencies

The integration imports only from the Python standard library and Home Assistant built-ins. Do not add third-party packages.
