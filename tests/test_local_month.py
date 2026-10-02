"""
tests/test_local_month.py — maand- en jaargrenzen volgen de lokale tijd.

Fluvius en de P1-meter rekenen het capaciteitstarief per kalendermaand in
Belgische tijd. Tijdstempels blijven in UTC opgeslagen; enkel de toewijzing
aan een maand gebeurt in lokale tijd (in de tests: Europe/Brussels).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from homeassistant.util import dt as dt_util

from custom_components.peak_guard.quarter_store import QuarterStore
from custom_components.peak_guard.sensor import SharedCapacityState
from custom_components.peak_guard.utils import local_year_month

from tests.conftest import MockHass
from tests.test_month_rollover import FakeDeviceSavingsStore, _avoid, _shared


def _utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


class TestLocalYearMonth:

    def test_summer_last_two_utc_hours_belong_to_next_month(self):
        """30 sep 22:30 UTC = 1 okt 00:30 in België (zomertijd, UTC+2)."""
        assert local_year_month(_utc(2026, 9, 30, 22, 30)) == (2026, 10)

    def test_summer_before_local_midnight_is_still_old_month(self):
        """30 sep 21:30 UTC = 30 sep 23:30 in België."""
        assert local_year_month(_utc(2026, 9, 30, 21, 30)) == (2026, 9)

    def test_winter_last_utc_hour_belongs_to_next_year(self):
        """31 dec 23:30 UTC = 1 jan 00:30 in België (wintertijd, UTC+1)."""
        assert local_year_month(_utc(2026, 12, 31, 23, 30)) == (2027, 1)

    def test_winter_before_local_midnight_is_still_old_year(self):
        assert local_year_month(_utc(2026, 12, 31, 22, 30)) == (2026, 12)


def _store(*entries) -> QuarterStore:
    store = QuarterStore(MockHass())
    for ts, kw in entries:
        store._entries.append({"ts": ts.isoformat(), "kw": kw})
    return store


class TestQuarterStoreLocalMonth:

    def test_quarter_after_local_midnight_counts_for_new_month(self):
        store = _store((_utc(2026, 9, 30, 22, 30), 4.0))
        assert store.get_month_peak(2026, 10) == pytest.approx(4.0)
        assert store.get_month_peak(2026, 9) is None

    def test_quarter_before_local_midnight_counts_for_old_month(self):
        store = _store((_utc(2026, 9, 30, 21, 45), 4.0))
        assert store.get_month_peak(2026, 9) == pytest.approx(4.0)
        assert store.get_month_peak(2026, 10) is None

    def test_current_month_peak_follows_local_now(self, monkeypatch):
        """Om 00:10 lokale tijd op 1 okt telt de septemberpiek niet meer mee."""
        monkeypatch.setattr(dt_util, "utcnow", lambda: _utc(2026, 9, 30, 22, 10))
        store = _store(
            (_utc(2026, 9, 15, 10, 0), 5.0),      # september
            (_utc(2026, 9, 30, 22, 0), 1.2),      # 1 okt 00:00 lokaal
        )
        assert store.get_current_month_peak() == pytest.approx(1.2)

    def test_last_12_months_groups_by_local_month(self, monkeypatch):
        monkeypatch.setattr(dt_util, "utcnow", lambda: _utc(2026, 10, 2, 12, 0))
        store = _store(
            (_utc(2026, 9, 30, 21, 45), 3.0),     # 30 sep 23:45 lokaal
            (_utc(2026, 9, 30, 22, 0), 4.0),      # 1 okt 00:00 lokaal
        )
        peaks = store.get_monthly_peaks_last_12()
        assert [(p["year"], p["month"], p["kw"]) for p in peaks] == [
            (2026, 9, 3.0), (2026, 10, 4.0),
        ]


class TestRolloverAtLocalMidnight:

    async def test_month_closes_at_local_midnight(self):
        device_store = FakeDeviceSavingsStore()
        shared, peak = _shared(device_store)
        before = _utc(2026, 9, 30, 21, 45)        # 23:45 lokaal
        await shared._async_update(before)
        _avoid(peak, nominal_kw=5.0, ts=before)
        assert peak.savings_euro_this_month == pytest.approx(25.0)

        await shared._async_update(_utc(2026, 9, 30, 22, 1))   # 00:01 lokaal

        assert peak.savings_euro_this_month == 0.0
        assert peak.savings_euro_this_year == pytest.approx(25.0)
        frozen = [u for u in device_store.upserts if u["finalized"]]
        assert [(u["year"], u["month"]) for u in frozen] == [(2026, 9)]

    async def test_month_does_not_close_at_utc_midnight(self):
        """1 okt 00:01 UTC is 02:01 lokaal: de maand is dan al lang gewisseld,
        een tweede wissel mag er niet komen."""
        device_store = FakeDeviceSavingsStore()
        shared, peak = _shared(device_store)
        after_local_midnight = _utc(2026, 9, 30, 22, 30)       # 00:30 lokaal
        await shared._async_update(after_local_midnight)
        _avoid(peak, nominal_kw=5.0, ts=after_local_midnight)

        await shared._async_update(_utc(2026, 10, 1, 0, 1))

        assert peak.savings_euro_this_month == pytest.approx(25.0)
        assert [u for u in device_store.upserts if u["finalized"]] == []

    async def test_year_closes_at_local_midnight(self):
        device_store = FakeDeviceSavingsStore()
        shared, peak = _shared(device_store)
        before = _utc(2026, 12, 31, 22, 45)       # 23:45 lokaal
        await shared._async_update(before)
        _avoid(peak, nominal_kw=5.0, ts=before)
        assert peak.savings_euro_this_year == pytest.approx(25.0)

        await shared._async_update(_utc(2026, 12, 31, 23, 1))  # 00:01 lokaal

        assert peak.savings_euro_this_year == 0.0
        frozen = [u for u in device_store.upserts if u["finalized"]]
        assert [(u["year"], u["month"]) for u in frozen] == [(2026, 12)]

    async def test_running_month_is_persisted_under_local_month(self):
        device_store = FakeDeviceSavingsStore()
        shared, peak = _shared(device_store)
        now = _utc(2026, 9, 30, 22, 30)           # 1 okt 00:30 lokaal
        await shared._async_update(now)
        _avoid(peak, nominal_kw=5.0, ts=now)

        await shared._async_update(now + timedelta(minutes=1))

        running = [u for u in device_store.upserts if not u["finalized"]]
        assert running and {(u["year"], u["month"]) for u in running} == {(2026, 10)}


class TestEntryInCurrentMonth:

    def test_entry_after_local_midnight_is_in_current_month(self):
        now = _utc(2026, 9, 30, 22, 20)
        entry = {"ts": _utc(2026, 9, 30, 22, 0).isoformat(), "kw": 1.0}
        assert SharedCapacityState._entry_in_current_month(entry, now) is True

    def test_entry_before_local_midnight_is_not_in_current_month(self):
        now = _utc(2026, 9, 30, 22, 20)
        entry = {"ts": _utc(2026, 9, 30, 21, 45).isoformat(), "kw": 1.0}
        assert SharedCapacityState._entry_in_current_month(entry, now) is False


class TestEventTableTimestamps:

    def test_event_time_is_shown_in_local_time(self):
        """14:03 UTC op 21 juli is 16:03 in België."""
        from custom_components.peak_guard.sensor import OverviewRecentEventsSensor
        iso = _utc(2026, 7, 21, 14, 3).isoformat()
        assert OverviewRecentEventsSensor._fmt_ts(iso) == "21 jul 16:03"

    def test_event_just_after_local_midnight_shows_next_day(self):
        from custom_components.peak_guard.sensor import OverviewRecentEventsSensor
        iso = _utc(2026, 9, 30, 22, 30).isoformat()
        assert OverviewRecentEventsSensor._fmt_ts(iso) == "1 okt 00:30"
