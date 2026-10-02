"""
tests/test_monthly_peak_history.py — maandpieken blijven 36 maanden bewaard.

De kwartierhistoriek gaat maar ruim een maand terug. De piek van elke maand
wordt daarom apart bijgehouden, zodat de aangerekende piek (gemiddelde van
12 maanden) en de historiek niet afhangen van kwartieren die al gewist zijn.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from homeassistant.util import dt as dt_util

from custom_components.peak_guard.const import (
    MONTHLY_PEAK_HISTORY_MONTHS,
    QUARTER_HISTORY_DAYS,
)
from custom_components.peak_guard.quarter_store import QuarterStore

from tests.conftest import MockHass


class FakeStore:
    """Stand-in voor HA's Store met vooraf geladen inhoud."""

    def __init__(self, data=None) -> None:
        self.data = data
        self.saved: list[dict] = []

    async def async_load(self):
        return self.data

    async def async_save(self, data: dict) -> None:
        self.saved.append(data)


def _utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def _store(data=None) -> QuarterStore:
    store = QuarterStore(MockHass())
    store._store = FakeStore(data)
    return store


NOW = _utc(2026, 10, 15, 12, 0)


@pytest.fixture(autouse=True)
def now_oct_2026(monkeypatch):
    """Alle tests in dit bestand draaien op 15 oktober 2026."""
    monkeypatch.setattr(dt_util, "utcnow", lambda: NOW)


