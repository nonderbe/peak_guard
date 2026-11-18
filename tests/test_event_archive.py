"""
tests/test_event_archive.py — event archive (v1.9.12).

Every completed peak and solar event is archived per month, 12 closed
months plus the running month, so the savings tab can show more than the
trackers' in-memory log of 100. Peak per-event values follow the month state
until the month change and are then frozen.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from custom_components.peak_guard.avoided_peak_tracker import (
    MAX_EVENTS,
    PeakAvoidTracker,
    SolarShiftTracker,
)
from custom_components.peak_guard.event_archive import (
    EVENT_ARCHIVE_MONTHS,
    EventArchive,
)
from custom_components.peak_guard.quarter_calculator import QuarterCalculator
from custom_components.peak_guard.quarter_store import QuarterStore
from custom_components.peak_guard.sensor import (
    SENSOR_EVENT_ROWS,
    OverviewRecentEventsSensor,
    PeakAvoidedEventsSensor,
    SharedCapacityState,
    SolarAvoidedEventsSensor,
)

from tests.conftest import FakeBudgetStore, MockHass

TARIEF = 120.0   # €/kW/jaar → 10 €/kW/maand
NOV = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)


class ArchiveHass(MockHass):
    """MockHass with an executor that runs the job inline."""

    async def _run(self, fn, *args):
        return fn(*args)

    def async_add_executor_job(self, fn, *args):
        return self.async_create_task(self._run(fn, *args))


async def _settle():
    """Let the fire-and-forget executor jobs (appends) finish."""
    import asyncio
    for _ in range(3):
        await asyncio.sleep(0)


def _peak_tracker() -> PeakAvoidTracker:
    t = PeakAvoidTracker()
    t.set_tarief(TARIEF)
    return t


def _avoid(peak: PeakAvoidTracker, ts: datetime, kw: float = 5.0, dev: str = "boiler"):
    peak.record_pending_avoid(dev, dev.title(), kw, ts=ts)
    peak.start_measurement_on_turnon(dev, dev.title(), ts=ts + timedelta(minutes=30))
    return peak.complete_peak_calculation(dev, now=ts + timedelta(minutes=45))


def _solar(solar: SolarShiftTracker, ts: datetime, dev: str = "tesla"):
    solar.start_solar_measurement(dev, dev.title(), 1.8, ts=ts)
    return solar.complete_solar_calculation(dev, now=ts + timedelta(minutes=30))


async def _archive(tmp_path, year=2026, month=11, peak=None, solar=None):
    arch = EventArchive(ArchiveHass(), base_dir=tmp_path)
    await arch.async_load(year, month,
                          peak.events if peak else (), solar.events if solar else ())
    if peak is not None:
        peak.on_event = arch.record
    if solar is not None:
        solar.on_event = arch.record
    await _settle()
    return arch


def _lines(path):
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


class TestRecording:

    async def test_completed_events_are_appended_to_the_month_file(self, tmp_path):
        peak, solar = _peak_tracker(), SolarShiftTracker()
        arch = await _archive(tmp_path, peak=peak, solar=solar)
        _avoid(peak, NOV)
        _solar(solar, NOV)
        await _settle()

        lines = _lines(tmp_path / "2026-11.jsonl")
        assert [l["modus"] for l in lines] == ["peak", "solar"]
        assert lines[0]["apparaat"] == "Boiler"
        assert lines[0]["avoid_quarters"]
        assert lines[1]["verschoven_kwh"] == pytest.approx(0.9)
        data = await arch.async_month(2026, 11, peak)
        assert len(data["peak"]) == 1 and len(data["solar"]) == 1

    async def test_tracker_events_of_the_month_are_seeded_once(self, tmp_path):
        """Events from before the archive existed (or before it loaded)."""
        peak, solar = _peak_tracker(), SolarShiftTracker()
        _avoid(peak, NOV)
        _solar(solar, NOV)
        await _archive(tmp_path, peak=peak, solar=solar)
        # A restart loads the same tracker events again: no duplicates.
        arch2 = await _archive(tmp_path, peak=peak, solar=solar)

        assert len(_lines(tmp_path / "2026-11.jsonl")) == 2
        data = await arch2.async_month(2026, 11, peak)
        assert len(data["peak"]) == 1 and len(data["solar"]) == 1

    async def test_archive_keeps_more_than_the_tracker_log(self, tmp_path):
        solar = SolarShiftTracker()
        arch = await _archive(tmp_path, solar=solar)
        for i in range(MAX_EVENTS + 20):
            _solar(solar, NOV + timedelta(hours=i))
        await _settle()

        assert len(solar.events) == MAX_EVENTS
        data = await arch.async_month(2026, 11)
        assert len(data["solar"]) == MAX_EVENTS + 20
        # Newest first
        assert data["solar"][0]["timestamp_start_inschakeling"] > data["solar"][-1]["timestamp_start_inschakeling"]

    async def test_events_before_load_are_not_lost(self, tmp_path):
        arch = EventArchive(ArchiveHass(), base_dir=tmp_path)
        peak = _peak_tracker()
        peak.on_event = arch.record
        ev = _avoid(peak, NOV)       # archive not loaded yet: ignored here
        assert ev is not None
        await arch.async_load(2026, 11, peak.events, ())
        await _settle()
        assert len(_lines(tmp_path / "2026-11.jsonl")) == 1

    async def test_a_failing_hook_never_breaks_the_tracker(self):
        peak = _peak_tracker()

        def boom(_ev):
            raise RuntimeError("disk full")

        peak.on_event = boom
        ev = _avoid(peak, NOV)
        assert ev is not None and len(peak.events) == 1

    async def test_a_truncated_line_is_skipped(self, tmp_path):
        (tmp_path / "2026-11.jsonl").write_text('{"key": "s|x|1", "modus": "solar"}\n{"key": "s|y|')
        arch = await _archive(tmp_path)
        data = await arch.async_month(2026, 11)
        assert len(data["solar"]) == 1


class TestPeakValues:

    async def test_running_month_values_follow_a_later_higher_peak(self, tmp_path):
        """Also for an event that fell out of the tracker's log of 100."""
        peak = _peak_tracker()
        arch = await _archive(tmp_path, peak=peak)
        _avoid(peak, NOV)
        peak.events.clear()   # as if pushed out by 100 newer events
        peak.set_context({}, 0.0)
        data = await arch.async_month(2026, 11, peak)
        assert data["peak"][0]["besparing_eur"] == pytest.approx(25.0)   # 5 − 2.5 kW

        # A later real peak of 4 kW: the event's quarter (5 kW) saves only 1 kW.
        q = datetime(2026, 11, 20, 18, 0, tzinfo=timezone.utc)
        peak.set_context({q: 4.0}, 4.0)
        data = await arch.async_month(2026, 11, peak)
        assert data["peak"][0]["vermeden_piek_kw"] == pytest.approx(1.0)
        assert data["peak"][0]["besparing_eur"] == pytest.approx(10.0)

    async def test_evaluate_quarters_matches_the_tracker_event(self):
        peak = _peak_tracker()
        ev = _avoid(peak, NOV)
        q = datetime(2026, 11, 20, 18, 0, tzinfo=timezone.utc)
        peak.set_context({q: 3.0}, 3.0)
        ev = peak.events[-1]
        assert peak.evaluate_quarters(ev.avoid_quarters) == (
            ev.hypothetical_peak_kw, ev.avoided_peak_kw, ev.savings_euro
        )
        assert peak.evaluate_quarters([]) == (0.0, 0.0, 0.0)


