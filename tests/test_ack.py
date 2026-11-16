"""
tests/test_ack.py — bevestiging (ack) van schakelacties, vermogenssensor,
max. uit / min. aan per apparaat (v1.9.7), en de fixes voor bug 3 (solarpad
op een EV die niet laadt) en bug 5 (achtergebleven pending afschakeling).

Een schakelaar of thermostaat telt pas als de entiteit de nieuwe toestand
meldt. Een cloud-apparaat (SmartThings-wasmachine) meldt dat pas seconden tot
een minuut later; tot dan blijft zijn vermogen gereserveerd, en de vermeden
piek wordt pas bij de bevestiging geboekt — vanaf het commando.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone

import pytest

from custom_components.peak_guard import const
from custom_components.peak_guard.avoided_peak_tracker import PeakAvoidTracker
from custom_components.peak_guard.deciders.ev_guard import EVGuard
from custom_components.peak_guard.deciders.injection_decider import InjectionDecider
from custom_components.peak_guard.deciders.peak_decider import PeakDecider
from custom_components.peak_guard.models import (
    CascadeContext,
    ClimateDevice,
    DeviceSnapshot,
    EVChargerDevice,
    SwitchOffDevice,
    SwitchOnDevice,
    from_dict,
    str_to_ts,
)

from tests.conftest import MockHass, MockPeakTracker, MockSolarTracker

PEAK_SENSOR = "sensor.p1_maandpiek"
T0 = datetime(2026, 11, 16, 10, 0, 0, tzinfo=timezone.utc)
WASHER = "switch.wasmachine_peak_guard"
WASHER_POWER = "sensor.wasmachine_geschat_vermogen"


def _t(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


async def _save():
    return None


def _washer(**kw) -> SwitchOffDevice:
    base = dict(id="washer", name="Wasmachine", entity_id=WASHER, priority=1,
                action_type="switch_off", power_watts=2000)
    base.update(kw)
    return SwitchOffDevice(**base)


def _boiler(priority=2) -> SwitchOffDevice:
    return SwitchOffDevice(id="boiler", name="Boiler", entity_id="switch.boiler",
                           priority=priority, action_type="switch_off", power_watts=2000)


def _peak(hass, ev_guard, cascade, snapshots, tracker=None) -> PeakDecider:
    return PeakDecider(
        hass=hass, config={"peak_sensor": PEAK_SENSOR, "buffer_watts": 100},
        peak_tracker=tracker or MockPeakTracker(), solar_tracker=MockSolarTracker(),
        ev_guard=ev_guard, iteration_actions=[], save_fn=_save,
        cascade=cascade, snapshots=snapshots,
    )


def _injection(hass, ev_guard, cascade, snapshots) -> InjectionDecider:
    return InjectionDecider(
        hass=hass, config={}, peak_tracker=MockPeakTracker(),
        solar_tracker=MockSolarTracker(), ev_guard=ev_guard, iteration_actions=[],
        save_fn=_save, cascade=cascade, snapshots=snapshots,
    )


def _ctx(hass, ev_guard, cascade_type="peak", now=T0, peak_tracker=None) -> CascadeContext:
    return CascadeContext(
        hass=hass, cascade_type=cascade_type,
        peak_tracker=peak_tracker or MockPeakTracker(), solar_tracker=MockSolarTracker(),
        ev_guard=ev_guard, track_action=lambda *a, **k: None,
        warn=lambda *a, **k: None, now=now,
    )


@pytest.fixture
def hass() -> MockHass:
    h = MockHass()
    h.states.set(PEAK_SENSOR, "5000")          # limiet 4900 W
    h.states.set(WASHER, "on")
    h.states.set("switch.boiler", "on")
    return h


@pytest.fixture
def ev_guard(hass) -> EVGuard:
    return EVGuard(hass=hass, config={}, iteration_actions=[])


def _laggy(hass, entity_id=WASHER):
    """Het apparaat voert het commando (nog) niet uit: geen nieuwe toestand."""
    hass.services.no_reflect.add(entity_id)


# ═══════════════════════════════════════════════════════════════════════════ #
#  Afschakelen: bevestiging                                                   #
# ═══════════════════════════════════════════════════════════════════════════ #

class TestShedAck:
    async def test_immediate_ack_books_as_before(self, hass, ev_guard):
        snaps: dict = {}
        tracker = MockPeakTracker()
        left = await _washer().apply(2500.0, snaps, _ctx(hass, ev_guard, peak_tracker=tracker))
        assert left == pytest.approx(500.0)
        snap = snaps[WASHER]
        assert snap.pending is None
        assert str_to_ts(snap.off_since) == T0
        assert snap.shed_w == 2000.0
        assert tracker.pending_avoids == [{"id": "washer", "kw": 2.0, "deferred": False}]

    async def test_laggy_device_reserves_but_books_nothing(self, hass, ev_guard):
        _laggy(hass)
        snaps: dict = {}
        tracker = MockPeakTracker()
        left = await _washer().apply(2500.0, snaps, _ctx(hass, ev_guard, peak_tracker=tracker))
        # Gereserveerd: de boiler erachter hoeft niet mee af.
        assert left == pytest.approx(500.0)
        assert snaps[WASHER].pending == "apply"
        assert snaps[WASHER].off_since is None
        assert tracker.pending_avoids == []

    async def test_pending_device_is_not_commanded_again_and_keeps_reserving(self, hass, ev_guard):
        _laggy(hass)
        snaps: dict = {}
        await _washer().apply(2500.0, snaps, _ctx(hass, ev_guard))
        left = await _washer().apply(2500.0, snaps, _ctx(hass, ev_guard, now=_t(60)))
        assert left == pytest.approx(500.0)
        assert len(hass.services.calls_for("turn_off")) == 1
        assert ev_guard.hold_for("washer")["code"] == "awaiting_ack"

    async def test_late_ack_books_from_the_command_time(self, hass, ev_guard):
        _laggy(hass)
        tracker = PeakAvoidTracker()
        snaps: dict = {}
        peak = _peak(hass, ev_guard, [_washer()], snaps, tracker=tracker)
        await peak.check(7500.0, T0)                       # 2600 W overschot
        assert "washer" not in tracker.get_pending_ids()
        hass.states.set(WASHER, "off")                     # SmartThings meldt de pauze
        await peak.check_restore(7500.0, _t(40))
        assert snaps[WASHER].pending is None
        assert tracker._pending["washer"].avoid_ts == T0   # commando, niet de bevestiging
        assert str_to_ts(snaps[WASHER].off_since) == _t(40)

    async def test_no_ack_after_timeout_moves_on_to_the_next_device(self, hass, ev_guard):
        _laggy(hass)
        snaps: dict = {}
        peak = _peak(hass, ev_guard, [_washer(), _boiler()], snaps)
        await peak.check(7000.0, T0)                       # 2100 W: wasmachine reserveert 2000
        assert hass.services.calls_for("turn_off") == [
            {"domain": "switch", "service": "turn_off", "data": {"entity_id": WASHER}},
            {"domain": "switch", "service": "turn_off", "data": {"entity_id": "switch.boiler"}},
        ]
        hass.services.calls.clear()
        await peak.check(7000.0, _t(60))                   # nog gereserveerd, boiler al uit
        assert hass.services.calls_for("turn_off") == []
        hass.states.set("switch.boiler", "on")             # boiler terug aan (manueel)
        await peak.check(7000.0, _t(120))                  # > 90 s: wasmachine telt niet meer
        assert snaps[WASHER].pending == "noack"
        assert [c["data"]["entity_id"] for c in hass.services.calls_for("turn_off")] == [
            "switch.boiler"
        ]
        assert ev_guard.hold_for("washer")["code"] == "no_ack"

    async def test_no_ack_is_resent_after_the_resend_interval(self, hass, ev_guard):
        _laggy(hass)
        snaps: dict = {}
        peak = _peak(hass, ev_guard, [_washer()], snaps)
        await peak.check(7500.0, T0)
        await peak.check(7500.0, _t(120))
        assert len(hass.services.calls_for("turn_off")) == 1
        await peak.check(7500.0, _t(const.ACK_RESEND_S + 1))
        assert len(hass.services.calls_for("turn_off")) == 2
        assert snaps[WASHER].pending == "apply"

    async def test_unconfirmed_shed_is_not_restored(self, hass, ev_guard):
        _laggy(hass)
        snaps: dict = {}
        peak = _peak(hass, ev_guard, [_washer()], snaps)
        await peak.check(7500.0, T0)
        await peak.check_restore(1000.0, _t(30))           # ruimte genoeg
        assert WASHER in snaps                              # wacht nog op de pauze
        assert not hass.services.has_call("turn_on")

    async def test_never_executed_shed_is_dropped_once_there_is_room(self, hass, ev_guard):
        _laggy(hass)
        tracker = MockPeakTracker()
        snaps: dict = {}
        peak = _peak(hass, ev_guard, [_washer()], snaps, tracker=tracker)
        await peak.check(7500.0, T0)
        await peak.check_restore(1000.0, _t(120))          # 'noack', nog altijd aan
        assert WASHER not in snaps
        assert not hass.services.has_call("turn_on")
        assert tracker.pending_avoids == []

    async def test_climate_shed_waits_for_off(self, hass, ev_guard):
        hass.states.set("climate.vloer", "heat", {"hvac_action": "heating", "temperature": 21.0})
        _laggy(hass, "climate.vloer")
        dev = ClimateDevice(id="vloer", name="Vloer", entity_id="climate.vloer", priority=1,
                            action_type="climate", power_watts=1800)
        snaps: dict = {}
        tracker = MockPeakTracker()
        left = await dev.apply(2000.0, snaps, _ctx(hass, ev_guard, peak_tracker=tracker))
        assert left == pytest.approx(200.0)
        assert snaps["climate.vloer"].pending == "apply"
        assert tracker.pending_avoids == []


# ═══════════════════════════════════════════════════════════════════════════ #
#  Herstel: bevestiging                                                       #
# ═══════════════════════════════════════════════════════════════════════════ #

def _paused_snapshot(off_since=T0, shed_w=2000.0) -> DeviceSnapshot:
    return DeviceSnapshot(entity_id=WASHER, original_state="on",
                          off_since=off_since.isoformat(), shed_w=shed_w)


class TestRestoreAck:
    async def test_laggy_restore_keeps_the_snapshot_and_reserves(self, hass, ev_guard):
        hass.states.set(WASHER, "off")
        _laggy(hass)
        tracker = MockPeakTracker()
        snaps = {WASHER: _paused_snapshot(),
                 "switch.boiler": DeviceSnapshot(entity_id="switch.boiler", original_state="on",
                                                 off_since=T0.isoformat(), shed_w=2000.0)}
        hass.states.set("switch.boiler", "off")
        peak = _peak(hass, ev_guard, [_boiler(priority=1), _washer(priority=2)], snaps,
                     tracker=tracker)
        # 3000 W ruimte: de wasmachine (laagste prioriteit eerst) gaat terug aan,
        # maar bevestigt niet. Haar 2000 W blijft gereserveerd: de boiler past niet.
        await peak.check_restore(1900.0, _t(600))
        assert snaps[WASHER].pending == "restore"
        assert "switch.boiler" in snaps
        assert tracker.turn_on_measurements == []
        assert ev_guard.hold_for("washer")["code"] == "awaiting_ack"

    async def test_restore_ack_starts_the_measurement(self, hass, ev_guard):
        hass.states.set(WASHER, "off")
        _laggy(hass)
        tracker = MockPeakTracker()
        snaps = {WASHER: _paused_snapshot()}
        peak = _peak(hass, ev_guard, [_washer()], snaps, tracker=tracker)
        await peak.check_restore(1000.0, _t(600))
        hass.states.set(WASHER, "on")
        await peak.check_restore(1000.0, _t(660))
        assert WASHER not in snaps
        assert tracker.turn_on_measurements == ["washer"]
        assert peak._restored_at[WASHER] == _t(660)

    async def test_restore_is_resent_and_finally_given_up(self, hass, ev_guard):
        hass.states.set(WASHER, "off")
        _laggy(hass)
        tracker = MockPeakTracker()
        snaps = {WASHER: _paused_snapshot()}
        peak = _peak(hass, ev_guard, [_washer()], snaps, tracker=tracker)
        await peak.check_restore(1000.0, _t(600))
        assert len(hass.services.calls_for("turn_on")) == 1
        await peak.check_restore(1000.0, _t(600 + const.ACK_RESEND_S + 1))
        assert len(hass.services.calls_for("turn_on")) == 2
        await peak.check_restore(1000.0, _t(600 + const.ACK_GIVE_UP_S + 1))
        assert WASHER not in snaps
        assert tracker.discarded == ["washer"]
        assert tracker.turn_on_measurements == []


# ═══════════════════════════════════════════════════════════════════════════ #
#  Vermogenssensor                                                            #
# ═══════════════════════════════════════════════════════════════════════════ #

class TestPowerSensor:
    async def test_sensor_value_is_shed_and_stored(self, hass, ev_guard):
        hass.states.set(WASHER_POWER, "1600", {"unit_of_measurement": "W"})
        snaps: dict = {}
        tracker = MockPeakTracker()
        left = await _washer(power_sensor=WASHER_POWER).apply(
            2500.0, snaps, _ctx(hass, ev_guard, peak_tracker=tracker))
        assert left == pytest.approx(900.0)
        assert snaps[WASHER].shed_w == 1600.0
        assert tracker.pending_avoids == [{"id": "washer", "kw": 1.6, "deferred": False}]

    async def test_kw_sensor_is_converted(self, hass, ev_guard):
        hass.states.set(WASHER_POWER, "1.6", {"unit_of_measurement": "kW"})
        snaps: dict = {}
        left = await _washer(power_sensor=WASHER_POWER).apply(2500.0, snaps, _ctx(hass, ev_guard))
        assert left == pytest.approx(900.0)

    async def test_device_drawing_nothing_is_left_alone(self, hass, ev_guard):
        hass.states.set(WASHER_POWER, "40", {"unit_of_measurement": "W"})   # spoelen
        snaps: dict = {}
        left = await _washer(power_sensor=WASHER_POWER).apply(2500.0, snaps, _ctx(hass, ev_guard))
        assert left == pytest.approx(2500.0)
        assert snaps == {}
        assert hass.services.calls == []

    async def test_unavailable_sensor_falls_back_to_power_watts(self, hass, ev_guard):
        hass.states.set(WASHER_POWER, "unavailable")
        snaps: dict = {}
        left = await _washer(power_sensor=WASHER_POWER).apply(2500.0, snaps, _ctx(hass, ev_guard))
        assert left == pytest.approx(500.0)

    async def test_restore_needs_the_power_at_shed_time(self, hass, ev_guard):
        hass.states.set(WASHER, "off")
        hass.states.set(WASHER_POWER, "0", {"unit_of_measurement": "W"})    # gepauzeerd
        snaps = {WASHER: _paused_snapshot(shed_w=1600.0)}
        peak = _peak(hass, ev_guard, [_washer(power_sensor=WASHER_POWER)], snaps)
        await peak.check_restore(3400.0, _t(600))          # 1500 W ruimte < 1600 W
        assert WASHER in snaps
        assert ev_guard.hold_for("washer")["code"] == "peak_headroom"
        await peak.check_restore(3200.0, _t(660))          # 1700 W ruimte
        assert WASHER not in snaps


# ═══════════════════════════════════════════════════════════════════════════ #
#  Max. uit / min. aan                                                        #
# ═══════════════════════════════════════════════════════════════════════════ #

class TestPauseLimits:
    async def test_max_off_restores_without_headroom(self, hass, ev_guard):
        hass.states.set(WASHER, "off")
        snaps = {WASHER: _paused_snapshot()}
        peak = _peak(hass, ev_guard, [_washer(max_off_s=1200)], snaps)
        await peak.check_restore(6000.0, _t(1199))         # geen ruimte, < 20 min
        assert WASHER in snaps
        await peak.check_restore(6000.0, _t(1200))
        assert WASHER not in snaps
        assert hass.services.has_call("turn_on")
        assert ev_guard.hold_for("washer")["code"] == "max_off"

    async def test_max_off_counts_from_the_ack(self, hass, ev_guard):
        _laggy(hass)
        snaps: dict = {}
        peak = _peak(hass, ev_guard, [_washer(max_off_s=1200)], snaps)
        await peak.check(7500.0, T0)
        hass.states.set(WASHER, "off")
        await peak.check_restore(7500.0, _t(60))           # bevestigd op t=60
        hass.services.no_reflect.clear()                   # hervatten gaat meteen
        await peak.check_restore(7500.0, _t(1230))
        assert WASHER in snaps
        await peak.check_restore(7500.0, _t(1260))
        assert WASHER not in snaps

    async def test_min_on_skips_the_device_and_sheds_the_next(self, hass, ev_guard):
        snaps: dict = {}
        washer = _washer(max_off_s=1200, min_on_s=600)
        peak = _peak(hass, ev_guard, [washer, _boiler()], snaps)
        await peak.check(6500.0, T0)                       # 1600 W: wasmachine pauzeert
        assert hass.states.get(WASHER).state == "off"
        await peak.check_restore(6500.0, _t(1200))         # max. uit: hervat
        assert hass.states.get(WASHER).state == "on"
        await peak.check(6500.0, _t(1260))                 # binnen 10 min: boiler i.p.v. wasmachine
        assert hass.states.get(WASHER).state == "on"
        assert hass.states.get("switch.boiler").state == "off"
        assert ev_guard.hold_for("washer")["code"] == "min_on"
        hass.states.set("switch.boiler", "on")
        snaps.pop("switch.boiler")
        await peak.check(6500.0, _t(1200 + 601))           # na 10 min weer wasmachine
        assert hass.states.get(WASHER).state == "off"

    async def test_no_limits_keeps_the_old_behaviour(self, hass, ev_guard):
        hass.states.set(WASHER, "off")
        snaps = {WASHER: _paused_snapshot()}
        peak = _peak(hass, ev_guard, [_washer()], snaps)
        await peak.check_restore(6000.0, _t(7200))
        assert WASHER in snaps

    def test_runtime_survives_a_restart(self, hass, ev_guard):
        peak = _peak(hass, ev_guard, [_washer(min_on_s=600)], {})
        peak._restored_at[WASHER] = T0
        other = _peak(hass, ev_guard, [_washer(min_on_s=600)], {})
        other.load_runtime(peak.runtime_to_dict())
        assert other._restored_at == {WASHER: T0}

    def test_fields_round_trip_and_clear(self):
        dev = from_dict(asdict(_washer(power_sensor=WASHER_POWER, max_off_s=1200, min_on_s=600)))
        assert (dev.power_sensor, dev.max_off_s, dev.min_on_s) == (WASHER_POWER, 1200, 600)
        cleared = from_dict({**asdict(dev), "max_off_s": None, "min_on_s": "", "power_sensor": None})
        assert (cleared.power_sensor, cleared.max_off_s, cleared.min_on_s) == ("", None, None)
        old = from_dict({"id": "b", "name": "B", "entity_id": "switch.b", "priority": 1,
                         "action_type": "switch_off"})
        assert (old.power_sensor, old.max_off_s, old.min_on_s) == ("", None, None)

    def test_snapshot_round_trips_as_json(self):
        snap = _paused_snapshot()
        snap.pending, snap.cmd_at = "restore", _t(5).isoformat()
        assert DeviceSnapshot(**asdict(snap)) == snap


# ═══════════════════════════════════════════════════════════════════════════ #
#  Solar-cascade: bevestiging                                                 #
# ═══════════════════════════════════════════════════════════════════════════ #

class TestSolarAck:
    async def test_inject_switch_measures_from_the_ack(self, hass, ev_guard):
        hass.states.set("switch.boiler", "off")
        _laggy(hass, "switch.boiler")
        dev = SwitchOnDevice(id="boiler", name="Boiler", entity_id="switch.boiler",
                             priority=1, action_type="switch_on", power_watts=2000)
        snaps: dict = {}
        inj = _injection(hass, ev_guard, [dev], snaps)
        await inj.check(-2500.0, T0)
        assert snaps["switch.boiler"].pending == "apply"
        assert inj.solar_tracker.started == []
        hass.states.set("switch.boiler", "on")
        await inj.check(-500.0, _t(60))
        assert snaps["switch.boiler"].pending is None
        assert inj.solar_tracker.started == [{"id": "boiler", "kw": 2.0}]
        assert len(hass.services.calls_for("turn_on")) == 1

    async def test_inject_restore_waits_for_off(self, hass, ev_guard):
        hass.states.set("switch.boiler", "on")
        _laggy(hass, "switch.boiler")
        dev = SwitchOnDevice(id="boiler", name="Boiler", entity_id="switch.boiler",
                             priority=1, action_type="switch_on", power_watts=2000)
        snaps = {"switch.boiler": DeviceSnapshot(entity_id="switch.boiler", original_state="off")}
        inj = _injection(hass, ev_guard, [dev], snaps)
        await inj.check_restore(300.0, T0)
        assert snaps["switch.boiler"].pending == "restore"
        assert inj.solar_tracker.completed == []
        hass.states.set("switch.boiler", "off")
        await inj.check_restore(300.0, _t(60))
        assert "switch.boiler" not in snaps
        assert inj.solar_tracker.completed == ["boiler"]


# ═══════════════════════════════════════════════════════════════════════════ #
#  Bug 5: achtergebleven pending                                              #
# ═══════════════════════════════════════════════════════════════════════════ #

class TestStalePending:
    def test_fresh_shed_replaces_a_stale_pending(self):
        tracker = PeakAvoidTracker()
        old = T0 - timedelta(days=3)
        tracker.record_pending_avoid("washer", "Wasmachine", 2.0, ts=old)
        tracker.record_pending_avoid("washer", "Wasmachine", 2.0, ts=T0, fresh=True)
        assert tracker._pending["washer"].avoid_ts == T0

    def test_reshed_without_fresh_keeps_the_original_time(self):
        tracker = PeakAvoidTracker()
        tracker.record_pending_avoid("washer", "Wasmachine", 2.0, ts=T0)
        tracker.record_pending_avoid("washer", "Wasmachine", 2.0, ts=_t(60))
        assert tracker._pending["washer"].avoid_ts == T0

    async def test_device_found_on_at_restore_discards_its_pending(self, hass, ev_guard):
        tracker = PeakAvoidTracker()
        tracker.record_pending_avoid("washer", "Wasmachine", 2.0, ts=T0)
        snaps = {WASHER: _paused_snapshot()}               # maar de wasmachine draait (manueel)
        peak = _peak(hass, ev_guard, [_washer()], snaps, tracker=tracker)
        await peak.check_restore(1000.0, _t(600))
        assert WASHER not in snaps
        assert tracker.get_pending_ids() == []

    async def test_manual_override_drop_discards_the_pending(self, hass, ev_guard):
        hass.states.set(WASHER, "off")
        tracker = MockPeakTracker()
        snaps = {WASHER: _paused_snapshot()}
        peak = _peak(hass, ev_guard, [_washer(manual_override=True)], snaps, tracker=tracker)
        await peak.check_restore(1000.0, _t(600))
        assert WASHER not in snaps
        assert tracker.discarded == ["washer"]


# ═══════════════════════════════════════════════════════════════════════════ #
#  Bug 3: solarpad op een EV die niet laadt                                   #
# ═══════════════════════════════════════════════════════════════════════════ #

def _ev(**kw) -> EVChargerDevice:
    base = dict(
        id="ev", name="Tesla", entity_id="switch.tesla_charge", priority=2,
        action_type="ev_charger", min_value=6.0, max_value=16.0,
        switch_entity="switch.tesla_charge", current_entity="number.tesla_charge_current",
        soc_entity="number.tesla_charge_limit", max_soc=100.0, phases=1, min_current=6.0,
    )
    base.update(kw)
    return EVChargerDevice(**base)


@pytest.fixture
def ev_hass() -> MockHass:
    h = MockHass()
    h.states.set("switch.tesla_charge", "off")
    h.states.set("number.tesla_charge_current", "16")
    h.states.set("number.tesla_charge_limit", "80")
    h.states.set("device_tracker.tesla", "not_home")
    return h


class TestSolarOnStoppedEV:
    async def test_away_car_gets_no_snapshot_and_no_soc_override(self, ev_hass):
        guard = EVGuard(hass=ev_hass, config={}, iteration_actions=[])
        snaps: dict = {}
        dev = _ev(location_tracker="device_tracker.tesla")
        left = await guard.apply_action(
            dev, 150.0, snaps, "solar", MockPeakTracker(), MockSolarTracker(), now=T0)
        assert left == pytest.approx(150.0)
        assert snaps == {}
        assert ev_hass.services.calls == []

    async def test_below_threshold_gets_no_snapshot(self, ev_hass):
        guard = EVGuard(hass=ev_hass, config={}, iteration_actions=[])
        snaps: dict = {}
        await guard.apply_action(
            _ev(start_threshold_w=1500), 150.0, snaps, "solar",
            MockPeakTracker(), MockSolarTracker(), now=T0)
        assert snaps == {}
        assert ev_hass.services.calls == []

    async def test_throttle_down_leaves_a_stopped_car_alone(self, ev_hass):
        guard = EVGuard(hass=ev_hass, config={}, iteration_actions=[])
        assert await guard.throttle_down_solar(_ev(), consumption=2000.0, now=T0) is False
        assert ev_hass.services.calls == []

    async def test_stale_snapshot_restore_heals_a_lowered_current(self, ev_hass):
        """Een vóór v1.9.7 verlaagde stroom van een stilstaande wagen komt terug."""
        guard = EVGuard(hass=ev_hass, config={}, iteration_actions=[])
        guard.get_guard("ev").last_sent_amps = 8.0
        ev_hass.states.set("number.tesla_charge_current", "8")
        snap = DeviceSnapshot(entity_id="switch.tesla_charge", original_state="off",
                              original_current=16.0, original_soc=80.0)
        ok = await guard.restore(_ev(), snap, MockPeakTracker(), MockSolarTracker(),
                                 cascade_type="solar", now=T0)
        assert ok is True
        values = [c["data"]["value"] for c in ev_hass.services.calls_for("set_value")]
        assert 16.0 in values
        assert not ev_hass.services.has_call("turn_off")

    async def test_unavailable_original_state_restores(self, ev_hass):
        guard = EVGuard(hass=ev_hass, config={}, iteration_actions=[])
        ev_hass.states.set("switch.tesla_charge", "on")
        snap = DeviceSnapshot(entity_id="switch.tesla_charge", original_state="unavailable",
                              original_current=16.0)
        ok = await guard.restore(_ev(), snap, MockPeakTracker(), MockSolarTracker(),
                                 cascade_type="solar", now=T0)
        assert ok is True
        assert ev_hass.services.has_call("turn_off")


# ═══════════════════════════════════════════════════════════════════════════ #
#  Review-bevindingen (v1.9.7)                                                #
# ═══════════════════════════════════════════════════════════════════════════ #

class TestReviewFindings:
    async def test_solar_restore_turns_off_a_switch_that_reports_on(self, ev_hass):
        """#1: laadstatus 'disconnected' maar schakelaar 'on' → toch uitzetten."""
        ev_hass.states.set("switch.tesla_charge", "on")
        ev_hass.states.set("sensor.tesla_opladen", "disconnected")
        guard = EVGuard(hass=ev_hass, config={}, iteration_actions=[])
        snap = DeviceSnapshot(entity_id="switch.tesla_charge", original_state="off",
                              original_current=16.0)
        ok = await guard.restore(_ev(charge_state_sensor="sensor.tesla_opladen"), snap,
                                 MockPeakTracker(), MockSolarTracker(),
                                 cascade_type="solar", now=T0)
        assert ok is True
        assert ev_hass.services.has_call("turn_off")

    async def test_unknown_switch_without_charge_state_is_throttled(self, ev_hass):
        """#2: Tesla zonder laadstatus, door Peak Guard gestart → verlagen, niet stoppen."""
        from custom_components.peak_guard.models import EVState
        ev_hass.states.set("switch.tesla_charge", "unknown")
        ev_hass.states.set("number.tesla_charge_current", "8")
        guard = EVGuard(hass=ev_hass, config={}, iteration_actions=[])
        g = guard.get_guard("ev")
        g.state, g.last_switch_state = EVState.CHARGING, True
        assert await guard.throttle_down_solar(_ev(), consumption=300.0, now=T0) is True
        assert not ev_hass.services.has_call("turn_off")

    async def test_noack_expires_and_a_later_off_books_nothing(self, hass, ev_guard):
        """#3: nooit uitgevoerde pauze, uren later gaat het apparaat zelf uit."""
        _laggy(hass)
        tracker = MockPeakTracker()
        snaps: dict = {}
        peak = _peak(hass, ev_guard, [_washer()], snaps, tracker=tracker)
        await peak.check(7500.0, T0)
        await peak.check(4000.0, _t(120))                  # 'noack'
        await peak.check(4000.0, _t(const.ACK_GIVE_UP_S + 1))
        assert WASHER not in snaps
        hass.states.set(WASHER, "off")
        await peak.check_restore(1000.0, _t(3 * 3600))
        assert tracker.pending_avoids == []
        assert not hass.services.has_call("turn_on")

    async def test_old_pending_apply_after_restart_counts_but_books_nothing(self, hass, ev_guard):
        """#11: bewaarde 'apply' van gisteren, apparaat staat uit."""
        hass.states.set(WASHER, "off")
        tracker = MockPeakTracker()
        old = (T0 - timedelta(days=1)).isoformat()
        snaps = {WASHER: DeviceSnapshot(entity_id=WASHER, original_state="on", pending="apply",
                                        cmd_at=old, pending_since=old, shed_w=2000.0)}
        peak = _peak(hass, ev_guard, [_washer()], snaps, tracker=tracker)
        await peak.check(1000.0, T0)
        assert snaps[WASHER].pending is None
        assert str_to_ts(snaps[WASHER].off_since) == T0
        assert tracker.pending_avoids == []

    async def test_dropping_a_never_executed_shed_does_not_start_min_on(self, hass, ev_guard):
        """#4: snapshot opgeruimd zonder herstel → min_on blokkeert niets."""
        _laggy(hass)
        snaps: dict = {}
        peak = _peak(hass, ev_guard, [_washer(min_on_s=600)], snaps)
        await peak.check(7500.0, T0)
        await peak.check_restore(1000.0, _t(120))          # noack, nog aan → opgeruimd
        assert WASHER not in snaps
        assert WASHER not in peak._restored_at
        hass.services.no_reflect.clear()
        await peak.check(7500.0, _t(180))
        assert hass.states.get(WASHER).state == "off"

    async def test_restart_resends_an_old_pending_restore_before_giving_up(self, hass, ev_guard):
        """#5: na een herstart eerst opnieuw versturen, pas later opgeven."""
        hass.states.set(WASHER, "off")
        _laggy(hass)
        old = (T0 - timedelta(hours=1)).isoformat()
        snaps = {WASHER: DeviceSnapshot(entity_id=WASHER, original_state="on", pending="restore",
                                        cmd_at=old, pending_since=old, shed_w=2000.0,
                                        off_since=old)}
        peak = _peak(hass, ev_guard, [_washer()], snaps)
        await peak.check_restore(1000.0, T0)
        assert len(hass.services.calls_for("turn_on")) == 1
        assert WASHER in snaps
        await peak.check_restore(1000.0, _t(const.ACK_RESEND_S + 1))
        assert WASHER not in snaps

    async def test_max_off_is_enforced_without_a_consumption_reading(self, hass, ev_guard):
        """#6: run_tick(None) dwingt max_off_s ook af."""
        from custom_components.peak_guard.deciders.dispatch import run_tick

        class _NoSchedule:
            async def check(self, consumption, now):
                return None

        hass.states.set(WASHER, "off")
        snaps = {WASHER: _paused_snapshot()}
        peak = _peak(hass, ev_guard, [_washer(max_off_s=1200)], snaps)
        await run_tick(None, _t(1200), peak, _NoSchedule(), _injection(hass, ev_guard, [], {}))
        assert WASHER not in snaps

    async def test_max_off_alone_still_keeps_the_device_on_for_a_while(self, hass, ev_guard):
        """#7: zonder min_on_s geldt minstens PEAK_SWITCH_MIN_OFF_S."""
        hass.states.set(WASHER, "off")
        snaps = {WASHER: _paused_snapshot()}
        peak = _peak(hass, ev_guard, [_washer(max_off_s=1200)], snaps)
        await peak.check_restore(6500.0, _t(1200))
        assert hass.states.get(WASHER).state == "on"
        await peak.check(6500.0, _t(1260))
        assert hass.states.get(WASHER).state == "on"
        assert ev_guard.hold_for("washer")["code"] == "min_on"

    async def test_climate_solar_ack_tolerates_a_rounded_setpoint(self, hass, ev_guard):
        """#8: thermostaat rondt 22,5 af naar 23."""
        dev = ClimateDevice(id="vloer", name="Vloer", entity_id="climate.vloer", priority=1,
                            action_type="climate", power_watts=1000, inject_temp=22.5)
        state = type("S", (), {"state": "heat", "attributes": {
            "temperature": 23.0, "hvac_modes": ["off", "heat"]}})()
        assert dev.applied(state, DeviceSnapshot(entity_id="climate.vloer",
                                                 original_state="heat"), "solar")

    async def test_manual_override_drops_a_pending_restore(self, hass, ev_guard):
        """#9: manuele bediening stopt het opnieuw versturen."""
        hass.states.set(WASHER, "off")
        _laggy(hass)
        tracker = MockPeakTracker()
        washer = _washer()
        snaps = {WASHER: _paused_snapshot()}
        peak = _peak(hass, ev_guard, [washer], snaps, tracker=tracker)
        await peak.check_restore(1000.0, _t(600))
        assert snaps[WASHER].pending == "restore"
        washer.manual_override = True
        await peak.check_restore(1000.0, _t(600 + const.ACK_RESEND_S + 1))
        assert WASHER not in snaps
        assert len(hass.services.calls_for("turn_on")) == 1
        assert tracker.discarded == ["washer"]

    async def test_peak_stopped_ev_is_left_to_solar_during_injection(self, ev_hass):
        """Review #10: tijdens injectie herstart het piekherstel een gestopte EV niet."""
        ev_hass.states.set(PEAK_SENSOR, "5000")
        guard = EVGuard(hass=ev_hass, config={}, iteration_actions=[])
        g = guard.get_guard("ev")
        g.last_switch_state = False
        g.turned_off_by_pg = True
        g.turned_off_at = T0 - timedelta(hours=1)
        ev = _ev()
        snaps = {"switch.tesla_charge": DeviceSnapshot(
            entity_id="switch.tesla_charge", original_state="on", original_current=16.0)}
        peak = _peak(ev_hass, guard, [ev], snaps)
        peak.set_solar_owner_fn(lambda d: True)
        await peak.check_restore(-2000.0, T0)
        assert not ev_hass.services.has_call("turn_on")
        assert guard.hold_for("ev")["code"] == "peak_solar_handover"
        await peak.check_restore(500.0, _t(60))            # geen injectie meer: piekherstel
        assert ev_hass.services.has_call("turn_on")

    async def test_resend_from_noack_restarts_the_ack_window(self, hass, ev_guard):
        """Review 3 #1: een herhaling vlak voor 30 min vervalt niet meteen."""
        _laggy(hass)
        snaps: dict = {}
        peak = _peak(hass, ev_guard, [_washer()], snaps)
        await peak.check(7500.0, T0)
        await peak.check(7500.0, _t(120))                  # noack
        await peak.check(7500.0, _t(const.ACK_GIVE_UP_S - 30))   # herhaling
        assert snaps[WASHER].pending == "apply"
        await peak.check_restore(7500.0, _t(const.ACK_GIVE_UP_S + 10))
        assert WASHER in snaps                             # nog binnen de nieuwe termijn
        hass.states.set(WASHER, "off")
        await peak.check_restore(7500.0, _t(const.ACK_GIVE_UP_S + 70))
        assert snaps[WASHER].pending is None

    async def test_disabled_device_still_finishes_its_restore(self, hass, ev_guard):
        """Review 3 #2: uitgeschakeld tijdens een onbevestigd herstel → blijft herstellen."""
        hass.states.set(WASHER, "off")
        _laggy(hass)
        washer = _washer()
        snaps = {WASHER: _paused_snapshot()}
        peak = _peak(hass, ev_guard, [washer], snaps)
        await peak.check_restore(1000.0, _t(600))
        washer.enabled = False
        await peak.check_restore(1000.0, _t(600 + const.ACK_RESEND_S + 1))
        assert snaps[WASHER].pending == "restore"
        assert len(hass.services.calls_for("turn_on")) == 2


