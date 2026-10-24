"""
tests/test_climate.py — thermostaat (climate.*) in de piek- en solar-cascade.

Piek: uitschakelen (hvac-modus 'off'), daarna terug naar de vorige modus en
doeltemperatuur. Solar: verwarmen op inject_temp zolang er overschot is,
daarna terug naar de vorige instelling.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone

import pytest

from custom_components.peak_guard.models import (
    CascadeContext,
    ClimateDevice,
    DeviceSnapshot,
    from_dict,
)

from tests.conftest import MockPeakTracker, MockSolarTracker

ENTITY = "climate.badkamer"
NOW = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)


def _device(**kw) -> ClimateDevice:
    d = {
        "id": "dev_bad", "name": "Badkamer", "entity_id": ENTITY, "priority": 3,
        "action_type": "climate", "power_watts": 1800, "inject_temp": 24,
    }
    d.update(kw)
    return from_dict(d)


def _ctx(hass, ev_guard, cascade_type: str) -> CascadeContext:
    return CascadeContext(
        hass=hass, cascade_type=cascade_type,
        peak_tracker=MockPeakTracker(), solar_tracker=MockSolarTracker(),
        ev_guard=ev_guard, track_action=lambda *a, **k: None,
        warn=lambda *a, **k: None, now=NOW,
    )


def _set(hass, mode: str, temp=21.0, room=20.0, action="heating"):
    attrs = {"temperature": temp, "current_temperature": room}
    if action is not None:
        attrs["hvac_action"] = action
    hass.states.set(ENTITY, mode, attrs)


class TestSerialisation:

    def test_from_dict_builds_a_climate_device(self):
        dev = _device()
        assert isinstance(dev, ClimateDevice)
        assert dev.inject_temp == 24.0
        assert from_dict(asdict(dev)) == dev

    def test_empty_inject_temp_is_none(self):
        assert _device(inject_temp="").inject_temp is None

    def test_snapshot_without_temperature_loads(self):
        """Opgeslagen snapshots van vóór v1.9.4 hebben geen original_temp."""
        snap = DeviceSnapshot(**{"entity_id": "switch.x", "original_state": "on",
                                 "original_current": None, "original_soc": None})
        assert snap.original_temp is None


class TestPeak:

    async def test_heating_thermostat_is_switched_off(self, hass, ev_guard):
        _set(hass, "heat", temp=21.0)
        snaps: dict = {}
        ctx = _ctx(hass, ev_guard, "peak")
        left = await _device().apply(2500.0, snaps, ctx)
        assert left == pytest.approx(700.0)
        (call,) = hass.services.calls
        assert (call["domain"], call["service"]) == ("climate", "set_hvac_mode")
        assert call["data"]["hvac_mode"] == "off"
        assert snaps[ENTITY].original_state == "heat"
        assert snaps[ENTITY].original_temp == 21.0
        assert ctx.peak_tracker.pending_avoids == [{"id": "dev_bad", "kw": 1.8, "deferred": False}]

    async def test_idle_thermostat_is_off_but_frees_nothing(self, hass, ev_guard):
        _set(hass, "heat", action="idle")
        ctx = _ctx(hass, ev_guard, "peak")
        left = await _device().apply(2500.0, {}, ctx)
        assert left == pytest.approx(2500.0)
        assert hass.services.has_call("set_hvac_mode")
        assert ctx.peak_tracker.pending_avoids == []

    async def test_already_off_is_skipped(self, hass, ev_guard):
        _set(hass, "off")
        assert await _device().apply(2500.0, {}, _ctx(hass, ev_guard, "peak")) == 2500.0
        assert hass.services.calls == []

    async def test_restore_returns_to_previous_mode_and_temperature(self, hass, ev_guard):
        _set(hass, "off")
        ctx = _ctx(hass, ev_guard, "peak")
        snap = DeviceSnapshot(entity_id=ENTITY, original_state="heat", original_temp=21.0)
        assert await _device().restore(snap, ctx) is True
        (call,) = hass.services.calls
        assert call["service"] == "set_temperature"
        assert call["data"] == {"entity_id": ENTITY, "temperature": 21.0, "hvac_mode": "heat"}
        assert ctx.peak_tracker.turn_on_measurements == ["dev_bad"]


class TestSolar:

    async def test_surplus_sets_the_inject_temperature(self, hass, ev_guard):
        _set(hass, "heat", temp=19.0, room=19.5)
        snaps: dict = {}
        ctx = _ctx(hass, ev_guard, "solar")
        left = await _device().apply(2000.0, snaps, ctx)
        assert left == pytest.approx(200.0)
        (call,) = hass.services.calls
        assert call["service"] == "set_temperature"
        assert call["data"] == {"entity_id": ENTITY, "temperature": 24.0}
        assert snaps[ENTITY].original_temp == 19.0
        assert ctx.solar_tracker.started == [{"id": "dev_bad", "kw": 1.8}]

    async def test_off_thermostat_is_switched_to_heat(self, hass, ev_guard):
        _set(hass, "off", temp=19.0, action=None)
        await _device().apply(2000.0, {}, _ctx(hass, ev_guard, "solar"))
        (call,) = hass.services.calls
        assert call["data"]["hvac_mode"] == "heat"

    async def test_small_surplus_does_nothing(self, hass, ev_guard):
        _set(hass, "heat", temp=19.0)
        assert await _device().apply(1000.0, {}, _ctx(hass, ev_guard, "solar")) == 1000.0
        assert hass.services.calls == []
        assert ev_guard._holds["dev_bad"]["code"] == "surplus_too_small"

    async def test_room_already_warm_is_left_alone(self, hass, ev_guard):
        _set(hass, "heat", temp=19.0, room=24.2)
        assert await _device().apply(3000.0, {}, _ctx(hass, ev_guard, "solar")) == 3000.0
        assert hass.services.calls == []

    async def test_already_at_inject_temperature_is_skipped(self, hass, ev_guard):
        _set(hass, "heat", temp=24.0, room=22.0)
        assert await _device().apply(3000.0, {}, _ctx(hass, ev_guard, "solar")) == 3000.0
        assert hass.services.calls == []

    async def test_no_inject_temperature_means_no_solar_action(self, hass, ev_guard):
        _set(hass, "heat", temp=19.0)
        dev = _device(inject_temp=None)
        assert await dev.apply(3000.0, {}, _ctx(hass, ev_guard, "solar")) == 3000.0
        assert hass.services.calls == []

    async def test_restore_after_surplus_turns_an_off_thermostat_off_again(self, hass, ev_guard):
        _set(hass, "heat", temp=24.0)
        ctx = _ctx(hass, ev_guard, "solar")
        snap = DeviceSnapshot(entity_id=ENTITY, original_state="off", original_temp=19.0)
        assert await _device().restore(snap, ctx) is True
        (call,) = hass.services.calls
        assert call["service"] == "set_hvac_mode"
        assert call["data"]["hvac_mode"] == "off"
        assert ctx.solar_tracker.completed == ["dev_bad"]

    async def test_failed_call_rolls_back_the_snapshot(self, hass, ev_guard):
        hass.services._raise_on = {"set_temperature"}
        _set(hass, "heat", temp=19.0)
        snaps: dict = {}
        assert await _device().apply(2000.0, snaps, _ctx(hass, ev_guard, "solar")) == 2000.0
        assert snaps == {}


# ═══════════════════════════════════════════════════════════════════════════ #
#  Zelfde thermostaat in beide cascades                                       #
# ═══════════════════════════════════════════════════════════════════════════ #

from custom_components.peak_guard.deciders.dispatch import run_tick
from custom_components.peak_guard.deciders.injection_decider import InjectionDecider
from custom_components.peak_guard.deciders.peak_decider import PeakDecider

PEAK_SENSOR = "sensor.p1_maandpiek"


class _NoSchedule:
    async def check(self, consumption, now):
        return None


async def _save():
    return None


def _deciders(hass, ev_guard, peak_snaps, inject_snaps):
    cfg = {"peak_sensor": PEAK_SENSOR, "buffer_watts": 100}
    hass.states.set(PEAK_SENSOR, "5000")
    peak = PeakDecider(
        hass=hass, config=cfg, peak_tracker=MockPeakTracker(), solar_tracker=MockSolarTracker(),
        ev_guard=ev_guard, iteration_actions=[], save_fn=_save,
        cascade=[_device(priority=3)], snapshots=peak_snaps,
    )
    inj = InjectionDecider(
        hass=hass, config=cfg, peak_tracker=MockPeakTracker(), solar_tracker=MockSolarTracker(),
        ev_guard=ev_guard, iteration_actions=[], save_fn=_save,
        cascade=[_device(priority=1)], snapshots=inject_snaps,
    )
    return peak, inj


class TestBothCascades:

    async def test_peak_shed_is_not_undone_by_the_inject_restore(self, hass, ev_guard):
        """Overschot had 24 °C gezet (gebruiker: 20 °C); daarna een piek."""
        _set(hass, "heat", temp=24.0, room=21.0)
        peak_snaps: dict = {}
        inject_snaps = {ENTITY: DeviceSnapshot(ENTITY, "heat", original_temp=20.0)}
        peak, inj = _deciders(hass, ev_guard, peak_snaps, inject_snaps)
        await run_tick(6500.0, NOW, peak=peak, schedule=_NoSchedule(), injection=inj)
        assert [c["service"] for c in hass.services.calls] == ["set_hvac_mode"]
        assert inject_snaps == {}
        assert peak_snaps[ENTITY].original_state == "heat"
        assert peak_snaps[ENTITY].original_temp == 20.0
        assert peak.solar_tracker.completed == ["dev_bad"]

    async def test_surplus_after_a_peak_ends_in_the_users_setting(self, hass, ev_guard):
        """Piek had de verwarming (heat, 20 °C) uitgezet; daarna overschot en weer afname."""
        _set(hass, "off", temp=20.0, room=19.0, action=None)
        peak_snaps = {ENTITY: DeviceSnapshot(ENTITY, "heat", original_temp=20.0)}
        inject_snaps: dict = {}
        peak, inj = _deciders(hass, ev_guard, peak_snaps, inject_snaps)
        await run_tick(-2500.0, NOW, peak=peak, schedule=_NoSchedule(), injection=inj)
        calls = hass.services.calls
        assert [c["data"].get("temperature") for c in calls] == [24.0]
        assert peak_snaps == {}
        assert inject_snaps[ENTITY].original_state == "heat"
        assert inject_snaps[ENTITY].original_temp == 20.0

        _set(hass, "heat", temp=24.0, room=22.0)
        hass.services.calls.clear()
        await run_tick(300.0, NOW, peak=peak, schedule=_NoSchedule(), injection=inj)
        (call,) = hass.services.calls
        assert call["data"] == {"entity_id": ENTITY, "temperature": 20.0}
        assert inject_snaps == {}

    async def test_takeover_of_a_device_that_was_off_forgets_the_peak_pending(self, hass, ev_guard):
        """Stond uit vóór het overschot: na de piek wordt niets herstart, dus geen vermeden piek."""
        _set(hass, "heat", temp=24.0, room=21.0)
        peak_snaps: dict = {}
        inject_snaps = {ENTITY: DeviceSnapshot(ENTITY, "off", original_temp=20.0)}
        peak, inj = _deciders(hass, ev_guard, peak_snaps, inject_snaps)
        await run_tick(6500.0, NOW, peak=peak, schedule=_NoSchedule(), injection=inj)
        assert peak_snaps[ENTITY].original_state == "off"
        assert peak.peak_tracker.discarded == ["dev_bad"]


class TestDiscardPending:

    def test_discarded_pending_gives_the_next_shed_a_fresh_avoid_ts(self):
        from custom_components.peak_guard.avoided_peak_tracker import PeakAvoidTracker
        t = PeakAvoidTracker()
        t.record_pending_avoid("dev_bad", "Badkamer", 1.8, ts=NOW)
        t.discard_pending("dev_bad")
        later = NOW.replace(hour=18)
        t.record_pending_avoid("dev_bad", "Badkamer", 1.8, ts=later)
        assert t._pending["dev_bad"].avoid_ts == later
