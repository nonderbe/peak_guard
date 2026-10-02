"""
tests/test_peak_verification.py — foute maandpieken worden automatisch verwijderd.

De P1-meter is de referentie: geen enkel afgesloten kwartier van de lopende
maand kan hoger zijn dan de maandpiek die de meter zelf rapporteert. Een
eigen kwartierwaarde die daar duidelijk boven ligt is een meetfout. Ze wordt
verwijderd en het maandpiek-record wordt opnieuw opgebouwd uit de overige
kwartieren, zonder tussenkomst van de gebruiker.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from homeassistant.util import dt as dt_util

from custom_components.peak_guard.avoided_peak_tracker import (
    PeakAvoidTracker,
    SolarShiftTracker,
)
from custom_components.peak_guard.quarter_calculator import QuarterCalculator
from custom_components.peak_guard.quarter_store import QuarterStore
from custom_components.peak_guard.sensor import SharedCapacityState

from tests.conftest import MockHass
from tests.test_month_rollover import FakeDeviceSavingsStore
from tests.test_monthly_peak_history import FakeStore

ENERGY_SENSOR = "sensor.energie_kwh"
PEAK_SENSOR = "sensor.p1_maandpiek"


def _utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


NOW = _utc(2026, 10, 15, 12, 0)


@pytest.fixture(autouse=True)
def now_oct_2026(monkeypatch):
    monkeypatch.setattr(dt_util, "utcnow", lambda: NOW)


async def _store(*quarters) -> QuarterStore:
    store = QuarterStore(MockHass())
    store._store = FakeStore()
    for ts, kw in quarters:
        await store.add_quarter(ts, kw)
    store._store.saved.clear()
    return store


class TestRemoveUnconfirmedPeaks:

    async def test_quarter_far_above_meter_peak_is_removed(self):
        store = await _store(
            (_utc(2026, 10, 3, 10, 0), 3.0),
            (_utc(2026, 10, 9, 18, 0), 11.08),     # meetfout
            (_utc(2026, 10, 10, 8, 0), 3.4),
        )
        removed = await store.async_remove_unconfirmed_peaks(3.5, NOW)
        assert [e["kw"] for e in removed] == [11.08]
        assert [e["kw"] for e in store.get_all_entries()] == [3.0, 3.4]

    async def test_month_record_is_rebuilt_from_remaining_quarters(self):
        store = await _store(
            (_utc(2026, 10, 3, 10, 0), 3.0),
            (_utc(2026, 10, 9, 18, 0), 11.08),
            (_utc(2026, 10, 10, 8, 0), 3.4),
        )
        await store.async_remove_unconfirmed_peaks(3.5, NOW)
        peak = store.get_monthly_peaks()[-1]
        assert peak["kw"] == pytest.approx(3.4)
        assert peak["ts"] == _utc(2026, 10, 10, 8, 0).isoformat()
        assert store._store.saved[-1]["monthly_peaks"][-1]["kw"] == pytest.approx(3.4)

    async def test_several_bad_quarters_are_removed_at_once(self):
        store = await _store(
            (_utc(2026, 10, 3, 10, 0), 9.0),
            (_utc(2026, 10, 9, 18, 0), 11.0),
            (_utc(2026, 10, 10, 8, 0), 3.4),
        )
        removed = await store.async_remove_unconfirmed_peaks(3.5, NOW)
        assert len(removed) == 2
        assert store.get_month_peak(2026, 10) == pytest.approx(3.4)

    async def test_quarter_within_tolerance_is_kept(self):
        """Eigen meting 4,5 kW tegenover 4,0 kW op de meter: binnen de marge
        (de eigen waarde is een schatting uit minuutmetingen)."""
        store = await _store((_utc(2026, 10, 9, 18, 0), 4.5))
        assert await store.async_remove_unconfirmed_peaks(4.0, NOW) == []
        assert store.get_month_peak(2026, 10) == pytest.approx(4.5)
        assert store._store.saved == []

    async def test_quarter_just_outside_tolerance_is_removed(self):
        """Grens = 4,0 × 1,15 + 0,25 = 4,85 kW."""
        store = await _store((_utc(2026, 10, 9, 18, 0), 4.9))
        assert len(await store.async_remove_unconfirmed_peaks(4.0, NOW)) == 1

    async def test_recent_quarter_is_not_judged_yet(self):
        """De meter heeft tijd nodig om een pas afgesloten kwartier te melden:
        een kwartier dat minder dan 10 minuten geleden eindigde blijft staan."""
        store = await _store((NOW - timedelta(minutes=20), 11.0))   # eindigde 5 min geleden
        assert await store.async_remove_unconfirmed_peaks(3.5, NOW) == []
        assert store.get_month_peak(2026, 10) == pytest.approx(11.0)

    async def test_quarter_is_judged_once_the_meter_had_time(self):
        store = await _store((NOW - timedelta(minutes=25), 11.0))   # eindigde 10 min geleden
        assert len(await store.async_remove_unconfirmed_peaks(3.5, NOW)) == 1

    async def test_previous_month_is_not_touched(self):
        """De meter kent alleen de piek van de lopende maand."""
        store = await _store(
            (_utc(2026, 9, 20, 18, 0), 6.0),
            (_utc(2026, 10, 3, 10, 0), 3.0),
        )
        assert await store.async_remove_unconfirmed_peaks(3.2, NOW) == []
        assert store.get_month_peak(2026, 9) == pytest.approx(6.0)

    async def test_record_without_backing_quarter_is_removed(self):
        """Een fout record waarvan het kwartier niet meer in de historiek zit."""
        store = await _store((_utc(2026, 10, 3, 10, 0), 3.0))
        store._monthly_peaks[(2026, 10)] = {
            "ts": _utc(2026, 10, 2, 9, 0).isoformat(), "kw": 40.0,
        }
        removed = await store.async_remove_unconfirmed_peaks(3.5, NOW)
        assert [e["kw"] for e in removed] == [40.0]
        assert store.get_month_peak(2026, 10) == pytest.approx(3.0)

    async def test_month_record_disappears_when_no_valid_quarter_remains(self):
        store = await _store((_utc(2026, 10, 9, 18, 0), 11.0))
        await store.async_remove_unconfirmed_peaks(3.5, NOW)
        assert store.get_month_peak(2026, 10) is None
        assert store.get_monthly_peaks() == []

    async def test_removal_is_logged(self, caplog):
        store = await _store((_utc(2026, 10, 9, 18, 0), 11.08))
        with caplog.at_level("WARNING"):
            await store.async_remove_unconfirmed_peaks(3.5, NOW)
        messages = [r.getMessage() for r in caplog.records]
        assert any("11.08" in m and "3.50" in m for m in messages)


def _shared(store: QuarterStore, peak_sensor_id=PEAK_SENSOR):
    hass = MockHass()
    hass.states.set(ENERGY_SENSOR, "100", {"unit_of_measurement": "kWh"})
    peak = PeakAvoidTracker()
    peak.set_tarief(120.0)
    shared = SharedCapacityState(
        hass=hass,
        energy_sensor_id=ENERGY_SENSOR,
        store=store,
        calculator=QuarterCalculator(),
        tarief=120.0,
        regio="Antwerpen",
        peak_tracker=peak,
        solar_tracker=SolarShiftTracker(),
        savings_store=None,
        solar_savings_store=None,
        peak_state_store=None,
        solar_state_store=None,
        device_savings_store=FakeDeviceSavingsStore(),
        peak_sensor_id=peak_sensor_id,
    )
    return shared, hass


async def _bad_store() -> QuarterStore:
    return await _store(
        (_utc(2026, 10, 3, 10, 0), 3.0),
        (_utc(2026, 10, 9, 18, 0), 11.08),
    )


class TestSensorUpdateVerifiesAgainstMeter:

    async def test_bad_peak_is_removed_during_the_sensor_update(self):
        shared, hass = _shared(await _bad_store())
        hass.states.set(PEAK_SENSOR, "3500", {"unit_of_measurement": "W"})
        await shared._async_update(NOW)
        assert shared.monthly_peak_kw == pytest.approx(3.0)
        assert shared.billed_peak_kw == pytest.approx(3.0)

    async def test_meter_peak_in_kw_is_understood(self):
        shared, hass = _shared(await _bad_store())
        hass.states.set(PEAK_SENSOR, "3.5", {"unit_of_measurement": "kW"})
        await shared._async_update(NOW)
        assert shared.monthly_peak_kw == pytest.approx(3.0)

    async def test_unavailable_meter_peak_removes_nothing(self):
        shared, hass = _shared(await _bad_store())
        hass.states.set(PEAK_SENSOR, "unavailable")
        await shared._async_update(NOW)
        assert shared.monthly_peak_kw == pytest.approx(11.08)

    async def test_without_peak_sensor_nothing_is_removed(self):
        shared, _ = _shared(await _bad_store(), peak_sensor_id=None)
        await shared._async_update(NOW)
        assert shared.monthly_peak_kw == pytest.approx(11.08)

    async def test_own_monthly_peak_sensor_is_not_used_as_reference(self):
        """Peak Guards eigen maandpiek-sensor als referentie zou een cirkel
        zijn: de fout bevestigt dan zichzelf. Dan wordt er niet gecontroleerd."""
        shared, hass = _shared(
            await _bad_store(), peak_sensor_id="sensor.peak_guard_monthly_peak_kw",
        )
        hass.states.set("sensor.peak_guard_monthly_peak_kw", "0.1",
                        {"unit_of_measurement": "kW"})
        await shared._async_update(NOW)
        assert shared.monthly_peak_kw == pytest.approx(11.08)