class TestMonthlyPeakRecord:

    async def test_month_peak_survives_when_quarters_are_gone(self):
        store = _store()
        await store.add_quarter(_utc(2026, 8, 3, 10, 0), 4.2)
        store._entries.clear()                 # kwartieren ouder dan de historiek
        assert store.get_month_peak(2026, 8) == pytest.approx(4.2)

    async def test_higher_quarter_raises_the_month_peak(self):
        store = _store()
        await store.add_quarter(_utc(2026, 8, 3, 10, 0), 3.0)
        await store.add_quarter(_utc(2026, 8, 9, 18, 0), 4.5)
        store._entries.clear()
        assert store.get_month_peak(2026, 8) == pytest.approx(4.5)

    async def test_lower_quarter_does_not_lower_the_month_peak(self):
        store = _store()
        await store.add_quarter(_utc(2026, 8, 3, 10, 0), 4.5)
        await store.add_quarter(_utc(2026, 8, 9, 18, 0), 1.0)
        store._entries.clear()
        assert store.get_month_peak(2026, 8) == pytest.approx(4.5)

    async def test_record_keeps_the_timestamp_of_the_peak(self, now_oct_2026):
        store = _store()
        peak_ts = _utc(2026, 10, 9, 18, 0)
        await store.add_quarter(_utc(2026, 10, 3, 10, 0), 3.0)
        await store.add_quarter(peak_ts, 4.5)
        await store.add_quarter(_utc(2026, 10, 10, 8, 0), 2.0)
        store._entries.clear()
        assert store.get_monthly_peaks()[-1]["ts"] == peak_ts.isoformat()

    async def test_record_is_assigned_to_the_local_month(self):
        """30 sep 22:30 UTC is 1 okt 00:30 in België."""
        store = _store()
        await store.add_quarter(_utc(2026, 9, 30, 22, 30), 4.0)
        store._entries.clear()
        assert store.get_month_peak(2026, 10) == pytest.approx(4.0)
        assert store.get_month_peak(2026, 9) is None

    async def test_only_the_last_36_months_are_kept(self):
        store = _store()
        for i in range(40):                    # jul 2023 … okt 2026
            await store.add_quarter(_utc(2023 + (i + 6) // 12, (i + 6) % 12 + 1, 10, 12, 0), 3.0)
        store._entries.clear()
        assert MONTHLY_PEAK_HISTORY_MONTHS == 36
        assert store.get_month_peak(2023, 10) is None      # 37e maand terug
        assert store.get_month_peak(2023, 11) == pytest.approx(3.0)
        assert store.get_month_peak(2026, 10) == pytest.approx(3.0)

    async def test_future_dated_record_does_not_wipe_the_history(self):
        """Een record met een datum in de toekomst (foute klok, beschadigde
        opslag) mag de echte historiek niet wegsnoeien."""
        store = _store({
            "quarters": [],
            "monthly_peaks": [
                {"year": 2026, "month": 9, "ts": "2026-09-04T17:00:00+00:00", "kw": 4.0},
                {"year": 2099, "month": 1, "ts": "2099-01-04T17:00:00+00:00", "kw": 4.0},
            ],
        })
        await store.async_load()
        await store.add_quarter(_utc(2026, 10, 15, 11, 45), 3.0)
        assert store.get_month_peak(2026, 9) == pytest.approx(4.0)
        assert store.get_month_peak(2026, 10) == pytest.approx(3.0)


class TestPersistence:

    async def test_monthly_peaks_are_saved(self):
        store = _store()
        await store.add_quarter(_utc(2026, 8, 3, 10, 0), 4.2)
        saved = store._store.saved[-1]
        assert saved["monthly_peaks"] == [
            {"year": 2026, "month": 8,
             "ts": _utc(2026, 8, 3, 10, 0).isoformat(), "kw": 4.2},
        ]
        assert len(saved["quarters"]) == 1

    async def test_monthly_peaks_are_loaded(self):
        store = _store({
            "quarters": [],
            "monthly_peaks": [
                {"year": 2025, "month": 11, "ts": "2025-11-04T17:00:00+00:00", "kw": 5.1},
            ],
        })
        await store.async_load()
        assert store.get_month_peak(2025, 11) == pytest.approx(5.1)

    async def test_upgrade_seeds_monthly_peaks_from_stored_quarters(self):
        """v1.8.15-opslag zonder 'monthly_peaks': de maandpieken worden uit de
        nog aanwezige kwartieren afgeleid en blijven daarna bewaard."""
        recent = NOW - timedelta(days=2)
        store = _store({"quarters": [
            {"ts": recent.isoformat(), "kw": 3.3},
            {"ts": (recent + timedelta(minutes=15)).isoformat(), "kw": 2.1},
        ]})
        await store.async_load()
        store._entries.clear()
        local = dt_util.as_local(recent)
        assert store.get_month_peak(local.year, local.month) == pytest.approx(3.3)

    async def test_malformed_monthly_peak_is_skipped(self):
        store = _store({
            "quarters": [],
            "monthly_peaks": [
                {"year": "x", "month": 11, "ts": "2025-11-04T17:00:00+00:00", "kw": 5.1},
                {"month": 11, "kw": 5.1},
                {"year": 2025, "month": 12, "ts": "2025-12-04T17:00:00+00:00", "kw": "veel"},
                {"year": 2026, "month": 1, "ts": "2026-01-04T17:00:00+00:00", "kw": 4.0},
            ],
        })
        await store.async_load()
        assert store.get_month_peak(2025, 11) is None
        assert store.get_month_peak(2025, 12) is None
        assert store.get_month_peak(2026, 1) == pytest.approx(4.0)

    @pytest.mark.parametrize("record", [
        {"year": 2026, "month": 13, "ts": "2026-08-04T17:00:00+00:00", "kw": 4.0},
        {"year": True, "month": 8, "ts": "2026-08-04T17:00:00+00:00", "kw": 4.0},
        {"year": 2026, "month": 8, "ts": 5, "kw": 4.0},
        {"year": 2026, "month": 8, "ts": "2026-08-04T17:00:00+00:00", "kw": -3},
        {"year": 2026, "month": 8, "ts": "2026-08-04T17:00:00+00:00", "kw": float("nan")},
        {"year": 2026, "month": 8, "ts": "2026-08-04T17:00:00+00:00", "kw": 3200.0},
        "geen dict",
    ])
    async def test_invalid_monthly_peak_record_is_skipped(self, record):
        store = _store({"quarters": [], "monthly_peaks": [record]})
        await store.async_load()
        assert store.get_monthly_peaks() == []

    async def test_quarters_cover_a_full_31_day_month(self):
        """Een kwartier van 31 dagen geleden hoort nog bij de lopende maand."""
        assert QUARTER_HISTORY_DAYS >= 32
        old = NOW - timedelta(days=31, hours=1)
        store = _store({"quarters": [{"ts": old.isoformat(), "kw": 3.3}]})
        await store.async_load()
        assert len(store.get_all_entries()) == 1


def _store_with_months(months_back: int) -> QuarterStore:
    """Store met een maandpiek voor elk van de laatste `months_back` maanden
    (t.o.v. oktober 2026): de piek in kW is gelijk aan het maandnummer-index."""
    store = _store()
    year, month = 2026, 10
    for i in range(months_back):
        store._monthly_peaks[(year, month)] = {
            "ts": _utc(year, month, 10, 12, 0).isoformat(), "kw": 3.0 + i,
        }
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return store


class TestHistoryQueries:

    def test_history_returns_up_to_36_months_oldest_first(self, now_oct_2026):
        peaks = _store_with_months(36).get_monthly_peaks()
        assert len(peaks) == 36
        assert (peaks[0]["year"], peaks[0]["month"]) == (2023, 11)
        assert (peaks[-1]["year"], peaks[-1]["month"]) == (2026, 10)

    def test_last_12_returns_only_twelve_months(self, now_oct_2026):
        peaks = _store_with_months(36).get_monthly_peaks_last_12()
        assert len(peaks) == 12
        assert (peaks[0]["year"], peaks[0]["month"]) == (2025, 11)

    def test_billed_average_uses_only_the_last_12_months(self, now_oct_2026):
        """Pieken 3,0 … 14,0 kW voor de laatste 12 maanden → gemiddeld 8,5."""
        store = _store_with_months(36)
        assert store.get_billed_avg_kw() == pytest.approx(8.5)
        assert store.get_rolling_12_month_avg() == pytest.approx(8.5)

    def test_months_without_data_are_left_out(self, now_oct_2026):
        store = _store_with_months(1)
        store._monthly_peaks[(2026, 7)] = {
            "ts": _utc(2026, 7, 10, 12, 0).isoformat(), "kw": 5.0,
        }
        peaks = store.get_monthly_peaks()
        assert [(p["year"], p["month"]) for p in peaks] == [(2026, 7), (2026, 10)]

    def test_current_month_combines_record_and_running_quarters(self, now_oct_2026):
        store = _store_with_months(1)                      # okt: 3,0 kW
        store._entries.append({"ts": _utc(2026, 10, 12, 9, 0).isoformat(), "kw": 3.8})
        assert store.get_current_month_peak() == pytest.approx(3.8)


class TestImplausibleQuarters:
    """
    Een maandpiek-record kan alleen stijgen en blijft 36 maanden staan. Een
    onmogelijke kwartierwaarde (meterstand die terugspringt, energiesensor in
    Wh onder een oudere versie) mag er dus nooit in terechtkomen.
    """

    @pytest.mark.parametrize("kw", [3200.0, 100.01, -1.0, float("nan"), float("inf")])
    async def test_implausible_quarter_is_not_stored(self, kw):
        store = _store()
        await store.add_quarter(_utc(2026, 10, 3, 10, 0), kw)
        assert store.get_all_entries() == []
        assert store.get_month_peak(2026, 10) is None
        assert store._store.saved == []

    async def test_plausible_high_quarter_is_stored(self):
        store = _store()
        await store.add_quarter(_utc(2026, 10, 3, 10, 0), 43.0)
        assert store.get_month_peak(2026, 10) == pytest.approx(43.0)

    async def test_implausible_stored_quarters_are_dropped_on_load(self):
        """Kwartieren die een Wh-sensor vóór v1.8.16 een factor 1000 te hoog
        opsloeg, mogen niet als maandpiek bevroren worden."""
        normal = [
            {"ts": (_utc(2026, 10, 5, 0, 0) + timedelta(minutes=15 * i)).isoformat(), "kw": 3.1}
            for i in range(30)
        ]
        store = _store({"quarters": [
            {"ts": _utc(2026, 9, 20, 10, 0).isoformat(), "kw": 3200.0},
            {"ts": _utc(2026, 10, 3, 10, 0).isoformat(), "kw": 2900.0},
            *normal,
        ]})
        await store.async_load()
        assert [e["kw"] for e in store.get_all_entries()] == [3.1] * 30
        assert store.get_month_peak(2026, 9) is None
        assert store.get_month_peak(2026, 10) == pytest.approx(3.1)
        assert store.get_billed_avg_kw() == pytest.approx(3.1)

    async def test_wrong_unit_history_is_discarded_entirely_on_upgrade(self):
        """
        Een Wh-sensor gaf vóór v1.8.16 ook waarden die toevallig onder de
        bovengrens blijven (nachtverbruik van 60 W → "60 kW"). Is een flink
        deel van de opgeslagen kwartieren onmogelijk, dan is de hele reeks
        onbetrouwbaar en wordt er niets uit overgenomen.
        """
        base = _utc(2026, 10, 3, 10, 0)
        values = [3200.0, 850.0, 60.0, 95.0, 0.0, 1400.0]
        store = _store({"quarters": [
            {"ts": (base + timedelta(minutes=15 * i)).isoformat(), "kw": kw}
            for i, kw in enumerate(values)
        ]})
        await store.async_load()
        assert store.get_all_entries() == []
        assert store.get_monthly_peaks() == []
        assert store.get_billed_avg_kw() == pytest.approx(2.5)

    async def test_single_bad_quarter_does_not_discard_the_history(self):
        base = _utc(2026, 10, 3, 10, 0)
        values = [3.0] * 19 + [3200.0]
        store = _store({"quarters": [
            {"ts": (base + timedelta(minutes=15 * i)).isoformat(), "kw": kw}
            for i, kw in enumerate(values)
        ]})
        await store.async_load()
        assert len(store.get_all_entries()) == 19
        assert store.get_month_peak(2026, 10) == pytest.approx(3.0)
