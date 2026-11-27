"""
tests/test_solar_stop.py — solar-stop van een EV (veldlog 27-11-2026, v1.9.13).

Twee onterechte stops van de Tesla door het solar-herstel:

1. De laadstatus (sensor.*_opladen) bleef na de eigen turn_on minutenlang op
   'stopped' staan terwijl de wagen 8 A laadde. throttle_down_solar zag een
   wagen die niet laadde en liet het herstel hem stoppen → 1,85 kW injectie.
   Nu geldt voor de solar-cascade dezelfde regel als voor de piek-cascade
   (EVGuard.stale_stopped_after_start).
2. Op het minimum (5 A) stopte één meting van 5 s (1451 W, ander toestel) de
   lading. Nu moet 'de zon draagt niets meer bij' bevestigd worden bij een
   meting minstens EV_SOLAR_STOP_CONFIRM_S later.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.peak_guard.const import (
    EV_STALE_STOPPED_S,
    EV_SOLAR_STOP_CONFIRM_S,
    EV_SOLAR_STOP_MAX_GAP_S,
)
from custom_components.peak_guard.deciders.ev_guard import EVGuard
from custom_components.peak_guard.deciders.injection_decider import InjectionDecider
from custom_components.peak_guard.models import (
    DeviceSnapshot,
    EVChargerDevice,
    EVState,
    SwitchOnDevice,
)

from tests.conftest import MockHass, MockPeakTracker, MockSolarTracker

T0 = datetime(2026, 11, 27, 12, 54, 43, tzinfo=timezone.utc)   # turn_on in de veldlog
CHARGE_STATE = "sensor.tesla_opladen"


def _t(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


async def _save():
    return None


def _tesla(**kw) -> EVChargerDevice:
    base = dict(
        id="ev", name="Tesla", entity_id="switch.tesla_charge", priority=2,
        action_type="ev_charger", min_value=5.0, max_value=8.0,
        switch_entity="switch.tesla_charge", current_entity="number.tesla_charge_current",
        phases=1, charge_state_sensor=CHARGE_STATE,
    )
    base.update(kw)
    return EVChargerDevice(**base)


def _started_by_solar(guard: EVGuard, at: datetime = T0, dev: EVChargerDevice | None = None) -> None:
    """Zoals een geslaagde solar-start: guard-toestand plus note_charge_start."""
    g = guard.get_guard("ev")
    g.state, g.last_switch_state, g.turned_on_at = EVState.CHARGING, True, at
    guard.note_charge_start(dev or _tesla(), at)


@pytest.fixture
def hass() -> MockHass:
    h = MockHass()
    h.states.set("switch.tesla_charge", "on")
    h.states.set("number.tesla_charge_current", "8")
    # Laatst gewijzigd om 12:51:36, vóór de turn_on van 12:54:43.
    h.states.set(CHARGE_STATE, "stopped", reported=datetime(2026, 11, 27, 12, 51, 36, tzinfo=timezone.utc))
    return h


@pytest.fixture
def ev_guard(hass) -> EVGuard:
    return EVGuard(hass=hass, config={}, iteration_actions=[])


# ═══════════════════════════════════════════════════════════════════════════ #
#  1. Verouderde 'stopped' na de eigen start                                  #
# ═══════════════════════════════════════════════════════════════════════════ #

class TestStaleStoppedSolar:
    @pytest.mark.parametrize("switch_state", ["on", "unknown"])
    async def test_field_case_keeps_charging(self, hass, ev_guard, switch_state):
        """12:59:45: netafname 13 W, wagen laadt 8 A, laadstatus nog 'stopped'."""
        hass.states.set("switch.tesla_charge", switch_state)
        _started_by_solar(ev_guard)
        assert await ev_guard.throttle_down_solar(_tesla(), consumption=13.0, now=_t(302)) is True
        assert ev_guard.hold_for("ev")["code"] == "solar_share"
        assert hass.services.calls == []

    async def test_field_case_through_inject_restore(self, hass, ev_guard):
        _started_by_solar(ev_guard)
        snaps = {"switch.tesla_charge": DeviceSnapshot("switch.tesla_charge", "off", 5.0, 80.0)}
        dec = InjectionDecider(
            hass=hass, config={}, peak_tracker=MockPeakTracker(), solar_tracker=MockSolarTracker(),
            ev_guard=ev_guard, iteration_actions=[], save_fn=_save,
            cascade=[_tesla()], snapshots=snaps,
        )
        await dec.check_restore(13.0, _t(302))
        assert "switch.tesla_charge" in snaps
        assert not hass.services.has_call("turn_off")

    async def test_stopped_after_the_start_is_real(self, hass, ev_guard):
        """Laadstatus veranderde ná de start naar 'stopped': de wagen laadt echt niet."""
        _started_by_solar(ev_guard)
        hass.states.set(CHARGE_STATE, "stopped", reported=_t(180))
        assert await ev_guard.throttle_down_solar(_tesla(), consumption=13.0, now=_t(302)) is False

    async def test_report_right_after_start_is_still_stale(self, hass, ev_guard):
        """Review #2: een peiling vlak na de turn_on toont nog de oude 'stopped'."""
        _started_by_solar(ev_guard)
        hass.states.set(CHARGE_STATE, "stopped", reported=_t(30), changed=_t(-200))
        assert ev_guard.charging_for_control(_tesla(), _t(302))
        assert await ev_guard.throttle_down_solar(_tesla(), consumption=13.0, now=_t(302)) is True

    async def test_early_real_stop_is_believed(self, hass, ev_guard):
        """Review 2 #6: de laadstatus veranderde na de start (charging → stopped) → echt."""
        _started_by_solar(ev_guard)
        hass.states.set(CHARGE_STATE, "stopped", reported=_t(60), changed=_t(60))
        assert not ev_guard.charging_for_control(_tesla(), _t(90))

    async def test_draw_follows_the_control_rule_but_is_charging_stays_observed(self, hass, ev_guard):
        """Review #4 / review 2 #1-#2: het laadvermogen telt de lading mee;
        is_charging en peak_is_off (startbevestigingen) blijven bij wat de wagen meldt."""
        # charging_draw_w / peak_is_off werken op de wandklok.
        started = datetime.now(timezone.utc) - timedelta(minutes=5)
        _started_by_solar(ev_guard, at=started)
        hass.states.set(CHARGE_STATE, "stopped", reported=started - timedelta(minutes=3))
        assert ev_guard.charging_for_control(_tesla(), datetime.now(timezone.utc))
        assert ev_guard.charging_draw_w(_tesla()) == pytest.approx(8 * 230)
        assert not ev_guard.is_charging(_tesla())
        assert ev_guard.peak_is_off(_tesla())

    async def test_turned_on_at_without_turn_on_is_not_a_start(self, hass, ev_guard):
        """Review 2 #3: enkel een echte turn_on telt, niet een herstel of overdracht."""
        g = ev_guard.get_guard("ev")
        g.state, g.last_switch_state, g.turned_on_at = EVState.CHARGING, True, T0
        assert not ev_guard.charging_for_control(_tesla(), _t(302))

    async def test_other_copy_of_the_same_car_shares_the_start(self, hass, ev_guard):
        """Review 2 #4: piek-kopie met een ander id ziet de solar-start ook."""
        _started_by_solar(ev_guard)
        assert ev_guard.charging_for_control(_tesla(id="ev_peak"), _t(302))

    async def test_turn_off_clears_the_start(self, hass, ev_guard):
        _started_by_solar(ev_guard)
        await ev_guard._svc("switch", "turn_off", {"entity_id": "switch.tesla_charge"})
        assert not ev_guard.charging_for_control(_tesla(), _t(302))

    async def test_solar_restore_turns_off_unknown_switch_on_stale_stopped(self, hass, ev_guard):
        """Review #1: bevestigde solar-stop met 'unknown' schakelaar → echt turn_off."""
        hass.states.set("switch.tesla_charge", "unknown")
        _started_by_solar(ev_guard)
        snap = DeviceSnapshot("switch.tesla_charge", "off", 5.0, 80.0)
        ok = await ev_guard.restore(_tesla(), snap, MockPeakTracker(), MockSolarTracker(),
                                    cascade_type="solar", now=_t(600))
        assert ok is True
        assert hass.services.has_call("turn_off")

    async def test_survives_one_missed_poll(self, hass, ev_guard):
        """De Tesla-integratie peilt om de ~10 min: 15 min na de start zonder nieuwe melding."""
        _started_by_solar(ev_guard)
        assert await ev_guard.throttle_down_solar(_tesla(), consumption=13.0, now=_t(900)) is True

    async def test_start_time_includes_wake_and_retries(self, hass, ev_guard):
        """Review 3 #4: de start is het moment van de geslaagde turn_on, niet het begin van de tick."""
        from time import monotonic
        ev_guard.begin_tick(T0)
        ev_guard._tick_clock = (T0, monotonic() - 70)   # 70 s wake-up/retries in deze tick
        ev_guard.note_charge_start(_tesla(), T0)
        started = ev_guard._charge_started["switch.tesla_charge"]
        assert (started - T0).total_seconds() == pytest.approx(70, abs=2)

    async def test_peak_keeps_the_turned_on_at_rule(self, hass, ev_guard):
        """Review 3 #2: manuele start of start vóór een herstart van HA (geen
        note_charge_start): de piek verlaagt nog steeds binnen 10 min."""
        hass.states.set("switch.tesla_charge", "on")
        g = ev_guard.get_guard("ev")
        g.state, g.last_switch_state, g.turned_on_at = EVState.CHARGING, True, T0
        left = await ev_guard.apply_action(
            _tesla(), 2000.0, {}, "peak", MockPeakTracker(), MockSolarTracker(), now=_t(120))
        assert left < 2000.0

    async def test_old_start_no_longer_overrides(self, hass, ev_guard):
        _started_by_solar(ev_guard)
        now = _t(EV_STALE_STOPPED_S + 1)
        assert await ev_guard.throttle_down_solar(_tesla(), consumption=13.0, now=now) is False

    async def test_complete_is_never_stale(self, hass, ev_guard):
        _started_by_solar(ev_guard)
        hass.states.set(CHARGE_STATE, "complete", reported=_t(-200))
        assert await ev_guard.throttle_down_solar(_tesla(), consumption=13.0, now=_t(302)) is False

    async def test_not_started_by_pg(self, hass, ev_guard):
        """Zonder eigen start blijft 'stopped' gewoon 'laadt niet'."""
        assert await ev_guard.throttle_down_solar(_tesla(), consumption=13.0, now=_t(302)) is False

    async def test_peak_path_uses_the_same_rule(self, hass, ev_guard):
        """Piek-cascade: 'stopped' van ná de start → niets af te schakelen."""
        hass.states.set("switch.tesla_charge", "unknown")
        _started_by_solar(ev_guard)
        hass.states.set(CHARGE_STATE, "stopped", reported=_t(180))
        left = await ev_guard.apply_action(
            _tesla(), 2000.0, {}, "peak", MockPeakTracker(), MockSolarTracker(), now=_t(120))
        assert left == pytest.approx(2000.0)
        assert hass.services.calls == []

    async def test_peak_path_reduces_on_stale_stopped(self, hass, ev_guard):
        hass.states.set("switch.tesla_charge", "unknown")
        _started_by_solar(ev_guard)
        left = await ev_guard.apply_action(
            _tesla(), 2000.0, {}, "peak", MockPeakTracker(), MockSolarTracker(), now=_t(120))
        assert left < 2000.0


