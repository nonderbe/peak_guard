"""
tests/test_power_units.py — vermogenssensoren in kW worden omgerekend naar W.

Peak Guard rekent in W. De verbruiks- en de maandpiek-sensor mogen ook in kW
rapporteren (bv. de DSMR-integratie voor Belgische meters): de waarde wordt
dan × 1000 gedaan in plaats van als een handvol watt gelezen te worden.
"""
from __future__ import annotations

import pytest

from custom_components.peak_guard.deciders.base import read_power_w

from tests.test_peak_floor import PEAK_SENSOR as SENSOR, FakeDevice, _decider, _logged_text


class TestReadPowerW:

    def test_kw_sensor_is_converted_to_watt(self, hass):
        hass.states.set(SENSOR, "3.2", {"unit_of_measurement": "kW"})
        assert read_power_w(hass, SENSOR) == pytest.approx(3200.0)

    def test_negative_kw_value_keeps_its_sign(self, hass):
        """Injectie: −1,5 kW moet −1500 W worden."""
        hass.states.set(SENSOR, "-1.5", {"unit_of_measurement": "kW"})
        assert read_power_w(hass, SENSOR) == pytest.approx(-1500.0)

    def test_unit_is_matched_case_insensitively(self, hass):
        hass.states.set(SENSOR, "3.2", {"unit_of_measurement": " KW "})
        assert read_power_w(hass, SENSOR) == pytest.approx(3200.0)

    def test_watt_sensor_is_unchanged(self, hass):
        hass.states.set(SENSOR, "3200", {"unit_of_measurement": "W"})
        assert read_power_w(hass, SENSOR) == pytest.approx(3200.0)

    def test_sensor_without_unit_is_read_as_watt(self, hass):
        hass.states.set(SENSOR, "3200")
        assert read_power_w(hass, SENSOR) == pytest.approx(3200.0)

    def test_unavailable_sensor_gives_none(self, hass):
        hass.states.set(SENSOR, "unavailable", {"unit_of_measurement": "kW"})
        assert read_power_w(hass, SENSOR) is None

    def test_missing_entity_id_gives_none(self, hass):
        assert read_power_w(hass, None) is None


class TestPeakDeciderWithKwSensor:

    async def test_cascade_uses_converted_peak(self, hass, ev_guard):
        """Piek 3,2 kW → grens 3100 W, niet de 2,5 kW-ondergrens."""
        hass.states.set(SENSOR, "3.2", {"unit_of_measurement": "kW"})
        device = FakeDevice()
        decider = _decider(hass, ev_guard, device)
        await decider.check(3000.0)
        assert device.applied_excess == []
        await decider.check(3300.0)
        assert device.applied_excess == [pytest.approx(200.0)]

    async def test_restore_uses_converted_peak(self, hass, ev_guard):
        """Headroom = 4200 − 100 − 3000 = 1100 W ≥ 1000 W → herstellen."""
        hass.states.set(SENSOR, "4.2", {"unit_of_measurement": "kW"})
        device = FakeDevice(power_watts=1000.0)
        snapshots = {device.entity_id: object()}
        await _decider(hass, ev_guard, device, snapshots).check_restore(3000.0)
        assert device.restored == 1


class TestDecisionLogWithKwSensor:

    async def test_log_shows_converted_peak(self, hass, ev_guard, tmp_path):
        hass.states.set(SENSOR, "3.2", {"unit_of_measurement": "kW"})
        text = await _logged_text(hass, ev_guard, tmp_path, 2000.0)
        assert "Maandpiek:         3200 W" in text
        assert "Target peak:       3100 W" in text