# ═══════════════════════════════════════════════════════════════════════════ #
#  Bugs 1, 2, 9, 10 uit de eerste review                                      #
# ═══════════════════════════════════════════════════════════════════════════ #

class TestFirstReviewBugs:
    async def test_stopped_ev_does_not_swallow_the_excess(self, ev_hass):
        """Bug 1: een door Peak Guard gestopte EV telt niet opnieuw als afgeschakeld."""
        ev_hass.states.set("switch.tesla_charge", "unknown")
        ev_hass.states.set("sensor.tesla_status", "on")         # binaire online-sensor
        ev_hass.states.set(PEAK_SENSOR, "5000")
        ev_hass.states.set("switch.boiler", "on")
        guard = EVGuard(hass=ev_hass, config={}, iteration_actions=[])
        guard.get_guard("ev").last_switch_state = False
        ev = _ev(priority=1, status_sensor="sensor.tesla_status")
        peak = _peak(ev_hass, guard, [ev, _boiler()], {})
        await peak.check(5800.0, T0)                       # 900 W overschot
        assert ev_hass.states.get("switch.boiler").state == "off"

    async def test_full_car_is_not_throttled_by_the_peak(self, ev_hass):
        """Bug 1-variant: laadstatus 'complete' → geen set_value, geen snapshot."""
        ev_hass.states.set("switch.tesla_charge", "on")
        ev_hass.states.set("sensor.tesla_opladen", "complete")
        guard = EVGuard(hass=ev_hass, config={}, iteration_actions=[])
        snaps: dict = {}
        left = await guard.apply_action(
            _ev(charge_state_sensor="sensor.tesla_opladen"), 920.0, snaps, "peak",
            MockPeakTracker(), MockSolarTracker(), now=T0)
        assert left == pytest.approx(920.0)
        assert not ev_hass.services.has_call("set_value")

    async def test_unexpected_error_skips_only_that_device(self, hass, ev_guard):
        """Bug 2: een onverwachte fout bij één apparaat stopt de cascade niet."""
        class Broken(SwitchOffDevice):
            async def apply(self, excess, snapshots, ctx):
                raise RuntimeError("cloud stuk")

        broken = Broken(id="kapot", name="Kapot", entity_id="switch.kapot", priority=1,
                        action_type="switch_off", power_watts=2000)
        peak = _peak(hass, ev_guard, [broken, _boiler()], {})
        await peak.check(6500.0, T0)
        assert hass.states.get("switch.boiler").state == "off"

    async def test_unexpected_error_in_restore_is_contained(self, hass, ev_guard):
        class Broken(SwitchOffDevice):
            async def restore(self, snapshot, ctx):
                raise RuntimeError("cloud stuk")

        hass.states.set("switch.kapot", "off")
        hass.states.set("switch.boiler", "off")
        snaps = {
            "switch.kapot": DeviceSnapshot(entity_id="switch.kapot", original_state="on",
                                           off_since=T0.isoformat(), shed_w=500.0),
            "switch.boiler": DeviceSnapshot(entity_id="switch.boiler", original_state="on",
                                            off_since=T0.isoformat(), shed_w=2000.0),
        }
        broken = Broken(id="kapot", name="Kapot", entity_id="switch.kapot", priority=2,
                        action_type="switch_off", power_watts=500)
        peak = _peak(hass, ev_guard, [_boiler(priority=1), broken], snaps)
        await peak.check_restore(1000.0, _t(600))
        assert "switch.kapot" in snaps
        assert "switch.boiler" not in snaps

    def test_open_measurements_survive_a_restart(self):
        """Bug 9: pending en actieve metingen worden bewaard."""
        tracker = PeakAvoidTracker()
        tracker.record_pending_avoid("boiler", "Boiler", 2.0, ts=T0)
        tracker.record_pending_avoid("vloer", "Vloer", 1.8, ts=T0)
        tracker.start_measurement_on_turnon("vloer", "Vloer", ts=_t(600))
        other = PeakAvoidTracker()
        other.measurements_from_dict(tracker.measurements_to_dict())
        assert other._pending["boiler"].avoid_ts == T0
        assert other._active["vloer"].turnon_ts == _t(600)
        assert other.measurements_fingerprint() == tracker.measurements_fingerprint()
        other.measurements_from_dict({"pending": [{"device_id": "x"}], "active": None})
        assert "x" not in other._pending

    async def test_reduced_charging_ev_gets_no_turn_on(self, ev_hass):
        """Bug 10: herstel van een verlaagde (ladende) EV stuurt geen turn_on."""
        ev_hass.states.set("switch.tesla_charge", "unknown")
        ev_hass.states.set("sensor.tesla_opladen", "charging")
        ev_hass.states.set("number.tesla_charge_current", "8")
        guard = EVGuard(hass=ev_hass, config={}, iteration_actions=[])
        snap = DeviceSnapshot(entity_id="switch.tesla_charge", original_state="on",
                              original_current=16.0)
        ok = await guard.restore(_ev(charge_state_sensor="sensor.tesla_opladen"), snap,
                                 MockPeakTracker(), MockSolarTracker(), cascade_type="peak", now=T0)
        assert ok is True
        assert not ev_hass.services.has_call("turn_on")
        assert [c["data"]["value"] for c in ev_hass.services.calls_for("set_value")] == [16.0]

    async def test_is_charging_refusal_does_not_fail_the_restore(self):
        """Bug 10: Tesla weigert turn_on met 'is_charging' → herstel gaat door."""
        h = MockHass(raise_on={"turn_on"})
        h.services.error_message = "Command was unsuccessful: is_charging"
        h.states.set("switch.tesla_charge", "unknown")
        h.states.set("number.tesla_charge_current", "8")
        guard = EVGuard(hass=h, config={}, iteration_actions=[])
        snap = DeviceSnapshot(entity_id="switch.tesla_charge", original_state="on",
                              original_current=16.0)
        ok = await guard.restore(_ev(soc_entity=None, max_soc=None), snap,
                                 MockPeakTracker(), MockSolarTracker(), cascade_type="peak", now=T0)
        assert ok is True
        assert h.services.has_call("set_value")


