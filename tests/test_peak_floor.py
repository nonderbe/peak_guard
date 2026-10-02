"""
tests/test_peak_floor.py — 2,5 kW-ondergrens van het capaciteitstarief.

Onder CAPACITY_MIN_KW (2,5 kW) is geen extra capaciteitstarief verschuldigd.
De maandpiek waarmee Peak Guard stuurt en rekent mag dus nooit lager zijn
dan 2500 W, ook al rapporteert de P1-meter aan het begin van de maand een
veel lagere waarde.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.peak_guard.avoided_peak_tracker import PeakAvoidTracker
from custom_components.peak_guard.decision_logger import DecisionLogger
from custom_components.peak_guard.deciders.peak_decider import PeakDecider
from custom_components.peak_guard.quarter_store import QuarterStore
from custom_components.peak_guard.utils import effective_peak_w

from tests.conftest import MockHass, MockPeakTracker, MockSolarTracker

PEAK_SENSOR = "sensor.p1_maandpiek"
Q = datetime(2026, 6, 10, 10, 0, 0, tzinfo=timezone.utc)
TARIEF = 120.0  # €/kW/jaar → 10 €/kW/maand


# ═══════════════════════════════════════════════════════════════════════════ #
#  effective_peak_w                                                           #
# ═══════════════════════════════════════════════════════════════════════════ #

class TestEffectivePeak:

    def test_peak_below_minimum_is_raised_to_2500(self):
        assert effective_peak_w(400.0) == 2500.0

    def test_peak_above_minimum_is_unchanged(self):
        assert effective_peak_w(3200.0) == 3200.0

    def test_peak_exactly_at_minimum_is_unchanged(self):
        assert effective_peak_w(2500.0) == 2500.0


# ═══════════════════════════════════════════════════════════════════════════ #
#  PeakDecider                                                                #
# ═══════════════════════════════════════════════════════════════════════════ #

class FakeDevice:
    """Minimaal cascade-apparaat dat registreert wat de decider ermee doet."""

    def __init__(self, power_watts: float = 1000.0) -> None:
        self.id = "dev1"
        self.name = "Boiler"
        self.entity_id = "switch.boiler"
        self.priority = 1
        self.action_type = "switch_off"
        self.enabled = True
        self.manual_override = False
        self.power_watts = power_watts
        self.applied_excess: list[float] = []
        self.restored = 0

    async def apply(self, excess, snapshots, ctx):
        self.applied_excess.append(excess)
        return 0.0

    async def restore(self, snapshot, ctx):
        self.restored += 1
        return True


def _decider(hass, ev_guard, device, snapshots=None, buffer_w=100):
    async def _save():
        return None

    return PeakDecider(
        hass=hass,
        config={"peak_sensor": PEAK_SENSOR, "buffer_watts": buffer_w},
        peak_tracker=MockPeakTracker(),
        solar_tracker=MockSolarTracker(),
        ev_guard=ev_guard,
        iteration_actions=[],
        save_fn=_save,
        cascade=[device],
        snapshots=snapshots if snapshots is not None else {},
    )


class TestPeakDeciderFloor:

    async def test_no_cascade_below_2500_when_p1_peak_is_low(self, hass, ev_guard):
        """P1-piek 400 W, verbruik 2000 W: ver boven de P1-piek, maar gratis."""
        hass.states.set(PEAK_SENSOR, "400")
        device = FakeDevice()
        await _decider(hass, ev_guard, device).check(2000.0)
        assert device.applied_excess == []

    async def test_cascade_starts_above_floor_minus_buffer(self, hass, ev_guard):
        """Grens = 2500 − 100 = 2400 W; verbruik 2600 W → 200 W overschot."""
        hass.states.set(PEAK_SENSOR, "400")
        device = FakeDevice()
        await _decider(hass, ev_guard, device).check(2600.0)
        assert device.applied_excess == [pytest.approx(200.0)]

    async def test_consumption_exactly_at_limit_does_not_start_cascade(self, hass, ev_guard):
        """Precies 2400 W is nog geen overschot; 2401 W wel."""
        hass.states.set(PEAK_SENSOR, "400")
        device = FakeDevice()
        decider = _decider(hass, ev_guard, device)
        await decider.check(2400.0)
        assert device.applied_excess == []
        await decider.check(2401.0)
        assert device.applied_excess == [pytest.approx(1.0)]

    async def test_zero_p1_peak_is_floored(self, hass, ev_guard):
        """Net na de maandwissel meldt de P1-meter 0 W."""
        hass.states.set(PEAK_SENSOR, "0")
        device = FakeDevice()
        await _decider(hass, ev_guard, device).check(2000.0)
        assert device.applied_excess == []

    async def test_p1_peak_above_floor_behaves_as_before(self, hass, ev_guard):
        """P1-piek 4000 W: de vloer speelt niet mee; grens = 3900 W."""
        hass.states.set(PEAK_SENSOR, "4000")
        device = FakeDevice()
        decider = _decider(hass, ev_guard, device)
        await decider.check(3800.0)
        assert device.applied_excess == []
        await decider.check(4100.0)
        assert device.applied_excess == [pytest.approx(200.0)]

    async def test_unavailable_peak_sensor_still_skips_check(self, hass, ev_guard):
        """Een onbeschikbare sensor mag niet stilzwijgend 2500 W worden."""
        hass.states.set(PEAK_SENSOR, "unavailable")
        device = FakeDevice()
        await _decider(hass, ev_guard, device).check(9000.0)
        assert device.applied_excess == []

    async def test_kw_peak_sensor_is_reported_once(self, hass, ev_guard, caplog):
        """
        Een piek-sensor in kW wordt door de vloer gemaskeerd (3,2 → 2500 W).
        Dat moet zichtbaar zijn in de log, maar niet elke cyclus opnieuw.
        """
        hass.states.set(PEAK_SENSOR, "3.2", {"unit_of_measurement": "kW"})
        decider = _decider(hass, ev_guard, FakeDevice())
        with caplog.at_level("WARNING"):
            await decider.check(1000.0)
            await decider.check(1000.0)
        unit_warnings = [r for r in caplog.records if "kW" in r.getMessage()]
        assert len(unit_warnings) == 1
        assert PEAK_SENSOR in unit_warnings[0].getMessage()

    async def test_watt_peak_sensor_gives_no_unit_warning(self, hass, ev_guard, caplog):
        hass.states.set(PEAK_SENSOR, "400", {"unit_of_measurement": "W"})
        with caplog.at_level("WARNING"):
            await _decider(hass, ev_guard, FakeDevice()).check(1000.0)
        assert caplog.records == []

    async def test_restore_uses_floored_headroom(self, hass, ev_guard):
        """
        P1-piek 400 W, verbruik 1000 W, apparaat 1000 W.
        Headroom = 2500 − 100 − 1000 = 1400 W ≥ 1000 W → herstellen.
        """
        hass.states.set(PEAK_SENSOR, "400")
        device = FakeDevice(power_watts=1000.0)
        snapshots = {device.entity_id: object()}
        await _decider(hass, ev_guard, device, snapshots).check_restore(1000.0)
        assert device.restored == 1
        assert snapshots == {}

    async def test_restore_blocked_when_floored_headroom_too_small(self, hass, ev_guard):
        """Headroom = 2500 − 100 − 2000 = 400 W < 1000 W → niet herstellen."""
        hass.states.set(PEAK_SENSOR, "400")
        device = FakeDevice(power_watts=1000.0)
        snapshots = {device.entity_id: object()}
        await _decider(hass, ev_guard, device, snapshots).check_restore(2000.0)
        assert device.restored == 0
        assert device.entity_id in snapshots


# ═══════════════════════════════════════════════════════════════════════════ #
#  PeakAvoidTracker — besparing met vloer                                     #
# ═══════════════════════════════════════════════════════════════════════════ #

class TestSavingsFloor:

    def _tracker(self) -> PeakAvoidTracker:
        t = PeakAvoidTracker()
        t.set_tarief(TARIEF)
        return t

    def _cycle(self, tracker, nominal_kw, device_id="dev1", avoid_ts=Q):
        tracker.record_pending_avoid(device_id, "Oven", nominal_kw, ts=avoid_ts)
        tracker.start_measurement_on_turnon(device_id, "Oven", ts=avoid_ts)
        return tracker.complete_peak_calculation(
            device_id, now=avoid_ts + timedelta(minutes=15)
        )

    def test_avoided_peak_entirely_below_floor_saves_nothing(self):
        """Hypo 1,8 kW vs werkelijk 1,2 kW: beide onder 2,5 kW → €0."""
        t = self._tracker()
        t.set_context(actual_quarters={Q: 1.2}, actual_monthly_peak=1.2)
        event = self._cycle(t, nominal_kw=0.6)
        assert event is not None
        assert t.hypothetical_monthly_peak_kw == pytest.approx(1.8)
        assert t.avoided_kw_this_month == 0.0
        assert t.savings_euro_this_month == 0.0
        assert event.savings_euro == 0.0

    def test_only_the_part_above_floor_counts(self):
        """Hypo 4,0 kW vs werkelijk 1,0 kW → 4,0 − 2,5 = 1,5 kW → €15."""
        t = self._tracker()
        t.set_context(actual_quarters={Q: 1.0}, actual_monthly_peak=1.0)
        self._cycle(t, nominal_kw=3.0)
        assert t.avoided_kw_this_month == pytest.approx(1.5)
        assert t.savings_euro_this_month == pytest.approx(15.0)

    def test_both_peaks_above_floor_are_unaffected(self):
        """Hypo 5,0 kW vs werkelijk 3,0 kW → 2,0 kW → €20 (zoals voorheen)."""
        t = self._tracker()
        t.set_context(actual_quarters={Q: 3.0}, actual_monthly_peak=3.0)
        self._cycle(t, nominal_kw=2.0)
        assert t.avoided_kw_this_month == pytest.approx(2.0)
        assert t.savings_euro_this_month == pytest.approx(20.0)

    def test_actual_peak_rising_above_hypo_clamps_savings_to_zero(self):
        """Een latere echte piek boven de hypothetische wist de besparing uit."""
        t = self._tracker()
        t.set_context(actual_quarters={Q: 1.0}, actual_monthly_peak=1.0)
        self._cycle(t, nominal_kw=3.0)
        t.set_context(actual_quarters={Q: 1.0}, actual_monthly_peak=4.5)
        assert t.avoided_kw_this_month == 0.0
        assert t.savings_euro_this_month == 0.0

    def test_device_savings_use_the_same_floor(self):
        """Per-apparaat besparing volgt dezelfde vloer als het maandtotaal."""
        t = self._tracker()
        t.set_context(actual_quarters={Q: 1.0}, actual_monthly_peak=1.0)
        self._cycle(t, nominal_kw=3.0)
        (d,) = t.get_device_monthly_savings()
        assert d["hypothetical_peak_kw"] == pytest.approx(4.0)
        assert d["actual_monthly_peak_kw"] == pytest.approx(1.0)
        assert d["avoided_kw"] == pytest.approx(1.5)
        assert d["savings_euro"] == pytest.approx(15.0)


# ═══════════════════════════════════════════════════════════════════════════ #
#  QuarterStore — aangerekende piek met minimum per maand                     #
# ═══════════════════════════════════════════════════════════════════════════ #

def _store_with_month_peaks(this_month_kw, prev_month_kw) -> QuarterStore:
    store = QuarterStore(MockHass())
    now = datetime.now(timezone.utc)
    this_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    prev_month = this_month - timedelta(days=1)
    if prev_month_kw is not None:
        store._entries.append({"ts": prev_month.isoformat(), "kw": prev_month_kw})
    if this_month_kw is not None:
        store._entries.append({"ts": this_month.isoformat(), "kw": this_month_kw})
    return store


class TestBilledAverage:

    def test_minimum_is_applied_per_month_before_averaging(self):
        """1,5 en 4,0 kW → (2,5 + 4,0) / 2 = 3,25 kW, niet max(2,75; 2,5)."""
        store = _store_with_month_peaks(this_month_kw=1.5, prev_month_kw=4.0)
        assert store.get_billed_avg_kw() == pytest.approx(3.25)

    def test_all_months_below_minimum_gives_minimum(self):
        store = _store_with_month_peaks(this_month_kw=1.0, prev_month_kw=2.0)
        assert store.get_billed_avg_kw() == pytest.approx(2.5)

    def test_months_above_minimum_are_a_plain_average(self):
        store = _store_with_month_peaks(this_month_kw=3.0, prev_month_kw=5.0)
        assert store.get_billed_avg_kw() == pytest.approx(4.0)

    def test_no_history_gives_minimum(self):
        store = _store_with_month_peaks(this_month_kw=None, prev_month_kw=None)
        assert store.get_billed_avg_kw() == pytest.approx(2.5)

    def test_raw_rolling_average_is_not_floored(self):
        """Het voortschrijdend gemiddelde blijft de gemeten waarden tonen."""
        store = _store_with_month_peaks(this_month_kw=1.5, prev_month_kw=4.0)
        assert store.get_rolling_12_month_avg() == pytest.approx(2.75)


# ═══════════════════════════════════════════════════════════════════════════ #
#  DecisionLogger                                                             #
# ═══════════════════════════════════════════════════════════════════════════ #

class _HassConfig:
    def __init__(self, base) -> None:
        self._base = base

    def path(self, name: str) -> str:
        return str(self._base / name)


async def _logged_text(hass, ev_guard, tmp_path, consumption: float) -> str:
    hass.config = _HassConfig(tmp_path)
    logger = DecisionLogger(
        hass=hass,
        config={"peak_sensor": PEAK_SENSOR, "buffer_watts": 100},
        peak_cascade=[],
        inject_cascade=[],
        peak_snapshots={},
        inject_snapshots={},
        ev_guard=ev_guard,
        iteration_actions=[],
    )
    await logger.log(consumption, {})
    return (tmp_path / "peak_guard_decisions.log").read_text(encoding="utf-8")


class TestDecisionLogFloor:

    async def test_log_shows_effective_and_raw_peak(self, hass, ev_guard, tmp_path):
        hass.states.set(PEAK_SENSOR, "400")
        text = await _logged_text(hass, ev_guard, tmp_path, 2000.0)
        assert "Maandpiek:         2500 W" in text
        assert "Maandpiek (P1):    400 W" in text
        assert "Target peak:       2400 W" in text
        assert "Peak limiting actief?    Nee" in text

    async def test_log_omits_raw_line_when_p1_peak_above_floor(self, hass, ev_guard, tmp_path):
        hass.states.set(PEAK_SENSOR, "4000")
        text = await _logged_text(hass, ev_guard, tmp_path, 2000.0)
        assert "Maandpiek:         4000 W" in text
        assert "Maandpiek (P1)" not in text

    async def test_log_reports_limiting_against_floored_target(self, hass, ev_guard, tmp_path):
        hass.states.set(PEAK_SENSOR, "400")
        text = await _logged_text(hass, ev_guard, tmp_path, 2600.0)
        assert "Peak limiting actief?    JA" in text
        assert "overschot: 200 W" in text
