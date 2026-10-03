# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

**Peak Guard** is a Home Assistant custom integration for Belgian electricity customers on Fluvius capacity-based tariffs. It has two operating modes, plus a charging schedule:

1. **Modus 1 – Peak Limitation**: Turns off configured devices when the current quarterly average power threatens to exceed the monthly peak, then restores them once the threat passes. Tracks avoided peaks and calculates cost savings.
2. **Modus 2 – Injection Prevention**: When solar surplus is injected into the grid, turns on consumers (EV charger, boiler, etc.) to shift that energy locally, avoiding poor sell-back rates.
3. **Planning – charging schedule**: During configured windows (time windows, or a tariff sensor such as `sensor.p1_meter_tarief` = 2), charges an EV to a target SoC or keeps a switch on. Overrides Modus 2; Modus 1 keeps priority. See "Charging schedule" below.

## Validation / CI

Local tests run from a project virtualenv (the system Python is externally managed, so `pip install` into it is refused):
```
python3 -m venv .venv && .venv/bin/python -m pip install -r requirements-test.txt   # once
.venv/bin/python -m pytest tests/ -v
```

`requirements-test.txt` (pytest, pytest-asyncio) is test tooling only — the integration itself still has no third-party dependencies.

The test suite uses stub modules in `tests/conftest.py` to avoid a live HA install; the stubbed `homeassistant.util.dt.as_local` uses a fixed Europe/Brussels time zone. Twelve test files:
- `tests/test_ev_guard.py` — 48 tests covering the EV state machine, rate limiter, debounce, and Tesla-specific paths
- `tests/test_tracker.py` — 21 tests covering the financial calculations in `PeakAvoidTracker` and `SolarShiftTracker`
- `tests/test_peak_floor.py` — 24 tests covering the 2.5 kW capacity-tariff floor (decider, savings, billed peak, decision log)
- `tests/test_month_rollover.py` — 4 tests covering the month/year rollover order in `SharedCapacityState`
- `tests/test_power_units.py` — 10 tests covering kW → W conversion of the consumption and peak sensors
- `tests/test_local_month.py` — 16 tests covering month/year boundaries in local time (quarter store, rollover, event-table timestamps)
- `tests/test_startup_restore.py` — 11 tests covering the startup restore of the year total and the finalisation of months missed while HA was down
- `tests/test_monthly_peak_history.py` — 33 tests covering the 36-month monthly-peak records in `QuarterStore` (recording, pruning, persistence, upgrade seeding, history queries, rejection of implausible quarters and invalid records)
- `tests/test_quarter_calculator.py` — 10 tests covering `QuarterCalculator` when the meter reading drops and recovers or yields an impossible value, and non-finite sensor states
- `tests/test_energy_units.py` — 7 tests covering Wh/MWh → kWh conversion of the energy sensor
- `tests/test_peak_verification.py` — 16 tests covering the automatic removal of quarters and monthly records that contradict the P1 meter's monthly peak
- `tests/test_schedule.py` — 63 tests covering the charging schedule: window logic (midnight, DST, overlap, tariff sensor), serialisation, `ScheduleDecider`, and the peak > schedule > injection interplay through `deciders/dispatch.run_tick`

`controller.py` and the frontend panel are not importable in this harness and are not covered.

GitHub Actions (`.github/workflows/validate.yml`) also runs on every push/PR to `main`:
- **Tests** — the pytest suite on Python 3.13 and 3.14
- **HACS validation** — checks integration structure, manifest, and metadata
- **hassfest validation** — checks HA integration conformance

## Architecture

The integration lives entirely in `custom_components/peak_guard/`.

### Core data flow

1. **Setup** (`__init__.py` → `async_setup_entry`): Loads config, creates `PeakGuardController`, registers REST API endpoints (`/api/peak_guard/cascade`, `/api/peak_guard/force_check`), registers the sidebar panel, and starts the monitoring loop.

2. **Monitoring loop** (`controller.py` → `_monitor_loop`; the per-tick decider order is in `deciders/dispatch.py::run_tick`): Runs every `update_interval` seconds with a hard minimum of 60 s — `_resolve_interval()` raises lower configured values (including the default of 5) to 60 and logs a warning, to protect the EV API quota. The loop also wakes early on an EV entity state change or a `force_check` call (`trigger_wakeup()`). Each tick reads current power and the monthly peak from HA sensors, then drives the **peak cascade** (turn devices off) or **inject cascade** (turn devices on) as needed.

