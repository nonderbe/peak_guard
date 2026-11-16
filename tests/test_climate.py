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

    async def test_idle_thermostat_is_left_on_its_schedule(self, hass, ev_guard):
        """Op temperatuur: niet uitzetten, anders stopt het eigen tijdschema."""
        _set(hass, "heat", action="idle")
        snaps: dict = {}
        ctx = _ctx(hass, ev_guard, "peak")
        left = await _device().apply(2500.0, snaps, ctx)
        assert left == pytest.approx(2500.0)
        assert hass.services.calls == []
        assert snaps == {}
        assert ctx.peak_tracker.pending_avoids == []

    async def test_already_off_is_skipped(self, hass, ev_guard):
        _set(hass, "off")
        assert await _device().apply(2500.0, {}, _ctx(hass, ev_guard, "peak")) == 2500.0
        assert hass.services.calls == []

    async def test_restore_after_a_peak_only_restores_the_mode(self, hass, ev_guard):
        """De piek wijzigde enkel de modus: geen set_temperature (Tuya → manueel)."""
        _set(hass, "off")
        ctx = _ctx(hass, ev_guard, "peak")
        snap = DeviceSnapshot(entity_id=ENTITY, original_state="heat", original_temp=21.0)
        assert await _device().restore(snap, ctx) is True
        (call,) = hass.services.calls
        assert call["service"] == "set_hvac_mode"
        assert call["data"] == {"entity_id": ENTITY, "hvac_mode": "heat"}
        assert ctx.peak_tracker.turn_on_measurements == ["dev_bad"]

    async def test_restore_keeps_a_setpoint_changed_by_the_schedule(self, hass, ev_guard):
        """Eigen schema zette tijdens het overschot 22 °C: dat blijft staan."""
        _set(hass, "heat", temp=22.0)
        ctx = _ctx(hass, ev_guard, "solar")
        snap = DeviceSnapshot(entity_id=ENTITY, original_state="heat", original_temp=18.0)
        assert await _device().restore(snap, ctx) is True
        assert hass.services.calls == []


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


class TestTuyaThermostat:
    """Tuya 'Vloerverwarming RM': hvac_modes [off, heat_cool], geen hvac_action."""

    def _tuya(self, hass, mode="heat_cool", temp=10.0, room=17.5):
        hass.states.set(ENTITY, mode, {
            "hvac_modes": ["off", "heat_cool"], "temperature": temp,
            "current_temperature": room, "target_temp_step": 0.5,
        })

    async def test_surplus_uses_the_thermostats_own_heat_mode(self, hass, ev_guard):
        self._tuya(hass, mode="off")
        await _device().apply(2000.0, {}, _ctx(hass, ev_guard, "solar"))
        (call,) = hass.services.calls
        assert call["data"] == {"entity_id": ENTITY, "temperature": 24.0, "hvac_mode": "heat_cool"}

    async def test_already_in_heat_cool_only_sets_the_temperature(self, hass, ev_guard):
        self._tuya(hass)
        await _device().apply(2000.0, {}, _ctx(hass, ev_guard, "solar"))
        (call,) = hass.services.calls
        assert call["data"] == {"entity_id": ENTITY, "temperature": 24.0}

    async def test_satisfied_thermostat_frees_nothing_on_a_peak(self, hass, ev_guard):
        """Doel 10 °C, gemeten 17,5 °C: verwarmt niet."""
        self._tuya(hass)
        ctx = _ctx(hass, ev_guard, "peak")
        assert await _device().apply(2500.0, {}, ctx) == pytest.approx(2500.0)
        assert ctx.peak_tracker.pending_avoids == []

    async def test_cold_room_counts_as_heating(self, hass, ev_guard):
        self._tuya(hass, temp=24.0, room=19.0)
        ctx = _ctx(hass, ev_guard, "peak")
        assert await _device().apply(2500.0, {}, ctx) == pytest.approx(700.0)