# ═══════════════════════════════════════════════════════════════════════════ #
#  2. Stop op het minimum pas na bevestiging                                  #
# ═══════════════════════════════════════════════════════════════════════════ #

class TestSolarStopConfirm:
    @pytest.fixture(autouse=True)
    def _charging_at_min(self, hass, ev_guard):
        hass.states.set(CHARGE_STATE, "charging", reported=_t(-600))
        hass.states.set("number.tesla_charge_current", "5")    # 5 A × 230 V = 1150 W

    async def _td(self, ev_guard, consumption, seconds):
        return await ev_guard.throttle_down_solar(_tesla(), consumption=consumption, now=_t(seconds))

    async def test_single_spike_does_not_stop(self, hass, ev_guard):
        """Veldlog 12:48:39: één meting 1451 W tussen ~870 W."""
        assert await self._td(ev_guard, 1451.0, 0) is True
        assert ev_guard.hold_for("ev")["code"] == "solar_stop_confirm"
        assert await self._td(ev_guard, 870.0, 60) is True
        assert ev_guard.hold_for("ev")["code"] == "solar_share"
        # Een nieuwe piek begint de bevestiging opnieuw.
        assert await self._td(ev_guard, 1451.0, 120) is True
        assert ev_guard.hold_for("ev")["code"] == "solar_stop_confirm"
        assert hass.services.calls == []

    async def test_sustained_import_stops_after_confirmation(self, ev_guard):
        assert await self._td(ev_guard, 1300.0, 0) is True
        assert await self._td(ev_guard, 1300.0, EV_SOLAR_STOP_CONFIRM_S - 1) is True
        assert await self._td(ev_guard, 1300.0, EV_SOLAR_STOP_CONFIRM_S) is False

    async def test_new_start_clears_the_confirmation(self, ev_guard):
        """Review 2 #7: na een herstart opnieuw bevestigen, ook binnen de gap."""
        assert await self._td(ev_guard, 1300.0, 0) is True
        assert await self._td(ev_guard, 1300.0, 60) is False
        ev_guard.note_charge_start(_tesla(), _t(120))
        assert await self._td(ev_guard, 1300.0, 150) is True
        assert ev_guard.hold_for("ev")["code"] == "solar_stop_confirm"

    async def test_failed_stop_needs_no_new_wait(self, ev_guard):
        """Review #5: lukte de stop niet (rate limit, fout), dan meteen opnieuw."""
        assert await self._td(ev_guard, 1300.0, 0) is True
        assert await self._td(ev_guard, 1300.0, 60) is False
        assert await self._td(ev_guard, 1300.0, 120) is False

    async def test_long_gap_restarts_confirmation(self, ev_guard):
        """Tussendoor weer overschot (geen check_restore): oude vaststelling telt niet."""
        assert await self._td(ev_guard, 1300.0, 0) is True
        gap = EV_SOLAR_STOP_MAX_GAP_S + 1
        assert await self._td(ev_guard, 1300.0, gap) is True
        assert ev_guard.hold_for("ev")["code"] == "solar_stop_confirm"
        assert await self._td(ev_guard, 1300.0, gap + EV_SOLAR_STOP_CONFIRM_S) is False

    async def test_reduction_resets_confirmation(self, hass, ev_guard):
        assert await self._td(ev_guard, 1300.0, 0) is True     # bevestiging loopt
        assert "switch.tesla_charge" in ev_guard._solar_stop
        # De gebruiker zette de wagen intussen op 8 A (1840 W): verlagen volstaat.
        hass.states.set("number.tesla_charge_current", "8")
        assert await self._td(ev_guard, 1000.0, 30) is True    # ceil(770/230) = 4 A → 5 A
        assert hass.services.calls_for("set_value")[-1]["data"]["value"] == 5.0
        assert not "switch.tesla_charge" in ev_guard._solar_stop

    async def test_surplus_between_two_spikes_resets(self, hass, ev_guard):
        """Review 3 #1: piek, overschot, piek → niet stoppen."""
        dec = InjectionDecider(
            hass=hass, config={}, peak_tracker=MockPeakTracker(), solar_tracker=MockSolarTracker(),
            ev_guard=ev_guard, iteration_actions=[], save_fn=_save, cascade=[_tesla()],
            snapshots={"switch.tesla_charge": DeviceSnapshot("switch.tesla_charge", "off", 5.0, 80.0)},
        )
        await dec.check_restore(1451.0, _t(0))
        await dec.check_restore(-300.0, _t(60))
        await dec.check_restore(1451.0, _t(120))
        assert not hass.services.has_call("turn_off")
        assert ev_guard.hold_for("ev")["code"] == "solar_stop_confirm"

    async def test_skipped_tick_resets(self, ev_guard):
        """Meer dan EV_SOLAR_STOP_MAX_GAP_S tussen twee vaststellingen: opnieuw bevestigen."""
        assert await self._td(ev_guard, 1300.0, 0) is True
        assert await self._td(ev_guard, 1300.0, EV_SOLAR_STOP_MAX_GAP_S + 1) is True

    async def test_slow_tick_still_stops(self, ev_guard):
        """Review 4 #3: 60 s lus + 45 s trage cyclus mag de bevestiging niet eindeloos herstarten."""
        assert await self._td(ev_guard, 1300.0, 0) is True
        assert await self._td(ev_guard, 1300.0, 105) is False

    async def test_successful_turn_off_clears_confirmation(self, ev_guard):
        """Review 4 #5: na een geslaagde stop bevestigt een (manuele) herstart opnieuw."""
        assert await self._td(ev_guard, 1300.0, 0) is True
        assert await self._td(ev_guard, 1300.0, 60) is False
        await ev_guard._svc("switch", "turn_off", {"entity_id": "switch.tesla_charge"})
        assert "switch.tesla_charge" not in ev_guard._solar_stop

    async def test_confirmation_is_shared_by_copies(self, ev_guard):
        """Review 3 #5: een start via de piek-kopie (ander id) wist ook de solar-bevestiging."""
        assert await self._td(ev_guard, 1300.0, 0) is True
        ev_guard.note_charge_start(_tesla(id="ev_peak"), _t(30))
        assert not "switch.tesla_charge" in ev_guard._solar_stop

    async def test_no_other_device_is_restored_while_confirming(self, hass, ev_guard):
        """Review #3: tijdens de bevestiging (mogelijk een korte piek) blijft ook de boiler aan."""
        hass.states.set("switch.boiler", "on")
        boiler = SwitchOnDevice(id="boiler", name="Boiler", entity_id="switch.boiler",
                                priority=1, action_type="switch_on", power_watts=2000.0)
        snaps = {
            "switch.tesla_charge": DeviceSnapshot("switch.tesla_charge", "off", 5.0, 80.0),
            "switch.boiler": DeviceSnapshot("switch.boiler", "off"),
        }
        dec = InjectionDecider(
            hass=hass, config={}, peak_tracker=MockPeakTracker(), solar_tracker=MockSolarTracker(),
            ev_guard=ev_guard, iteration_actions=[], save_fn=_save,
            cascade=[boiler, _tesla()], snapshots=snaps,
        )
        await dec.check_restore(1451.0, _t(0))
        assert not hass.services.has_call("turn_off")
        assert set(snaps) == {"switch.tesla_charge", "switch.boiler"}
        assert ev_guard.hold_for("ev")["code"] == "solar_stop_confirm"