3. **Cascade execution**: Devices in each cascade are stored as `_BaseCascadeDevice` subclass instances in priority order. The controller iterates them, calls `device.apply(excess, snapshots, ctx)` polymorphically, and records events in the appropriate tracker. `CascadeContext` bundles `hass`, trackers, `ev_guard`, and callbacks into a single dependency-injection object threaded through the loop.

4. **Trackers** (`avoided_peak_tracker.py`, and the solar equivalent): Record avoidance/shift events through a 3-phase lifecycle (pending → active → completed), compute kW impact on the quarterly history, and calculate EUR savings using Fluvius 2026 tariffs from `const.py`.

5. **Sensor updates** (`sensor.py`): 16+ read-only sensors are updated each monitoring cycle and also on a 1-minute interval. `SharedCapacityState` derives the quarterly average from the cumulative kWh sensor via `QuarterCalculator` on that timer. The sensors expose quarter kW, monthly peak, capacity costs, savings, shifted kWh, etc.

6. **Frontend panel** (`frontend/peak_guard_panel.js`): A ~3500-line custom Web Component (no framework) that polls `/api/peak_guard/cascade` every 15 seconds, shows real-time status (countdown, last loop timestamp), and lets users drag-drop reorder devices and configure EV charger setups.

7. **Persistence** (HA `Store` API): Eight stores survive restarts — cascade config (incl. the charging schedule and its runtime state), quarter history with monthly peaks, peak and solar year savings, peak and solar month state (events), per-device monthly savings, and the EV daily call budget. Keys are in `const.py` (`STORAGE_KEY_*`) and `sensor.py` (`_STORAGE_KEY_*_STATE`).

### Key classes

| Class | File | Role |
|---|---|---|
| `PeakGuardController` | `controller.py` | Core orchestrator; owns cascades, monitoring loop, EV state machines |
| `_BaseCascadeDevice` | `models.py` | Abstract base for all cascade entries; exposes `apply()` / `restore()` |
| `SwitchOffDevice` | `models.py` | Simple switch-off device (peak cascade) |
| `SwitchOnDevice` | `models.py` | Simple switch-on device (inject cascade) |
| `ThrottleDevice` | `models.py` | Throttleable device with min/max/power_per_unit |
| `EVChargerDevice` | `models.py` | EV charger — all EV fields directly on the class (switch_entity, current_entity, phases, soc_entity, charge_state_sensor, …) |
| `CascadeContext` | `models.py` | Dependency-injection bag threaded through cascade loop (hass, trackers, ev_guard, callbacks) |
| `from_dict()` | `models.py` | Factory that deserialises a dict into the correct subclass; migrates old `ev_*`-prefixed formats automatically |
| `PeakAvoidTracker` | `avoided_peak_tracker.py` | Tracks peak avoidance events and computes kW/EUR impact |
| `QuarterCalculator` | `quarter_calculator.py` | Derives quarterly average power from cumulative kWh sensor |
| `QuarterStore` | `quarter_store.py` | Persists the rolling 32-day quarter history and one monthly-peak record per month for 36 months |
| `EVRateLimiter` | `models.py` | Sliding-window rate limiter (max 12 calls / 10 min, one instance shared globally across all EV devices on the `EVGuard`) |
| `EVDailyCallBudget` | `ev_call_budget.py` | Persistent (HA `Store`) daily cap on real EV-API calls — long-horizon backstop above `EVRateLimiter`; survives restarts |
| `EVDeviceGuard` | `models.py` | Per-device state machine for EV charger (idle → waiting_for_stable → charging → sleeping); `scheduled` flag while the schedule drives it |
| `ScheduleEntry` / `ScheduleWindow` | `models.py` | A scheduled device (a copy of a cascade device, same `id`) and its time/sensor windows |
| `ScheduleDecider` | `deciders/schedule_decider.py` | Runs the charging schedule; decides ownership against the peak and inject cascades |

`CascadeDevice` remains as a backward-compat alias for `_BaseCascadeDevice`.

#### Device serialisation

`dataclasses.asdict(device)` is used for saving; `from_dict(d)` reconstructs the correct subclass on load. The migration function `_migrate_flat_format` inside `models.py` transparently converts two legacy formats:
- **Old flat format**: `ev_switch_entity`, `ev_phases`, etc. (pre-1.6)
- **Intermediate nested format**: `{"ev": {"switch_entity": …}}` (1.6.x)

### EV charger handling

EV chargers are significantly more complex than simple switches. All logic lives in `deciders/ev_guard.py` (`EVGuard`), called via `EVChargerDevice.apply()` / `.restore()`.

