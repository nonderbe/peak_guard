# Peak Guard — Next Steps

Updated: 2026-06-10.

## Status Legend
- ✅ Done
- 🔄 In progress
- ⏳ Not started

---

## Previous items (all complete)

| # | Issue | Status |
|---|-------|--------|
| 1 | Split `apply_action` into `_apply_peak` + `_apply_solar` | ✅ |
| 2 | Fix cascade aliasing | ✅ |
| 3 | `CascadeDevice` god-dataclass | ✅ |
| 4–5 | `_sensor_value` / `_track_action` duplicated | ✅ |
| 6–13 | Various cleanup | ✅ |

---

## New items — architecture review 2026-05-21

### P0 — Critical

| # | Issue | Status | Location |
|---|-------|--------|----------|
| P0-1 | `DEFAULT_EV_CABLE_ENTITY` hardcoded to `"sensor.tesla_opladen"` → `None` | ✅ | `const.py:59` |
| P0-2 | `CascadeContext` fields typed `Any` → proper types | ✅ | `models.py:144` |
| P0-3 | `_apply_solar` god-method + 15-second `asyncio.sleep` loop blocks HA event loop | ✅ | `ev_guard.py` — state-machine wake-up (v1.8.0) |

### P1 — Should Fix Soon

| # | Issue | Status | Location |
|---|-------|--------|----------|
| P1-1 | Empty `if TYPE_CHECKING: pass` block | ✅ | `models.py:19` |
| P1-2 | `except Exception: pass` silences failures | ✅ | `controller.py:253`, `_teardown_ev_listeners` |
| P1-3 | `_quarter_start` defined twice → extract to `utils.py` | ✅ | `avoided_peak_tracker.py:38`, `quarter_calculator.py:27` |
| P1-4 | `_BaseCascadeDevice` naming contradiction → rename to `BaseCascadeDevice`, drop `CascadeDevice` alias | ✅ | `models.py` + all imports |
| P1-5 | `datetime.now(timezone.utc)` called 30+ times per loop → thread `now` through signatures | ⏳ | `ev_guard.py`, trackers, deciders |
| P1-6 | `from_dict` manual string-dispatch → class registry on subclasses | ✅ | `models.py` |
| P1-7 | Stale-sensor workaround duplicated in two places | ✅ | `ev_guard.py` — extracted to `_effective_current_amps()` |

### P2 — Worth Addressing

| # | Issue | Status | Location |
|---|-------|--------|----------|
| P2-1 | Test coverage thin for financial calculations | ✅ | `tests/test_tracker.py` — 16 tests for PeakAvoidTracker + SolarShiftTracker |
| P2-2 | `_monitor_loop` 80+ lines mixing concerns | ✅ | `controller.py` — extracted `_resolve_interval()`, `_read_consumption()`, `_dispatch()` |
| P2-3 | `to_dict()` has inline datetime arithmetic in dict comprehension | ✅ | `ev_guard.py` — moved to `status_dict()` |
| P2-4 | `_warn` duplicated between `BaseDecider` and `EVGuard` | ⏳ | `base.py`, `ev_guard.py` |

---

## BLE home detection — requires user action, no code changes needed

### Background

The `device_tracker.niels_en_puji_tesla_location` entity is permanently `unknown`
(Tesla cloud integration unreliable). The v1.8.3 fix (last_known_home fallback) is
a workaround. A better long-term solution: replace the cloud-based tracker with
BLE proximity detection, which is local and doesn't depend on the Tesla API.

**Peak Guard itself needs zero code changes.** The only action needed is:
1. Verify the Tesla BLE MAC is stable (user action — see below)
2. Configure `bluetooth_le_tracker` in HA's `configuration.yaml`
3. Point the Peak Guard `location_tracker` field to the new entity

**The plan**: use HA's built-in `bluetooth_le_tracker` (YAML, no HACS) to expose a
`device_tracker.tesla_ble` entity. Point the Peak Guard `location_tracker` config
to that entity. Zero code changes needed in peak_guard itself.