# ═══════════════════════════════════════════════════════════════════════════ #
#  Tweede review (v1.9.7): verouderde Tesla-laadstatus                        #
# ═══════════════════════════════════════════════════════════════════════════ #

def _tesla(**kw) -> EVChargerDevice:
    return _ev(charge_state_sensor="sensor.tesla_opladen", **kw)


@pytest.fixture
def stale_hass() -> MockHass:
    h = MockHass()
    h.states.set("switch.tesla_charge", "unknown")
    h.states.set("sensor.tesla_opladen", "charging")
    h.states.set("number.tesla_charge_current", "16")
    h.states.set("number.tesla_charge_limit", "80")
    return h


class TestStaleChargeState:
    async def test_peak_stopped_car_restarted_despite_stale_charging(self, stale_hass):
        """H1: na de eigen turn_off blijft de laadstatus 'charging' — het
        herstel moet toch turn_on sturen, anders blijft de wagen stil."""
        g = EVGuard(hass=stale_hass, config={}, iteration_actions=[])
        dev, snaps = _tesla(), {}
        await g.apply_action(dev, 5000.0, snaps, "peak", MockPeakTracker(), MockSolarTracker(), now=T0)
        assert stale_hass.services.has_call("turn_off")
        n = len(stale_hass.services.calls)
        ok = await g.restore(dev, snaps["switch.tesla_charge"], MockPeakTracker(), MockSolarTracker(),
                             cascade_type="peak", now=T0 + timedelta(minutes=6))
        assert ok is True
        assert any(c["service"] == "turn_on" for c in stale_hass.services.calls[n:])

    async def test_no_phantom_reduction_right_after_own_stop(self, stale_hass):
        """M2: tick na de eigen turn_off, laadstatus en stroom nog oud."""
        stale_hass.services.no_reflect.update({"number.tesla_charge_current", "switch.tesla_charge"})
        g = EVGuard(hass=stale_hass, config={}, iteration_actions=[])
        dev, snaps = _tesla(), {}
        await g.apply_action(dev, 5000.0, snaps, "peak", MockPeakTracker(), MockSolarTracker(), now=T0)
        n = len(stale_hass.services.calls)
        left = await g.apply_action(dev, 500.0, snaps, "peak", MockPeakTracker(), MockSolarTracker(),
                                    now=T0 + timedelta(seconds=60))
        assert left == pytest.approx(500.0)
        assert stale_hass.services.calls[n:] == []

    async def test_is_charging_refusal_confirms_charging_until_state_changes(self, stale_hass):
        stale_hass.states.set("sensor.tesla_opladen", "stopped")
        stale_hass.services._raise_on.add("turn_on")
        stale_hass.services.error_message = "Command was unsuccessful: is_charging"
        g = EVGuard(hass=stale_hass, config={}, iteration_actions=[])
        dev = _tesla()
        assert not g.is_charging(dev)
        ok, err = await g._turn_on_with_retry(dev, "switch.tesla_charge", "SCHEMA", "laadschema")
        assert ok and err is not None
        assert g.is_charging(dev), "de wagen meldde zelf dat hij laadt"
        stale_hass.states.set("sensor.tesla_opladen", "complete")
        assert not g.is_charging(dev), "een nieuwe laadstatus wint"

    async def test_confirmation_ignored_after_own_turn_off(self, stale_hass):
        stale_hass.states.set("sensor.tesla_opladen", "stopped")
        g = EVGuard(hass=stale_hass, config={}, iteration_actions=[])
        dev = _tesla()
        g.confirm_charging(dev)
        assert g.is_charging(dev)
        await g._svc("switch", "turn_off", {"entity_id": "switch.tesla_charge"})
        assert not g.is_charging(dev)

    async def test_peak_reduces_a_freshly_started_car_with_stale_stopped(self, stale_hass):
        """R1: schema start de wagen, HA toont nog 'stopped' — de piek moet
        hem toch kunnen verlagen (het schema verlaagt zelf niet)."""
        stale_hass.states.set("sensor.tesla_opladen", "stopped")
        stale_hass.states.set("binary_sensor.tesla_status", "on")
        stale_hass.services.no_reflect.add("switch.tesla_charge")
        g = EVGuard(hass=stale_hass, config={}, iteration_actions=[])
        dev = _tesla(status_sensor="binary_sensor.tesla_status")
        assert await g.schedule_start(dev, 16, T0) is None
        n = len(stale_hass.services.calls)
        left = await g.apply_action(dev, 2000.0, {}, "peak", MockPeakTracker(), MockSolarTracker(),
                                    now=T0 + timedelta(minutes=2))
        assert left < 2000.0
        assert any(c["service"] in ("set_value", "turn_off") for c in stale_hass.services.calls[n:])

    async def test_full_car_not_counted_after_start(self, stale_hass):
        """'complete' blijft gelden, ook vlak na een eigen start."""
        stale_hass.states.set("sensor.tesla_opladen", "complete")
        stale_hass.states.set("binary_sensor.tesla_status", "on")
        g = EVGuard(hass=stale_hass, config={}, iteration_actions=[])
        dev = _tesla(status_sensor="binary_sensor.tesla_status")
        guard = g.get_guard("ev")
        guard.last_switch_state, guard.turned_on_at = True, T0
        left = await g.apply_action(dev, 2000.0, {}, "peak", MockPeakTracker(), MockSolarTracker(),
                                    now=T0 + timedelta(minutes=2))
        assert left == pytest.approx(2000.0)

    async def test_schedule_start_on_charging_car_keeps_the_current(self, stale_hass):
        """M1: geen set_value naar een lading die al loopt."""
        stale_hass.states.set("sensor.tesla_opladen", "stopped")
        stale_hass.services._raise_on.add("turn_on")
        stale_hass.services.error_message = "Command was unsuccessful: is_charging"
        g = EVGuard(hass=stale_hass, config={}, iteration_actions=[])
        skip = await g.schedule_start(_tesla(), 6, T0)
        assert skip is not None and "laadt al" in skip
        assert not stale_hass.services.has_call("set_value")
        assert g.get_guard("ev").last_switch_state is True


