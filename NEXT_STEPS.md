# Peak Guard — Refactoring Progress

Started: 2026-05-21. Working through the prioritized list from the architecture review.

## Status Legend
- ✅ Done
- 🔄 In progress
- ⏳ Not started

---

## Items

| # | Issue | Status | Notes |
|---|-------|--------|-------|
| 1 | Split `apply_action` (750-line method) into `_apply_peak` + `_apply_solar` | ✅ | `ev_guard.py` |
| 2 | Fix cascade aliasing (controller ↔ deciders share mutable list with fragile manual resync) | ✅ | `async_load` + `update_cascade` now use `.clear()` + `.extend()`, no pointer resync |
| 3 | `CascadeDevice` god-dataclass (17 fields, EV-specific ones optional) | ⏳ | HIGH EFFORT — sealed hierarchy. Defer to separate session. |
| 4 | `_sensor_value` duplicated in `controller.py` and `base.py` | ✅ | Extracted to `read_sensor()` in `base.py` |
| 5 | `_track_action` duplicated between `base.py` and `ev_guard.py` | ✅ | Extracted to `track_action()` in `base.py` |
| 6 | `SharedCapacityState` half-baked construction (6 `set_*` calls post-ctor) | ✅ | All 6 deps now passed to constructor; `set_*` methods removed |
| 7 | Anonymous class hack `type("", (), {"state": "??"})()` | ✅ | `ev_guard.py` |
| 8 | `_log_decision` (260 lines) in controller instead of a dedicated class | ✅ | Extracted to `decision_logger.py` — `DecisionLogger` class |
| 9 | `_warn` used for INFO-level cascade status messages | ✅ | `base.py` — changed to `_LOGGER.info` / `_LOGGER.warning` as appropriate |
| 10 | 22 near-identical sensor classes (constructor boilerplate explosion) | ✅ | `sensor.py` — `_SensorDef` dataclass + `_TrackerSensorBase`; 9 sensors each reduced to `super().__init__` |
| 11 | Useless `f"peak_guard.peak_state"` f-string with no interpolation | ✅ | `sensor.py` |
| 12 | In-function imports (`PeakEvent`, `SolarEvent`) | ✅ | `sensor.py` |
| 13 | Dead `AvoidedPeakTracker = PeakAvoidTracker` alias | ✅ | `avoided_peak_tracker.py` |

---

## How to resume

1. Read this file first.
2. Only item 3 remains (`CascadeDevice` sealed hierarchy — HIGH EFFORT, defer to a dedicated session).
3. The session limit policy (from CLAUDE.md) applies: commit all work before stopping.

## Key files map

| File | Role |
|------|------|
| `custom_components/peak_guard/controller.py` | Core orchestrator, monitoring loop |
| `custom_components/peak_guard/decision_logger.py` | Extracted from controller — writes peak_guard_decisions.log |
| `custom_components/peak_guard/deciders/ev_guard.py` | EV state machine |
| `custom_components/peak_guard/deciders/base.py` | Shared cascade logic, `read_sensor()`, `track_action()` |
| `custom_components/peak_guard/deciders/peak_decider.py` | Peak cascade decisions |
| `custom_components/peak_guard/deciders/injection_decider.py` | Solar cascade decisions |
| `custom_components/peak_guard/sensor.py` | 22+ sensor classes, SharedCapacityState |
| `custom_components/peak_guard/avoided_peak_tracker.py` | PeakAvoidTracker + SolarShiftTracker |
| `custom_components/peak_guard/models.py` | Dataclasses, EVDeviceGuard, EVRateLimiter |