class TestPeakSetback:
    """peak_setback: doeltemperatuur bijstellen i.p.v. uitschakelen (airco)."""

    def _ac(self, **kw):
        return _device(peak_setback=2, inject_temp=None, power_watts=800, **kw)

    def test_serialisation(self):
        dev = self._ac()
        assert dev.peak_setback == 2.0
        assert from_dict(asdict(dev)) == dev
        assert _device(peak_setback="").peak_setback is None
        assert _device(peak_setback=0).peak_setback is None

    async def test_heating_lowers_the_setpoint(self, hass, ev_guard):
        _set(hass, "heat", temp=21.0, room=20.0, action=None)
        snaps: dict = {}
        ctx = _ctx(hass, ev_guard, "peak")
        left = await self._ac().apply(2500.0, snaps, ctx)
        assert left == pytest.approx(1700.0)
        (call,) = hass.services.calls
        assert call["service"] == "set_temperature"
        assert call["data"] == {"entity_id": ENTITY, "temperature": 19.0}
        snap = snaps[ENTITY]
        assert (snap.original_state, snap.original_temp, snap.set_temp) == ("heat", 21.0, 19.0)
        assert snap.pending is None
        assert ctx.peak_tracker.pending_avoids == [{"id": "dev_bad", "kw": 0.8, "deferred": False}]

    async def test_cooling_raises_the_setpoint(self, hass, ev_guard):
        _set(hass, "cool", temp=24.0, room=26.0, action=None)
        snaps: dict = {}
        await self._ac().apply(2500.0, snaps, _ctx(hass, ev_guard, "peak"))
        (call,) = hass.services.calls
        assert call["data"]["temperature"] == 26.0

    async def test_auto_mode_follows_hvac_action(self, hass, ev_guard):
        _set(hass, "heat_cool", temp=21.0, room=21.0, action="heating")
        snaps: dict = {}
        await self._ac().apply(2500.0, snaps, _ctx(hass, ev_guard, "peak"))
        assert hass.services.calls[0]["data"]["temperature"] == 19.0

    async def test_fan_only_and_unknown_direction_are_skipped(self, hass, ev_guard):
        for mode in ("fan_only", "dry"):
            _set(hass, mode, action=None)
            assert await self._ac().apply(2500.0, {}, _ctx(hass, ev_guard, "peak")) == 2500.0
        _set(hass, "auto", temp=21.0, room=21.0, action=None)
        assert await self._ac().apply(2500.0, {}, _ctx(hass, ev_guard, "peak")) == 2500.0
        assert hass.services.calls == []

    async def test_idle_unit_is_skipped(self, hass, ev_guard):
        _set(hass, "heat", action="idle")
        assert await self._ac().apply(2500.0, {}, _ctx(hass, ev_guard, "peak")) == 2500.0
        assert hass.services.calls == []

    async def test_not_lowered_twice(self, hass, ev_guard):
        _set(hass, "heat", temp=21.0, action=None)
        snaps: dict = {}
        dev = self._ac()
        await dev.apply(2500.0, snaps, _ctx(hass, ev_guard, "peak"))
        left = await dev.apply(2500.0, snaps, _ctx(hass, ev_guard, "peak"))
        assert left == 2500.0
        assert len(hass.services.calls) == 1
        assert hass.states.get(ENTITY).attributes["temperature"] == 19.0

    async def test_clamped_to_min_temp(self, hass, ev_guard):
        hass.states.set(ENTITY, "heat", {"temperature": 11.0, "min_temp": 10.0})
        await self._ac().apply(2500.0, {}, _ctx(hass, ev_guard, "peak"))
        assert hass.services.calls[0]["data"]["temperature"] == 10.0
        hass.services.calls.clear()
        hass.states.set(ENTITY, "heat", {"temperature": 10.0, "min_temp": 10.0})
        assert await self._ac().apply(2500.0, {}, _ctx(hass, ev_guard, "peak")) == 2500.0
        assert hass.services.calls == []

    async def test_restore_sets_the_original_temperature_only(self, hass, ev_guard):
        _set(hass, "heat", temp=19.0, action=None)
        ctx = _ctx(hass, ev_guard, "peak")
        snap = DeviceSnapshot(entity_id=ENTITY, original_state="heat",
                              original_temp=21.0, set_temp=19.0)
        assert await self._ac().restore(snap, ctx) is True
        (call,) = hass.services.calls
        assert call["service"] == "set_temperature"
        assert call["data"] == {"entity_id": ENTITY, "temperature": 21.0}
        assert ctx.peak_tracker.turn_on_measurements == ["dev_bad"]

    async def test_restore_keeps_a_user_change(self, hass, ev_guard):
        """Gebruiker zette tijdens de piek 23 °C, of zette de airco uit: niets terugzetten."""
        for mode, temp in (("heat", 23.0), ("off", 19.0)):
            hass.services.calls.clear()
            _set(hass, mode, temp=temp, action=None)
            snap = DeviceSnapshot(entity_id=ENTITY, original_state="heat",
                                  original_temp=21.0, set_temp=19.0)
            assert await self._ac().restore(snap, _ctx(hass, ev_guard, "peak")) is True
            assert hass.services.calls == []

    async def test_unacknowledged_setback_then_late_ack(self, hass, ev_guard):
        """Cloud-airco die het commando nog niet uitvoerde: open tot de nieuwe setpoint."""
        _set(hass, "heat", temp=21.0, action=None)
        hass.services.no_reflect.add(ENTITY)
        snaps: dict = {}
        ctx = _ctx(hass, ev_guard, "peak")
        dev = self._ac()
        assert await dev.apply(2500.0, snaps, ctx) == pytest.approx(1700.0)
        snap = snaps[ENTITY]
        assert snap.pending == "apply"
        assert ctx.peak_tracker.pending_avoids == []
        assert dev.applied(hass.states.get(ENTITY), snap, "peak") is False
        _set(hass, "heat", temp=19.0, action=None)
        assert dev.applied(hass.states.get(ENTITY), snap, "peak") is True

    async def test_snapshot_round_trip_keeps_set_temp(self):
        snap = DeviceSnapshot(entity_id=ENTITY, original_state="heat",
                              original_temp=21.0, set_temp=19.0)
        assert DeviceSnapshot(**asdict(snap)) == snap
