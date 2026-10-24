"""
tests/test_field_fixes.py — fouten uit de veldgegevens van oktober 2026 (v1.9.3).

- Kwartieren lagen 3–7 % te laag: de energie tussen de kwartiergrens en de
  eerste meting erna ging verloren (QuarterCalculator).
- Een herstart midden in een kwartier sloeg een onvolledig kwartier op.
- De piek-cascade schakelde af op een korte uitschieter van het momentane
  vermogen, terwijl het kwartiergemiddelde ver onder de piek bleef.
- Een wake-up vlak na de eigen afschakeling schakelde opnieuw af op een
  verouderde meting; een boiler pendelde aan/uit op een wisselende huislast.
- Een EV-stop werd als "inhalen na herinschakelen" geboekt (verschuiving
  binnen hetzelfde kwartier → altijd 0) en de natuurlijke stop kwam van een
  daling van het huisverbruik.
- De maandpiek van de P1-meter is de facturatiereferentie.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from homeassistant.util import dt as dt_util

from custom_components.peak_guard.avoided_peak_tracker import PeakAvoidTracker
from custom_components.peak_guard.quarter_budget import (
    PowerAverager,
    make_budget,
    quarter_allowance_w,
)
from custom_components.peak_guard.quarter_calculator import QuarterCalculator
from custom_components.peak_guard.quarter_store import QuarterStore

from tests.conftest import MockHass
from tests.test_monthly_peak_history import FakeStore
from tests.test_peak_floor import FakeDevice, PEAK_SENSOR, _decider


def _t(minute: int, second: int = 0, hour: int = 18) -> datetime:
    return datetime(2026, 10, 23, hour, minute, second, tzinfo=timezone.utc)


def _run_constant(calc: QuarterCalculator, kw: float, start: datetime, end: datetime,
                  offset_s: int, energy0: float = 1000.0) -> list:
    """Constant vermogen, één meting per minuut op offset_s; afgesloten kwartieren."""
    closed = []
    t = start.replace(second=0) + timedelta(seconds=offset_s)
    while t <= end:
        energy = energy0 + kw * (t - start).total_seconds() / 3600.0
        calc.update(energy, t)
        if calc.quarter_just_finished:
            closed.append((calc.last_finished_ts, calc.last_finished_value))
        t += timedelta(minutes=1)
    return closed


# ═══════════════════════════════════════════════════════════════════════════ #
#  QuarterCalculator — interpolatie op de grens                               #
# ═══════════════════════════════════════════════════════════════════════════ #

class TestQuarterBoundaryInterpolation:

    @pytest.mark.parametrize("offset_s", [0, 26, 55, 59])
    def test_constant_power_closes_at_the_true_value(self, offset_s):
        """Vroeger: 6,1 % te laag bij een timer op seconde 55."""
        calc = QuarterCalculator()
        closed = _run_constant(calc, 4.0, _t(0), _t(46), offset_s)
        full = [kw for ts, kw in closed if ts >= _t(15)]
        assert full, closed
        for kw in full:
            assert kw == pytest.approx(4.0, rel=0.005)

    def test_first_quarter_after_a_mid_quarter_start_is_not_stored(self):
        """Begin van het kwartier niet gemeten: de waarde is enkel een ondergrens."""
        calc = QuarterCalculator()
        calc.update(1000.0, _t(7, 30))
        calc.update(1000.5, _t(14, 59))
        calc.update(1000.5, _t(15, 0))
        assert calc.quarter_just_finished is False

    def test_running_value_of_a_partial_quarter_uses_its_own_start(self):
        calc = QuarterCalculator()
        calc.update(1000.0, _t(7))
        assert calc.update(1000.2, _t(10)) == pytest.approx(4.0, abs=0.01)
        assert calc.baseline() is None
        assert calc.energy_so_far_kwh() is None

    def test_long_gap_around_the_boundary_does_not_close_the_quarter(self):
        calc = QuarterCalculator()
        calc.update(1000.0, _t(0))
        calc.update(1000.5, _t(10))
        calc.update(1001.0, _t(16))        # 6 min gat over de grens
        assert calc.quarter_just_finished is False
        assert calc.baseline() is None     # het nieuwe kwartier is ook onvolledig

    def test_baseline_and_energy_so_far_of_a_complete_quarter(self):
        calc = QuarterCalculator()
        calc.update(1000.0, _t(0))
        calc.update(1000.3, _t(5))
        q_start, start_energy = calc.baseline()
        assert q_start == _t(0)
        assert start_energy == pytest.approx(1000.0)
        assert calc.energy_so_far_kwh() == pytest.approx(0.3)


class TestQuarterCalculatorRestart:

    def test_restart_within_the_same_quarter_measures_on_exactly(self):
        calc = QuarterCalculator()
        calc.update(1000.0, _t(15))
        calc.update(1000.3, _t(19))
        saved = calc.to_dict()

        after = QuarterCalculator()
        assert after.restore(saved) is True
        after.update(1000.8, _t(29, 59))
        after.update(1000.8 + 4.0 / 3600, _t(30, 0))
        assert after.quarter_just_finished is True
        assert after.last_finished_ts == _t(15)
        assert after.last_finished_value == pytest.approx(3.2, abs=0.01)

    def test_restart_across_the_boundary_with_a_short_gap_closes_it(self):
        calc = QuarterCalculator()
        calc.update(1000.0, _t(0))
        calc.update(1000.9, _t(14, 0))      # 3,857 kW
        saved = calc.to_dict()

        after = QuarterCalculator()
        after.restore(saved)
        after.update(1000.9 + 0.0643 * 2, _t(16, 0))   # 2 min gat, constant vermogen
        assert after.quarter_just_finished is True
        assert after.last_finished_value == pytest.approx(3.857, abs=0.01)
        assert after.baseline() == (_t(15), pytest.approx(1000.9643, abs=1e-3))

    def test_long_downtime_does_not_store_the_interrupted_quarter(self):
        calc = QuarterCalculator()
        calc.update(1000.0, _t(15))
        calc.update(1000.3, _t(19))
        saved = calc.to_dict()

        after = QuarterCalculator()
        after.restore(saved)
        after.update(1002.0, _t(23, hour=19))
        assert after.quarter_just_finished is False
        assert after.baseline() is None

    @pytest.mark.parametrize("data", [None, {}, {"quarter_start": "x"}, "garbage"])
    def test_invalid_saved_state_is_ignored(self, data):
        assert QuarterCalculator().restore(data) is False


# ═══════════════════════════════════════════════════════════════════════════ #
#  Kwartierbudget                                                             #
# ═══════════════════════════════════════════════════════════════════════════ #

class TestQuarterAllowance:

    def test_at_the_start_the_allowance_is_the_limit(self):
        assert quarter_allowance_w(4000.0, 0.0, 0.0) == pytest.approx(4000.0)

    def test_unused_budget_allows_more_later_capped(self):
        # 8 min op 2 kW: 0,2667 kWh; rest 7 min → (1,0 − 0,2667) / (7/60) = 6,29 kW → cap 6 kW
        assert quarter_allowance_w(4000.0, 2.0 * 8 / 60, 8 * 60) == pytest.approx(6000.0)

    def test_used_budget_allows_less(self):
        # 10 min op 5 kW = 0,833 kWh; rest 5 min → (1,0 − 0,833) × 12 = 2 kW
        assert quarter_allowance_w(4000.0, 5.0 * 10 / 60, 600) == pytest.approx(2000.0, abs=1)

    def test_exhausted_budget_is_negative(self):
        assert quarter_allowance_w(4000.0, 1.2, 600) < 0

    def test_no_baseline_falls_back_to_instantaneous(self):
        b = make_budget(4000.0, 4500.0, _t(5), 3000.0, None, None)
        assert b.on_budget is False
        assert b.excess_w == pytest.approx(500.0)
        assert b.restore_headroom_w == pytest.approx(-500.0)

    def test_restore_never_counts_on_more_than_the_limit(self):
        b = make_budget(4000.0, 1000.0, _t(10), 1200.0, _t(0), 0.1)
        assert b.allowed_w > 4000.0
        assert b.restore_headroom_w == pytest.approx(4000.0 - 1200.0)


class TestPowerAverager:

    def test_average_over_the_last_minute(self):
        avg = PowerAverager()
        assert avg.add(_t(0), 1000.0) is None
        assert avg.add(_t(0, 30), 1000.02) is None          # nog geen 45 s
        assert avg.add(_t(1), 1000.08) == pytest.approx(4800.0)

    def test_meter_drop_resets(self):
        avg = PowerAverager()
        avg.add(_t(0), 1000.0)
        assert avg.add(_t(1), 999.0) is None


# ═══════════════════════════════════════════════════════════════════════════ #
#  PeakDecider op het kwartierbudget                                          #
# ═══════════════════════════════════════════════════════════════════════════ #

ENERGY = "sensor.energie"


def _budget_decider(hass, ev_guard, device, baseline, snapshots=None):
    hass.states.set(PEAK_SENSOR, "4958")
    d = _decider(hass, ev_guard, device, snapshots=snapshots)
    d.config["energy_sensor"] = ENERGY
    d.set_quarter_baseline_fn(lambda: baseline)
    return d


class TestPeakDeciderOnBudget:

    async def test_short_spike_does_not_shed(self, hass, ev_guard):
        """00:53 CEST: 7708 W momentaan, 4,8 kW gemiddeld, kwartier op 2,6 kW."""
        device = FakeDevice()
        d = _budget_decider(hass, ev_guard, device, (_t(45), 1000.0))
        hass.states.set(ENERGY, str(1000.0 + 0.333))
        await d.check(4800.0, _t(52, 0))
        hass.states.set(ENERGY, str(1000.0 + 0.333 + 0.080))
        await d.check(7708.0, _t(53, 0))
        assert device.applied_excess == []

    async def test_sustained_overload_late_in_a_quarter_sheds(self, hass, ev_guard):
        """18:45 CEST: 5,5 kW van bij het begin van het kwartier."""
        device = FakeDevice()
        d = _budget_decider(hass, ev_guard, device, (_t(45), 1000.0))
        hass.states.set(ENERGY, str(1000.0 + 5.5 * 5 / 60))
        await d.check(5500.0, _t(50, 0))
        # (4,858 × 0,25 − 0,458) / (10/60) = 4,54 kW toegelaten → ~963 W overschot
        assert device.applied_excess == [pytest.approx(963.0, abs=5)]

    async def test_wakeup_right_after_shedding_does_not_shed_again(self, hass, ev_guard):
        device = FakeDevice()
        d = _budget_decider(hass, ev_guard, device, None)       # momentane regel
        await d.check(6000.0, _t(10, 0))
        await d.check(6000.0, _t(10, 1))                         # zelfde (oude) meting
        assert len(device.applied_excess) == 1
        await d.check(6000.0, _t(10, 30))
        assert len(device.applied_excess) == 2

    async def test_switch_stays_off_for_the_minimum_off_time(self, hass, ev_guard):
        device = FakeDevice(power_watts=2000.0)

        async def shed(excess, snapshots, ctx):
            from custom_components.peak_guard.models import DeviceSnapshot
            snapshots[device.entity_id] = DeviceSnapshot(
                entity_id=device.entity_id, original_state="on",
            )
            return 0.0

        device.apply = shed
        snapshots: dict = {}
        d = _budget_decider(hass, ev_guard, device, None, snapshots=snapshots)
        await d.check(6000.0, _t(10, 0))
        assert device.entity_id in snapshots
        await d.check_restore(1000.0, _t(12, 0))                 # ruimte genoeg, maar < 5 min
        assert device.restored == 0
        assert ev_guard._holds[device.id]["code"] == "min_off"
        await d.check_restore(1000.0, _t(15, 1))
        assert device.restored == 1

    async def test_exhausted_cascade_is_recorded(self, hass, ev_guard):
        device = FakeDevice()

        async def nothing_left(excess, snapshots, ctx):
            return excess

        device.apply = nothing_left
        d = _budget_decider(hass, ev_guard, device, None)
        await d.check(6000.0, _t(50, 0))
        assert d.peak_tracker.exhausted == [pytest.approx(1142.0)]


# ═══════════════════════════════════════════════════════════════════════════ #
#  EV: uitgestelde lading                                                     #
# ═══════════════════════════════════════════════════════════════════════════ #

class TestEvDeferral:

    def _tracker(self) -> PeakAvoidTracker:
        t = PeakAvoidTracker()
        t.set_tarief(120.0)
        return t

    def test_stop_and_restart_books_the_off_interval(self):
        """22:47 → 22:53: 6 min × 1,84 kW in kwartier 22:45 (+0,736 kW), geen minus."""
        t = self._tracker()
        t.record_pending_avoid("ev", "tesla", 1.84, ts=_t(47, hour=20), deferred=True)
        t.start_measurement_on_turnon("ev", "tesla", ts=_t(53, hour=20), max_kw=1.84)
        assert t.get_active_ids() == []
        (ev,) = t.events
        assert ev.measured_duration_min == pytest.approx(6.0)
        assert ev.avoid_quarters == [_t(45, hour=20)]
        t.set_context({_t(45, hour=20): 3.484}, actual_monthly_peak=4.958)
        assert ev.hypothetical_peak_kw == pytest.approx(3.484 + 0.736, abs=0.001)
        assert ev.avoided_peak_kw == 0.0

    def test_deferral_that_creates_the_month_peak_counts(self):
        t = self._tracker()
        t.record_pending_avoid("ev", "tesla", 1.84, ts=_t(0), deferred=True)
        t.start_measurement_on_turnon("ev", "tesla", ts=_t(15))
        t.set_context({_t(0): 3.5}, actual_monthly_peak=4.0)
        assert t.hypothetical_monthly_peak_kw == pytest.approx(5.34)
        assert t.avoided_kw_this_month == pytest.approx(1.34)

    def test_window_end_completes_a_deferral(self):
        t = self._tracker()
        t.record_pending_avoid("ev", "tesla", 1.84, ts=_t(0), deferred=True)
        event = t.complete_deferral("ev", _t(10))
        assert event is not None and event.measured_duration_min == pytest.approx(10.0)
        assert t.get_pending_ids() == []

    def test_switch_devices_keep_the_catch_up_model(self):
        t = self._tracker()
        t.record_pending_avoid("boiler", "Boiler", 2.0, ts=_t(0))
        t.start_measurement_on_turnon("boiler", "Boiler", ts=_t(5))
        assert t.get_active_ids() == ["boiler"]
        assert t.complete_deferral("boiler", _t(6)) is None


# ═══════════════════════════════════════════════════════════════════════════ #
#  Maandpiek van de P1-meter                                                  #
# ═══════════════════════════════════════════════════════════════════════════ #

NOW = datetime(2026, 10, 24, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def now_oct(monkeypatch):
    monkeypatch.setattr(dt_util, "utcnow", lambda: NOW)


class TestRaiseToMeter:

    async def _store(self) -> QuarterStore:
        store = QuarterStore(MockHass())
        store._store = FakeStore()
        await store.add_quarter(datetime(2026, 10, 23, 16, 45, tzinfo=timezone.utc), 4.6363)
        return store

    async def test_higher_meter_peak_raises_the_month_record(self, now_oct):
        store = await self._store()
        assert await store.async_raise_month_peak(4.958, NOW) is True
        assert store.get_current_month_peak() == pytest.approx(4.958)
        assert store.get_current_month_peak_entry()["ts"] == "2026-10-23T16:45:00+00:00"

    async def test_lower_or_equal_meter_peak_changes_nothing(self, now_oct):
        store = await self._store()
        assert await store.async_raise_month_peak(4.6363, NOW) is False
        assert await store.async_raise_month_peak(3.0, NOW) is False
        assert store.get_current_month_peak() == pytest.approx(4.6363)

    async def test_implausible_meter_value_is_ignored(self, now_oct):
        store = await self._store()
        assert await store.async_raise_month_peak(4958.0, NOW) is False


# ═══════════════════════════════════════════════════════════════════════════ #
#  Review v1.9.3: lokale tijd van de timer, begrensd uitstel                  #
# ═══════════════════════════════════════════════════════════════════════════ #

class TestLocalTimeFromTimer:
    """async_track_time_change geeft lokale tijd; de wintertijdwissel mag niets kosten."""

    @pytest.mark.parametrize("start,end", [
        (datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc),
         datetime(2026, 10, 25, 2, 31, tzinfo=timezone.utc)),     # CEST → CET
        (datetime(2027, 3, 28, 0, 30, tzinfo=timezone.utc),
         datetime(2027, 3, 28, 2, 31, tzinfo=timezone.utc)),      # CET → CEST
    ])
    def test_dst_change_keeps_every_quarter(self, start, end):
        from zoneinfo import ZoneInfo
        brussels = ZoneInfo("Europe/Brussels")
        calc = QuarterCalculator()
        closed = []
        t = start
        while t <= end:
            calc.update(1000.0 + 2.0 * (t - start).total_seconds() / 3600.0, t.astimezone(brussels))
            if calc.quarter_just_finished:
                closed.append((calc.last_finished_ts, calc.last_finished_value))
            t += timedelta(minutes=1)
        assert [ts for ts, _ in closed] == [start + timedelta(minutes=15 * i) for i in range(8)]
        assert all(ts.utcoffset() == timedelta(0) for ts, _ in closed)
        assert all(kw == pytest.approx(2.0, rel=0.005) for _, kw in closed)


class TestDeferralBound:

    def test_long_deferral_is_capped_at_a_quarter(self):
        t = PeakAvoidTracker()
        t.record_pending_avoid("ev", "tesla", 1.84, ts=_t(0), deferred=True)
        t.start_measurement_on_turnon("ev", "tesla", ts=_t(0, hour=23))
        (ev,) = t.events
        assert ev.measured_duration_min == pytest.approx(15.0)
        assert ev.avoid_quarters == [_t(0)]

    def test_deferral_without_restart_expires_after_a_quarter(self):
        """Kabel uit of vol: geen herstart, toch geen uren aan schijnbesparing."""
        t = PeakAvoidTracker()
        t.record_pending_avoid("ev", "tesla", 1.84, ts=_t(5), deferred=True)
        assert t.complete_expired(_t(19)) == []
        (ev,) = t.complete_expired(_t(21))
        assert ev.measured_duration_min == pytest.approx(15.0)
        assert t.get_pending_ids() == []
        t.start_measurement_on_turnon("ev", "tesla", ts=_t(30))   # latere herstart: niets meer
        assert len(t.events) == 1
