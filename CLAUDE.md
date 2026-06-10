# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

**Peak Guard** is a Home Assistant custom integration for Belgian electricity customers on Fluvius capacity-based tariffs. It has two operating modes:

1. **Modus 1 – Peak Limitation**: Turns off configured devices when the current quarterly average power threatens to exceed the monthly peak, then restores them once the threat passes. Tracks avoided peaks and calculates cost savings.
2. **Modus 2 – Injection Prevention**: When solar surplus is injected into the grid, turns on consumers (EV charger, boiler, etc.) to shift that energy locally, avoiding poor sell-back rates.

## Validation / CI

Local tests run with:
```
python3 -m pytest tests/ -v
```

The test suite uses stub modules in `tests/conftest.py` to avoid a live HA install. Two test files:
- `tests/test_ev_guard.py` — 38 tests covering the EV state machine, rate limiter, debounce, and Tesla-specific paths
- `tests/test_tracker.py` — 16 tests covering the financial calculations in `PeakAvoidTracker` and `SolarShiftTracker`

GitHub Actions also runs on every push/PR to `main`:
- **HACS validation** — checks integration structure, manifest, and metadata
- **hassfest validation** — checks HA integration conformance

## Architecture

The integration lives entirely in `custom_components/peak_guard/`.

### Core data flow

1. **Setup** (`__init__.py` → `async_setup_entry`): Loads config, creates `PeakGuardController`, registers REST API endpoints (`/api/peak_guard/cascade`, `/api/peak_guard/force_check`), registers the sidebar panel, and starts the monitoring loop.

2. **Monitoring loop** (`controller.py` → `_monitoring_loop_async`): Runs every 5–60 seconds (configurable). Reads current power and monthly peak from HA sensors, computes quarterly average via `QuarterCalculator`, then drives the **peak cascade** (turn devices off) or **inject cascade** (turn devices on) as needed.

3. **Cascade execution**: Devices in each cascade are stored as `_BaseCascadeDevice` subclass instances in priority order. The controller iterates them, calls `device.apply(excess, snapshots, ctx)` polymorphically, and records events in the appropriate tracker. `CascadeContext` bundles `hass`, trackers, `ev_guard`, and callbacks into a single dependency-injection object threaded through the loop.

4. **Trackers** (`avoided_peak_tracker.py`, and the solar equivalent): Record avoidance/shift events through a 3-phase lifecycle (pending → active → completed), compute kW impact on the quarterly history, and calculate EUR savings using Fluvius 2026 tariffs from `const.py`.

5. **Sensor updates** (`sensor.py`): 16+ read-only sensors are updated each monitoring cycle and also on a 1-minute interval. They expose quarter kW, monthly peak, capacity costs, savings, shifted kWh, etc.

6. **Frontend panel** (`frontend/peak_guard_panel.js`): A ~2000-line custom Web Component (no framework) that polls `/api/peak_guard/cascade` every 15 seconds, shows real-time status (countdown, last loop timestamp), and lets users drag-drop reorder devices and configure EV charger setups.

7. **Persistence** (HA `Store` API): Three stores — cascade config (`peak_guard.cascade`), 30-day quarter history (`peak_guard.quarters`), and savings state (`peak_guard.savings`) — survive restarts.

### Key classes

| Class | File | Role |
|---|---|---|
| `PeakGuardController` | `controller.py` | Core orchestrator; owns cascades, monitoring loop, EV state machines |
| `_BaseCascadeDevice` | `models.py` | Abstract base for all cascade entries; exposes `apply()` / `restore()` |
| `SwitchOffDevice` | `models.py` | Simple switch-off device (peak cascade) |
| `SwitchOnDevice` | `models.py` | Simple switch-on device (inject cascade) |
| `ThrottleDevice` | `models.py` | Throttleable device with min/max/power_per_unit |
| `EVChargerDevice` | `models.py` | EV charger — all EV fields directly on the class (switch_entity, current_entity, phases, soc_entity, …) |
| `CascadeContext` | `models.py` | Dependency-injection bag threaded through cascade loop (hass, trackers, ev_guard, callbacks) |
| `from_dict()` | `models.py` | Factory that deserialises a dict into the correct subclass; migrates old `ev_*`-prefixed formats automatically |
| `PeakAvoidTracker` | `avoided_peak_tracker.py` | Tracks peak avoidance events and computes kW/EUR impact |
| `QuarterCalculator` | `quarter_calculator.py` | Derives quarterly average power from cumulative kWh sensor |
| `QuarterStore` | `quarter_store.py` | Persists rolling 30-day quarter history |
| `EVRateLimiter` | `models.py` | Sliding-window rate limiter (max 12 calls / 10 min per EV device) |
| `EVDeviceGuard` | `models.py` | Per-device state machine for EV charger (idle → waiting_for_stable → charging → sleeping) |

`CascadeDevice` remains as a backward-compat alias for `_BaseCascadeDevice`.

#### Device serialisation

`dataclasses.asdict(device)` is used for saving; `from_dict(d)` reconstructs the correct subclass on load. The migration function `_migrate_flat_format` inside `models.py` transparently converts two legacy formats:
- **Old flat format**: `ev_switch_entity`, `ev_phases`, etc. (pre-1.6)
- **Intermediate nested format**: `{"ev": {"switch_entity": …}}` (1.6.x)

### EV charger handling

EV chargers are significantly more complex than simple switches. All logic lives in `deciders/ev_guard.py` (`EVGuard`), called via `EVChargerDevice.apply()` / `.restore()`.

- **Entities**: switch (on/off), number (charge current in A), optional SOC-limit number
- **Rate limiter**: max 12 service calls per 10 minutes per device (`EVRateLimiter`)
- **Start-threshold gate**: surplus must reach `start_threshold_w` before debounce even begins; drops below → debounce is reset
- **Debounce / `_surplus_floor`**: 20 s wallclock timer (`EV_DEBOUNCE_STABLE_S`); the 10th-percentile floor of the surplus history must be positive before the EV is started; state is `WAITING_FOR_STABLE` while waiting
- **1 A hysteresis** (`EV_HYSTERESIS_AMPS`): prevents thrashing on small surplus changes
- **Minimum update interval** (`EV_MIN_UPDATE_INTERVAL_S`): rate-limits `set_value` calls even within a single rate-limit window
- **Min-OFF cooldown** (`EV_MIN_OFF_DURATION_S`): prevents restart too soon after PG turned the EV off
- **Wake-up support**: detects sleeping EV (via `status_sensor`), calls `wake_button`, waits up to `EV_WAKE_TIMEOUT_S`, then backs off for `EV_WAKE_COOLDOWN_S` on failure
- **Location guard**: skips all action when `location_tracker` is present and EV is not home
- **Manual-start detection**: if `switch_entity` reports `unknown`/`unavailable` but `status_sensor` confirms charging, the EV is treated as already on. This "handmatige start" path sets `guard.state = CHARGING` **and** calls `solar_tracker.start_solar_measurement` so the session is tracked even though Peak Guard didn't initiate it. Relevant for Tesla, whose switch entity is permanently `unknown`.

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

# Session limit recovery policy

When approaching session or quota exhaustion:

1. Stop unsafe operations
2. Commit all current work to git
3. Update NEXT_STEPS.md with:

   * current status
   * pending tasks
   * unresolved issues
4. Never rely on session-only timers
5. Use persistent resume mechanisms only:

   * crontab
   * tmux + cron
   * launchd (macOS)
6. Automatically resume after quota reset when possible

If automatic resume is impossible, clearly write the exact resume command in NEXT_STEPS.md.
