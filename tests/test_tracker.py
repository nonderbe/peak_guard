"""
tests/test_tracker.py — Financial calculation tests for PeakAvoidTracker and SolarShiftTracker.

Covers the savings/shift arithmetic that was previously untested (P2-1).
All tests run without a live Home Assistant instance — the trackers have no HA imports.
"""
from __future__ import annotations

import pytest
from datetime import datetime, timedelta, timezone

from custom_components.peak_guard.avoided_peak_tracker import (
    PeakAvoidTracker,
    SolarShiftTracker,
)

# Anchor timestamp: exact start of a 15-minute quarter block.
Q = datetime(2026, 6, 10, 10, 0, 0, tzinfo=timezone.utc)
TARIEF = 120.0  # €/kW/year → 10 €/kW/month, easy mental arithmetic


# ═══════════════════════════════════════════════════════════════════════════ #
#  SolarShiftTracker                                                          #
# ═══════════════════════════════════════════════════════════════════════════ #

class TestSolarShiftTracker:

    def _tracker(self, netto_eur: float = 0.25) -> SolarShiftTracker:
        t = SolarShiftTracker()
        t.set_netto_eur_per_kwh(netto_eur)
        return t

    def test_complete_without_start_returns_none(self):
        """complete_solar_calculation returns None when no measurement was started."""
        t = self._tracker()
        result = t.complete_solar_calculation("dev1", now=Q)
        assert result is None

    def test_too_short_session_discarded(self):
        """Sessions under 0.5 minutes (30 s) are silently discarded."""
        t = self._tracker()
        t.start_solar_measurement("dev1", "Boiler", 2.0, ts=Q)
        result = t.complete_solar_calculation("dev1", now=Q + timedelta(seconds=20))
        assert result is None
        assert len(t.events) == 0

    def test_shifted_kwh_and_savings_are_correct(self):
        """30 min at 2 kW → 1.0 kWh shifted, €0.25 saved at €0.25/kWh."""
        t = self._tracker(netto_eur=0.25)
        t.start_solar_measurement("dev1", "Boiler", 2.0, ts=Q)
        event = t.complete_solar_calculation("dev1", now=Q + timedelta(minutes=30))
        assert event is not None
        assert event.shifted_kwh == pytest.approx(1.0)
        assert event.savings_euro == pytest.approx(0.25)
        assert event.measured_duration_min == pytest.approx(30.0)
        assert event.device_name == "Boiler"

    def test_monthly_totals_accumulate_across_sessions(self):
        """Two separate 30-min sessions sum to 2.0 kWh and €0.50."""
        t = self._tracker(netto_eur=0.25)
        t.start_solar_measurement("dev1", "Boiler", 2.0, ts=Q)
        t.complete_solar_calculation("dev1", now=Q + timedelta(minutes=30))
        t.start_solar_measurement("dev1", "Boiler", 2.0, ts=Q + timedelta(hours=1))
        t.complete_solar_calculation("dev1", now=Q + timedelta(hours=1, minutes=30))
        assert t.shifted_kwh_this_month == pytest.approx(2.0)
        assert t.savings_euro_this_month == pytest.approx(0.50)
        assert len(t.events) == 2

    def test_higher_tariff_scales_savings(self):
        """€0.50/kWh rate doubles the savings compared to €0.25/kWh."""
        t_low  = self._tracker(netto_eur=0.25)
        t_high = self._tracker(netto_eur=0.50)
        for t in (t_low, t_high):
            t.start_solar_measurement("dev1", "Boiler", 2.0, ts=Q)
            t.complete_solar_calculation("dev1", now=Q + timedelta(minutes=30))
        assert t_high.savings_euro_this_month == pytest.approx(
            2 * t_low.savings_euro_this_month
        )

    def test_reset_month_clears_monthly_data(self):
        """reset_month wipes events, kWh and monthly EUR."""
        t = self._tracker()
        t.start_solar_measurement("dev1", "Boiler", 2.0, ts=Q)
        t.complete_solar_calculation("dev1", now=Q + timedelta(minutes=30))
        t.reset_month()
        assert t.shifted_kwh_this_month == 0.0
        assert t.savings_euro_this_month == 0.0
        assert len(t.events) == 0

    def test_year_savings_survive_reset_month(self):
        """Year-to-date savings are not cleared by a monthly reset."""
        t = self._tracker(netto_eur=0.25)
        t.start_solar_measurement("dev1", "Boiler", 2.0, ts=Q)
        t.complete_solar_calculation("dev1", now=Q + timedelta(minutes=30))
        year_before = t.savings_euro_this_year
        assert year_before == pytest.approx(0.25)
        t.reset_month()
        assert t.savings_euro_this_year == pytest.approx(year_before)


