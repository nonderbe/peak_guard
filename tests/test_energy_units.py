"""
tests/test_energy_units.py — de energiesensor mag ook in Wh of MWh tellen.

De kwartierpiek wordt berekend uit een cumulatieve energiesensor in kWh.
Het configuratiescherm laat elke energiesensor toe; een sensor in Wh of MWh
wordt daarom omgerekend in plaats van een factor 1000 naast te zitten.
"""
from __future__ import annotations

import pytest

from custom_components.peak_guard.deciders.base import read_energy_kwh

from tests.test_month_rollover import ENERGY_SENSOR, FakeDeviceSavingsStore, _shared

SENSOR = "sensor.p1_energie"


class TestReadEnergyKwh:

    def test_wh_sensor_is_converted_to_kwh(self, hass):
        hass.states.set(SENSOR, "12345678", {"unit_of_measurement": "Wh"})
        assert read_energy_kwh(hass, SENSOR) == pytest.approx(12345.678)

    def test_mwh_sensor_is_converted_to_kwh(self, hass):
        hass.states.set(SENSOR, "12.345678", {"unit_of_measurement": "MWh"})
        assert read_energy_kwh(hass, SENSOR) == pytest.approx(12345.678)

    def test_kwh_sensor_is_unchanged(self, hass):
        hass.states.set(SENSOR, "12345.678", {"unit_of_measurement": "kWh"})
        assert read_energy_kwh(hass, SENSOR) == pytest.approx(12345.678)

    def test_sensor_without_unit_is_read_as_kwh(self, hass):
        hass.states.set(SENSOR, "12345.678")
        assert read_energy_kwh(hass, SENSOR) == pytest.approx(12345.678)

    def test_unavailable_sensor_gives_none(self, hass):
        hass.states.set(SENSOR, "unavailable", {"unit_of_measurement": "Wh"})
        assert read_energy_kwh(hass, SENSOR) is None

    def test_missing_entity_id_gives_none(self, hass):
        assert read_energy_kwh(hass, None) is None


class TestSharedStateReadsEnergyInKwh:

    def test_wh_energy_sensor_is_converted(self):
        shared, _ = _shared(FakeDeviceSavingsStore())
        shared.hass.states.set(ENERGY_SENSOR, "100000", {"unit_of_measurement": "Wh"})
        assert shared._read_energy() == pytest.approx(100.0)