- **Entities**: switch (on/off), number (charge current in A), optional SOC-limit number
- **Rate limiter (short window)**: max 12 service calls per 10 minutes, one `EVRateLimiter` instance shared globally across every EV device on the `EVGuard` (not per-device). `_record_call()` is invoked for every real `_svc()` attempt — including failed retries — so a command that exhausts all `EV_CMD_MAX_RETRIES` still counts fully against the window; retry loops re-check `_rate_check()` before each attempt after the first so a filled window aborts the retry instead of continuing to call the underlying (e.g. Tesla) API unthrottled
- **Daily call budget (long window)**: `EVDailyCallBudget` (`ev_call_budget.py`) caps real EV-API calls per calendar day (`EV_API_DAILY_BUDGET` in `const.py`, default `10_000 // 30` = 333). Deliberately a *daily* cap computed from the monthly account quota rather than a cumulative monthly counter: a bad day exhausts only that day's budget and resets fully the next day, instead of a monthly budget where a problem early in the month keeps degrading every day after it. Does not reserve headroom for the base Tesla integration's own independent background polling, which shares the same account quota — tune `EV_API_DAILY_BUDGET` against the real account quota and observed background load. Persisted via HA `Store` (`STORAGE_KEY_EV_CALL_BUDGET`) so it survives restarts, unlike `EVRateLimiter`. Enforced in two places: `_rate_check()` consults it as a cheap early pre-check (skips before a retry loop even starts), and `_svc()` itself hard-enforces it unconditionally on every real service call — including call sites that never go through `_rate_check()` at all (e.g. the wake-button `button.press`) — by raising `HomeAssistantError` before the underlying `hass.services.async_call` fires. Injected via `EVGuard.set_daily_budget()` from `__init__.py::async_setup_entry`; `None` (no cap) in contexts that don't wire it up, e.g. tests
- **Start-threshold gate**: surplus must reach `start_threshold_w` before debounce even begins; drops below → debounce is reset
- **Debounce / `_surplus_floor`**: 20 s wallclock timer (`EV_DEBOUNCE_STABLE_S`); the 10th-percentile floor of the surplus history must be positive before the EV is started; state is `WAITING_FOR_STABLE` while waiting
- **1 A hysteresis** (`EV_HYSTERESIS_AMPS`): prevents thrashing on small surplus changes
- **Minimum update interval** (`EV_MIN_UPDATE_INTERVAL_S`): rate-limits `set_value` calls even within a single rate-limit window
- **Min-OFF cooldown** (`EV_MIN_OFF_DURATION_S`): prevents restart too soon after PG turned the EV off
- **Wake-up support**: detects sleeping EV (via `status_sensor`), calls `wake_button`, waits up to `EV_WAKE_TIMEOUT_S`, then backs off for `EV_WAKE_COOLDOWN_S` on failure
- **Location guard**: skips all action when `location_tracker` is present and EV is not home
- **Manual-start detection**: if `switch_entity` reports `unknown`/`unavailable` but `status_sensor` confirms charging, the EV is treated as already on. This "handmatige start" path sets `guard.state = CHARGING` **and** calls `solar_tracker.start_solar_measurement` so the session is tracked even though Peak Guard didn't initiate it. Relevant for Tesla, whose switch entity is permanently `unknown`.

### Charging schedule (Planning tab)

`schedule.py` holds the pure window logic and parsing; `deciders/schedule_decider.py` the control. Entries are stored in the cascade store under `schedule`, runtime state (active window, fulfilled, pending stop/rest limit, tariff-sensor memory) under `schedule_state`. REST: POST `/api/peak_guard/cascade` with `type: "schedule"`, `entries: [...]`; GET returns `schedule` and `status.schedule`.

