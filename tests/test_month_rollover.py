"""
tests/test_month_rollover.py — maand- en jaarwissel in SharedCapacityState.

Bij de jaarwissel moet de afgelopen decembermaand eerst afgesloten worden
(onder het oude jaar) en pas daarna het jaartotaal gereset. In de omgekeerde
volgorde lekt de decemberbesparing in het nieuwe jaar.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.peak_guard.avoided_peak_tracker import (
    PeakAvoidTracker,
    SolarShiftTracker,
)
from custom_components.peak_guard.quarter_calculator import QuarterCalculator
from custom_components.peak_guard.quarter_store import QuarterStore
from custom_components.peak_guard.sensor import SharedCapacityState

from tests.conftest import FakeBudgetStore, MockHass

ENERGY_SENSOR = "sensor.energie_kwh"
TARIEF = 120.0  # €/kW/jaar → 10 €/kW/maand


class FakeDeviceSavingsStore:
    def __init__(self) -> None:
        self.upserts: list[dict] = []

    async def async_upsert(self, **kwargs) -> None:
        self.upserts.append(kwargs)


def _shared(device_store: FakeDeviceSavingsStore):
    hass = MockHass()
    hass.states.set(ENERGY_SENSOR, "100")
    store = QuarterStore(hass)
    store._store = FakeBudgetStore()
    peak = PeakAvoidTracker()
    peak.set_tarief(TARIEF)
    shared = SharedCapacityState(
        hass=hass,
        energy_sensor_id=ENERGY_SENSOR,
        store=store,
        calculator=QuarterCalculator(),
        tarief=TARIEF,
        regio="Antwerpen",
        peak_tracker=peak,
        solar_tracker=SolarShiftTracker(),
        savings_store=None,
        solar_savings_store=None,
        peak_state_store=None,
        solar_state_store=None,
        device_savings_store=device_store,
    )
    return shared, peak


def _avoid(peak: PeakAvoidTracker, nominal_kw: float, ts: datetime) -> None:
    peak.record_pending_avoid("boiler", "Boiler", nominal_kw, ts=ts)
    peak.start_measurement_on_turnon("boiler", "Boiler", ts=ts)
    peak.complete_peak_calculation("boiler", now=ts + timedelta(minutes=15))


class TestYearRollover:

    async def test_december_savings_do_not_leak_into_new_year(self):
        device_store = FakeDeviceSavingsStore()
        shared, peak = _shared(device_store)
        dec = datetime(2026, 12, 31, 22, 0, tzinfo=timezone.utc)
        await shared._async_update(dec)
        _avoid(peak, nominal_kw=5.0, ts=dec)          # 5,0 − 2,5 = 2,5 kW → €25
        assert peak.savings_euro_this_year == pytest.approx(25.0)

        await shared._async_update(datetime(2027, 1, 1, 0, 1, tzinfo=timezone.utc))

        assert peak.savings_euro_this_month == 0.0
        assert peak.savings_euro_this_year == 0.0

    async def test_december_device_savings_are_frozen_under_the_old_year(self):
        device_store = FakeDeviceSavingsStore()
        shared, peak = _shared(device_store)
        dec = datetime(2026, 12, 31, 22, 0, tzinfo=timezone.utc)
        await shared._async_update(dec)
        _avoid(peak, nominal_kw=5.0, ts=dec)

        await shared._async_update(datetime(2027, 1, 1, 0, 1, tzinfo=timezone.utc))

        frozen = [u for u in device_store.upserts if u["finalized"]]
        assert [(u["year"], u["month"]) for u in frozen] == [(2026, 12)]
        assert frozen[0]["savings_euro"] == pytest.approx(25.0)

    async def test_ordinary_month_change_keeps_year_total(self):
        device_store = FakeDeviceSavingsStore()
        shared, peak = _shared(device_store)
        nov = datetime(2026, 11, 30, 22, 0, tzinfo=timezone.utc)
        await shared._async_update(nov)
        _avoid(peak, nominal_kw=5.0, ts=nov)

        await shared._async_update(datetime(2026, 12, 1, 0, 1, tzinfo=timezone.utc))

        assert peak.savings_euro_this_month == 0.0
        assert peak.savings_euro_this_year == pytest.approx(25.0)
