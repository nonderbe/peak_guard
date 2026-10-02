"""
tests/test_quarter_calculator.py — kwartierberekening bij een terugspringende meterstand.

Een cumulatieve energiesensor die even een lagere waarde geeft (bv. een
template-sensor die dag- en nachtregister optelt terwijl één register kort
onbeschikbaar is) en daarna terugspringt, mag geen reusachtige kwartierpiek
opleveren: dat kwartier is onbetrouwbaar en wordt niet afgesloten.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from custom_components.peak_guard.deciders.base import read_sensor
from custom_components.peak_guard.quarter_calculator import QuarterCalculator


def _t(minute: int, second: int = 0, hour: int = 10) -> datetime:
    return datetime(2026, 10, 3, hour, minute, second, tzinfo=timezone.utc)


class TestNormalQuarter:

    def test_quarter_closes_with_its_average_power(self):
        """1 kWh in 15 minuten = 4 kW."""
        calc = QuarterCalculator()
        calc.update(100.0, _t(0))
        assert calc.update(101.0, _t(14, 59)) == pytest.approx(4.0, abs=0.01)
        calc.update(101.0, _t(15))
        assert calc.quarter_just_finished is True
        assert calc.last_finished_value == pytest.approx(4.0, abs=0.01)
        assert calc.last_finished_ts == _t(0)


class TestMeterDropAndRecover:

    def _dropped(self) -> QuarterCalculator:
        calc = QuarterCalculator()
        calc.update(45000.0, _t(0))
        calc.update(45000.1, _t(5))
        calc.update(25000.0, _t(6))            # register kort onbeschikbaar
        return calc

    def test_recovery_does_not_produce_a_huge_running_value(self):
        calc = self._dropped()
        assert calc.update(45000.2, _t(7)) == 0.0
        assert calc.current_kw == 0.0

    def test_unreliable_quarter_is_not_closed(self):
        calc = self._dropped()
        calc.update(45000.2, _t(7))
        calc.update(45000.4, _t(15))
        assert calc.quarter_just_finished is False

    def test_next_quarter_is_measured_normally_again(self):
        calc = self._dropped()
        calc.update(45000.2, _t(7))
        calc.update(45000.4, _t(15))
        assert calc.update(45001.4, _t(29, 59)) == pytest.approx(4.0, abs=0.01)
        calc.update(45001.4, _t(30))
        assert calc.quarter_just_finished is True
        assert calc.last_finished_value == pytest.approx(4.0, abs=0.01)
        assert calc.last_finished_ts == _t(15)


class TestNonFiniteSensorState:

    @pytest.mark.parametrize("state", ["nan", "inf", "-inf"])
    def test_non_finite_state_is_treated_as_unavailable(self, hass, state):
        hass.states.set("sensor.x", state)
        assert read_sensor(hass, "sensor.x") is None


class TestImplausibleRunningValue:
    """
    Is de eerste meting van een kwartier zelf de foute (te lage) waarde, dan
    is er geen negatieve delta: de sprong terug omhoog lijkt gewoon verbruik.
    """

    def _glitch_at_quarter_start(self) -> QuarterCalculator:
        calc = QuarterCalculator()
        calc.update(45000.0, _t(0))
        calc.update(25000.0, _t(15))           # eerste meting van het kwartier
        return calc

    def test_impossible_running_value_is_not_reported(self):
        calc = self._glitch_at_quarter_start()
        assert calc.update(45000.2, _t(17)) == 0.0
        assert calc.current_kw == 0.0

    def test_quarter_with_impossible_value_is_not_closed(self):
        calc = self._glitch_at_quarter_start()
        calc.update(45000.2, _t(17))
        calc.update(45000.4, _t(30))
        assert calc.quarter_just_finished is False

    def test_coarse_sensor_tick_early_in_quarter_does_not_void_it(self):
        """Een sensor met stappen van 0,2 kWh die 5 s na het kwartierbegin
        verspringt geeft kort 144 kW; dat is geen meetfout."""
        calc = QuarterCalculator()
        calc.update(100.0, _t(0))
        assert calc.update(100.2, _t(0, 5)) == 0.0
        assert calc.update(101.0, _t(14, 59)) == pytest.approx(4.0, abs=0.01)
        calc.update(101.0, _t(15))
        assert calc.quarter_just_finished is True
        assert calc.last_finished_value == pytest.approx(4.0, abs=0.01)