# ═══════════════════════════════════════════════════════════════════════════ #
#  v1.9.8: bug 7 — door solar gestarte EV niet herstarten na een piek         #
# ═══════════════════════════════════════════════════════════════════════════ #

class TestSolarStartedEVAfterPeak:
    def _setup(self, stale_hass, stopped: bool):
        g = EVGuard(hass=stale_hass, config={}, iteration_actions=[])
        dev = _tesla()
        guard = g.get_guard("ev")
        if stopped:
            stale_hass.states.set("sensor.tesla_opladen", "stopped")
            guard.last_switch_state, guard.turned_off_by_pg, guard.turned_off_at = False, True, T0
        peak_snaps = {"switch.tesla_charge": DeviceSnapshot(
            entity_id="switch.tesla_charge", original_state="on", original_current=16.0)}
        inject_snaps = {"switch.tesla_charge": DeviceSnapshot(
            entity_id="switch.tesla_charge", original_state="off", original_current=16.0,
            original_soc=80.0)}
        tracker = MockPeakTracker()
        peak = _peak(stale_hass, g, [dev], peak_snaps, tracker)
        return peak, peak_snaps, inject_snaps, tracker

    async def test_peak_stopped_solar_ev_is_not_restarted(self, stale_hass):
        stale_hass.states.set(PEAK_SENSOR, "5000")
        peak, peak_snaps, inject_snaps, tracker = self._setup(stale_hass, stopped=True)
        peak.take_over_from_solar(inject_snaps, _t(60))
        assert peak_snaps == {}, "piek-herstel mag hem niet herstarten"
        assert "switch.tesla_charge" in inject_snaps, "solar-herstel ruimt zelf op"
        assert "ev" in tracker.discarded
        await peak.check_restore(300.0, _t(900))
        assert not stale_hass.services.has_call("turn_on")

    async def test_reduced_solar_ev_keeps_both_snapshots(self, stale_hass):
        peak, peak_snaps, inject_snaps, _ = self._setup(stale_hass, stopped=False)
        peak.take_over_from_solar(inject_snaps, _t(60))
        assert "switch.tesla_charge" in peak_snaps
        assert "switch.tesla_charge" in inject_snaps