### Before doing anything: verify Tesla BLE MAC stability

| Step | Action |
|---|---|
| 1 | Install **nRF Connect** (iOS/Android) |
| 2 | Wake Tesla (unlock it or open app) |
| 3 | Scan in nRF Connect — note MAC of Tesla BLE device |
| 4 | Let car sleep, wake again, scan again |
| 5 | Check if MAC is the same both times |

**If MAC is stable** → proceed with `bluetooth_le_tracker` YAML config below.  
**If MAC is randomized** → BLE MAC tracking won't work; consider alternative (UUID scan, or accept v1.8.3 as good enough).

### If MAC is stable: configure `bluetooth_le_tracker` in `configuration.yaml`

```yaml
device_tracker:
  - platform: bluetooth_le_tracker
    interval_seconds: 12
    consider_home: 300
    track_new_devices: false
```

In `known_devices.yaml`:

```yaml
tesla_ble:
  name: Tesla BLE
  mac: 'AA:BB:CC:DD:EE:FF'   # replace with actual MAC from nRF Connect
  track: true
  hide_if_away: false
```

Then in Peak Guard UI, change the Tesla `location_tracker` field from
`device_tracker.niels_en_puji_tesla_location` to `device_tracker.tesla_ble`.

### Caveats

- HA host must have Bluetooth (Pi 3/4/5, HA Yellow, HA Green all do — check
  Settings → System → Hardware)
- Tesla BLE is off when sleeping → tracker shows `not_home` while car is home
  but asleep. The `consider_home: 300` grace period (5 min) helps, and the
  v1.8.3 `last_known_home` guard bridges the rest.
- BLE only activates when car wakes up → presence detection lags slightly.
  For injection prevention this is acceptable (can't charge a sleeping car anyway).

---

## Recent fixes (for context)

| Version | Fix |
|---------|-----|
| v1.8.6 | Solar events missing after first session — `last_switch_state` was never reset in solar `restore()` paths, so the manual-start gate failed from session 2 onwards. Fixed by setting `guard.last_switch_state = None` in all three solar restore paths. |
| v1.8.4 | EV solar events missing (first session) — `start_solar_measurement` was never called on the "manual start" path (switch `unknown`, status_sensor confirms charging). |
| v1.8.3 | `is_home()` uses `last_known_home` fallback to handle holiday / unknown tracker state |
| v1.8.2 | Solar injection prevention blocked when Tesla switch/tracker entities report `unknown` |
| v1.8.1 | Keep EV charging at hw-min when solar still covers part of draw |
| v1.8.0 | Tesla API JSONL logging + Logboek tab; replace 15-second sleep loop with state-machine wake-up (P0-3) |

---

## How to resume

1. Read this file first.
2. Continue with first ⏳ item above.
3. Mark ✅ when done and committed.
4. Session limit policy (CLAUDE.md): commit all work before stopping.

## Key files

| File | Role |
|------|------|
| `custom_components/peak_guard/controller.py` | Core orchestrator, monitoring loop |
| `custom_components/peak_guard/decision_logger.py` | Writes peak_guard_decisions.log |
| `custom_components/peak_guard/deciders/ev_guard.py` | EV state machine |
| `custom_components/peak_guard/deciders/base.py` | Shared cascade logic |
| `custom_components/peak_guard/deciders/peak_decider.py` | Peak cascade decisions |
| `custom_components/peak_guard/deciders/injection_decider.py` | Solar cascade decisions |
| `custom_components/peak_guard/sensor.py` | 16+ sensors, SharedCapacityState |
| `custom_components/peak_guard/avoided_peak_tracker.py` | PeakAvoidTracker + SolarShiftTracker |
| `custom_components/peak_guard/models.py` | Dataclasses, EVDeviceGuard, EVRateLimiter |
| `custom_components/peak_guard/utils.py` | Shared helpers (after P1-3) |
