# Peak Guard — Next Steps

Updated: 2026-05-21. Previous refactoring items all done. New round from deeper architecture review.

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
| P0-3 | `_apply_solar` god-method + 15-second `asyncio.sleep` loop blocks HA event loop | ⏳ | `ev_guard.py`; sleep loop in wake-up section |

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
| P2-1 | Test coverage thin for financial calculations | ⏳ | `tests/` |
| P2-2 | `_monitor_loop` 80+ lines mixing concerns | ✅ | `controller.py` — extracted `_resolve_interval()`, `_read_consumption()`, `_dispatch()` |
| P2-3 | `to_dict()` has inline datetime arithmetic in dict comprehension | ✅ | `ev_guard.py` — moved to `status_dict()` |
| P2-4 | `_warn` duplicated between `BaseDecider` and `EVGuard` | ⏳ | `base.py`, `ev_guard.py` |

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