class TestNoChargeStateCharger:
    async def test_switch_back_on_is_shed_again(self, ev_hass):
        """L1: lader zonder laadstatus, door PG gestopt, snapshot zonder herstel
        verdwenen, schakelaar weer 'on' → de piek mag hem opnieuw afschakelen."""
        ev_hass.states.set("switch.tesla_charge", "on")
        g = EVGuard(hass=ev_hass, config={}, iteration_actions=[])
        guard = g.get_guard("ev")
        guard.last_switch_state, guard.turned_off_by_pg = False, True
        guard.turned_off_at = T0 - timedelta(hours=1)
        left = await g.apply_action(_ev(), 5000.0, {}, "peak", MockPeakTracker(),
                                    MockSolarTracker(), now=T0)
        assert left < 5000.0
        assert ev_hass.services.has_call("turn_off") or ev_hass.services.has_call("set_value")


class TestRestoredPendingPrune:
    def test_restored_pending_without_snapshot_is_dropped(self):
        """L2: herstart vlak na een herstel brengt een al voltooide afschakeling terug."""
        t = PeakAvoidTracker()
        item = {"device_id": "washer", "device_name": "Wasmachine", "nominal_kw": 2.0,
                "avoid_ts": T0.isoformat(), "deferred": False}
        item2 = dict(item, device_id="boiler", device_name="Boiler")
        t.measurements_from_dict({"pending": [item, item2], "active": []})
        t.prune_restored_pendings({"boiler"})
        assert set(t._pending) == {"boiler"}
        # Daarna raakt het nieuwe afschakelingen niet meer.
        t.prune_restored_pendings(set())
        assert set(t._pending) == {"boiler"}

    async def test_peak_decider_prunes_via_its_snapshots(self, hass, ev_guard):
        tracker = PeakAvoidTracker()
        item = {"device_id": "washer", "device_name": "Wasmachine", "nominal_kw": 2.0,
                "avoid_ts": T0.isoformat(), "deferred": False}
        tracker.measurements_from_dict({"pending": [item], "active": []})
        snaps = {WASHER: DeviceSnapshot(entity_id=WASHER, original_state="on")}
        peak = _peak(hass, ev_guard, [_washer()], snaps, tracker)
        peak.prune_restored_measurements()
        assert "washer" in tracker._pending


