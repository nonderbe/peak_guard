"""
tests/test_startup_restore.py — herstel van de piekbesparing bij het opstarten.

Staat Home Assistant uit terwijl de maand wisselt, dan mist Peak Guard de
maandafsluiting. Bij de volgende start moet het jaartotaal behouden blijven
en moet de per-apparaat-besparing van de gemiste maand alsnog bevroren worden.
"""
from __future__ import annotations

import pytest

from custom_components.peak_guard.avoided_peak_tracker import PeakAvoidTracker
from custom_components.peak_guard.monthly_device_savings_store import (
    MonthlyDeviceSavingsStore,
)
from custom_components.peak_guard.sensor import restore_peak_tracker

from tests.conftest import FakeBudgetStore, MockHass

TARIEF = 120.0  # €/kW/jaar → 10 €/kW/maand


def _tracker() -> PeakAvoidTracker:
    t = PeakAvoidTracker()
    t.set_tarief(TARIEF)
    return t


class TestRestorePeakTracker:

    def test_same_month_restores_month_and_year(self):
        t = _tracker()
        restore_peak_tracker(
            t,
            saved_year={"year": 2026, "savings_euro_this_year": 40.0},
            peak_state={"year": 2026, "month": 10, "avoided_kw_this_month": 1.5,
                        "savings_euro_this_month": 15.0,
                        "hypothetical_peaks_this_month": [4.0]},
            year=2026, month=10,
        )
        assert t.savings_euro_this_year == pytest.approx(40.0)
        assert t.savings_euro_this_month == pytest.approx(15.0)
        assert t.hypothetical_peaks_this_month == [4.0]
        # Jaarbasis = jaar − lopende maand: een herberekening zonder besparing
        # deze maand laat de vorige maanden (€25) staan.
        t._recalc_month_savings()
        assert t.savings_euro_this_year == pytest.approx(25.0)

    def test_month_changed_while_offline_keeps_year_total(self):
        """Opgeslagen staat is van september, we starten in oktober."""
        t = _tracker()
        restore_peak_tracker(
            t,
            saved_year={"year": 2026, "savings_euro_this_year": 40.0},
            peak_state={"year": 2026, "month": 9, "avoided_kw_this_month": 1.5,
                        "savings_euro_this_month": 15.0,
                        "hypothetical_peaks_this_month": [4.0]},
            year=2026, month=10,
        )
        assert t.savings_euro_this_month == 0.0
        assert t.hypothetical_peaks_this_month == []
        t._recalc_month_savings()
        assert t.savings_euro_this_year == pytest.approx(40.0)

    def test_missing_month_state_keeps_year_total(self):
        t = _tracker()
        restore_peak_tracker(
            t,
            saved_year={"year": 2026, "savings_euro_this_year": 40.0},
            peak_state=None,
            year=2026, month=10,
        )
        t._recalc_month_savings()
        assert t.savings_euro_this_year == pytest.approx(40.0)

    def test_year_changed_while_offline_starts_at_zero(self):
        t = _tracker()
        restore_peak_tracker(
            t,
            saved_year={"year": 2026, "savings_euro_this_year": 40.0},
            peak_state={"year": 2026, "month": 12,
                        "savings_euro_this_month": 15.0},
            year=2027, month=1,
        )
        t._recalc_month_savings()
        assert t.savings_euro_this_year == 0.0
        assert t.savings_euro_this_month == 0.0

    def test_nothing_saved_starts_at_zero(self):
        t = _tracker()
        restore_peak_tracker(t, saved_year=None, peak_state=None, year=2026, month=10)
        assert t.savings_euro_this_year == 0.0


def _record(year, month, finalized, device_id="boiler") -> dict:
    return {
        "year": year, "month": month, "device_id": device_id,
        "device_name": "Boiler", "hypothetical_peak_kw": 4.0,
        "actual_monthly_peak_kw": 1.0, "avoided_kw": 1.5,
        "savings_euro": 15.0, "finalized": finalized,
    }


def _device_store(*records) -> MonthlyDeviceSavingsStore:
    store = MonthlyDeviceSavingsStore(MockHass())
    store._store = FakeBudgetStore()
    store._entries = list(records)
    return store


class TestFinalizeMissedMonths:

    async def test_open_record_of_earlier_month_is_finalized(self):
        store = _device_store(_record(2026, 9, finalized=False))
        closed = await store.async_finalize_before(2026, 10)
        assert closed == 1
        assert store.get_month(2026, 9)[0]["finalized"] is True
        assert store.get_month(2026, 9)[0]["savings_euro"] == 15.0

    async def test_current_month_stays_open(self):
        store = _device_store(_record(2026, 10, finalized=False))
        assert await store.async_finalize_before(2026, 10) == 0
        assert store.get_month(2026, 10)[0]["finalized"] is False

    async def test_december_is_finalized_in_january(self):
        store = _device_store(_record(2026, 12, finalized=False))
        assert await store.async_finalize_before(2027, 1) == 1
        assert store.get_month(2026, 12)[0]["finalized"] is True

    async def test_several_missed_months_and_devices(self):
        store = _device_store(
            _record(2026, 7, finalized=False),
            _record(2026, 8, finalized=True),
            _record(2026, 9, finalized=False, device_id="oven"),
            _record(2026, 10, finalized=False),
        )
        assert await store.async_finalize_before(2026, 10) == 2
        assert [e["finalized"] for e in store.get_all_entries()] == [
            True, True, True, False,
        ]

    async def test_store_is_only_written_when_something_changed(self):
        store = _device_store(_record(2026, 9, finalized=True))
        await store.async_finalize_before(2026, 10)
        assert store._store.saved == []

        store = _device_store(_record(2026, 9, finalized=False))
        await store.async_finalize_before(2026, 10)
        assert len(store._store.saved) == 1

    async def test_malformed_record_is_skipped_not_fatal(self):
        """Een beschadigde record mag het opstarten van de sensoren niet breken."""
        store = _device_store(
            {"device_id": "kapot"},
            {"year": None, "month": 9, "device_id": "kapot2", "finalized": False},
            _record(2026, 9, finalized=False),
        )
        assert await store.async_finalize_before(2026, 10) == 1
        assert store.get_all_entries()[2]["finalized"] is True