# ═══════════════════════════════════════════════════════════════════════════ #
#  PeakAvoidTracker                                                           #
# ═══════════════════════════════════════════════════════════════════════════ #

class TestPeakAvoidTracker:

    def _tracker(self, tarief: float = TARIEF) -> PeakAvoidTracker:
        t = PeakAvoidTracker()
        t.set_tarief(tarief)
        return t

    def _full_cycle(
        self,
        tracker: PeakAvoidTracker,
        nominal_kw: float = 4.0,
        duration_min: float = 15.0,
        avoid_ts: datetime = Q,
    ):
        """Record pending → turn on → complete cycle; returns the event."""
        # Terug aan een half uur later: de energie valt dan in andere kwartieren.
        turnon_ts    = avoid_ts + timedelta(minutes=30)
        stop_ts      = turnon_ts + timedelta(minutes=duration_min)
        tracker.record_pending_avoid("dev1", "Oven", nominal_kw, ts=avoid_ts)
        tracker.start_measurement_on_turnon("dev1", "Oven", ts=turnon_ts)
        return tracker.complete_peak_calculation("dev1", now=stop_ts)

    # ── guard rails ──────────────────────────────────────────────────────── #

    def test_complete_without_active_returns_none(self):
        """complete_peak_calculation returns None when no active measurement exists."""
        t = self._tracker()
        assert t.complete_peak_calculation("dev1", now=Q) is None

    def test_pending_only_no_active_returns_none(self):
        """Calling complete without start_measurement_on_turnon returns None."""
        t = self._tracker()
        t.record_pending_avoid("dev1", "Oven", 4.0, ts=Q)
        # NOT calling start_measurement_on_turnon
        assert t.complete_peak_calculation("dev1", now=Q + timedelta(minutes=15)) is None

    def test_too_short_session_discarded(self):
        """Sessions under 0.5 minutes (30 s) are silently discarded."""
        t = self._tracker()
        t.record_pending_avoid("dev1", "Oven", 4.0, ts=Q)
        t.start_measurement_on_turnon("dev1", "Oven", ts=Q)
        result = t.complete_peak_calculation("dev1", now=Q + timedelta(seconds=20))
        assert result is None
        assert len(t.events) == 0

    # ── core calculation ──────────────────────────────────────────────────── #

    def test_full_quarter_computes_correct_avoided_kw(self):
        """
        4 kW device avoided for a full 15-min quarter.
        Extra load in that quarter = 4 kW (1 kWh / 0.25 h).
        With no actual load → hypo = 4 kW. The actual peak (0 kW) is billed
        at the 2.5 kW minimum, so only 4.0 − 2.5 = 1.5 kW is really avoided.
        """
        t = self._tracker(tarief=TARIEF)
        event = self._full_cycle(t, nominal_kw=4.0, duration_min=15.0)
        assert event is not None
        assert event.avoided_peak_kw == pytest.approx(1.5)
        # savings = 1.5 kW × (120 €/kW/year) / 12 months = 15 €
        assert event.savings_euro == pytest.approx(15.0)

    def test_hypothetical_peak_kw_field_is_populated(self):
        """event.hypothetical_peak_kw reflects the computed monthly hypo."""
        t = self._tracker()
        event = self._full_cycle(t, nominal_kw=4.0, duration_min=15.0)
        assert event is not None
        assert event.hypothetical_peak_kw == pytest.approx(4.0)

    def test_actual_quarters_raise_hypothetical_peak(self):
        """
        Existing 3 kW actual load + 2 kW avoided device → hypo 5 kW.
        Actual monthly peak = 3 kW → avoided = 2 kW.
        """
        t = self._tracker(tarief=TARIEF)
        t.set_context(actual_quarters={Q: 3.0}, actual_monthly_peak=3.0)
        event = self._full_cycle(t, nominal_kw=2.0, duration_min=15.0)
        assert event is not None
        assert event.avoided_peak_kw == pytest.approx(2.0)
        # savings = 2.0 × 120 / 12 = 20 €
        assert event.savings_euro == pytest.approx(20.0)

    def test_tariff_scales_savings_linearly(self):
        """Doubling the tariff doubles the savings, with everything else equal."""
        t_low  = self._tracker(tarief=60.0)
        t_high = self._tracker(tarief=120.0)
        ev_low  = self._full_cycle(t_low,  nominal_kw=4.0, duration_min=15.0)
        ev_high = self._full_cycle(t_high, nominal_kw=4.0, duration_min=15.0)
        assert ev_low is not None and ev_high is not None
        assert ev_high.savings_euro == pytest.approx(2 * ev_low.savings_euro)

    def test_reset_month_clears_monthly_data(self):
        """reset_month wipes events, avoided_kw_this_month and savings_euro_this_month."""
        t = self._tracker()
        self._full_cycle(t)
        t.reset_month()
        assert t.avoided_kw_this_month == 0.0
        assert t.savings_euro_this_month == 0.0
        assert len(t.events) == 0
        assert len(t.extra_dict) == 0

    def test_event_added_to_log(self):
        """A completed cycle appends a PeakEvent to tracker.events."""
        t = self._tracker()
        self._full_cycle(t)
        assert len(t.events) == 1
        assert t.events[0].device_name == "Oven"