class TestRepeatedDeviceError:
    async def test_failing_device_warns_once(self, hass, ev_guard):
        """L3: een apparaat dat elke cyclus faalt, geeft één paneelwaarschuwing."""
        class Broken(SwitchOffDevice):
            async def apply(self, excess, snapshots, ctx):
                raise RuntimeError("cloud stuk")

        broken = Broken(id="kapot", name="Kapot", entity_id="switch.kapot", priority=1,
                        action_type="switch_off", power_watts=2000)
        peak = _peak(hass, ev_guard, [broken], {})
        for i in range(3):
            await peak.check(6500.0, _t(60 * i))
        msgs = [w["message"] for w in ev_guard.recent_warnings if "Kapot" in w["message"]]
        assert len(msgs) == 1


class TestSolarStartedReducedEV:
    """Tweede review v1.9.8: de piek verlaagde (niet stopte) een door solar gestarte EV."""

    def _setup(self, stale_hass, owner=True):
        g = EVGuard(hass=stale_hass, config={}, iteration_actions=[])
        stale_hass.states.set(PEAK_SENSOR, "5000")
        stale_hass.states.set("number.tesla_charge_current", "11")
        dev = _tesla()
        peak_snaps = {"switch.tesla_charge": DeviceSnapshot(
            entity_id="switch.tesla_charge", original_state="on", original_current=16.0)}
        inject_snaps = {"switch.tesla_charge": DeviceSnapshot(
            entity_id="switch.tesla_charge", original_state="off", original_current=16.0,
            original_soc=80.0)}
        tracker = MockPeakTracker()
        peak = _peak(stale_hass, g, [dev], peak_snaps, tracker)
        peak.set_solar_owner_fn(lambda d: owner)
        peak.take_over_from_solar(inject_snaps, _t(60))
        return peak, peak_snaps, inject_snaps, tracker

    async def test_not_restarted_once_solar_is_done(self, stale_hass):
        peak, peak_snaps, inject_snaps, tracker = self._setup(stale_hass)
        assert peak_snaps["switch.tesla_charge"].solar_started
        inject_snaps.clear()            # solar-herstel stopte hem na het overschot
        stale_hass.states.set("sensor.tesla_opladen", "stopped")
        await peak.check_restore(300.0, _t(900))
        assert peak_snaps == {}
        assert not stale_hass.services.has_call("turn_on")
        assert not stale_hass.services.has_call("set_value")

    async def test_no_current_raise_while_solar_steers(self, stale_hass):
        peak, peak_snaps, inject_snaps, _ = self._setup(stale_hass)
        await peak.check_restore(300.0, _t(900))
        assert "switch.tesla_charge" in peak_snaps
        assert not stale_hass.services.has_call("set_value")
        assert peak.ev_guard.hold_for("ev")["code"] == "peak_solar_handover"

    async def test_hand_over_keeps_solar_original_off(self, stale_hass):
        peak, peak_snaps, inject_snaps, _ = self._setup(stale_hass)
        peak.hand_over_to_solar(inject_snaps, _t(120))
        assert peak_snaps == {}
        assert inject_snaps["switch.tesla_charge"].original_state == "off"

    async def test_solar_copy_in_manual_override_keeps_peak_restore(self, stale_hass):
        """Kan de solar-cascade niet handelen, dan blijft het gewone piekherstel."""
        peak, peak_snaps, _, _ = self._setup(stale_hass, owner=False)
        assert not peak_snaps["switch.tesla_charge"].solar_started


