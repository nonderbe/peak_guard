"""
Peak Guard — tests/test_charger_state.py

Stap 1d van docs/charger_state_plan.md: het geheugen van Peak Guard over een
lader (EVDeviceGuard) bestaat per schakelaar, gedeeld door alle kopieën van de
wagen (piek, injectie, schema), ook als die een ander device.id hebben.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from custom_components.peak_guard.deciders.ev_guard import EVGuard
from custom_components.peak_guard.models import EVChargerDevice, EVState
from tests.conftest import MockHass, MockPeakTracker, MockSolarTracker

T0 = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)
SW = "switch.tesla_charge"


def _ev(device_id: str, switch: str = SW) -> EVChargerDevice:
    return EVChargerDevice(
        id=device_id, name="Tesla", entity_id=switch, priority=1,
        action_type="ev_charger", switch_entity=switch,
        current_entity="number.tesla_current", phases=1,
        min_value=6, max_value=16, charge_state_sensor="sensor.tesla_charging",
    )


def _guard() -> tuple:
    hass = MockHass()
    return hass, EVGuard(hass, {}, [])


def test_copies_with_distinct_ids_share_one_guard():
    _, ev = _guard()
    peak, solar = _ev("ev_peak"), _ev("ev_solar")
    assert ev.guard_for(peak) is ev.guard_for(solar)


def test_other_switch_gets_its_own_guard():
    _, ev = _guard()
    assert ev.guard_for(_ev("a")) is not ev.guard_for(_ev("b", "switch.other"))


def test_status_dict_keeps_one_entry_per_device_id():
    _, ev = _guard()
    g = ev.guard_for(_ev("ev_peak"))
    ev.guard_for(_ev("ev_solar"))
    g.turned_off_by_pg = True
    st = ev.status_dict()["ev_guards"]
    assert set(st) == {"ev_peak", "ev_solar"}
    assert st["ev_peak"] == st["ev_solar"]
    assert st["ev_solar"]["turned_off_by_pg"] is True


def test_find_guard_creates_nothing():
    _, ev = _guard()
    assert ev.find_guard(_ev("x")) is None
    assert ev.guards == {}


def test_guard_made_by_id_is_adopted_by_the_switch():
    """get_guard(id) (tests, oudere aanroepers) en daarna guard_for: één guard."""
    _, ev = _guard()
    g = ev.get_guard("ev_peak")
    g.last_sent_amps = 9.0
    assert ev.guard_for(_ev("ev_peak")) is g
    assert ev.guard_for(_ev("ev_solar")) is g


async def test_peak_stop_by_one_copy_is_seen_by_the_inject_copy():
    """De piekkopie stopt de wagen; de injectiekopie (ander id) ziet hem als
    door Peak Guard gestopt: geen stroomverlaging naar een stilstaande wagen,
    ook al meldt de Tesla nog 'charging'."""
    hass, ev = _guard()
    hass.states.set(SW, "on")
    hass.states.set("sensor.tesla_charging", "charging")
    hass.states.set("number.tesla_current", "6")
    solar, peak = _ev("ev_solar"), _ev("ev_peak")
    g = ev.guard_for(solar)
    g.state, g.last_switch_state, g.turned_on_at = EVState.CHARGING, True, T0 - timedelta(minutes=30)

    await ev.apply_action(peak, 5000.0, {}, "peak", MockPeakTracker(),
                          MockSolarTracker(), now=T0)
    assert hass.services.has_call("turn_off")
    assert ev.view(solar).stopped_by_pg_not_restarted()
    assert ev.peak_is_off(solar) is True

    calls = len(hass.services.calls)
    assert await ev.throttle_down_solar(solar, consumption=500.0,
                                        now=T0 + timedelta(seconds=30)) is False
    assert len(hass.services.calls) == calls


async def test_release_schedule_keeps_a_peak_stop():
    """Bekende beperking t.e.m. v1.9.14: het schema laat de wagen los (doel
    bereikt / entry verwijderd) terwijl de piek-cascade hem gestopt heeft;
    de stop mag niet vergeten worden, ook al meldt de Tesla nog 'charging'."""
    hass, ev = _guard()
    hass.states.set(SW, "on")
    hass.states.set("sensor.tesla_charging", "charging")
    hass.states.set("number.tesla_current", "6")
    sched, peak = _ev("ev_sched"), _ev("ev_peak")
    g = ev.guard_for(sched)
    g.state, g.last_switch_state, g.turned_on_at = EVState.CHARGING, True, T0 - timedelta(minutes=30)
    g.scheduled = True

    await ev.apply_action(peak, 5000.0, {}, "peak", MockPeakTracker(),
                          MockSolarTracker(), now=T0)
    assert hass.services.has_call("turn_off")
    ev.release_schedule(sched)
    assert g.scheduled is False
    assert ev.peak_is_off(peak) is True
    assert ev.view(peak).stopped_by_pg_not_restarted()
    calls = len(hass.services.calls)
    assert await ev.apply_action(peak, 5000.0, {}, "peak", MockPeakTracker(),
                                 MockSolarTracker(), now=T0 + timedelta(seconds=30)) == 5000.0
    assert len(hass.services.calls) == calls


def test_release_schedule_still_forgets_its_own_running_charge():
    """Een lopende lading van het schema: last_switch_state → None (de
    solar-cascade herkent een start daarna als manueel)."""
    _, ev = _guard()
    sched = _ev("ev_sched")
    g = ev.guard_for(sched)
    g.state, g.last_switch_state, g.scheduled = EVState.CHARGING, True, True
    ev.release_schedule(sched)
    assert g.last_switch_state is None and g.state == EVState.IDLE