# ═══════════════════════════════════════════════════════════════════════════ #
#  Per-device savings attribution (get_device_monthly_savings)                #
# ═══════════════════════════════════════════════════════════════════════════ #

class TestDeviceMonthlySavings:
    """
    Covers PeakAvoidTracker.get_device_monthly_savings(): isolates, per
    device, the highest hypothetical monthly peak it avoided vs. the actual
    monthly peak — the basis for the monthly_device_savings_store history.

    Each device's hypo is the actual quarters plus only that device's own
    shift, so a device is never credited for what another device avoided.
    """

    def _tracker(self, tarief: float = TARIEF) -> PeakAvoidTracker:
        t = PeakAvoidTracker()
        t.set_tarief(tarief)
        return t

    def _cycle(self, tracker: PeakAvoidTracker, device_id: str, device_name: str,
               nominal_kw: float, avoid_ts: datetime, duration_min: float = 15.0):
        turnon_ts = avoid_ts + timedelta(minutes=30)
        stop_ts = turnon_ts + timedelta(minutes=duration_min)
        tracker.record_pending_avoid(device_id, device_name, nominal_kw, ts=avoid_ts)
        tracker.start_measurement_on_turnon(device_id, device_name, ts=turnon_ts)
        return tracker.complete_peak_calculation(device_id, now=stop_ts)

    def test_no_events_returns_empty_list(self):
        t = self._tracker()
        assert t.get_device_monthly_savings() == []

    def test_single_device_matches_overall_month_totals(self):
        """One device's isolated savings equal the tracker's overall monthly totals."""
        t = self._tracker(tarief=TARIEF)
        self._cycle(t, "boiler", "Boiler", nominal_kw=4.0, avoid_ts=Q)
        result = t.get_device_monthly_savings()
        assert len(result) == 1
        assert result[0]["device_id"] == "boiler"
        assert result[0]["avoided_kw"] == pytest.approx(t.avoided_kw_this_month)
        assert result[0]["savings_euro"] == pytest.approx(t.savings_euro_this_month)

    def test_two_devices_in_separate_quarters_are_isolated(self):
        """
        Tesla avoids 2 kW in one quarter, then Boiler avoids 5 kW in a later,
        distinct quarter. Each device's own snapshot reflects the hypo at the
        moment its event completed, so both keep their own genuine value.
        With no actual load the billed peak is the 2.5 kW minimum: Tesla's
        2 kW stays below it (nothing avoided), Boiler avoids 5.0 − 2.5 kW.
        """
        t = self._tracker(tarief=TARIEF)
        self._cycle(t, "tesla", "Tesla", nominal_kw=2.0, avoid_ts=Q)
        self._cycle(t, "boiler", "Boiler", nominal_kw=5.0, avoid_ts=Q + timedelta(minutes=15))

        by_device = {d["device_id"]: d for d in t.get_device_monthly_savings()}
        assert by_device["tesla"]["hypothetical_peak_kw"] == pytest.approx(2.0)
        assert by_device["tesla"]["avoided_kw"] == pytest.approx(0.0)
        assert by_device["boiler"]["hypothetical_peak_kw"] == pytest.approx(5.0)
        assert by_device["boiler"]["avoided_kw"] == pytest.approx(2.5)
        # savings = avoided_kw × tarief / 12
        assert by_device["boiler"]["savings_euro"] == pytest.approx(2.5 * TARIEF / 12)

    def test_later_higher_event_raises_the_devices_own_snapshot(self):
        """
        A device that completes a second, bigger event later in the month
        picks up the new, higher combined hypo for itself.
        """
        t = self._tracker(tarief=TARIEF)
        self._cycle(t, "boiler", "Boiler", nominal_kw=3.0, avoid_ts=Q)
        self._cycle(t, "boiler", "Boiler", nominal_kw=6.0, avoid_ts=Q + timedelta(minutes=15))

        by_device = {d["device_id"]: d for d in t.get_device_monthly_savings()}
        assert by_device["boiler"]["hypothetical_peak_kw"] == pytest.approx(6.0)

    def test_reset_month_clears_device_savings(self):
        t = self._tracker()
        self._cycle(t, "boiler", "Boiler", nominal_kw=4.0, avoid_ts=Q)
        t.reset_month()
        assert t.get_device_monthly_savings() == []
        assert t.device_max_hypo_this_month == {}
        assert t.device_names_this_month == {}