class TestMonthChange:

    async def test_close_month_freezes_final_values(self, tmp_path):
        peak, solar = _peak_tracker(), SolarShiftTracker()
        arch = await _archive(tmp_path, peak=peak, solar=solar)
        _avoid(peak, NOV)
        _solar(solar, NOV)
        await _settle()
        q = datetime(2026, 11, 20, 18, 0, tzinfo=timezone.utc)
        peak.set_context({q: 4.0}, 4.0)

        await arch.async_close_month(peak, 2026, 12, totals={"piek_besparing_eur": 10.0},
                                     maandpiek={"kw": 4.0})

        assert not (tmp_path / "2026-11.jsonl").exists()
        final = json.loads((tmp_path / "2026-11.json").read_text())
        assert final["final"] is True and final["late"] is False
        assert final["totals"] == {"piek_besparing_eur": 10.0}
        assert final["maandpiek"] == {"kw": 4.0}
        assert final["peak"][0]["besparing_eur"] == pytest.approx(10.0)
        assert len(final["solar"]) == 1
        # Reset of the tracker afterwards does not change the closed month.
        peak.reset_month()
        data = await arch.async_month(2026, 11, peak)
        assert data["peak"][0]["besparing_eur"] == pytest.approx(10.0)
        # New events go to the new month.
        _solar(solar, datetime(2026, 12, 2, 12, 0, tzinfo=timezone.utc))
        await _settle()
        assert len(_lines(tmp_path / "2026-12.jsonl")) == 1
        assert await arch.async_months() == ["2026-12", "2026-11"]

    async def test_missed_month_change_is_closed_late_on_load(self, tmp_path):
        solar = SolarShiftTracker()
        arch = await _archive(tmp_path, solar=solar)
        _solar(solar, NOV)
        await _settle()

        # HA was down over the month change; the next start is in December.
        arch2 = await _archive(tmp_path, year=2026, month=12)
        assert not (tmp_path / "2026-11.jsonl").exists()
        final = json.loads((tmp_path / "2026-11.json").read_text())
        assert final["late"] is True and final["totals"] is None
        assert len(final["solar"]) == 1
        data = await arch2.async_month(2026, 11)
        assert data["final"] is True

    async def test_months_older_than_twelve_closed_months_are_removed(self, tmp_path):
        for name in ("2025-10.json", "2025-11.json", "2025-12.json", "2027-05.json"):
            (tmp_path / name).write_text('{"peak": [], "solar": []}')
        await _archive(tmp_path, year=2026, month=11)
        remaining = sorted(p.name for p in tmp_path.iterdir())
        # 2026-11 running + 12 closed back to 2025-11; a future file stays.
        assert "2025-10.json" not in remaining
        assert "2025-11.json" in remaining and "2025-12.json" in remaining
        assert "2027-05.json" in remaining
        assert EVENT_ARCHIVE_MONTHS == 13

    async def test_unknown_month_returns_none(self, tmp_path):
        arch = await _archive(tmp_path)
        assert await arch.async_month(2024, 1) is None


