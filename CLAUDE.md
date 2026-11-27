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

The test suite uses stub modules in `tests/conftest.py` to avoid a live HA install; the stubbed `homeassistant.util.dt.as_local` uses a fixed Europe/Brussels time zone. Twenty test files:
- `tests/test_ev_guard.py` — 60 tests covering the EV state machine, rate limiter and its reserve, daily-budget reserve, unreachable-vehicle back-off, turn_on refusals, debounce, and Tesla-specific paths
- `tests/test_tracker.py` — 32 tests covering the financial calculations in `PeakAvoidTracker` and `SolarShiftTracker`, incl. per-event and per-device hypothetical peaks, shift bookkeeping and measurement lifecycle
- `tests/test_peak_floor.py` — 24 tests covering the 2.5 kW capacity-tariff floor (decider, savings, billed peak, decision log)
- `tests/test_month_rollover.py` — 4 tests covering the month/year rollover order in `SharedCapacityState`
- `tests/test_power_units.py` — 10 tests covering kW → W conversion of the consumption and peak sensors
- `tests/test_local_month.py` — 16 tests covering month/year boundaries in local time (quarter store, rollover, event-table timestamps)
- `tests/test_startup_restore.py` — 15 tests covering the startup restore of the year total, the finalisation of months missed while HA was down, and the replacement of the pre-v1.9.2 legacy floor by rebuilt events
- `tests/test_monthly_peak_history.py` — 33 tests covering the 36-month monthly-peak records in `QuarterStore` (recording, pruning, persistence, upgrade seeding, history queries, rejection of implausible quarters and invalid records)
- `tests/test_quarter_calculator.py` — 10 tests covering `QuarterCalculator` when the meter reading drops and recovers or yields an impossible value, and non-finite sensor states
- `tests/test_energy_units.py` — 7 tests covering Wh/MWh → kWh conversion of the energy sensor
- `tests/test_peak_verification.py` — 16 tests covering the automatic removal of quarters and monthly records that contradict the P1 meter's monthly peak
- `tests/test_schedule.py` — 71 tests covering the charging schedule: window logic (midnight, DST, overlap, tariff sensor), serialisation, `ScheduleDecider`, and the peak > schedule > injection interplay through `deciders/dispatch.run_tick`, and (v1.9.13) a failed window-end stop of a car with a stale `stopped` that stays pending, and (v1.9.14) the same commands whether the peak copy of the EV has the same or a different id
- `tests/test_field_fixes.py` — 45 tests covering the fixes from the October 2026 field data (v1.9.3): quarter-boundary interpolation and the calculator restart state, the quarter budget (`quarter_budget.py`) and `PeakDecider` on it (spike vs. sustained overload, settle after own shedding, switch min-OFF, exhausted cascade), EV deferral bookkeeping, and raising the month record to the P1 meter, plus the DST change with local-time timer callbacks (real `zoneinfo`) and the 15-min bound on EV deferrals, and (v1.9.8) the slow-energy-sensor extrapolation (`PowerHistory`)
- `tests/test_climate.py` — 50 tests covering `ClimateDevice` (thermostat): serialisation, switching off in the peak cascade (an idle thermostat is left alone), the inject temperature in the solar cascade (surplus rule, room already warm, no inject temperature), restore of the mode (setpoint only when it is still `inject_temp`), rollback on a failed call, the same thermostat in both cascades through `run_tick` (peak takes over from solar, solar takes over from peak), and a Tuya thermostat (`heat_cool` mode, no `hvac_action`), plus (v1.9.9) the `peak_setback` path (lower when heating / raise when cooling, never twice, clamped to `min_temp`, restore only while the setpoint is still Peak Guard's, late ack) and its review fixes (unavailable at restore, no escalation on resend, user change during the peak, warm room reading without `hvac_action` still sets back, setback removed mid-peak, both cascades, default `min_on`), and (v1.9.11) the climate trigger log (start/end lines, end reasons, nothing for an unacknowledged apply or the solar cascade, a failing log never breaks the cascade, the JSONL writer)
- `tests/test_event_archive.py` — 15 tests covering the event archive (v1.9.12): appending completed events, seeding from the trackers without duplicates, more than the trackers' 100, live peak values for events that left the tracker log, freezing at the month change (also through `SharedCapacityState`), a missed month change closed late, pruning after 12 closed months, and the sensor attributes kept out of the recorder and capped
- `tests/test_status_holds.py` — 25 tests covering the status explanations (`holds`) and the control fixes found with them: inject switch start rule, inject restore past a held EV, peak restore without `break`, stepwise EV restore after a peak, min-OFF on peak restore (also with a stale charge state), giving up a capped restart that never charges, headroom reservation for a restarted EV, and the hand-over of a peak-limited EV to the solar cascade
- `tests/test_solar_stop.py` — 31 tests covering the solar stop of an EV (v1.9.13, field data 2026-11-27): a `stopped` charge state left over from before Peak Guard's own `turn_on` counts as charging for control (`charging_for_control`: solar throttle and restore incl. an `unknown` switch, inject restore, peak path, `charging_draw_w`) but not for start confirmations (`is_charging`, `peak_is_off`); a change after the start or a report ≥ 2 min after it is real; only a real `turn_on` counts (shared by all copies of the car, cleared by `turn_off`); `complete` never stale; and the stop at the minimum only after a confirming reading (spike, surplus between two spikes, sustained, failed stop without new wait, new start clears it also via another copy, skipped tick, reduction resets, nothing else restored meanwhile), the start stamped at the successful `turn_on`, and the peak path keeping its `turned_on_at` rule
- `tests/test_charger_state_baseline.py` — 2 tests (step 1a of `docs/charger_state_plan.md`): a characterization baseline of the EV charger-state judgments — 24 840 scenarios (switch × charge state/status sensor × time of the last report × what Peak Guard did: start, stop, `is_charging` refusal; plus cable, location, wake button, refusals, stale current sensor) through the read functions (`is_charging`, `charging_for_control`, `peak_is_off`, `charging_draw_w`, …) and the public paths (`restore`, `throttle_down_solar`, `apply_action` peak/solar), compared with `tests/fixtures/charger_state_baseline.json.gz`; the clock of `ev_guard` and `charger_state` is frozen at the test's `NOW`, a second test checks the answers survive a 20 s clock shift. Pins current behaviour, including doubtful parts; a deliberate behaviour change regenerates it with `PG_BASELINE_REGEN=1` and explains the difference in the commit. Not covered on purpose: rate-limit, retry and update-interval branches (command sending, step 2)
- `tests/test_charger_state.py` — 8 tests (step 1d, v1.9.14): one `EVDeviceGuard` per switch shared by copies with different ids, one `status_dict` entry per id, `find_guard` creates nothing, a guard made by `get_guard(id)` is adopted, a peak stop by one copy seen by the inject copy, and `release_schedule` keeping a peak stop (but still forgetting its own running charge)
- `tests/test_ack.py` — 74 tests covering v1.9.7 (incl. regressions for the Fable review findings, bugs 1, 2, 9, 10 of the first review, and a stale Tesla charge state) and the v1.9.8 fixes (solar-started EV after a peak, charger without charge state, restored pendings, repeated device errors): acknowledgement of switch/thermostat actions (immediate, laggy, no-ack timeout and resend, restore ack/resend/give-up, solar side), the `power_sensor` field, `max_off_s`/`min_on_s`, the stale-pending fix (bug 5) and the solar path on a stopped EV (bug 3)

The mock `hass.services` reflects `switch.turn_on/off` and `climate.set_hvac_mode/set_temperature` into the mock state (like HA does); `hass.services.no_reflect.add(entity_id)` simulates a cloud device that hasn't executed the command yet. An autouse fixture sets `const.ACK_WAIT_S` to 0.

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

4. **Trackers** (`avoided_peak_tracker.py`, and the solar equivalent): Record avoidance/shift events through a 3-phase lifecycle (pending → active → completed), compute kW impact on the quarterly history, and calculate EUR savings using Fluvius 2026 tariffs from `const.py`. See "Peak savings calculation" below.

5. **Sensor updates** (`sensor.py`): 16+ read-only sensors are updated each monitoring cycle and also every minute at second 0 (`async_track_time_change`). `SharedCapacityState` derives the quarterly average from the cumulative kWh sensor via `QuarterCalculator` on that timer. The meter reading at a quarter boundary is interpolated between the last reading before and the first after it (gap ≤ 180 s); a quarter whose start was not measured (first reading after a restart without usable saved state, or a longer gap) is incomplete and not stored. The calculator state is persisted (`peak_guard.quarter_calc`, delayed save) so a restart within a quarter measures on exactly. `async_track_time_change` calls back with *local* time: `_async_update` and `QuarterCalculator` normalise to UTC first, otherwise the quarter rollover stalls at the autumn DST change (same-tzinfo datetimes compare ignoring `fold`). The sensors expose quarter kW, monthly peak, capacity costs, savings, shifted kWh, etc.

6. **Frontend panel** (`frontend/peak_guard_panel.js`): A ~3500-line custom Web Component (no framework) that polls `/api/peak_guard/cascade` every 15 seconds, shows real-time status (countdown, last loop timestamp), and lets users drag-drop reorder devices and configure EV charger setups.

7. **Persistence** (HA `Store` API): Nine stores survive restarts (plus the append-only thermostat trigger log `peak_guard/climate_events.jsonl` and the event archive `peak_guard/events/`) — cascade config (incl. the charging schedule and its runtime state), quarter history with monthly peaks, the running-quarter calculator state, peak and solar year savings, peak and solar month state (events), per-device monthly savings, and the EV daily call budget. Keys are in `const.py` (`STORAGE_KEY_*`) and `sensor.py` (`_STORAGE_KEY_*_STATE`).

### Key classes

| Class | File | Role |
|---|---|---|
| `PeakGuardController` | `controller.py` | Core orchestrator; owns cascades, monitoring loop, EV state machines |
| `_BaseCascadeDevice` | `models.py` | Abstract base for all cascade entries; exposes `apply()` / `restore()` |
| `SwitchOffDevice` | `models.py` | Simple switch-off device (peak cascade) |
| `SwitchOnDevice` | `models.py` | Simple switch-on device (inject cascade) |
| `ThrottleDevice` | `models.py` | Throttleable device with min/max/power_per_unit |
| `ClimateDevice` | `models.py` | Thermostat (`climate.*`, action type `climate`): off in the peak cascade, `inject_temp` in the solar cascade, restores mode + temperature (`DeviceSnapshot.original_temp`) |
| `EVChargerDevice` | `models.py` | EV charger — all EV fields directly on the class (switch_entity, current_entity, phases, soc_entity, charge_state_sensor, …) |
| `CascadeContext` | `models.py` | Dependency-injection bag threaded through cascade loop (hass, trackers, ev_guard, callbacks) |
| `from_dict()` | `models.py` | Factory that deserialises a dict into the correct subclass; migrates old `ev_*`-prefixed formats automatically |
| `PeakAvoidTracker` | `avoided_peak_tracker.py` | Tracks peak avoidance events and computes kW/EUR impact |
| `QuarterCalculator` | `quarter_calculator.py` | Derives quarterly average power from cumulative kWh sensor |
| `QuarterStore` | `quarter_store.py` | Persists the rolling 32-day quarter history and one monthly-peak record per month for 36 months |
| `EVRateLimiter` | `models.py` | Sliding-window rate limiter (max 12 calls / 10 min, one instance shared globally across all EV devices on the `EVGuard`) |
| `EVDailyCallBudget` | `ev_call_budget.py` | Persistent (HA `Store`) daily cap on real EV-API calls — long-horizon backstop above `EVRateLimiter`; survives restarts |
| `EVDeviceGuard` | `models.py` | Per-charger state machine (idle → waiting_for_stable → charging → sleeping), one per switch entity (`EVGuard.guard_for`); `scheduled` flag while the schedule drives it |
| `ChargerView` / `ChargerMemory` | `deciders/charger_state.py` | What the charger is doing: read-only judgments of one car / what Peak Guard itself did per switch. See "Charger state" |
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
- **Rate limiter (short window)**: max 12 service calls per 10 minutes, one `EVRateLimiter` instance shared globally across every EV device on the `EVGuard` (not per-device). `_svc()` itself records every real attempt — including failed retries — so a command that exhausts all `EV_CMD_MAX_RETRIES` still counts fully against the window, while a call `_svc()` blocks (`EVCallBlocked`) counts nowhere; retry loops re-check `_rate_check()` before each attempt after the first so a filled window aborts the retry instead of continuing to call the underlying (e.g. Tesla) API unthrottled
- **Daily call budget (long window)**: `EVDailyCallBudget` (`ev_call_budget.py`) caps real EV-API calls per calendar day (`EV_API_DAILY_BUDGET` in `const.py`, default `10_000 // 30` = 333). Deliberately a *daily* cap computed from the monthly account quota rather than a cumulative monthly counter: a bad day exhausts only that day's budget and resets fully the next day, instead of a monthly budget where a problem early in the month keeps degrading every day after it. Does not reserve headroom for the base Tesla integration's own independent background polling, which shares the same account quota — tune `EV_API_DAILY_BUDGET` against the real account quota and observed background load. Persisted via HA `Store` (`STORAGE_KEY_EV_CALL_BUDGET`) so it survives restarts, unlike `EVRateLimiter`. Enforced in two places: `_rate_check()` consults it as a cheap early pre-check (skips before a retry loop even starts), and `_svc()` itself hard-enforces it unconditionally on every real service call — including call sites that never go through `_rate_check()` at all (e.g. the wake-button `button.press`) — by raising `EVCallBlocked` (a `HomeAssistantError`) before the underlying `hass.services.async_call` fires. Injected via `EVGuard.set_daily_budget()` from `__init__.py::async_setup_entry`; `None` (no cap) in contexts that don't wire it up, e.g. tests
- **Rate-limit reserve**: the last `EV_RATE_LIMIT_RESERVE` (2) of the 12 slots in the short window are kept for draw-reducing calls (`_rate_check(..., reducing=True)` at the peak, throttle, stop and cable-disconnect call sites; `_svc()` enforces it for everything else), so faster schedule raises can never crowd out a peak reduction. A peak `set_value` that is skipped or fails returns the full excess, so the peak cascade moves on to the next device.
- **Budget reserve**: the last `EV_API_BUDGET_RESERVE` (35, at most a quarter of the budget) calls of the day are kept for commands that reduce the EV draw — `switch.turn_off` and a `number.set_value` lower than the entity's current state (`_reduces_draw`). Starting, raising, SoC overrides and polls are refused inside the reserve, so retries or tracking can't use up the budget the peak cascade needs. One warning per day.
- **Unreachable vehicle**: an error containing one of `EV_UNREACHABLE_ERRORS` (not online, asleep, offline, `software_update_in_progress`, …) marks the vehicle (keyed by `device.name` from `_log_ctx`) unreachable for `EV_UNREACHABLE_BACKOFF_S` (15 min). Meanwhile `_svc()` refuses everything except reducing commands and the wake button, retry loops stop after the first such error (the peak `turn_off` tries once per loop), and any successful call except the wake button clears it. Exposed as `ev_unreachable` in the status. The schedule additionally waits `SCHEDULE_CURRENT_RETRY_S` (5 min) after any failed current change.
- **Start-threshold gate**: surplus must reach `start_threshold_w` before debounce even begins; drops below → debounce is reset
- **Debounce / `_surplus_floor`**: 20 s wallclock timer (`EV_DEBOUNCE_STABLE_S`); the 10th-percentile floor of the surplus history must be positive before the EV is started; state is `WAITING_FOR_STABLE` while waiting
- **1 A hysteresis** (`EV_HYSTERESIS_AMPS`): prevents thrashing on small surplus changes
- **Minimum update interval** (`EV_MIN_UPDATE_INTERVAL_S`): rate-limits `set_value` calls even within a single rate-limit window
- **Min-OFF cooldown** (`EV_MIN_OFF_DURATION_S`): prevents restart too soon after PG turned the EV off, to spare the switch. There is deliberately no min-ON time and no minimum interval for current changes beyond `EV_MIN_UPDATE_INTERVAL_S`: the current should track surplus and peak headroom as closely as possible
- **Wake-up support**: detects sleeping EV (via `status_sensor`), calls `wake_button`, waits up to `EV_WAKE_TIMEOUT_S`, then backs off for `EV_WAKE_COOLDOWN_S` on failure
- **Location guard**: skips all action when `location_tracker` is present and EV is not home
- **Manual-start detection**: if `switch_entity` reports `unknown`/`unavailable` but `status_sensor` confirms charging, the EV is treated as already on. This "handmatige start" path sets `guard.state = CHARGING` **and** calls `solar_tracker.start_solar_measurement` so the session is tracked even though Peak Guard didn't initiate it. Relevant for Tesla, whose switch entity is permanently `unknown`.

### Charger state (`deciders/charger_state.py`, v1.9.14)

Every question "what is the charger doing?" has one answer in one place (`docs/charger_state_plan.md`, step 1). `EVGuard.view(device)` returns a `ChargerView` (read-only, cheap, one per question) over the HA states, the car's `EVDeviceGuard` and `ChargerMemory` (per switch: last real start `charge_started`, `is_charging` refusals `charging_confirmed`, the tick clock; in memory only). `EVGuard`'s old read methods (`is_charging`, `peak_is_off`, `charging_for_control`, `charging_draw_w`, `cable_connected`, `is_home`, …) delegate to it. `datetime.now()` is read in both modules; a test that freezes the clock patches both.

| Question | `ChargerView` | Rule |
|---|---|---|
| Is it really charging? (start confirmations) | `observed_charging()` (`is_charging`) | known charge state `charging`/`starting` wins; else switch `on`; an `is_charging` refusal counts for `EV_CHARGING_CONFIRM_S` (10 min) while the charge state is unchanged. `complete`/`connected` are **not** charging |
| Charging, to steer a running charge | `charging_for_control(now)` | the above or `stale_stopped_after_start` (`stopped` from before the own start, < `EV_STALE_STOPPED_S` 20 min, report grace `EV_STALE_REPORT_GRACE_S` 2 min) |
| Stopped for the peak restore | `stopped_by_pg()` (`peak_is_off`) | `stopped_by_pg_not_restarted()`, else not `observed_charging` |
| Just started | `just_started(now)` | `turned_on_at` < `SCHEDULE_START_CONFIRM_S` (3 min): solar restore, solar throttle, schedule start confirm |
| Just stopped by Peak Guard | `just_stopped_by_pg(now)` | own `turn_off` < `PEAK_RESTART_CONFIRM_S` (3 min) |
| Older peak rule | `pg_start_with_stale_stopped(now)` | `stopped`, switch on per Peak Guard, `turned_on_at` < 10 min |
| On, for `apply_action` | `switch_on_for_apply(sw_state, now)` | switch `on`; with `unknown`/`unavailable`: status sensor `on`/`charging`/`connected`/`complete` (**complete counts as on here**), else `charging_for_control` |
| Solar throttle | `charging_for_throttle(now)` | `charging_for_control`; without charge state and an unknown switch: guard `CHARGING` + `last_switch_state` |
| Solar restore: already stopped? | `already_stopped_for_solar_restore(sw_state, now)` | switch `off`, or unknown switch + not `just_started` + known charge state not charging |
| Peak path | `peak_already_stopped(now)`, `peak_not_charging(now)` | stopped by PG and (just stopped or not observed charging); known charge state not charging unless `charging_for_control` or the older peak rule |
| Solar start refused with `is_charging` | `refusal_was_manual_start()` | the charge state at the refusal was not `complete` |
| Current / power | `current_a()`, `effective_current_a(...)`, `draw_w(now)` | sensor; `last_sent_amps` when the sensor is older than `EV_SENSOR_STALE_S`; draw 0 unless `charging_for_control` |
| Cable / awake / home | `cable_connected()`, `awake()`, `home(guard)` | as before (`is_connected`, `is_home`) |

The windows and the two meanings of "on" (`complete`) differ on purpose and were kept as they were; merging them is a behaviour change for later, with field data. `tests/test_charger_state_baseline.py` pins all of it.

### Thermostats (`ClimateDevice`)

Action type `climate`, entity `climate.*`, `power_watts` = draw while heating. Same class in both cascades, behaviour by `ctx.cascade_type`:
- **Peak** (without `peak_setback`): `climate.set_hvac_mode` → `off`; snapshot keeps the hvac mode (`original_state`) and the target temperature (`original_temp`). Heating = `hvac_action == heating`, or without `hvac_action` (e.g. Tuya) `current_temperature < temperature`. When it is not heating it is left alone (no call, no snapshot): switching it off would interrupt the thermostat's own firmware schedule (a Tuya switch-only thermostat is then really off). If it starts heating during the peak, the next cycle sheds it. Restore needs `power_watts` headroom and the switch min-OFF, like a switch.
- **Solar**: `climate.set_temperature` to `inject_temp` (plus the heat mode when it was in another mode; `heat_mode` empty = the thermostat's own mode from `hvac_modes`: heat, heat_cool, auto, else the first non-off mode) only when the surplus covers `power_watts`, the room (`current_temperature`) is below `inject_temp` and it is not already heating at ≥ `inject_temp`. No `inject_temp` → no solar action. Solar savings count `power_watts` × duration even when the thermostat cycles off at temperature (over-estimate).
- Restore (both): `set_temperature` with the original temperature (+ mode when it differs) only when the setpoint is still `inject_temp` (what Peak Guard set) — otherwise only `set_hvac_mode` back to the original mode, so a setpoint changed by the thermostat's own schedule or the user stays, and a Tuya thermostat isn't pushed from program into manual mode by a temperature write. `set_hvac_mode` when the original mode was `off`.
- **Peak setback** (v1.9.9, `peak_setback` °C, e.g. the Daikin airco's): in the peak cascade the setpoint is shifted instead of switching off — lower in `heat`, higher in `cool`, in `heat_cool`/`auto` by `hvac_action` or room vs. target, nothing in `fan_only`/`dry` or an unknown direction. Rounded to `target_temp_step`, clamped to `min_temp`/`max_temp`. Skipped when `hvac_action` says idle/off/fan/drying; without `hvac_action` (Daikin Onecta) the unit counts as running — deliberately no room-vs-target check: the Daikin's `current_temperature` comes from the indoor unit and reads 3–4 °C above the target while heating (field data 2026-11-16, v1.9.10). The shift always starts from `snapshot.original_temp` (a resend after `noack` sends the same value, never a further step), and for a device the solar cascade had raised, from the inject snapshot's pre-surplus `original_temp` (`CascadeContext.inject_snapshots`, filled for the peak cascade). The applied setpoint is `DeviceSnapshot.set_temp`; tolerance `_SETBACK_TOL` (0.5 °C, devices round) for the ack, the never-twice guard and `_setpoint_still_ours`. After the ack, a setpoint changed by the user stays (no new shift, restore sends nothing). Restore sends `set_temperature(original_temp)` only when the unit is on and still at `set_temp`; unavailable or no setpoint → `False`, the snapshot is kept and retried. Restore ack = setpoint back at `original_temp` or no longer near `set_temp`. The mode is never touched. A setback device without `min_on_s` gets `PEAK_SWITCH_MIN_OFF_S` (5 min) as min-ON, against flapping on a marginal peak. Removing `peak_setback` mid-peak clears `set_temp` on the next off-shed. `power_watts` (or `power_sensor`) is booked as shed, an estimate: a setback lowers the draw rather than stopping it (restore also waits for that full headroom).
- **Daikin Onecta** (owner's 8 split units, `climate.*` via the `daikin_onecta` custom integration — cloud only, the BRP069C adapters have no local API): Daikin's API allows 200 calls/day per account; the integration's own polling (10 min day / 30 min night) uses ~110, every Peak Guard setback or restore is one call. Its `set_temperature` updates the HA state optimistically, so the ack is immediate.
- **Trigger log** (v1.9.11, `climate_event_log.py`): every peak-cascade intervention on a thermostat (setback or off) is appended to `<config>/peak_guard/climate_events.jsonl` — outside `custom_components` so an update doesn't remove it, kept for an end-of-winter analysis. A `start` line on the apply ack (`ClimateDevice.on_apply_ack`: `start` = `off_since`, `room_start` = `current_temperature`, also stored as `DeviceSnapshot.room_at_start`) and an `end` line on the restore ack (`end_reason` `restored`) or the not-sent restore (`user_changed`, `unit_off`, `already_restored`), with `duration_min`, setpoints, `room_end`, `power_w`; times UTC. Pair on (`entity`, `start`). A start without an end: aborted (manual override, ack give-up) or still running. Wired by `controller` via `PeakDecider.set_event_log_fn`; `CascadeContext.event_log` is only set for the peak cascade; writing goes to the executor and an error is only debug-logged. The Daikin `room_*` values come from the indoor unit (reads high while heating); the recorder keeps the `sensor.*_ruimte_temperatuur` and `*_buiten_temperatuur` sensors for 365 days for a cross-check.
- Not schedulable (the Planning tab only lists switches and EVs).
- **Same device in both cascades** (any non-EV type): one owner at a time. `PeakDecider.take_over_from_solar()` (in `run_tick` right after `peak.check`) moves an inject snapshot into the peak snapshot when the peak sheds a device the solar cascade had turned on — otherwise the inject restore (consumption ≥ 0) turns it back on in the same tick — and keeps the pre-surplus original. `hand_over_to_solar()` now also covers non-EV devices: a peak-shed device that the solar cascade turns on loses its peak snapshot and the inject snapshot gets the pre-peak original, so the end of the surplus returns to the user's setting instead of "off".
- The inject restore keeps the `solar_share` rule (owner's choice, 2026-10-24): an EV held at minimum is skipped and the next device — also a thermostat with a higher priority — is restored first.
- A setpoint change while Peak Guard holds a snapshot survives the restore unless the setpoint is back at `inject_temp`.

### Charging schedule (Planning tab)

`schedule.py` holds the pure window logic and parsing; `deciders/schedule_decider.py` the control. Entries are stored in the cascade store under `schedule`, runtime state (active window, fulfilled, pending stop/rest limit, tariff-sensor memory) under `schedule_state`. REST: POST `/api/peak_guard/cascade` with `type: "schedule"`, `entries: [...]`; GET returns `schedule` and `status.schedule`.

- **Windows**: `kind="time"` (start days, `start`/`end` in local wall-clock time; `end <= start` runs into the next day, `start == end` is 24 h) or `kind="sensor"` (active while the sensor equals `active_state`, numeric states normalised so `2.0 == 2`). A tariff sensor that goes unavailable keeps its last state for `SCHEDULE_SENSOR_STALE_S` (15 min), and for the first 15 min after a (re)start regardless of age (the last state is persisted at least every 5 min), so a restart doesn't end and restart a window. Active = any window active; overlapping windows take the highest `target_soc`. Edges are detected on that merged state.
- **Ownership per tick**: peak (peak snapshot) > schedule (window active, EV target not reached) > solar (inject snapshot). `InjectionDecider` skips schedule-owned devices (`is_controlled`); `PeakDecider.check_restore` skips scheduled EVs (`skip_peak_restore`) because restoring an EV (`power_watts=0`, Tesla switch possibly `unknown`) would flap — the schedule restores it itself when there is headroom for the minimum current. `PeakDecider.check()` is never skipped.
- **EV in window**: takes over an inject snapshot; sets the charge limit to the window target (only when it differs); starts at `floor((effective_peak − buffer − house load) / V)` capped at `max_current`; only raises the current later (straight to the whole-amp value the headroom allows, never below the hardware minimum, with the same `EV_MIN_UPDATE_INTERVAL_S` (20 s) as the solar and peak paths since the last change by any copy of the device — so in practice every loop); after a stop by Peak Guard (any copy, e.g. the peak cascade) it waits `EV_MIN_OFF_DURATION_S` (5 min) before starting again, to spare the switch, decreases are left to the peak cascade. The reason for the current setting is exposed as `detail` in `status.schedule` and shown on the Planning tab and in the device info popup, which replaces the solar checklist while the schedule owns the EV. A `turn_on` that doesn't lead to charging is retried after 3 min, and after 3 attempts backs off 30 min. Fulfilled (sticky per window) when the battery ≥ target, or the charge state is `complete` at the target limit → released; solar may then charge on to `max_soc`.
- **Window end**: drops any peak snapshot; hands a running charge to solar when there would be surplus without the EV (and the EV is in the inject cascade), otherwise stops it; then sets the rest limit (`rest_soc`, deferred while the car is asleep). `EVGuard._set_soc_override(False)` uses `soc_rest_for()` (window target in a window, else `rest_soc`) instead of the snapshot value.
- **Outside windows** (`block_unplanned`): a charge Peak Guard didn't start and that isn't solar-owned is stopped after `SCHEDULE_UNPLANNED_GRACE_S` (2 min), unless there would be surplus without the EV (→ handed to solar) or `manual_override` is set on any copy of the device. The block wins over `rest_soc`.
- **Is it charging?** `EVGuard.is_charging()`: switch `on` (and charge state not stopped/complete), or charge state `charging`/`starting`. The charge state comes from the EV field `charge_state_sensor` (e.g. Tesla `sensor.*_opladen`), falling back to textual values of `status_sensor`; a binary online/asleep status sensor is not used for this.
- **Without consumption reading** `run_tick(None)` still runs the schedule so windows close and stops happen; nothing is started.
- **Switch entries**: ON in the window if headroom ≥ `power_watts`, previous state restored at the end. A peak snapshot of a switch that was already on before the window is left for the peak restore.
- **Shared guards** (v1.9.14): `EVGuard.guard_for(device)` keys the `EVDeviceGuard` by the switch entity, so the schedule, peak and inject copies of an EV share it even with different ids (`_guards` keeps an alias per `device.id` for the status). Before, `_sync_guards()` copied the schedule guard to the other copies and thereby erased a peak stop of a copy with another id. Everything on the guard is shared, as it already was for copies with the same id: `skip_reason`, `scheduled`, and `release_schedule()` (target reached, entry removed) resetting `state`/`last_switch_state` for all copies — except a stop by Peak Guard that still holds (`last_switch_state` False + `turned_off_by_pg`, e.g. the peak cascade stopped the car), which stays so `peak_is_off` doesn't take a lagging Tesla `charging` for a running charge. The control paths use `guard_for`/`find_guard`; `get_guard(device_id)` is kept for tests (a guard made that way is adopted by the first `guard_for` of that id). Guards of removed devices are not pruned (as before).
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

### Peak savings calculation

`PeakAvoidTracker` keeps, per device, the net shifted power per quarter (`extra_by_device`): `+` in the quarters from `avoid_ts` (where the energy would have fallen without Peak Guard), `−` in the quarters from `turnon_ts` (where it actually fell, already inside the measured quarter). A shift within one quarter therefore avoids nothing. `_recalc()` (on every `set_context`, i.e. every sensor update) derives from it:

- **Month**: hypothetical peak = max over quarters of `actual + Σ shifts` (never below 0); savings as in the floor section. A quarter wiped by the P1 check or a later higher actual peak flows through automatically.
- **Per device** (`get_device_monthly_savings`): actual + only that device's shift. Not additive across devices.
- **Per event** (savings-tab rows, "Kwartier zonder PG"): hypothetical peak = max over the event's `avoid_quarters` of `actual + Σ shifts` — one quarter, not the month peak; avoided kW and € against the current month peak. Recomputed until month end; rows overlap and don't sum to the month total.
- **EV stops** are recorded as a deferral (`record_pending_avoid(deferred=True)`): the car would have kept charging, so `nominal × off time` is booked `+` in the quarters from `avoid_ts` to the actual restart (at most 15 min; `complete_expired` ends a deferral that sees no restart after 15 min — unplugged, full, restarted by something else), and the catch-up at the end of the session (hours later) is not booked as `−`. The event is completed by `start_measurement_on_turnon` at the real `turn_on` (peak restore, schedule start after min-OFF, hand-over to solar) or by `complete_deferral` at a window end; there is no natural-stop detection for EVs (a drop of the house load is not the EV).
- **Legacy month state** (pre-v1.9.2, `hypothetical_peaks_this_month`): that floor came from an algorithm that counted an in-quarter shift twice. On restore its events are rebuilt with the current +/− rule (`rebuild_legacy_events`) and the floor is dropped (also the per-device floor); if older events already fell out of the 100-event log the result is a lower bound.

`extra_by_device`, `device_names` and each event's `avoid_quarters` are persisted in the peak month state. `hypothetical_peaks_this_month` is only a legacy floor read from a pre-v1.9.2 month state. The open quarter enters the context as energy-so-far ÷ 15 min (a lower bound that grows to the final value), not the extrapolated running average. Measurement lifecycle: a re-shed while a measurement is active completes it first and a re-shed while pending keeps the original `avoid_ts`; a measurement whose natural stop is never detected is completed after 15 min (`complete_expired`). An EV restarted at a lower current after a peak shifts only what it draws (`start_measurement_on_turnon(max_kw=...)`). Throttle and EV current reductions are not tracked as avoided peaks.

### Robustness fixes (v1.9.7)

- **Solar-started EV and the peak** (v1.9.8): a peak snapshot of an EV whose inject snapshot says it was off before the surplus is flagged `solar_started` (`PeakDecider._mark_solar_started`, in `take_over_from_solar`; only when the solar cascade can act on it — `set_solar_owner_fn`, i.e. not in manual override). Such a car is never restarted or raised by the peak restore: stopped by the peak → peak snapshot dropped at once (pending discarded), the inject snapshot stays so the solar restore resets the SoC limit and the solar cascade restarts it on new surplus; only reduced → `check_restore` skips it (hold `peak_solar_handover`, no raise/lower ping-pong with the solar throttle) and drops the peak snapshot once the inject snapshot is gone (`_release_solar_started`); `hand_over_to_solar` drops the flagged peak snapshot without copying its `on` into the inject snapshot. `PeakDecider` gets the inject snapshots via `set_inject_snapshots` in `run_tick`. `throttle_down_solar` returns False for an EV that Peak Guard stopped and didn't restart (no `set_value` to a stopped car).
- **Charger without charge-state sensor** (v1.9.8): `_apply_peak` resets `last_switch_state` when PG once stopped it but the real switch reports `on` again (snapshot gone without a restore), so it can be shed again.
- **EV peak path counts only real draw**: `_apply_peak` skips an EV that Peak Guard stopped and didn't restart (`last_switch_state is False`, unless the charge state says charging) and an EV whose known charge state is not charging/starting (full, stopped, unplugged). Before, the status fallback in `apply_action` treated it as on and the peak cascade counted its current as shed, so devices behind it stayed on.
- **Per-device isolation**: `_run_cascade` catches any exception from one device's `apply` (logged, warning in the panel, next device), `_restore_device` any exception from `restore`, and the inject restore one from `throttle_down_solar`. `EVGuard.report_device_error` logs the stack trace and adds the panel warning at most once per `DEVICE_ERROR_REPORT_S` (15 min) per device and action; repeats go to debug with the exception type and message (v1.9.8). `ThrottleDevice.apply` catches `HomeAssistantError`.
- **Open peak measurements persisted**: `PeakAvoidTracker.measurements_to_dict()/measurements_from_dict()` store pending and active measurements in the peak month state (`measurements`), restored by `restore_peak_tracker`, so a device shed across a restart is still booked. A restored pending whose device has no peak snapshot was already completed before the restart (the month state is saved on the minute) and is dropped (`prune_restored_pendings`, via `controller.prune_restored_measurements()` right after `restore_peak_tracker` in `sensor.py`, before the first sensor update can book it) (v1.9.8).
- **Peak restore of an EV that is still charging** (only reduced) sends no `turn_on`; a `turn_on` refused with `is_charging` (Tesla) does not fail the restore. Every EV API log entry carries `charge_state` (the charge state before the call), so the logs show whether a `turn_on` was needed.
- **Tesla `turn_on` refusals** (`_turn_on_with_retry`): `is_charging` counts as started and returns `(True, err)` (no retries; field log Nov 2026: the schedule's 3-min start confirm saw a lagging charge state and spent 3 calls per start on it); `complete` and `disconnected` (`EV_TURN_ON_FINAL_REFUSALS`) are not retried. The refusal is remembered per switch entity (`confirm_charging`, shared by all copies of the EV): `is_charging()` is True for `EV_CHARGING_CONFIRM_S` (10 min) while the HA charge state still shows the value from that moment; any successful `switch.turn_off` of that entity (`_svc`) clears it. `schedule_start` then changes no current and counts no start attempt; the solar start makes no new measurement, and unless the stale charge state was `complete` (only then can Peak Guard's SoC override have resumed it) it is taken as a manual/app start: the new inject snapshot gets `original_state="on"` and the current is left alone.
- **Stale `stopped` after the own start** (v1.9.13): the Tesla integration polls only every ~10 min. On 2026-11-27 each poll fell in a Peak Guard stop, the `stopped` stayed on screen during the next start, and the solar restore stopped the car at 8 A (1.85 kW injection) every 5–9 min. `EVGuard.note_charge_start()` records each successful real `switch.turn_on` per switch entity (`ChargerMemory.charge_started`, shared by every copy of the car, stamped at the moment the `turn_on` succeeded — the tick's `now` plus the time elapsed since `begin_tick(now)` (wake-up, retries, earlier devices); solar start, schedule start, peak restore — not a refused `is_charging` or a mere `turned_on_at` refresh; a successful `turn_off` clears it, a new start also clears the solar-stop confirmation; in memory only, not persisted). `stale_stopped_after_start()`: less than `EV_STALE_STOPPED_S` (20 min) after that start, a `stopped` whose `last_changed` is before the start and whose `last_reported` (the integration's last poll, also with an unchanged value) is less than `EV_STALE_REPORT_GRACE_S` (2 min) after it is a leftover. `charging_for_control()` = `is_charging()` or that rule; it is used to steer a running charge — the solar throttle and restore, `_apply_peak`, the unknown-switch `sw_on` fallback, `charging_draw_w`, the schedule's window end and the outside-window check (`_ev_outside`, so a failed stop stays `stop_pending`). The in-window branch keeps `is_charging()` on purpose (start confirm and back-off). `is_charging()` and `peak_is_off()` stay observation-only, so the peak capped-restart give-up and the schedule's start retry/back-off still see a car that didn't start (the schedule may therefore send one `turn_on` that the car refuses with `is_charging`). `_apply_peak` additionally keeps the older rule — `last_switch_state` True, `turned_on_at` < `EV_CHARGING_CONFIRM_S` (10 min), charge state `stopped` → reduce — which also covers a manual start and a start from before an HA restart: rather one call too many than a peak. Accepted: a `turn_on` that the API accepts but that doesn't start the car is counted as a running charge by the peak path until the next poll (as before v1.9.13 for 10 min). Known gap: a real stop in the first 2 min after the start that the integration reports without a value change (it was `stopped` before) is only believed at the next poll.
- **Solar stop at the minimum is confirmed** (v1.9.13): `throttle_down_solar` returns False (stop) only when "the sun contributes nothing" (consumption ≥ EV draw) still holds at a reading ≥ `EV_SOLAR_STOP_CONFIRM_S` (45 s) after the first (`EVGuard._solar_stop` per switch entity; the readings must be consecutive: cleared by a tick with surplus in `InjectionDecider.check_restore`, by `solar_share`, a reduction or a new start, and by a successful `switch.turn_off`, and restarted after a gap > `EV_SOLAR_STOP_MAX_GAP_S` (150 s: a skipped tick, but a slow tick of 60 s + wake-up still confirms); kept after a failed stop so it is retried without a new wait). Meanwhile hold `solar_stop_confirm` and the inject restore does nothing else that cycle (the import may be a short spike). One 5-s spike (kettle) no longer stops the charge.
- **Stale `charging` after the own stop**: the peak restore sends `turn_on` when `peak_is_off` (not on the charge state), and `_apply_peak` skips an EV that Peak Guard stopped less than `PEAK_RESTART_CONFIRM_S` ago whatever its charge state. Conversely an EV Peak Guard started is still reduced while the charge state shows a stale `stopped` (`charging_for_control`, see the v1.9.13 bullet; the schedule leaves decreases to the peak cascade); `complete`/`disconnected` still mean not charging. With an `unknown` switch the `sw_on` fallback also uses `is_charging()` (charge-state sensor), not only `status_sensor`.
- **Options apply live**: an update listener (`_async_options_updated`) updates the controller's shared config dict when the options change (debug decision logging), without reloading.

### Acknowledgement before counting (v1.9.7)

Switches (`switch_off`, `switch_on`) and thermostats only count once the entity reports the new state. Not for EV chargers (they have their own confirmation: `peak_is_off`, `PEAK_RESTART_CONFIRM_S`, the schedule start confirm) nor the legacy throttle.

- After the command `wait_for_state()` polls up to `ACK_WAIT_S` (2 s). Confirmed → book as before. Not confirmed → `DeviceSnapshot.pending = "apply"` (shed/turn-on) or `"restore"`, with `cmd_at`/`pending_since` (ISO strings, persisted with the snapshot).
- `BaseDecider._poll_acks()` runs once per tick at the start of `check()`/`check_restore()` of both deciders. A state change of an entity with a pending snapshot wakes the loop (`controller._setup_ev_listeners`, `ack_entities`).
- **Pending apply**: the device is not commanded again; for `ACK_TIMEOUT_S` (90 s) its power stays reserved (`apply` returns `excess − shed_w`), so a slow cloud device doesn't make the cascade shed everything behind it. Nothing is booked. On ack: `on_apply_ack` — peak: `record_pending_avoid(ts=cmd_at, fresh=True)` (the load stopped at the command) only when the original state was on, `off_since` = ack time, `PEAK_SETTLE_S` restarts; solar: `start_solar_measurement`. After 90 s → `"noack"`: no longer reserved (hold `no_ack`), resent after `ACK_RESEND_S` (5 min); a late ack is still processed until `ACK_GIVE_UP_S` (30 min from the first command, `pending_since`) — after that the noack snapshot is dropped without booking, so a later unrelated off (wash finished, manual) is not taken as the ack. A pending `"apply"` that old (persisted across a restart) and found applied counts as off (`off_since`) but books nothing (`on_apply_ack(book=False)`); not applied → dropped. A noack snapshot whose device is still on is dropped silently by the next restore with headroom. Restore candidates skip `"apply"`.
- **Pending restore**: `restore()` returns `True` only when confirmed. Otherwise the snapshot stays (`"restore"`), its need stays reserved in the peak restore headroom (hold `awaiting_ack`), resent every `ACK_RESEND_S`, given up after `ACK_GIVE_UP_S` (30 min: snapshot dropped, pending discarded / solar measurement completed, warning) — but only after at least one resend in this session (`_resent_restore`), so after a restart it is first retried. On ack: snapshot dropped, `start_measurement_on_turnon(ts=ack)` (peak) or `complete_solar_calculation` (solar), `_restored_at` set. `on_restore_ack` marks the snapshot `pending = "done"` just before it is removed; only that starts `min_on_s` (a restore that found the device already on, or had nothing to do, does not).
- `manual_override` or a disabled device drops an open action in `_poll_acks` (no more resends; peak pending discarded, solar measurement completed).
- Without a consumption reading `run_tick(None)` still calls `PeakDecider.enforce_limits()` (acks + `max_off_s`).
- The switch min-OFF (`PEAK_SWITCH_MIN_OFF_S`) counts from `snapshot.off_since` (persisted; previously an in-memory `_shed_at`), so it also holds after a restart and refreshes on a re-shed.

### Per-device power sensor and pause limits (v1.9.7)

Optional fields on every cascade device (ignored for EV), editable in the panel for peak devices:
- `power_sensor`: current draw (W/kW, `read_power_state_w`). At shed time its value replaces `power_watts` and is stored as `snapshot.shed_w`; restore headroom and the tracker's `nominal_kw` use `shed_w` (the live value is ~0 while paused). Below `POWER_SENSOR_MIN_W` (100 W) the device is not shed (draws nothing). Unavailable → `power_watts`. The inject start rule and the schedule keep `power_watts`. `controller._check_power_drop` uses the tracker's nominal for all devices.
- `max_off_s`: after that long confirmed off (`off_since`), `PeakDecider._restore_max_off()` restores it **regardless of headroom** (hold `max_off`, warning). Deliberate trade-off: can set a new month peak.
- `min_on_s`: after a confirmed restore the peak cascade skips the device for that long (hold `min_on`) and sheds the next devices. With `max_off_s` but no `min_on_s`, `PEAK_SWITCH_MIN_OFF_S` (5 min) applies, otherwise a persistent peak gives 20 min off / 1 min on. `_restored_at` is persisted in the cascade store under `peak_runtime`.

The climate solar ack accepts a setpoint within 0.5 °C of `inject_temp` (thermostats round). The EV solar restore judges "already stopped" by the charge state only when the switch is `unknown`/`unavailable` (a switch reporting `on` is always turned off); `throttle_down_solar` falls back to the guard (`CHARGING` and `last_switch_state`) when there is no charge state and the switch is `unknown`.

The Samsung washer (SmartThings) is driven through a template switch in the user's HA (`switch.wasmachine_peak_guard`: on = running with remote control on; off = pause; on = resume, only from pause). The washer reports no live power, so its `power_sensor` is an HA template estimate from the wash phase and water temperature.

### Status explanations (`holds`)

`EVGuard` also keeps a per-device (`device.id`) explanation of states that look illogical: `set_hold(device_id, code, text, until)`, cleared together with every guard's `skip_reason` by `begin_tick()` at the start of `run_tick`, exposed as `status.holds` (`code`, `text`, `remaining_s`) and shown as "Waarom deze toestand?" in the device info popup. Codes: `solar_share` (EV at minimum keeps charging because the sun still covers part of it; stopping would inject), `throttled`, `update_interval`, `rate_limit`, `max_current`, `surplus_too_small` (inject switch), `restore_queue` (inject restore is one device per cycle), `peak_no_headroom`, `peak_headroom`, `peak_stepwise`, `min_off`, `restore_failed`, since v1.9.7 `awaiting_ack`, `no_ack`, `max_off`, `min_on`, `peak_solar_handover`, and since v1.9.13 `solar_stop_confirm`. There is still no min-ON time: the "keeps charging without surplus" case is the solar-share rule.

Control rules that came with it:
- An inject switch (`SwitchOnDevice`) only turns on when the surplus covers its `power_watts`; otherwise it would draw from the grid and be switched off again next cycle.
- Inject restore: an EV that holds on `solar_share` without acting is skipped, so the next device (e.g. a boiler) is switched off instead. Still one action per cycle.
- `_apply_solar` for a running EV returns `excess − (new_a − current_a) × V` (only the extra it claims), so later inject devices still see the remaining surplus.
- Peak restore uses `continue`: a device that doesn't fit doesn't block smaller ones. EV need is `peak_restore_need_w` (start minimum when stopped, difference to the original current when reduced). A reduced EV is raised stepwise (`peak_raise_current`, whole amps that fit in the headroom, `EV_MIN_UPDATE_INTERVAL_S`), a stopped EV waits for min-OFF and restarts at the current that fits while keeping the snapshot for the further steps. "Stopped" is `EVGuard.peak_is_off`: stopped by Peak Guard and not restarted (`last_switch_state is False`), else not charging — a Tesla charge state lags after `turn_off`, which must not bypass min-OFF. A stopped EV reserves its original current (or `power_watts`) when restored in full. After a capped restart the EV's current stays reserved while the car doesn't charge yet; if it still doesn't charge after `PEAK_RESTART_CONFIRM_S` (3 min; full or unplugged), the snapshot is dropped instead of sending `turn_on`/`set_value` every loop. `restore()` records the current it sends in `last_sent_amps`/`last_current_update`, so a stale current sensor can't make the next step lower the current.
- Peak-stopped EV during injection: since v1.9.7 a stopped EV has no inject snapshot until the solar path starts it, so `check_restore` skips a peak-stopped EV that is also in the inject cascade (`set_solar_owner_fn`, wired in the controller) while consumption < 0 (hold `peak_solar_handover`); otherwise it would restart it at its original current before solar takes over. Once consumption ≥ 0 the normal peak restore applies.
- Hand-over to solar: when the inject cascade takes an EV that still has a peak snapshot (injection after a peak limitation), `PeakDecider.hand_over_to_solar()` (called in `run_tick` right after `injection.check`) drops the peak snapshot and copies its `original_state`/`original_current` into the inject snapshot. Otherwise the peak restore would reset the current to the original value in the same cycle (lower than the surplus allows, or higher so the solar path lowers it again), and after the surplus the charge returns to its pre-peak state instead of the reduced current or off.

### Peak control on the quarter budget

`PeakDecider` does not compare the instantaneous power with the peak but uses the remaining quarter budget (`quarter_budget.py`): `allowed = (limit × 15 min − energy so far) / remaining time`, with `limit = effective_peak − buffer`, at most `PEAK_ALLOWANCE_CAP_FACTOR` (1.5) × limit, remaining time at least 60 s. The quarter start comes from `QuarterCalculator.baseline()` (via `controller.set_quarter_baseline_fn`, wired in `sensor.py`); the energy so far uses a fresh reading of the energy sensor. Shedding uses `min(instantaneous, average over the last ≥ 45 s)` (`PowerAverager`), so a short spike does not shed; restoring uses `max(...)` and never more than the limit itself, because a restored device runs into the next quarter. Without a usable baseline (restart, incomplete quarter, simulation mode) it falls back to the instantaneous rule. After a cascade that handled anything, no further shedding for `PEAK_SETTLE_S` (20 s): a wake-up right after the own `turn_off` still reads the old consumption. A switch shed by the peak cascade stays off at least `PEAK_SWITCH_MIN_OFF_S` (5 min, hold `min_off`). When the cascade ends with excess left, the quarter is recorded in `PeakAvoidTracker.exhausted_quarters` (persisted) and the panel explains a month peak in such a quarter as unavoidable (`maandpiek` attribute of `sensor.peak_guard_peak_avoided_events`).

**Slow energy sensor** (v1.9.8): the age of the energy reading is HA's `last_reported` (else `last_updated`). Older than `PEAK_ENERGY_STALE_S` (30 s): the energy since that reading is estimated from the consumption of each cycle (`PowerHistory`, fed every tick by `run_tick` → `note_consumption`, injection counted as 0, cleared when simulation mode toggles; each value held until the next) and the one-minute average is not used (it would be too low). Older than `PEAK_ENERGY_MAX_STALE_S` (5 min): instantaneous rule. The owner's P1 sensor reports every ~5 s, so this never applies there.

The charging schedule's allowance is still instantaneous (`effective_peak − buffer − house load`).

### 2.5 kW capacity floor

Below `CAPACITY_MIN_KW` (2.5 kW) no extra capacity tariff is due, so there is no financial reason to limit consumption under it. The floor is applied in every place that uses the monthly peak:

- **Control**: `PeakDecider.check()` / `check_restore()` pass the P1 peak-sensor reading through `utils.effective_peak_w()` (`max(raw, 2500 W)`). The cascade starts at `effective_peak − buffer`. An unavailable sensor still skips the check — it is never silently replaced by 2500 W.
- **Savings**: `PeakAvoidTracker._avoided_kw()` computes `max(hypo, 2.5) − max(actual, 2.5)`, for both the month total and per-device attribution. Avoided peaks that stay entirely below 2.5 kW count as €0.
- **Billed peak**: `QuarterStore.get_billed_avg_kw()` floors each monthly peak at 2.5 kW *before* averaging (Fluvius applies the minimum per month, not on the 12-month average).
- **Display**: the decision log and the panel show the effective peak, plus the raw P1 value when it is lower. The panel gets the floor from `config.capacity_min_w` in `/api/peak_guard/cascade` rather than hard-coding it. "Huidig verbruik" is red at or above the effective peak and orange in the buffer zone below it (`peak − buffer < consumption < peak`), where the cascade is already shedding devices.

Deliberately *not* floored (but raised to the P1 meter's monthly peak when that is higher): `sensor.peak_guard_monthly_peak_kw` (exposes the floored value as attribute `effectieve_piek_kw`), the historical monthly peaks and the raw rolling 12-month average — these keep showing measured values.

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
- Only quarters that are too high are removed. A month record that is too low is raised to the meter's peak (`QuarterStore.async_raise_month_peak`, keeping the own peak's timestamp), but only when the peak sensor changed in the current local month and not in the first 20 min of it (just after the month change, or a restart then, it can still show last month's peak).
- The check trusts the peak sensor: a sensor stuck on a stale low value would cause genuine new peaks to be deleted from Peak Guard's own history (steering is unaffected by that; it already depends on the same sensor).

Every query (`get_month_peak`, `get_monthly_peaks`, the 12-month average and the billed peak) reads the merge of both layers (`_peaks_by_month`). The billed peak and rolling average use the last 12 months; the history sensor exposes all stored months. With fewer than 12 months of history the average runs over the months available.

### Event archive (v1.9.12)

The trackers keep at most `MAX_EVENTS` (100) events per month in memory and in the month state (solar reaches that in ~11 days in November). `event_archive.py` keeps every event for the running month plus 12 closed months (`EVENT_ARCHIVE_MONTHS` = 13) in `<config>/peak_guard/events/` — outside `custom_components`:
- `YYYY-MM.jsonl`: running month, one line per completed event, appended via the tracker hook `on_event` (`_emit`, a failing hook never breaks the tracker; writes go to the executor). Records use the sensor-attribute field names plus `key` (`p|device_id|avoid_ts` / `s|device_id|turnon_ts`) and, for peak events, `avoid_quarters`. The running month is also held in memory.
- `YYYY-MM.json`: closed month with `totals` (`SharedCapacityState.month_totals()`), `maandpiek` (`month_peak_info()` of the last update before the change) and the final per-event values. Written by `async_close_month` in the rollover, before `reset_month`.
- Peak per-event values are recalculated from the tracker (`PeakAvoidTracker.evaluate_quarters`, the same rule as `_recalc`): live for the running month (also for events that fell out of the 100), once more at the month change.
- `async_load` (in `sensor.py` setup, after the trackers are restored) seeds tracker events that aren't archived yet, closes a `.jsonl` of an earlier month as `late` (month change missed while HA was down: last written values, no totals), and prunes months ≥ 13 back (future-dated files stay).
- The savings tab fetches `/api/peak_guard/events` (month picker; the running month refreshes on the 15-s poll) and falls back to the sensor attributes until the first fetch. ~1.5 MB/year at November's rate.

The event sensors' attributes (`events` on `sensor.peak_guard_peak_avoided_events` / `_solar_avoided_events`, the three tables on the overview "recente gebeurtenissen" sensor) are capped at `SENSOR_EVENT_ROWS` (25) and listed in `_unrecorded_attributes`: they exceeded the recorder's 16 KB attribute limit, and a dropped attribute row also drops the unit, which suspended the long-term statistics of the overview sensor.

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
- **A small meter drop exactly on a quarter boundary** is mostly caught by the boundary interpolation (a reading lower than the previous one is not interpolated, so that quarter is incomplete and not stored); a dip of the last reading before the boundary can still yield a plausible phantom quarter, removed by the P1-meter check once it is 10 minutes old — except for the last quarter of a month.
- **Quarter history before v1.9.3 is 3–7 % too low** (boundary bug); the running month is corrected via the P1 meter's peak, older months are not.
- **This month's savings in the month v1.9.3 is installed** compare a hypothetical peak built from the old, too-low own quarters with an actual peak raised to the meter: a lower bound until the month ends. The month-peak note in the panel uses the own peak's quarter time also when the record was raised to the meter.
- **Single quarters can be off by a few percent** when the load jumps within the minute around a boundary (linear interpolation); unbiased on average.
- **Monthly-peak history starts at v1.8.16.** Older months are absent; until 12 months exist the billed peak averages over the months available. Downgrading to ≤ v1.8.15 drops the monthly records.
- **Meter replacement costs one quarter** (the drop voids it).
- **Slow energy sensor at a quarter boundary**: the baseline is the last reading before the boundary, so the extrapolation from it also counts the consumption between that reading and the boundary (at most sensor interval × power). Errs towards shedding earlier.
- **Simulation mode in the panel** refreshes on the 15-second poll, not live.
- **EV daily call budget and EV API log file date run on UTC days**, not local days.
- **`controller.py` and the frontend panel are not covered by the test suite.** Panel helpers have only been exercised in Node, not in a browser.
- **Time-zone handling is tested against a stub** of `homeassistant.util.dt`, not a live HA.
- **Schedule precision is the loop interval** (~60 s) for time windows; tariff-sensor changes wake the loop immediately.
- **Schedule fulfilment relies on the battery sensor and charge state**, which can be stale while the car sleeps.
- **A schedule stores a copy of the device.** The panel refreshes it when the device is edited in a cascade; edits made elsewhere (e.g. directly in the store) don't propagate.
- **A forced resume (`max_off_s`) ignores the peak**: it can set a new month peak; `min_on_s` then sheds the next devices instead.
- **No acknowledgement for EV chargers, the legacy throttle, or schedule switch entries** (the schedule calls the switch services directly).
- **A stopped EV gets an inject snapshot only when Peak Guard starts it** (v1.9.7). An unplanned charge after an injection tick is therefore now blocked by the schedule's `block_unplanned` as designed (the stale snapshot used to mark it solar-owned).
- **A template switch that reads `off` when its device is unreachable** acknowledges a pause that never happened; give such a switch an `availability` template so it goes `unavailable` instead.
- **Peak restore of a reduced EV with a stale `stopped`** (pre-existing): `peak_is_off()` is observation-only, so a car Peak Guard started and the peak then only reduced looks stopped while the charge state is a leftover `stopped`; the restore may send a `turn_on` (refused with `is_charging`, which then confirms charging) instead of raising the current. Not changed in v1.9.13 because the capped-restart give-up needs the observed state.
- **The EV wizard can't clear an optional entity field** (empty input falls back to the old value) — pre-existing.

## REST API

| Endpoint | Methods | Purpose |
|---|---|---|
| `/api/peak_guard/cascade` | GET, POST | Fetch or update the full cascade configuration (`type`: `peak`, `inject` or `schedule`); GET also returns `config.version` (from `manifest.json`, also set as `sw_version` on the devices and shown in the panel header) |
| `/api/peak_guard/force_check` | POST | Trigger an immediate monitoring cycle |
| `/api/peak_guard/events` | GET | Event archive: all peak and solar events of one month (`?month=YYYY-MM`, default the running month), plus `months`, `totals`, `maandpiek`, `final`/`late` |
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