class TestThrottleOnPeakStoppedEV:
    async def test_no_set_value_to_a_car_pg_just_stopped(self, stale_hass):
        g = EVGuard(hass=stale_hass, config={}, iteration_actions=[])
        guard = g.get_guard("ev")
        guard.last_switch_state, guard.turned_off_by_pg, guard.turned_off_at = False, True, T0
        assert await g.throttle_down_solar(_tesla(), consumption=2000.0, now=_t(30)) is False
        assert stale_hass.services.calls == []


class TestSolarStartedPersistence:
    async def test_flag_is_saved_in_the_same_tick(self, stale_hass):
        saves = []

        async def save():
            saves.append(1)

        g = EVGuard(hass=stale_hass, config={}, iteration_actions=[])
        stale_hass.states.set(PEAK_SENSOR, "5000")
        peak_snaps = {"switch.tesla_charge": DeviceSnapshot(
            entity_id="switch.tesla_charge", original_state="on", original_current=16.0)}
        inject_snaps = {"switch.tesla_charge": DeviceSnapshot(
            entity_id="switch.tesla_charge", original_state="off")}
        peak = _peak(stale_hass, g, [_tesla()], peak_snaps)
        peak._save_fn = save
        peak.take_over_from_solar(inject_snaps, _t(60))
        await peak.check_restore(9000.0, _t(60))      # geen headroom
        assert peak_snaps["switch.tesla_charge"].solar_started
        assert saves, "markering moet bewaard worden"