- **Windows**: `kind="time"` (start days, `start`/`end` in local wall-clock time; `end <= start` runs into the next day, `start == end` is 24 h) or `kind="sensor"` (active while the sensor equals `active_state`, numeric states normalised so `2.0 == 2`). A tariff sensor that goes unavailable keeps its last state for `SCHEDULE_SENSOR_STALE_S` (15 min), and for the first 15 min after a (re)start regardless of age (the last state is persisted at least every 5 min), so a restart doesn't end and restart a window. Active = any window active; overlapping windows take the highest `target_soc`. Edges are detected on that merged state.
- **Ownership per tick**: peak (peak snapshot) > schedule (window active, EV target not reached) > solar (inject snapshot). `InjectionDecider` skips schedule-owned devices (`is_controlled`); `PeakDecider.check_restore` skips scheduled EVs (`skip_peak_restore`) because restoring an EV (`power_watts=0`, Tesla switch possibly `unknown`) would flap — the schedule restores it itself when there is headroom for the minimum current. `PeakDecider.check()` is never skipped.
- **EV in window**: takes over an inject snapshot; sets the charge limit to the window target (only when it differs); starts at `floor((effective_peak − buffer − house load) / V)` capped at `max_current`; only raises the current later (≥ 2 A, ≥ 5 min), decreases are left to the peak cascade. A `turn_on` that doesn't lead to charging is retried after 3 min, and after 3 attempts backs off 30 min. Fulfilled (sticky per window) when the battery ≥ target, or the charge state is `complete` at the target limit → released; solar may then charge on to `max_soc`.
- **Window end**: drops any peak snapshot; hands a running charge to solar when there would be surplus without the EV (and the EV is in the inject cascade), otherwise stops it; then sets the rest limit (`rest_soc`, deferred while the car is asleep). `EVGuard._set_soc_override(False)` uses `soc_rest_for()` (window target in a window, else `rest_soc`) instead of the snapshot value.
- **Outside windows** (`block_unplanned`): a charge Peak Guard didn't start and that isn't solar-owned is stopped after `SCHEDULE_UNPLANNED_GRACE_S` (2 min), unless there would be surplus without the EV (→ handed to solar) or `manual_override` is set on any copy of the device. The block wins over `rest_soc`.
- **Is it charging?** `EVGuard.is_charging()`: switch `on` (and charge state not stopped/complete), or charge state `charging`/`starting`. The charge state comes from the EV field `charge_state_sensor` (e.g. Tesla `sensor.*_opladen`), falling back to textual values of `status_sensor`; a binary online/asleep status sensor is not used for this.
- **Without consumption reading** `run_tick(None)` still runs the schedule so windows close and stops happen; nothing is started.
- **Switch entries**: ON in the window if headroom ≥ `power_watts`, previous state restored at the end. A peak snapshot of a switch that was already on before the window is left for the peak restore.
- **Shared guards**: `EVGuard` keys guards by `device.id`. When the same EV has a different id in the peak cascade, `_sync_guards()` copies the switch state to that guard after every schedule action, otherwise `_apply_peak` would skip a needed `turn_off` as redundant.
- **Removing or disabling an entry mid-window** closes the window the same way as its end (stop, rest limit).
- **Is it charging?** A known charge state wins over the switch (a Tesla switch can stay `on` after unplugging).

### Configuration constants (`const.py`)

- `FLUVIUS_REGIO_TARIEVEN`: 2026 capacity tariffs in €/kW/year, keyed by Flemish region name
- `CAPACITY_MIN_KW = 2.5` — minimum billed monthly peak; see "2.5 kW capacity floor" below
- `QUARTER_HISTORY_DAYS = 32`, `MONTHLY_PEAK_HISTORY_MONTHS = 36`, `MAX_PLAUSIBLE_QUARTER_KW = 100`, `PEAK_VERIFY_*` — see "Quarter history and monthly peaks" below
- `DEFAULT_BUFFER_WATTS = 100` — threshold margin in watts
- `DEFAULT_UPDATE_INTERVAL = 5` — configured monitoring loop interval in seconds; the controller enforces a 60 s minimum, so the effective default is 60 s
- `DEFAULT_POWER_DETECTION_TOLERANCE_PERCENT = 10` — tolerance for "natural stop" detection
- `DEFAULT_SOLAR_NETTO_EUR_PER_KWH = 0.25` — assumed injection savings in €/kWh

### 2.5 kW capacity floor

Below `CAPACITY_MIN_KW` (2.5 kW) no extra capacity tariff is due, so there is no financial reason to limit consumption under it. The floor is applied in every place that uses the monthly peak:

- **Control**: `PeakDecider.check()` / `check_restore()` pass the P1 peak-sensor reading through `utils.effective_peak_w()` (`max(raw, 2500 W)`). The cascade starts at `effective_peak − buffer`. An unavailable sensor still skips the check — it is never silently replaced by 2500 W.
- **Savings**: `PeakAvoidTracker._avoided_kw()` computes `max(hypo, 2.5) − max(actual, 2.5)`, for both the month total and per-device attribution. Avoided peaks that stay entirely below 2.5 kW count as €0.
- **Billed peak**: `QuarterStore.get_billed_avg_kw()` floors each monthly peak at 2.5 kW *before* averaging (Fluvius applies the minimum per month, not on the 12-month average).
- **Display**: the decision log and the panel show the effective peak, plus the raw P1 value when it is lower. The panel gets the floor from `config.capacity_min_w` in `/api/peak_guard/cascade` rather than hard-coding it. "Huidig verbruik" is red at or above the effective peak and orange in the buffer zone below it (`peak − buffer < consumption < peak`), where the cascade is already shedding devices.