class TestSharedRollover:

    async def test_month_change_in_shared_state_closes_the_archive(self, tmp_path):
        hass = ArchiveHass()
        hass.states.set("sensor.energie_kwh", "100")
        store = QuarterStore(hass)
        store._store = FakeBudgetStore()
        peak, solar = _peak_tracker(), SolarShiftTracker()
        arch = EventArchive(hass, base_dir=tmp_path)
        await arch.async_load(2026, 11)
        peak.on_event = arch.record
        solar.on_event = arch.record
        shared = SharedCapacityState(
            hass=hass, energy_sensor_id="sensor.energie_kwh", store=store,
            calculator=QuarterCalculator(), tarief=TARIEF, regio="Antwerpen",
            peak_tracker=peak, solar_tracker=solar,
            savings_store=None, solar_savings_store=None,
            peak_state_store=None, solar_state_store=None,
            device_savings_store=None, event_archive=arch,
        )
        nov = datetime(2026, 11, 30, 21, 0, tzinfo=timezone.utc)
        await shared._async_update(nov)
        _avoid(peak, nov)
        await shared._async_update(nov + timedelta(hours=1))
        await shared._async_update(datetime(2026, 12, 1, 0, 1, tzinfo=timezone.utc))
        await _settle()

        final = json.loads((tmp_path / "2026-11.json").read_text())
        assert final["totals"]["piek_besparing_eur"] == pytest.approx(25.0)
        assert final["peak"][0]["besparing_eur"] == pytest.approx(25.0)
        assert final["maandpiek"] is not None
        assert arch.current_month == (2026, 12)


class TestSensorAttributes:
    """The event tables stay out of the recorder (16 KB limit) and are capped."""

    def test_event_attributes_are_not_recorded(self):
        assert "events" in PeakAvoidedEventsSensor._unrecorded_attributes
        assert "events" in SolarAvoidedEventsSensor._unrecorded_attributes
        assert {"piek_events_tabel", "solar_events_tabel", "gecombineerd_tabel"} <= \
            OverviewRecentEventsSensor._unrecorded_attributes

    def test_sensor_rows_are_capped(self):
        solar = SolarShiftTracker()
        for i in range(MAX_EVENTS):
            _solar(solar, NOV + timedelta(hours=i))
        sensor = SolarAvoidedEventsSensor.__new__(SolarAvoidedEventsSensor)
        sensor._tracker = solar
        events = sensor.extra_state_attributes["events"]
        assert len(events) == SENSOR_EVENT_ROWS
        assert events[0]["timestamp_start_inschakeling"] == solar.events[-1].turnon_ts.isoformat()
        assert len(json.dumps(sensor.extra_state_attributes)) < 16384
