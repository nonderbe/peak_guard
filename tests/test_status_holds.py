"""
tests/test_status_holds.py — verklaringen voor toestanden die onlogisch lijken.

De status-popup toont per apparaat waarom het in zijn toestand blijft (bv. een
EV die laadt zonder zichtbaar overschot). Daarnaast de stuurfouten die met die
verklaringen aan het licht kwamen: een schakelaar die klappert, een groot
apparaat dat kleinere blokkeert bij het herstel, en een EV die na een piek in
één keer naar zijn originele stroom springt.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.peak_guard.deciders.dispatch import run_tick
from custom_components.peak_guard.deciders.ev_guard import EVGuard
from custom_components.peak_guard.deciders.injection_decider import InjectionDecider
from custom_components.peak_guard.deciders.peak_decider import PeakDecider
from custom_components.peak_guard.models import (
    CascadeContext,
    DeviceSnapshot,
    EVChargerDevice,
    SwitchOffDevice,
    SwitchOnDevice,
    EV_MIN_OFF_DURATION_S,
)

from tests.conftest import MockHass, MockPeakTracker, MockSolarTracker

PEAK_SENSOR = "sensor.p1_maandpiek"
NOW = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)


async def _save():
    return None


def _ev(**kw) -> EVChargerDevice:
    base = dict(
        id="ev", name="Tesla", entity_id="switch.tesla_charge", priority=2,
        action_type="ev_charger", min_value=6.0, max_value=16.0,
        switch_entity="switch.tesla_charge", current_entity="number.tesla_charge_current",
        phases=1, min_current=6.0,
    )
    base.update(kw)
    return EVChargerDevice(**base)


def _switch_on(power=2000.0, priority=1) -> SwitchOnDevice:
    return SwitchOnDevice(id="boiler", name="Boiler", entity_id="switch.boiler",
                          priority=priority, action_type="switch_on", power_watts=power)


def _switch_off(dev_id, power, priority) -> SwitchOffDevice:
    return SwitchOffDevice(id=dev_id, name=dev_id, entity_id=f"switch.{dev_id}",
                           priority=priority, action_type="switch_off", power_watts=power)


def _injection(hass, ev_guard, cascade, snapshots):
    return InjectionDecider(
        hass=hass, config={}, peak_tracker=MockPeakTracker(),
        solar_tracker=MockSolarTracker(), ev_guard=ev_guard, iteration_actions=[],
        save_fn=_save, cascade=cascade, snapshots=snapshots,
    )


def _peak(hass, ev_guard, cascade, snapshots):
    return PeakDecider(
        hass=hass, config={"peak_sensor": PEAK_SENSOR, "buffer_watts": 100},
        peak_tracker=MockPeakTracker(), solar_tracker=MockSolarTracker(),
        ev_guard=ev_guard, iteration_actions=[], save_fn=_save,
        cascade=cascade, snapshots=snapshots,
    )


@pytest.fixture
def hass() -> MockHass:
    return MockHass()


@pytest.fixture
def ev_guard(hass) -> EVGuard:
    return EVGuard(hass=hass, config={}, iteration_actions=[])


# ═══════════════════════════════════════════════════════════════════════════ #
#  EV laadt door zonder overschot                                             #
# ═══════════════════════════════════════════════════════════════════════════ #

class TestSolarHolds:

    async def test_charging_at_minimum_explains_the_solar_share(self, hass, ev_guard):
        """Het voorbeeld van de gebruiker: laden zonder overschot heeft een reden."""
        hass.states.set("switch.tesla_charge", "on")
        hass.states.set("number.tesla_charge_current", "6")
        assert await ev_guard.throttle_down_solar(_ev(), consumption=500.0, now=NOW)
        hold = ev_guard.hold_for("ev")
        assert hold["code"] == "solar_share"
        assert "880 W" in hold["text"]          # 1380 W laadvermogen − 500 W netafname
        assert ev_guard.status_dict()["holds"]["ev"]["code"] == "solar_share"

    async def test_update_interval_is_explained_with_a_countdown(self, hass, ev_guard):
        hass.states.set("number.tesla_charge_current", "16")
        guard = ev_guard.get_guard("ev")
        guard.last_current_update = datetime.now(timezone.utc) - timedelta(seconds=5)
        assert await ev_guard.throttle_down_solar(_ev(), consumption=1000.0)
        hold = ev_guard.status_dict()["holds"]["ev"]
        assert hold["code"] == "update_interval"
        assert 0 < hold["remaining_s"] <= 15

    async def test_begin_tick_clears_old_explanations(self, ev_guard):
        ev_guard.set_hold("ev", "solar_share", "x")
        ev_guard.get_guard("ev").skip_reason = "min OFF-duur: 10s < 300s"
        ev_guard.begin_tick()
        assert ev_guard.hold_for("ev") is None
        assert ev_guard.get_guard("ev").skip_reason == ""


# ═══════════════════════════════════════════════════════════════════════════ #
#  Inject-cascade                                                             #
# ═══════════════════════════════════════════════════════════════════════════ #

class TestInjection:

    async def test_switch_stays_off_when_surplus_does_not_cover_it(self, hass, ev_guard):
        """Aanzetten bij 500 W overschot voor 2 kW zou elke cyclus klapperen."""
        hass.states.set("switch.boiler", "off")
        snapshots = {}
        await _injection(hass, ev_guard, [_switch_on(2000.0)], snapshots).check(-500.0, NOW)
        assert not hass.services.has_call("turn_on")
        assert snapshots == {}
        assert ev_guard.hold_for("boiler")["code"] == "surplus_too_small"

    async def test_switch_turns_on_when_surplus_covers_it(self, hass, ev_guard):
        hass.states.set("switch.boiler", "off")
        snapshots = {}
        await _injection(hass, ev_guard, [_switch_on(2000.0)], snapshots).check(-2100.0, NOW)
        assert hass.services.has_call("turn_on")
        assert "switch.boiler" in snapshots

    async def test_held_ev_lets_the_next_device_switch_off(self, hass, ev_guard):
        """EV laadt op zijn minimum door; de boiler (hogere prioriteit) gaat wel uit."""
        hass.states.set("switch.tesla_charge", "on")
        hass.states.set("number.tesla_charge_current", "6")
        hass.states.set("switch.boiler", "on")
        snapshots = {
            "switch.tesla_charge": DeviceSnapshot("switch.tesla_charge", "off", 6.0, None),
            "switch.boiler": DeviceSnapshot("switch.boiler", "off"),
        }
        ev = _ev(priority=2)
        boiler = _switch_on(2000.0, priority=1)
        await _injection(hass, ev_guard, [boiler, ev], snapshots).check_restore(500.0, NOW)
        turn_offs = hass.services.calls_for("turn_off")
        assert [c["data"]["entity_id"] for c in turn_offs] == ["switch.boiler"]
        assert "switch.boiler" not in snapshots
        assert "switch.tesla_charge" in snapshots
        assert ev_guard.hold_for("ev")["code"] == "solar_share"

    async def test_waiting_devices_get_a_queue_explanation(self, hass, ev_guard):
        hass.states.set("switch.boiler", "on")
        hass.states.set("switch.pump", "on")
        pump = SwitchOnDevice(id="pump", name="Pomp", entity_id="switch.pump", priority=2,
                              action_type="switch_on", power_watts=500.0)
        snapshots = {
            "switch.boiler": DeviceSnapshot("switch.boiler", "off"),
            "switch.pump": DeviceSnapshot("switch.pump", "off"),
        }
        await _injection(hass, ev_guard, [_switch_on(), pump], snapshots).check_restore(100.0, NOW)
        assert "switch.pump" not in snapshots
        assert ev_guard.hold_for("boiler")["code"] == "restore_queue"

    async def test_running_ev_claims_only_its_extra_current(self, hass, ev_guard):
        """Een EV die al laadt, mag niet zijn volledige verbruik van het overschot aftrekken."""
        hass.states.set("switch.tesla_charge", "on")
        hass.states.set("number.tesla_charge_current", "8")
        guard = ev_guard.get_guard("ev")
        guard.last_switch_state = True
        snapshots = {"switch.tesla_charge": DeviceSnapshot("switch.tesla_charge", "off", 6.0, None)}
        remaining = await ev_guard.apply_action(
            _ev(max_value=10.0), 1000.0, snapshots, "solar",
            MockPeakTracker(), MockSolarTracker(), now=NOW,
        )
        # 8 A → 10 A (max): de EV neemt 2 × 230 W, de rest blijft voor de cascade.
        assert remaining == pytest.approx(1000.0 - 2 * 230.0)


# ═══════════════════════════════════════════════════════════════════════════ #
#  Herstel na piekbeperking                                                   #
# ═══════════════════════════════════════════════════════════════════════════ #

class TestPeakRestore:

    async def test_big_device_does_not_block_a_smaller_one(self, hass, ev_guard):
        hass.states.set(PEAK_SENSOR, "5000")
        hass.states.set("switch.big", "off")
        hass.states.set("switch.small", "off")
        # Het grote apparaat heeft de laagste prioriteit en wordt dus eerst geprobeerd.
        big, small = _switch_off("big", 3000.0, 2), _switch_off("small", 500.0, 1)
        snapshots = {
            "switch.big": DeviceSnapshot("switch.big", "on"),
            "switch.small": DeviceSnapshot("switch.small", "on"),
        }
        # Headroom = 5000 − 100 − 4000 = 900 W: klein past, groot niet.
        await _peak(hass, ev_guard, [big, small], snapshots).check_restore(4000.0, NOW)
        assert [c["data"]["entity_id"] for c in hass.services.calls_for("turn_on")] == ["switch.small"]
        hold = ev_guard.hold_for("big")
        assert hold["code"] == "peak_headroom"
        assert "3000 W" in hold["text"]

    async def test_no_headroom_is_explained(self, hass, ev_guard):
        hass.states.set(PEAK_SENSOR, "3000")
        hass.states.set("switch.big", "off")
        snapshots = {"switch.big": DeviceSnapshot("switch.big", "on")}
        await _peak(hass, ev_guard, [_switch_off("big", 1000.0, 1)], snapshots).check_restore(
            2950.0, NOW)
        assert ev_guard.hold_for("big")["code"] == "peak_no_headroom"

    async def test_reduced_ev_is_raised_stepwise(self, hass, ev_guard):
        """Na een verlaging 16 → 8 A niet in één keer terug naar 16 A."""
        hass.states.set(PEAK_SENSOR, "5000")
        hass.states.set("switch.tesla_charge", "on")
        hass.states.set("number.tesla_charge_current", "8")
        snapshots = {"switch.tesla_charge": DeviceSnapshot("switch.tesla_charge", "on", 16.0, None)}
        # Headroom = 5000 − 100 − 4200 = 700 W → 3 A erbij.
        await _peak(hass, ev_guard, [_ev()], snapshots).check_restore(4200.0, NOW)
        (call,) = hass.services.calls_for("set_value")
        assert call["data"]["value"] == 11.0
        assert "switch.tesla_charge" in snapshots
        assert ev_guard.hold_for("ev")["code"] == "peak_stepwise"

    async def test_reduced_ev_with_room_for_all_is_restored(self, hass, ev_guard):
        hass.states.set(PEAK_SENSOR, "8000")
        hass.states.set("switch.tesla_charge", "on")
        hass.states.set("number.tesla_charge_current", "8")
        snapshots = {"switch.tesla_charge": DeviceSnapshot("switch.tesla_charge", "on", 16.0, None)}
        await _peak(hass, ev_guard, [_ev()], snapshots).check_restore(4000.0, NOW)
        (call,) = hass.services.calls_for("set_value")
        assert call["data"]["value"] == 16.0
        assert snapshots == {}

    async def test_stopped_ev_waits_for_min_off(self, hass, ev_guard):
        hass.states.set(PEAK_SENSOR, "8000")
        hass.states.set("switch.tesla_charge", "off")
        hass.states.set("number.tesla_charge_current", "6")
        guard = ev_guard.get_guard("ev")
        guard.turned_off_at = NOW - timedelta(seconds=60)
        guard.turned_off_by_pg = True
        snapshots = {"switch.tesla_charge": DeviceSnapshot("switch.tesla_charge", "on", 16.0, None)}
        await _peak(hass, ev_guard, [_ev()], snapshots).check_restore(2000.0, NOW)
        assert not hass.services.has_call("turn_on")
        hold = ev_guard.status_dict()["holds"]["ev"]
        assert hold["code"] == "min_off"

    async def test_stopped_ev_restarts_at_the_current_that_fits(self, hass, ev_guard):
        hass.states.set(PEAK_SENSOR, "5000")
        hass.states.set("switch.tesla_charge", "off")
        hass.states.set("number.tesla_charge_current", "6")
        guard = ev_guard.get_guard("ev")
        guard.turned_off_at = NOW - timedelta(seconds=EV_MIN_OFF_DURATION_S + 1)
        guard.turned_off_by_pg = True
        snapshots = {"switch.tesla_charge": DeviceSnapshot("switch.tesla_charge", "on", 16.0, None)}
        # Headroom = 5000 − 100 − 2900 = 2000 W → 8 A past, 16 A niet.
        await _peak(hass, ev_guard, [_ev()], snapshots).check_restore(2900.0, NOW)
        assert hass.services.has_call("turn_on")
        assert hass.services.calls_for("set_value")[-1]["data"]["value"] == 8.0
        assert "switch.tesla_charge" in snapshots


class TestPeakRestoreEdgeCases:
    """Bevindingen uit de tweede review van het stapsgewijze EV-herstel."""

    def _stopped(self, hass, ev_guard, **ev_kw):
        hass.states.set("switch.tesla_charge", "off")
        hass.states.set("number.tesla_charge_current", "6")
        guard = ev_guard.get_guard("ev")
        guard.turned_off_at = NOW - timedelta(seconds=EV_MIN_OFF_DURATION_S + 1)
        guard.turned_off_by_pg = True
        guard.last_switch_state = False
        return _ev(**ev_kw)

    async def test_capped_restart_that_never_charges_is_given_up(self, hass, ev_guard):
        """Een volle of losgekoppelde wagen krijgt niet elke cyclus turn_on/set_value."""
        hass.states.set(PEAK_SENSOR, "5000")
        ev = self._stopped(hass, ev_guard)
        snapshots = {"switch.tesla_charge": DeviceSnapshot("switch.tesla_charge", "on", 16.0, None)}
        peak = _peak(hass, ev_guard, [ev], snapshots)
        await peak.check_restore(2900.0, NOW)
        n_calls = len(hass.services.calls)
        hass.states.set("switch.tesla_charge", "off")   # wagen laadt niet
        await peak.check_restore(2700.0, NOW + timedelta(seconds=60))
        assert len(hass.services.calls) == n_calls
        assert ev_guard.hold_for("ev")["code"] == "peak_stepwise"
        await peak.check_restore(2700.0, NOW + timedelta(seconds=200))
        assert len(hass.services.calls) == n_calls
        assert snapshots == {}

    async def test_capped_restart_reserves_headroom_while_waiting(self, hass, ev_guard):
        hass.states.set(PEAK_SENSOR, "5000")
        hass.states.set("switch.small", "off")
        ev = self._stopped(hass, ev_guard)
        small = _switch_off("small", 500.0, 1)
        snapshots = {
            "switch.tesla_charge": DeviceSnapshot("switch.tesla_charge", "on", 16.0, None),
            "switch.small": DeviceSnapshot("switch.small", "off"),
        }
        peak = _peak(hass, ev_guard, [small, ev], snapshots)
        await peak.check_restore(2900.0, NOW)   # 2000 W → EV op 8 A (1840 W), klein past niet
        hass.states.set("switch.tesla_charge", "off")
        snapshots["switch.small"] = DeviceSnapshot("switch.small", "on")
        hass.services.calls.clear()
        # Wagen trekt nog niets: 2000 W lijkt vrij, maar 1840 W is voor de EV.
        await peak.check_restore(2900.0, NOW + timedelta(seconds=60))
        assert not hass.services.has_call("turn_on")
        assert ev_guard.hold_for("small")["code"] == "peak_headroom"

    async def test_raise_after_capped_restart_starts_from_the_restart_current(self, hass, ev_guard):
        """Een verouderde stroomsensor (6 A) mag de 8 A van de herstart niet verlagen."""
        hass.states.set(PEAK_SENSOR, "5000")
        ev = self._stopped(hass, ev_guard)
        snapshots = {"switch.tesla_charge": DeviceSnapshot("switch.tesla_charge", "on", 16.0, None)}
        await _peak(hass, ev_guard, [ev], snapshots).check_restore(2900.0, NOW)
        guard = ev_guard.get_guard("ev")
        assert guard.last_sent_amps == 8.0
        assert guard.last_current_update == NOW

    async def test_full_restart_reserves_the_original_current(self, hass, ev_guard):
        """16 A past: de EV vraagt 3680 W, niet het startminimum van 1380 W."""
        hass.states.set(PEAK_SENSOR, "9000")
        hass.states.set("switch.boiler", "off")
        ev = self._stopped(hass, ev_guard)
        boiler = _switch_off("boiler", 2000.0, 1)
        snapshots = {
            "switch.tesla_charge": DeviceSnapshot("switch.tesla_charge", "on", 16.0, None),
            "switch.boiler": DeviceSnapshot("switch.boiler", "on"),
        }
        # Headroom = 9000 − 100 − 3900 = 5000 W: EV (3680 W) past, boiler erbij niet.
        await _peak(hass, ev_guard, [boiler, ev], snapshots).check_restore(3900.0, NOW)
        turned_on = [c["data"]["entity_id"] for c in hass.services.calls_for("turn_on")]
        assert turned_on == ["switch.tesla_charge"]
        assert ev_guard.hold_for("boiler")["code"] == "peak_headroom"

    async def test_min_off_holds_while_charge_state_still_says_charging(self, hass, ev_guard):
        """Een Tesla meldt na turn_off nog even 'charging': min-OFF blijft gelden."""
        hass.states.set(PEAK_SENSOR, "8000")
        hass.states.set("switch.tesla_charge", "off")
        hass.states.set("number.tesla_charge_current", "6")
        hass.states.set("sensor.tesla_opladen", "charging")
        guard = ev_guard.get_guard("ev")
        guard.turned_off_at = NOW - timedelta(seconds=60)
        guard.turned_off_by_pg = True
        guard.last_switch_state = False
        snapshots = {"switch.tesla_charge": DeviceSnapshot("switch.tesla_charge", "on", 16.0, None)}
        ev = _ev(charge_state_sensor="sensor.tesla_opladen")
        await _peak(hass, ev_guard, [ev], snapshots).check_restore(2000.0, NOW)
        assert hass.services.calls == []
        assert ev_guard.hold_for("ev")["code"] == "min_off"

    async def test_no_headroom_text_for_a_reduced_ev(self, hass, ev_guard):
        hass.states.set(PEAK_SENSOR, "3000")
        hass.states.set("switch.tesla_charge", "on")
        hass.states.set("number.tesla_charge_current", "8")
        snapshots = {"switch.tesla_charge": DeviceSnapshot("switch.tesla_charge", "on", 16.0, None)}
        await _peak(hass, ev_guard, [_ev()], snapshots).check_restore(2950.0, NOW)
        assert ev_guard.hold_for("ev")["text"].startswith("Laadstroom blijft verlaagd")

    async def test_no_headroom_skips_schedule_owned_devices(self, hass, ev_guard):
        hass.states.set(PEAK_SENSOR, "3000")
        snapshots = {"switch.tesla_charge": DeviceSnapshot("switch.tesla_charge", "on", 16.0, None)}
        peak = _peak(hass, ev_guard, [_ev()], snapshots)
        peak.set_skip_fn(lambda d: True)
        await peak.check_restore(2950.0, NOW)
        assert ev_guard.hold_for("ev") is None


class TestSolarTakesOverAfterPeak:
    """Na een piekverlaging 1800 W injectie: het overschot stuurt, niet het piekherstel."""

    class _Schedule:
        async def check(self, consumption, now):
            return None

    async def _tick(self, hass, ev_guard, original_a, inject_snaps):
        hass.states.set(PEAK_SENSOR, "5000")
        hass.states.set("switch.tesla_charge", "on")
        hass.states.set("number.tesla_charge_current", "6")
        ev_guard.get_guard("ev").last_current_update = NOW - timedelta(seconds=60)
        ev = _ev()
        peak_snaps = {"switch.tesla_charge":
                      DeviceSnapshot("switch.tesla_charge", "on", float(original_a), None)}
        await run_tick(-1800.0, NOW, peak=_peak(hass, ev_guard, [ev], peak_snaps),
                       schedule=self._Schedule(),
                       injection=_injection(hass, ev_guard, [ev], inject_snaps))
        return peak_snaps

    async def test_surplus_above_the_original_current_is_used(self, hass, ev_guard):
        inject = {}
        peak_snaps = await self._tick(hass, ev_guard, 8, inject)
        # 6 A + 1800 W / 230 V → 14 A; het piekherstel zet niet terug naar 8 A.
        assert [c["data"]["value"] for c in hass.services.calls_for("set_value")] == [14.0]
        assert peak_snaps == {}
        assert inject["switch.tesla_charge"].original_current == 8.0

    async def test_no_second_current_change_toward_a_higher_original(self, hass, ev_guard):
        inject = {}
        await self._tick(hass, ev_guard, 16, inject)
        assert [c["data"]["value"] for c in hass.services.calls_for("set_value")] == [14.0]
        # Na het overschot terug naar de stroom van vóór de piek.
        assert inject["switch.tesla_charge"].original_current == 16.0

    async def test_peak_stop_restarted_by_solar_returns_to_charging(self, ev_guard):
        """Gestopt door de piek, gestart door de zon: na de zon niet uitschakelen."""
        peak_snaps = {"switch.tesla_charge":
                      DeviceSnapshot("switch.tesla_charge", "on", 16.0, None)}
        inject = {"switch.tesla_charge": DeviceSnapshot("switch.tesla_charge", "off", 6.0, None)}
        tracker = MockPeakTracker()
        peak = _peak(MockHass(), ev_guard, [_ev()], peak_snaps)
        peak.peak_tracker = tracker
        peak.hand_over_to_solar(inject, NOW)
        assert inject["switch.tesla_charge"].original_state == "on"
        assert inject["switch.tesla_charge"].original_current == 16.0
        assert tracker.turn_on_measurements == ["ev"]


async def test_run_tick_starts_with_fresh_explanations(hass, ev_guard):
    ev_guard.set_hold("old", "solar_share", "verouderd")

    class _Schedule:
        async def check(self, consumption, now):
            return None

    peak = _peak(hass, ev_guard, [], {})
    inj = _injection(hass, ev_guard, [], {})
    hass.states.set(PEAK_SENSOR, "5000")
    await run_tick(1000.0, NOW, peak=peak, schedule=_Schedule(), injection=inj)
    assert ev_guard.hold_for("old") is None