Deliberately *not* floored: `sensor.peak_guard_monthly_peak_kw` (exposes the floored value as attribute `effectieve_piek_kw`), the historical monthly peaks and the raw rolling 12-month average — these keep showing measured values.

### Power units (W vs kW)

Peak Guard computes in W. The consumption sensor and the monthly-peak sensor are read through `deciders/base.py::read_power_w()`, which multiplies by 1000 when the entity's `unit_of_measurement` is kW (case-insensitive) and treats any other or missing unit as W. The panel applies the same rule in `_powerW()`. This matters because the floor would otherwise mask a kW peak sensor (3.2 → 2500 W). Device power sensors and other entities still go through plain `read_sensor()`.

The cumulative energy sensor that feeds the quarter calculation is read through `read_energy_kwh()`: Wh is divided by 1000, MWh multiplied by 1000, anything else is read as kWh.

### Quarter history and monthly peaks

`QuarterStore` keeps two layers in one HA store (`peak_guard.quarters`):

- **Quarters** — `QUARTER_HISTORY_DAYS` (32) days of 15-minute values, enough to cover any full calendar month. Needed for the running month (hypothetical-peak calculation in the tracker).
- **Monthly peaks** — one record per local calendar month (`year`, `month`, `ts`, `kw`), raised whenever a higher quarter is added, kept for `MONTHLY_PEAK_HISTORY_MONTHS` (36). On load the records are also seeded from whatever quarters are still stored, which is how an older store without `monthly_peaks` upgrades.

Because a monthly record can only rise and stays for 36 months, measurement errors are kept out at three points:

