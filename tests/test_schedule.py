"""
Peak Guard — tests/test_schedule.py

Laadschema (Planning-tab): venster-logica, (de)serialisatie, ScheduleDecider
en de wisselwerking piek > schema > injectie via deciders/dispatch.run_tick.

De Tesla-configuratie volgt een echte installatie: 1 fase, max 8 A,
schakelaar on/off, laadstatus via een aparte sensor (charging/stopped/complete),
status_sensor enkel online/slaap.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.peak_guard.const import CONF_BUFFER_WATTS, CONF_PEAK_SENSOR
from custom_components.peak_guard.deciders.dispatch import run_tick
from custom_components.peak_guard.deciders.ev_guard import EVGuard
from custom_components.peak_guard.deciders.injection_decider import InjectionDecider
from custom_components.peak_guard.deciders.peak_decider import PeakDecider
from custom_components.peak_guard.deciders.schedule_decider import (
    ScheduleDecider,
    ScheduleRunState,
)
from custom_components.peak_guard.models import (
    DeviceSnapshot,
    EVChargerDevice,
    ScheduleWindow,
    SwitchOnDevice,
)
from custom_components.peak_guard.schedule import (
    days_label,
    entry_from_dict,
    evaluate_entry,
    normalize_state,
    parse_hhmm,
    time_window_covers,
    window_from_dict,
)
from tests.conftest import (
    TEST_TIME_ZONE,
    MockHass,
    MockPeakTracker,
    MockSolarTracker,
)

SW = "switch.tesla_opladen"
CUR = "number.tesla_charge_current"
SOC = "number.tesla_charge_limit"
BAT = "sensor.tesla_batterij"
CHG = "sensor.tesla_opladen"
TARIFF = "sensor.p1_meter_tarief"


def local(y, mo, d, h, mi=0) -> datetime:
    """Lokale Brusselse tijd → UTC."""
    return datetime(y, mo, d, h, mi, tzinfo=TEST_TIME_ZONE).astimezone(timezone.utc)


# 2026-10-05 is een maandag.
MON_23 = local(2026, 10, 5, 23, 0)


def tesla(dev_id="ev_tesla") -> EVChargerDevice:
    return EVChargerDevice(
        id=dev_id, name="Tesla", entity_id=SW, priority=1, action_type="ev_charger",
        max_value=8.0, switch_entity=SW, current_entity=CUR, soc_entity=SOC,
        battery_entity=BAT, max_soc=100, phases=1, charge_state_sensor=CHG,
    )


def entry_dict(windows, **kw) -> dict:
    d = {"id": "sch1", "device": asdict(tesla()), "windows": windows}
    d.update(kw)
    return d


WEEKNIGHT = {"kind": "time", "days": [0, 1, 2, 3, 4], "start": "22:00", "end": "06:00", "target_soc": 65}


def build(entries, *, inject=True, peak=True, peak_w=5000.0):
    hass = MockHass()
    config = {CONF_PEAK_SENSOR: "sensor.peak", CONF_BUFFER_WATTS: 100}
    hass.states.set("sensor.peak", str(peak_w))
    hass.states.set(SW, "off")
    hass.states.set(CUR, "6")
    hass.states.set(SOC, "50")
    hass.states.set(BAT, "40")
    hass.states.set(CHG, "stopped")
    ev_guard = EVGuard(hass=hass, config=config, iteration_actions=[])
    peak_snaps: dict = {}
    inject_snaps: dict = {}
    peak_cascade = [tesla()] if peak else []
    inject_cascade = [tesla()] if inject else []
    pt, st = MockPeakTracker(), MockSolarTracker()
    save = AsyncMock()
    sd = ScheduleDecider(
        hass=hass, config=config, peak_tracker=pt, solar_tracker=st, ev_guard=ev_guard,
        iteration_actions=[], save_fn=save, peak_cascade=peak_cascade,
        inject_cascade=inject_cascade, peak_snapshots=peak_snaps, inject_snapshots=inject_snaps,
    )
    sd.set_entries([e for e in (entry_from_dict(d) for d in entries) if e is not None])
    ev_guard.set_soc_rest_lookup(sd.soc_rest_for)
    common = dict(hass=hass, config=config, peak_tracker=pt, solar_tracker=st,
                  ev_guard=ev_guard, iteration_actions=[], save_fn=save)
    pd = PeakDecider(cascade=peak_cascade, snapshots=peak_snaps, **common)
    pd.set_skip_fn(sd.skip_peak_restore)
    inj = InjectionDecider(cascade=inject_cascade, snapshots=inject_snaps, **common)
    inj.set_skip_fn(sd.is_controlled)

    async def tick(consumption, now):
        await run_tick(consumption, now, peak=pd, schedule=sd, injection=inj)

    return SimpleNamespace(
        hass=hass, sd=sd, ev=ev_guard, peak=pd, inj=inj, pt=pt, st=st,
        peak_snaps=peak_snaps, inject_snaps=inject_snaps, tick=tick,
        calls=hass.services.calls,
    )


def services(env):
    return [(c["service"], c["data"].get("entity_id"), c["data"].get("value")) for c in env.calls]


# ═══════════════════════════════════════════════════════════════════════════ #
#  1. Venster-logica                                                           #
# ═══════════════════════════════════════════════════════════════════════════ #

class TestWindowLogic:
    def _w(self, days, start, end):
        return ScheduleWindow(days=days, start=start, end=end)

    def test_parse_hhmm(self):
        assert parse_hhmm("22:00") == 1320
        assert parse_hhmm("24:00") == 1440
        assert parse_hhmm("24:30") is None
        assert parse_hhmm("7") is None

    def test_plain_window(self):
        w = self._w([0], "08:00", "10:00")
        assert time_window_covers(w, datetime(2026, 10, 5, 8, 0))
        assert time_window_covers(w, datetime(2026, 10, 5, 9, 59))
        assert not time_window_covers(w, datetime(2026, 10, 5, 10, 0))
        assert not time_window_covers(w, datetime(2026, 10, 6, 9, 0))   # dinsdag

    def test_midnight_crossing_belongs_to_start_day(self):
        w = self._w([4], "22:00", "06:00")                               # vrijdag
        assert time_window_covers(w, datetime(2026, 10, 9, 22, 0))       # vr 22:00
        assert time_window_covers(w, datetime(2026, 10, 10, 5, 59))      # za 05:59
        assert not time_window_covers(w, datetime(2026, 10, 10, 6, 0))   # za 06:00
        assert not time_window_covers(w, datetime(2026, 10, 9, 5, 0))    # vr 05:00 (do-nacht)

    def test_full_day(self):
        w = self._w([5], "00:00", "00:00")                               # zaterdag
        assert time_window_covers(w, datetime(2026, 10, 10, 0, 0))
        assert time_window_covers(w, datetime(2026, 10, 10, 23, 59))
        assert not time_window_covers(w, datetime(2026, 10, 11, 0, 0))

    def test_dst_fall_back_night(self):
        # Nacht van 24 → 25 oktober 2026: 03:00 CEST → 02:00 CET.
        entry = entry_from_dict(entry_dict([{**WEEKNIGHT, "days": [5]}]))
        assert evaluate_entry(entry, local(2026, 10, 25, 5, 30), {}).active
        assert not evaluate_entry(entry, local(2026, 10, 25, 6, 0), {}).active

    def test_dst_spring_forward_night(self):
        # Nacht van 28 → 29 maart 2026: 02:00 CET → 03:00 CEST.
        entry = entry_from_dict(entry_dict([{**WEEKNIGHT, "days": [5]}]))
        assert evaluate_entry(entry, local(2026, 3, 29, 3, 30), {}).active
        assert not evaluate_entry(entry, local(2026, 3, 29, 6, 0), {}).active

    def test_overlap_takes_highest_target(self):
        entry = entry_from_dict(entry_dict([
            {**WEEKNIGHT, "target_soc": 60},
            {"kind": "time", "days": [0], "start": "23:00", "end": "01:00", "target_soc": 80},
        ]))
        assert evaluate_entry(entry, MON_23, {}).target_soc == 80
        assert evaluate_entry(entry, local(2026, 10, 5, 22, 30), {}).target_soc == 60

    def test_disabled_window_ignored(self):
        entry = entry_from_dict(entry_dict([{**WEEKNIGHT, "enabled": False}]))
        assert not evaluate_entry(entry, MON_23, {}).active

    def test_bad_windows_dropped(self):
        assert window_from_dict({"kind": "time", "days": [], "start": "22:00", "end": "06:00"}) is None
        assert window_from_dict({"kind": "time", "days": [1], "start": "25:00", "end": "06:00"}) is None
        assert window_from_dict({"kind": "sensor", "entity_id": "", "active_state": "2"}) is None
        assert window_from_dict({"kind": "weird"}) is None

    def test_sensor_window(self):
        entry = entry_from_dict(entry_dict([
            {"kind": "sensor", "entity_id": TARIFF, "active_state": "2", "target_soc": 65},
        ]))
        assert evaluate_entry(entry, MON_23, {TARIFF: normalize_state("2.0")}).active
        assert not evaluate_entry(entry, MON_23, {TARIFF: "1"}).active
        assert not evaluate_entry(entry, MON_23, {TARIFF: None}).active

    def test_days_label(self):
        assert days_label([0, 1, 2, 3, 4]) == "ma–vr"
        assert days_label([5, 6]) == "za, zo"
        assert days_label(range(7)) == "elke dag"


class TestSerialisation:
    def test_entry_round_trip(self):
        entry = entry_from_dict(entry_dict([WEEKNIGHT], rest_soc=50, max_current=7))
        again = entry_from_dict(asdict(entry))
        assert asdict(again) == asdict(entry)
        assert again.device.charge_state_sensor == CHG
        assert again.rest_soc == 50 and again.max_current == 7.0

    def test_unschedulable_type_rejected(self):
        d = entry_dict([WEEKNIGHT])
        d["device"]["action_type"] = "throttle"
        assert entry_from_dict(d) is None

    def test_run_state_round_trip(self):
        rs = ScheduleRunState(active=True, started_at=MON_23, target_soc=65)
        again = ScheduleRunState.from_dict(rs.to_dict())
        assert again.started_at == MON_23 and again.target_soc == 65

    def test_duplicate_entity_ignored(self):
        env = build([entry_dict([WEEKNIGHT]), {**entry_dict([WEEKNIGHT]), "id": "sch2"}])
        assert len(env.sd.entries) == 1


# ═══════════════════════════════════════════════════════════════════════════ #
#  2. EV in een venster                                                        #
# ═══════════════════════════════════════════════════════════════════════════ #

class TestEVInWindow:
    async def test_starts_with_peak_aware_current(self):
        # Piek 5000 W − buffer 100 − huis 3300 W = 1600 W → 6 A (≥ hw-min 6 A).
        env = build([entry_dict([WEEKNIGHT])])
        await env.tick(3300.0, MON_23)
        assert ("set_value", SOC, 65.0) in services(env)
        assert ("turn_on", SW, None) in services(env)
        assert ("set_value", CUR, 6.0) in services(env)
        assert env.ev.get_guard("ev_tesla").scheduled

    async def test_caps_at_max_current(self):
        env = build([entry_dict([WEEKNIGHT], max_current=7)])
        await env.tick(500.0, MON_23)
        assert ("set_value", CUR, 7.0) in services(env)

    async def test_no_start_without_headroom(self):
        env = build([entry_dict([WEEKNIGHT])])
        await env.tick(4000.0, MON_23)             # ruimte 900 W < 6 A × 230 V
        assert not any(s == "turn_on" for s, _, _ in services(env))
        assert env.sd.status_dict()["sch1"]["status"] == "wacht op piekruimte"

    async def test_no_start_when_peak_sensor_unavailable(self):
        env = build([entry_dict([WEEKNIGHT])])
        env.hass.states.set("sensor.peak", "unavailable")
        await env.tick(500.0, MON_23)
        assert not any(s == "turn_on" for s, _, _ in services(env))

    async def test_no_action_when_cable_disconnected(self):
        env = build([entry_dict([WEEKNIGHT])])
        env.sd.entries[0].device.cable_entity = "binary_sensor.kabel"
        env.hass.states.set("binary_sensor.kabel", "off")
        await env.tick(500.0, MON_23)
        assert services(env) == []

    async def test_waits_for_start_confirmation(self):
        env = build([entry_dict([WEEKNIGHT])])
        await env.tick(500.0, MON_23)
        n = len(env.calls)
        await env.tick(500.0, MON_23 + timedelta(minutes=1))   # nog niet aan het laden
        assert len(env.calls) == n

    async def test_backoff_after_repeated_failed_starts(self):
        env = build([entry_dict([WEEKNIGHT])])
        t = MON_23
        for _ in range(3):
            await env.tick(500.0, t)
            t += timedelta(minutes=4)
        n = len(env.calls)
        await env.tick(500.0, t)
        assert len(env.calls) == n
        assert "nieuwe poging later" in env.sd.status_dict()["sch1"]["status"]

    async def test_charging_increase_needs_margin_and_interval(self):
        env = build([entry_dict([WEEKNIGHT])])
        env.hass.states.set(SW, "on")
        env.hass.states.set(CHG, "charging")
        env.hass.states.set(SOC, "65")
        env.hass.states.set(CUR, "6")
        guard = env.ev.get_guard("ev_tesla")
        guard.last_current_update = MON_23 - timedelta(seconds=60)
        await env.tick(2000.0, MON_23)            # ruimte voor 8 A, maar interval nog niet om
        assert not any(e == CUR for _, e, _ in services(env))
        guard.last_current_update = MON_23 - timedelta(seconds=400)
        await env.tick(2000.0, MON_23)
        assert ("set_value", CUR, 8.0) in services(env)

    async def test_fulfilled_releases_device(self):
        env = build([entry_dict([WEEKNIGHT])])
        env.hass.states.set(BAT, "65")
        env.hass.states.set(SOC, "65")
        await env.tick(500.0, MON_23)
        st = env.sd.status_dict()["sch1"]
        assert st["fulfilled"] and not st["controlled"]
        assert not any(s == "turn_on" for s, _, _ in services(env))

    async def test_complete_counts_as_fulfilled_with_stale_battery(self):
        env = build([entry_dict([WEEKNIGHT])])
        env.hass.states.set(BAT, "60")              # verouderd
        env.hass.states.set(SOC, "65")
        env.hass.states.set(CHG, "complete")
        await env.tick(500.0, MON_23)
        assert env.sd.status_dict()["sch1"]["fulfilled"]

    async def test_complete_at_old_limit_is_not_fulfilled(self):
        env = build([entry_dict([WEEKNIGHT])])
        env.hass.states.set(CHG, "complete")        # stond vol op oude limiet 50 %
        await env.tick(500.0, MON_23)
        assert not env.sd.status_dict()["sch1"]["fulfilled"]
        assert ("set_value", SOC, 65.0) in services(env)

    async def test_takes_over_solar_session(self):
        env = build([entry_dict([WEEKNIGHT])])
        env.inject_snaps[SW] = DeviceSnapshot(entity_id=SW, original_state="off", original_soc=50)
        env.hass.states.set(SW, "on")
        env.hass.states.set(CHG, "charging")
        await env.tick(500.0, MON_23)
        assert SW not in env.inject_snaps
        assert env.st.completed == ["ev_tesla"]
        assert env.sd._run["sch1"].started_by_schedule

    async def test_injection_cascade_skips_scheduled_ev(self):
        env = build([entry_dict([WEEKNIGHT])])
        env.hass.states.set(SW, "on")
        env.hass.states.set(CHG, "charging")
        env.hass.states.set(SOC, "65")
        sat_noon = local(2026, 10, 10, 12, 0)
        entry = env.sd.entries[0]
        entry.windows.append(ScheduleWindow(days=[5], start="00:00", end="00:00", target_soc=65))
        await env.tick(-2000.0, sat_noon)
        assert SW not in env.inject_snaps          # geen solar-snapshot
        assert ("set_value", SOC, 100.0) not in services(env)   # geen solar SOC-override

    async def test_target_raised_by_adjacent_window_unfulfills(self):
        env = build([entry_dict([
            {"kind": "time", "days": [4], "start": "22:00", "end": "06:00", "target_soc": 60},
            {"kind": "time", "days": [5], "start": "00:00", "end": "00:00", "target_soc": 80},
        ])])
        env.hass.states.set(BAT, "62")
        env.hass.states.set(SOC, "60")
        await env.tick(500.0, local(2026, 10, 9, 23, 0))      # vr 23:00, doel 60
        assert env.sd._run["sch1"].fulfilled
        await env.tick(500.0, local(2026, 10, 10, 0, 1))      # za, doel 80
        rs = env.sd._run["sch1"]
        assert rs.active and not rs.fulfilled and rs.target_soc == 80


# ═══════════════════════════════════════════════════════════════════════════ #
#  3. Piekprioriteit                                                           #
# ═══════════════════════════════════════════════════════════════════════════ #

class TestPeakPriority:
    async def test_peak_cascade_still_sheds_scheduled_ev(self):
        env = build([entry_dict([WEEKNIGHT])])
        env.hass.states.set(SW, "on")
        env.hass.states.set(CHG, "charging")
        env.hass.states.set(CUR, "8")
        await env.tick(6000.0, MON_23)             # 1100 W boven piek − buffer → 3 A < 6 A
        assert SW in env.peak_snaps
        assert ("turn_off", SW, None) in services(env)
        assert env.sd.status_dict()["sch1"]["status"] == "uitgesteld door piekbeperking"

    async def test_schedule_restores_peak_snapshot_without_flapping(self):
        env = build([entry_dict([WEEKNIGHT])])
        env.sd._run["sch1"] = ScheduleRunState(active=True, started_at=MON_23, target_soc=65)
        env.peak_snaps[SW] = DeviceSnapshot(entity_id=SW, original_state="on", original_current=8.0)
        env.hass.states.set(SOC, "65")
        # Te weinig ruimte: snapshot blijft, geen herstel door de piek-decider.
        await env.tick(4200.0, MON_23)
        assert SW in env.peak_snaps
        assert services(env) == []
        # Ruimte terug: het schema neemt het herstel over.
        await env.tick(1000.0, MON_23 + timedelta(minutes=1))
        assert SW not in env.peak_snaps
        assert env.pt.turn_on_measurements == ["ev_tesla"]
        assert ("turn_on", SW, None) in services(env)

    async def test_idle_ev_gets_no_peak_snapshot(self):
        # B2: piek-cascade mag een niet-ladende EV geen 'off'-snapshot geven.
        env = build([])
        await env.tick(6000.0, MON_23)
        assert SW not in env.peak_snaps
        assert not any(s == "turn_off" for s, _, _ in services(env))

    async def test_window_end_drops_peak_snapshot(self):
        env = build([entry_dict([WEEKNIGHT])])
        env.sd._run["sch1"] = ScheduleRunState(active=True, started_at=MON_23, target_soc=65)
        env.peak_snaps[SW] = DeviceSnapshot(entity_id=SW, original_state="on", original_current=8.0)
        await env.tick(500.0, local(2026, 10, 6, 6, 0))
        assert SW not in env.peak_snaps
        assert env.pt.completed == ["ev_tesla"]
        assert not any(s == "turn_on" for s, _, _ in services(env))


# ═══════════════════════════════════════════════════════════════════════════ #
#  4. Einde venster en rust-laadlimiet                                         #
# ═══════════════════════════════════════════════════════════════════════════ #

TUE_06 = local(2026, 10, 6, 6, 0)


def charging_in_window(env):
    env.sd._run["sch1"] = ScheduleRunState(
        active=True, started_at=MON_23, target_soc=65, started_by_schedule=True,
    )
    env.hass.states.set(SW, "on")
    env.hass.states.set(CHG, "charging")
    env.hass.states.set(CUR, "8")
    env.hass.states.set(SOC, "65")


class TestWindowEnd:
    async def test_stops_and_sets_rest_limit(self):
        env = build([entry_dict([WEEKNIGHT], rest_soc=50)])
        charging_in_window(env)
        await env.tick(2500.0, TUE_06)
        assert ("turn_off", SW, None) in services(env)
        assert ("set_value", SOC, 50.0) in services(env)
        guard = env.ev.get_guard("ev_tesla")
        assert guard.turned_off_by_pg and not guard.scheduled

    async def test_hands_over_to_solar_when_surplus_without_ev(self):
        # Huis + EV = 500 W import, EV trekt 1840 W → zonder EV −1340 W.
        env = build([entry_dict([WEEKNIGHT], rest_soc=50)])
        charging_in_window(env)
        await env.tick(500.0, TUE_06)
        assert not any(s == "turn_off" for s, _, _ in services(env))
        assert SW in env.inject_snaps
        assert env.st.started and env.st.started[0]["id"] == "ev_tesla"

    async def test_no_handover_when_not_in_inject_cascade(self):
        env = build([entry_dict([WEEKNIGHT])], inject=False)
        charging_in_window(env)
        await env.tick(500.0, TUE_06)
        assert ("turn_off", SW, None) in services(env)

    async def test_solar_restore_uses_rest_limit(self):
        env = build([entry_dict([WEEKNIGHT], rest_soc=50)])
        assert env.sd.soc_rest_for(SW) == 50
        env.sd._run["sch1"] = ScheduleRunState(active=True, target_soc=65)
        assert env.sd.soc_rest_for(SW) == 65

    async def test_rest_limit_deferred_while_asleep(self):
        env = build([entry_dict([WEEKNIGHT], rest_soc=50)])
        env.sd._run["sch1"] = ScheduleRunState(active=True, started_at=MON_23, target_soc=65)
        env.hass.states.set(SOC, "unavailable")
        await env.tick(2500.0, TUE_06)
        assert env.sd._run["sch1"].rest_pending
        env.hass.states.set(SOC, "65")
        await env.tick(2500.0, TUE_06 + timedelta(minutes=1))
        assert ("set_value", SOC, 50.0) in services(env)
        assert not env.sd._run["sch1"].rest_pending

    async def test_window_end_without_consumption_still_stops(self):
        env = build([entry_dict([WEEKNIGHT])])
        charging_in_window(env)
        await env.tick(None, TUE_06)
        assert ("turn_off", SW, None) in services(env)


# ═══════════════════════════════════════════════════════════════════════════ #
#  5. Ongeplande lading buiten venster                                         #
# ═══════════════════════════════════════════════════════════════════════════ #

TUE_18 = local(2026, 10, 6, 18, 0)


def plugged_in_charging(env):
    env.hass.states.set(SW, "on")
    env.hass.states.set(CHG, "charging")
    env.hass.states.set(CUR, "8")


class TestUnplanned:
    async def test_stopped_after_grace(self):
        env = build([entry_dict([WEEKNIGHT])])
        plugged_in_charging(env)
        await env.tick(3000.0, TUE_18)
        assert not any(s == "turn_off" for s, _, _ in services(env))
        await env.tick(3000.0, TUE_18 + timedelta(seconds=60))
        assert not any(s == "turn_off" for s, _, _ in services(env))
        await env.tick(3000.0, TUE_18 + timedelta(seconds=130))
        assert ("turn_off", SW, None) in services(env)

    async def test_not_stopped_with_manual_override(self):
        env = build([entry_dict([WEEKNIGHT])])
        for d in env.sd._inject_cascade:
            d.manual_override = True
        plugged_in_charging(env)
        await env.tick(3000.0, TUE_18)
        await env.tick(3000.0, TUE_18 + timedelta(seconds=200))
        assert not any(s == "turn_off" for s, _, _ in services(env))

    async def test_not_stopped_when_block_disabled(self):
        env = build([entry_dict([WEEKNIGHT], block_unplanned=False)])
        plugged_in_charging(env)
        await env.tick(3000.0, TUE_18)
        await env.tick(3000.0, TUE_18 + timedelta(seconds=200))
        assert not any(s == "turn_off" for s, _, _ in services(env))

    async def test_surplus_without_ev_hands_over_to_solar(self):
        env = build([entry_dict([WEEKNIGHT])])
        plugged_in_charging(env)
        await env.tick(300.0, TUE_18)              # zonder EV: −1540 W
        assert SW in env.inject_snaps
        await env.tick(300.0, TUE_18 + timedelta(seconds=200))
        assert not any(s == "turn_off" for s, _, _ in services(env))

    async def test_solar_session_not_stopped(self):
        env = build([entry_dict([WEEKNIGHT])])
        plugged_in_charging(env)
        env.inject_snaps[SW] = DeviceSnapshot(entity_id=SW, original_state="off")
        await env.sd.check(3000.0, TUE_18)
        await env.sd.check(3000.0, TUE_18 + timedelta(seconds=200))
        assert services(env) == []
        assert env.sd._run["sch1"].unplanned_since is None

    async def test_peak_snapshot_outside_window_not_restored(self):
        env = build([entry_dict([WEEKNIGHT])])
        env.peak_snaps[SW] = DeviceSnapshot(entity_id=SW, original_state="on", original_current=8.0)
        await env.tick(1000.0, TUE_18)
        assert SW not in env.peak_snaps
        assert not any(s == "turn_on" for s, _, _ in services(env))


# ═══════════════════════════════════════════════════════════════════════════ #
#  6. Tariefsensor-venster                                                     #
# ═══════════════════════════════════════════════════════════════════════════ #

SENSOR_WINDOW = {"kind": "sensor", "entity_id": TARIFF, "active_state": "2", "target_soc": 65}


class TestTariffSensor:
    async def test_active_on_off_peak_tariff(self):
        env = build([entry_dict([SENSOR_WINDOW])])
        env.hass.states.set(TARIFF, "2")
        await env.tick(500.0, TUE_18)
        assert env.sd._run["sch1"].active
        assert ("turn_on", SW, None) in services(env)

    async def test_switch_to_peak_tariff_ends_window(self):
        env = build([entry_dict([SENSOR_WINDOW])])
        env.hass.states.set(TARIFF, "2")
        await env.tick(500.0, TUE_18)
        env.hass.states.set(SW, "on")
        env.hass.states.set(CHG, "charging")
        env.hass.states.set(TARIFF, "1")
        await env.tick(2500.0, TUE_18 + timedelta(minutes=5))
        assert not env.sd._run["sch1"].active
        assert ("turn_off", SW, None) in services(env)

    async def test_short_unavailability_keeps_state(self):
        env = build([entry_dict([SENSOR_WINDOW])])
        env.hass.states.set(TARIFF, "2")
        await env.tick(4500.0, TUE_18)
        env.hass.states.set(TARIFF, "unavailable")
        await env.tick(4500.0, TUE_18 + timedelta(minutes=10))
        assert env.sd._run["sch1"].active
        await env.tick(4500.0, TUE_18 + timedelta(minutes=16))
        assert not env.sd._run["sch1"].active

    async def test_sensor_memory_survives_restart(self):
        env = build([entry_dict([SENSOR_WINDOW])])
        env.hass.states.set(TARIFF, "2")
        await env.tick(4500.0, TUE_18)
        saved = env.sd.state_to_dict()
        env2 = build([entry_dict([SENSOR_WINDOW])])
        env2.sd.load_state(saved)
        env2.hass.states.set(TARIFF, "unavailable")
        await env2.tick(4500.0, TUE_18 + timedelta(minutes=5))
        assert env2.sd._run["sch1"].active

    async def test_mixed_sensor_and_time_windows(self):
        env = build([entry_dict([SENSOR_WINDOW, WEEKNIGHT])])
        env.hass.states.set(TARIFF, "1")
        await env.tick(4500.0, MON_23)
        assert env.sd._run["sch1"].active
        assert env.sd.watched_entities() == {TARIFF}


# ═══════════════════════════════════════════════════════════════════════════ #
#  7. Schakelaar                                                               #
# ═══════════════════════════════════════════════════════════════════════════ #

BOILER = "switch.boiler"


def boiler_entry(power=2000):
    dev = SwitchOnDevice(id="boiler", name="Boiler", entity_id=BOILER, priority=1,
                         action_type="switch_on", power_watts=power)
    return {"id": "sch_b", "device": asdict(dev), "windows": [WEEKNIGHT]}


class TestSwitch:
    async def test_on_in_window_and_restored_after(self):
        env = build([boiler_entry()])
        env.hass.states.set(BOILER, "off")
        await env.tick(1000.0, MON_23)
        assert ("turn_on", BOILER, None) in services(env)
        env.hass.states.set(BOILER, "on")
        await env.tick(1000.0, TUE_06)
        assert ("turn_off", BOILER, None) in services(env)

    async def test_stays_on_if_it_was_on(self):
        env = build([boiler_entry()])
        env.hass.states.set(BOILER, "on")
        await env.tick(1000.0, MON_23)
        await env.tick(1000.0, TUE_06)
        assert services(env) == []

    async def test_waits_for_headroom(self):
        env = build([boiler_entry(power=3000)])
        env.hass.states.set(BOILER, "off")
        await env.tick(2500.0, MON_23)             # ruimte 2400 W < 3000 W
        assert services(env) == []
        assert env.sd.status_dict()["sch_b"]["status"] == "wacht op piekruimte"

    async def test_takes_over_inject_snapshot(self):
        env = build([boiler_entry()])
        env.hass.states.set(BOILER, "on")
        env.inject_snaps[BOILER] = DeviceSnapshot(entity_id=BOILER, original_state="off")
        await env.tick(1000.0, MON_23)
        assert BOILER not in env.inject_snaps
        env.hass.states.set(BOILER, "on")
        await env.tick(1000.0, TUE_06)
        assert ("turn_off", BOILER, None) in services(env)


# ═══════════════════════════════════════════════════════════════════════════ #
#  8. Regressies uit de code-review                                            #
# ═══════════════════════════════════════════════════════════════════════════ #

class TestReviewRegressions:
    async def test_peak_sheds_again_with_distinct_ids_per_cascade(self):
        # Zelfde EV met een ander id in de piek-cascade: na een herstart door
        # het schema moet de piek-cascade hem opnieuw kunnen afschakelen.
        env = build([entry_dict([WEEKNIGHT])])
        env.peak._cascade[0].id = "ev_peak"
        plugged_in_charging(env)
        await env.tick(6000.0, MON_23)
        assert ("turn_off", SW, None) in services(env)
        env.calls.clear()
        env.hass.states.set(SW, "off")
        env.hass.states.set(CHG, "stopped")
        env.hass.states.set(SOC, "65")
        await env.tick(1000.0, MON_23 + timedelta(minutes=1))
        assert ("turn_on", SW, None) in services(env)
        env.calls.clear()
        plugged_in_charging(env)
        await env.tick(6000.0, MON_23 + timedelta(minutes=10))
        assert ("turn_off", SW, None) in services(env)

    async def test_disconnected_with_switch_on_is_not_charging(self):
        env = build([entry_dict([WEEKNIGHT])])
        env.hass.states.set(SW, "on")
        env.hass.states.set(CHG, "disconnected")
        assert not env.ev.is_charging(env.sd.entries[0].device)
        await env.tick(3000.0, TUE_18)
        await env.tick(3000.0, TUE_18 + timedelta(seconds=200))
        assert services(env) == []

    async def test_switch_on_without_charge_state_counts_as_charging(self):
        env = build([entry_dict([WEEKNIGHT])])
        dev = env.sd.entries[0].device
        dev.charge_state_sensor = None
        env.hass.states.set(SW, "on")
        assert env.ev.is_charging(dev)

    async def test_restart_with_stale_sensor_memory_keeps_window(self):
        env = build([entry_dict([SENSOR_WINDOW])])
        env.hass.states.set(TARIFF, "2")
        await env.tick(4500.0, TUE_18)
        saved = env.sd.state_to_dict()
        env2 = build([entry_dict([SENSOR_WINDOW])])
        env2.sd.load_state(saved)
        env2.hass.states.set(TARIFF, "unavailable")
        # Herstart drie uur later, sensor nog niet beschikbaar.
        later = TUE_18 + timedelta(hours=3)
        await env2.tick(4500.0, later)
        assert env2.sd._run["sch1"].active
        assert not any(s == "turn_off" for s, _, _ in services(env2))

    async def test_sensor_memory_is_persisted_periodically(self):
        env = build([entry_dict([SENSOR_WINDOW])])
        env.hass.states.set(TARIFF, "2")
        await env.tick(4500.0, TUE_18)
        n = env.sd._save_fn.await_count
        await env.tick(4500.0, TUE_18 + timedelta(minutes=1))
        assert env.sd._save_fn.await_count == n
        await env.tick(4500.0, TUE_18 + timedelta(minutes=6))
        assert env.sd._save_fn.await_count == n + 1

    async def test_switch_shed_by_peak_keeps_snapshot_if_it_was_on(self):
        env = build([boiler_entry()])
        env.hass.states.set(BOILER, "on")
        await env.tick(1000.0, MON_23)
        env.peak_snaps[BOILER] = DeviceSnapshot(entity_id=BOILER, original_state="on")
        env.hass.states.set(BOILER, "off")
        await env.sd.check(1000.0, TUE_06)
        assert BOILER in env.peak_snaps

    async def test_rest_limit_after_handover_without_max_soc(self):
        env = build([entry_dict([WEEKNIGHT], rest_soc=50)])
        dev = env.inj._cascade[0]
        dev.max_soc = None
        snap = DeviceSnapshot(entity_id=SW, original_state="off", original_soc=65)
        env.hass.states.set(SW, "on")
        await env.ev._set_soc_override(dev, override=False, original_soc=snap.original_soc)
        assert ("set_value", SOC, 50.0) in services(env)

    async def test_no_soc_write_while_waiting_for_headroom(self):
        env = build([entry_dict([WEEKNIGHT])])
        await env.tick(4500.0, MON_23)
        assert services(env) == []

    async def test_removing_active_entry_stops_charge(self):
        env = build([entry_dict([WEEKNIGHT], rest_soc=50)])
        charging_in_window(env)
        env.sd.set_entries([])
        await env.tick(2500.0, MON_23 + timedelta(minutes=5))
        assert ("turn_off", SW, None) in services(env)
        assert ("set_value", SOC, 50.0) in services(env)