# ═══════════════════════════════════════════════════════════════════════════ #
#  Per-event hypothetical peak and shift bookkeeping                          #
# ═══════════════════════════════════════════════════════════════════════════ #

class TestPerEventValues:

    def _tracker(self) -> PeakAvoidTracker:
        t = PeakAvoidTracker()
        t.set_tarief(TARIEF)
        return t

    def _cycle(self, t, device_id, nominal_kw, avoid_ts, turnon_delay_min=30.0,
               duration_min=15.0):
        turnon = avoid_ts + timedelta(minutes=turnon_delay_min)
        t.record_pending_avoid(device_id, device_id, nominal_kw, ts=avoid_ts)
        t.start_measurement_on_turnon(device_id, device_id, ts=turnon)
        return t.complete_peak_calculation(
            device_id, now=turnon + timedelta(minutes=duration_min))

    def test_each_event_has_its_own_hypothetical_peak(self):
        """The reported bug: every row showed the month-wide maximum."""
        t = self._tracker()
        q2 = Q + timedelta(hours=2)
        t.set_context(actual_quarters={Q: 3.0, q2: 1.0}, actual_monthly_peak=3.0)
        boiler = self._cycle(t, "boiler", 2.0, Q)
        pump = self._cycle(t, "pump", 0.5, q2)
        assert boiler.hypothetical_peak_kw == pytest.approx(5.0)
        assert pump.hypothetical_peak_kw == pytest.approx(1.5)
        assert boiler.avoided_peak_kw == pytest.approx(2.0)
        assert pump.avoided_peak_kw == 0.0
        assert t.hypothetical_monthly_peak_kw == pytest.approx(5.0)

    def test_lower_restart_draw_caps_the_shifted_power(self):
        """EV herstart na de piek op 8 A i.p.v. 16 A: verschuif enkel wat hij trekt."""
        t = self._tracker()
        t.record_pending_avoid("ev", "EV", 3.68, ts=Q)
        t.start_measurement_on_turnon("ev", "EV", ts=Q + timedelta(minutes=30), max_kw=1.84)
        assert t.get_active_nominal_kw("ev") == pytest.approx(1.84)

    def test_device_savings_are_not_credited_with_other_devices_shift(self):
        t = self._tracker()
        q2 = Q + timedelta(hours=2)
        t.set_context(actual_quarters={Q: 3.0, q2: 1.0}, actual_monthly_peak=3.0)
        self._cycle(t, "boiler", 2.0, Q)
        self._cycle(t, "pump", 0.5, q2)
        by_device = {d["device_id"]: d for d in t.get_device_monthly_savings()}
        assert by_device["boiler"]["avoided_kw"] == pytest.approx(2.0)
        assert by_device["pump"]["hypothetical_peak_kw"] == pytest.approx(3.0)
        assert by_device["pump"]["avoided_kw"] == 0.0

    def test_two_devices_in_same_quarter_add_up_in_event_hypo(self):
        t = self._tracker()
        t.set_context(actual_quarters={Q: 3.0}, actual_monthly_peak=3.0)
        first = self._cycle(t, "boiler", 2.0, Q)
        self._cycle(t, "oven", 1.0, Q)
        assert first.hypothetical_peak_kw == pytest.approx(6.0)
        by_device = {d["device_id"]: d for d in t.get_device_monthly_savings()}
        assert by_device["boiler"]["hypothetical_peak_kw"] == pytest.approx(5.0)
        assert by_device["oven"]["hypothetical_peak_kw"] == pytest.approx(4.0)

    def test_shift_within_the_same_quarter_avoids_nothing(self):
        """Off at 10:02, on at 10:05, stop at 10:12: the energy stays in the quarter."""
        t = self._tracker()
        t.set_context(actual_quarters={Q: 3.0}, actual_monthly_peak=3.0)
        event = self._cycle(t, "boiler", 4.0, Q + timedelta(minutes=2),
                            turnon_delay_min=3.0, duration_min=7.0)
        assert event.hypothetical_peak_kw == pytest.approx(3.0)
        assert event.avoided_peak_kw == 0.0
        assert t.avoided_kw_this_month == 0.0

    def test_event_values_follow_a_later_higher_actual_peak(self):
        t = self._tracker()
        t.set_context(actual_quarters={Q: 3.0}, actual_monthly_peak=3.0)
        event = self._cycle(t, "boiler", 2.0, Q)
        assert event.savings_euro == pytest.approx(20.0)
        t.set_context(actual_quarters={Q: 3.0, Q + timedelta(days=1): 4.5},
                      actual_monthly_peak=4.5)
        assert event.avoided_peak_kw == pytest.approx(0.5)
        assert event.savings_euro == pytest.approx(5.0)

    def test_removed_phantom_quarter_lowers_the_hypo(self):
        """A quarter wiped by the P1 check no longer props up the hypo."""
        t = self._tracker()
        t.set_context(actual_quarters={Q: 11.0}, actual_monthly_peak=11.0)
        self._cycle(t, "boiler", 2.0, Q)
        assert t.hypothetical_monthly_peak_kw == pytest.approx(13.0)
        t.set_context(actual_quarters={Q: 3.0}, actual_monthly_peak=3.0)
        assert t.hypothetical_monthly_peak_kw == pytest.approx(5.0)

    def test_reshed_completes_the_running_measurement(self):
        t = self._tracker()
        t.record_pending_avoid("boiler", "Boiler", 2.0, ts=Q)
        t.start_measurement_on_turnon("boiler", "Boiler", ts=Q + timedelta(minutes=30))
        t.record_pending_avoid("boiler", "Boiler", 2.0, ts=Q + timedelta(minutes=40))
        assert len(t.events) == 1
        assert t.events[0].measured_duration_min == pytest.approx(10.0)
        assert t.get_pending_ids() == ["boiler"]

    def test_repeated_shed_keeps_the_original_avoid_time(self):
        t = self._tracker()
        t.record_pending_avoid("ev", "EV", 4.0, ts=Q)
        t.record_pending_avoid("ev", "EV", 4.0, ts=Q + timedelta(minutes=5))
        t.start_measurement_on_turnon("ev", "EV", ts=Q + timedelta(minutes=30))
        event = t.complete_peak_calculation("ev", now=Q + timedelta(minutes=45))
        assert event.avoid_ts == Q

    def test_measurement_without_detected_stop_completes_after_a_quarter(self):
        t = self._tracker()
        t.record_pending_avoid("boiler", "Boiler", 2.0, ts=Q)
        t.start_measurement_on_turnon("boiler", "Boiler", ts=Q + timedelta(minutes=30))
        assert t.complete_expired(Q + timedelta(minutes=40)) == []
        (event,) = t.complete_expired(Q + timedelta(minutes=50))
        assert event.measured_duration_min == pytest.approx(15.0)
        assert t.get_active_ids() == []

    def test_shift_survives_a_restart(self):
        t = self._tracker()
        t.set_context(actual_quarters={Q: 3.0}, actual_monthly_peak=3.0)
        self._cycle(t, "boiler", 2.0, Q)
        saved = t.extra_to_dict()

        t2 = self._tracker()
        t2.extra_from_dict(saved)
        t2.set_context(actual_quarters={Q: 3.0}, actual_monthly_peak=3.0)
        assert t2.hypothetical_monthly_peak_kw == pytest.approx(5.0)
        assert t2.avoided_kw_this_month == pytest.approx(2.0)
        assert t2.device_max_hypo_this_month["boiler"] == pytest.approx(5.0)