- `QuarterCalculator` declares the running quarter invalid when the cumulative reading drops (sensor reset, or a register briefly missing from a summed template sensor). It reports 0 kW for the rest of that quarter and does not close it, so the jump back up is never counted as consumption. The same happens when the running value exceeds `MAX_PLAUSIBLE_QUARTER_KW` after the first minute of a quarter (a glitch that is the quarter's first reading produces no negative delta); within the first minute such a value is only shown as 0, because one step of a coarse sensor can cause it (0.2 kWh after 5 s reads as 144 kW).
- `QuarterStore.add_quarter()` and the load path reject quarters that are not finite, negative, or above `MAX_PLAUSIBLE_QUARTER_KW` (100 kW) — e.g. values stored 1000× too high by a Wh energy sensor before v1.8.16. If more than 10% of the stored quarters are implausible at load, the whole quarter history is discarded as wrong-unit data, because the values that happen to fall under the cap are just as wrong.
- `read_sensor()` returns `None` for `nan`/`inf` states.

Pruning counts back from the current local month; a future-dated record (wrong clock) is left alone and cannot push real history out.

**Automatic correction against the P1 meter.** Errors that pass those filters (wrong but plausible, e.g. 11 kW) are removed without user action. Every sensor update, `SharedCapacityState._verify_against_meter()` reads the configured peak sensor — the meter's own monthly peak, the billing reference — and calls `QuarterStore.async_remove_unconfirmed_peaks()`. No closed quarter of the running month can exceed the meter's monthly peak, so any own quarter above `meter_peak × PEAK_VERIFY_TOLERANCE (1.15) + PEAK_VERIFY_MARGIN_KW (0.25)` is deleted, the month's record is rebuilt from the remaining quarters, and a warning is logged with the removed values. The tolerance absorbs the difference between the meter's exact quarter average and Peak Guard's estimate from one-minute readings. Details:

- A quarter is only judged `PEAK_VERIFY_GRACE_MINUTES` (10) after it ended, so the meter has had time to report it.
- Only the running month is checked — the meter resets its peak at month change. Consequently the last quarter of each month (23:45–00:00) is never verified, and neither are months recorded before v1.8.17.
- Nothing happens when the peak sensor is unavailable, not configured, or one of Peak Guard's own sensors (circular reference).
- Only values that are too high are removed. A record that is too low (HA was down during the real peak, or the quarter was voided) is not raised to the meter's value.
- The check trusts the peak sensor: a sensor stuck on a stale low value would cause genuine new peaks to be deleted from Peak Guard's own history (steering is unaffected by that; it already depends on the same sensor).

Every query (`get_month_peak`, `get_monthly_peaks`, the 12-month average and the billed peak) reads the merge of both layers (`_peaks_by_month`). The billed peak and rolling average use the last 12 months; the history sensor exposes all stored months. With fewer than 12 months of history the average runs over the months available.

### Simulation mode

`/api/peak_guard/simulate` pins the consumption the controller steers on (`_simulation_consumption`, W). `/api/peak_guard/cascade` reports it under `simulation`; the panel shows that value instead of the real sensor, with a "Simulatie" note on the consumption card. The panel only refreshes it on its 15-second poll.

### Time: stored in UTC, calendar boundaries in local time

Fluvius bills the capacity tariff per calendar month in Belgian time and the P1 meter resets its monthly peak at local midnight. All timestamps are stored and compared in UTC, but everything that assigns a moment to a month or year goes through `utils.local_year_month()` (HA's configured time zone via `dt_util.as_local`): `QuarterStore` month peaks, the month/year rollover and persistence keys in `SharedCapacityState`, and the startup restore. Event timestamps are shown in local time, both in the sensor attribute tables (`_fmt_ts`) and in the panel's event log (browser-local). Still UTC on purpose: the EV daily call budget day (`ev_call_budget.py`) and the EV API log file date.

### Startup after a missed month change

If HA was down across a month boundary, the live rollover in `SharedCapacityState._async_update` never ran. `sensor.py::restore_peak_tracker()` then keeps the stored year total as the year base (the stale month state is discarded), and `MonthlyDeviceSavingsStore.async_finalize_before()` freezes the still-open per-device records of earlier months with their last persisted values. The live rollover calls it too, because after a mid-month restart the tracker only knows devices with a new event.

## Known limitations

Accepted as-is; listed so they are not rediscovered as bugs.

- **Energy sensor must be strictly increasing.** Any drop in the cumulative reading, however small, voids the running quarter (`QuarterCalculator`). A computed sensor that occasionally dips (e.g. a Riemann integral of net power, or a sum of registers with a `float(0)` fallback) loses each such quarter, with one log warning per occurrence. There is no epsilon.
- **Unrecognised energy units are read as kWh.** Only Wh and MWh are converted (`read_energy_kwh`). GWh, joule units, or a Wh sensor without a `unit_of_measurement` are taken as kWh with no warning; a unitless Wh sensor then has most quarters rejected by the 100 kW cap.
- **Unrecognised power units are read as W.** Only kW is converted (`read_power_w`).
- **A small meter drop exactly on a quarter boundary** (the first reading of a quarter is too low by less than ~25 kWh and recovers) yields a plausible phantom quarter. It is stored, and removed again by the P1-meter check once that quarter is 10 minutes old — except for the last quarter of a month.
- **Monthly-peak history starts at v1.8.16.** Older months are absent; until 12 months exist the billed peak averages over the months available. Downgrading to ≤ v1.8.15 drops the monthly records.
- **Meter replacement costs one quarter** (the drop voids it).
- **Simulation mode in the panel** refreshes on the 15-second poll, not live.
- **EV daily call budget and EV API log file date run on UTC days**, not local days.
- **`controller.py` and the frontend panel are not covered by the test suite.** Panel helpers have only been exercised in Node, not in a browser.
- **Time-zone handling is tested against a stub** of `homeassistant.util.dt`, not a live HA.
- **Schedule precision is the loop interval** (~60 s) for time windows; tariff-sensor changes wake the loop immediately.
- **Schedule fulfilment relies on the battery sensor and charge state**, which can be stale while the car sleeps.
- **A schedule stores a copy of the device.** The panel refreshes it when the device is edited in a cascade; edits made elsewhere (e.g. directly in the store) don't propagate.
- **The EV wizard can't clear an optional entity field** (empty input falls back to the old value) — pre-existing.

## REST API

| Endpoint | Methods | Purpose |
|---|---|---|
| `/api/peak_guard/cascade` | GET, POST | Fetch or update the full cascade configuration (`type`: `peak`, `inject` or `schedule`) |
| `/api/peak_guard/force_check` | POST | Trigger an immediate monitoring cycle |
| `/api/peak_guard/simulate` | GET, POST | Read or set simulation mode (`consumption_w` in W, `null` to switch it off) |

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
