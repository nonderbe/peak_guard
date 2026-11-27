"""
tests/test_charger_state_baseline.py — gedrag van de lader-toestand vastleggen
(plan stap 1a, docs/charger_state_plan.md).

Karakteriseringstests: elke combinatie van schakelaar, laadstatus/status-
sensor, tijdstip van de laatste melding en wat Peak Guard zelf deed (start,
stop, weigering met 'is_charging') wordt door de huidige code gestuurd, en het
antwoord wordt vergeleken met tests/fixtures/charger_state_baseline.json.gz
(gzip, één regel per scenario; de test toont zelf de afwijkende scenario's).

Dit legt het huidige gedrag vast, ook waar dat gedrag twijfelachtig is: de
refactor van stap 1b-1d mag géén enkel antwoord veranderen. Een bewuste
gedragswijziging later = de fixture opnieuw genereren en het verschil in de
commit uitleggen:

    PG_BASELINE_REGEN=1 .venv/bin/python -m pytest tests/test_charger_state_baseline.py

Twee lagen:
1. Leesfuncties (synchroon): _charge_state, is_charging, charging_for_control,
   stale_stopped_after_start, peak_is_off, charging_draw_w, charge_complete.
2. De eigen afleidingen in de sturing, via de publieke paden: restore (solar-
   en piek-tak), throttle_down_solar, apply_action (piek en solar). Vastgelegd:
   returnwaarde, verstuurde service-calls, hold-code, snapshots en de
   guard-toestand erna.

Tijden zijn relatief t.o.v. NOW; de klok van ev_guard (datetime.now()) staat
tijdens de test stil op NOW. De offsets liggen minstens 30 s van de vensters
(2, 3, 10, 20 min); test_baseline_survives_clock_drift bewaakt dat.
"""
from __future__ import annotations

import gzip
import itertools
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from custom_components.peak_guard.deciders.ev_guard import EVGuard
from custom_components.peak_guard.models import (
    DeviceSnapshot,
    EVChargerDevice,
    EVState,
)

from tests.conftest import MockHass, MockPeakTracker, MockSolarTracker

FIXTURE = Path(__file__).parent / "fixtures" / "charger_state_baseline.json.gz"
REGEN = os.environ.get("PG_BASELINE_REGEN") == "1"

NOW = datetime.now(timezone.utc).replace(microsecond=0)

SW = "switch.tesla_opladen"
CUR = "number.tesla_laadstroom"
SOC = "number.tesla_laadlimiet"
CS = "sensor.tesla_opladen"
ST = "binary_sensor.tesla_status"
STT = "sensor.tesla_status_tekst"


_REAL_DATETIME = datetime


class _AnyDatetime(type):
    # isinstance(x, datetime) in de code blijft werken voor echte datetimes.
    def __instancecheck__(cls, obj):
        return isinstance(obj, _REAL_DATETIME)


def _frozen_datetime(at: datetime):
    class _Frozen(_REAL_DATETIME, metaclass=_AnyDatetime):
        @classmethod
        def now(cls, tz=None):
            return at if tz is not None else at.replace(tzinfo=None)
    return _Frozen


@pytest.fixture(autouse=True)
def _frozen_clock(monkeypatch):
    """De klok van ev_guard en charger_state staat stil op NOW: datetime.now() in de code
    (is_charging, confirm_charging, charging_draw_w, …) en de tijden van de
    scenario's lopen niet uit elkaar, hoe lang de test ook duurt."""
    _freeze(monkeypatch, NOW)


def _freeze(monkeypatch, at: datetime) -> None:
    from custom_components.peak_guard.deciders import charger_state as _cs_mod
    from custom_components.peak_guard.deciders import ev_guard as _ev_mod
    for mod in (_ev_mod, _cs_mod):
        monkeypatch.setattr(mod, "datetime", _frozen_datetime(at))


@pytest.fixture(autouse=True)
def _no_retry_delay(monkeypatch):
    """Geen echte pauze tussen herhaalde EV-commando's (de beslissingen blijven gelijk)."""
    from custom_components.peak_guard.deciders import ev_guard as _ev_mod
    monkeypatch.setattr(_ev_mod, "EV_CMD_RETRY_DELAY_S", 0.0)


def _t(seconds: float) -> datetime:
    return NOW + timedelta(seconds=seconds)


# ──────────────────────────────────────────────────────────────────────────── #
#  Dimensies                                                                    #
# ──────────────────────────────────────────────────────────────────────────── #

SWITCH_STATES = ["on", "off", "unknown", "unavailable", None]   # None = entiteit ontbreekt

# Sensor-opstellingen: (naam, charge_state_sensor, status_sensor, waarden)
# Elke waarde is (laadstatus, status) — None = die sensor bestaat niet.
SENSOR_SETUPS = {
    "cs": [(v, None) for v in
           ("charging", "starting", "stopped", "complete", "disconnected", "unknown", "unavailable")],
    "st_text": [(None, v) for v in ("charging", "stopped", "complete", "connected", "on", "off")],
    "cs+st_bin": [(v, "on") for v in ("charging", "stopped", "complete")],
    "none": [(None, None)],
}

# Wanneer meldde de laadstatus-sensor voor het laatst (reported) en wanneer
# veranderde hij (changed)?
REPORTS = {
    "old": (-1800, -1800),           # lang vóór elke start
    "recent_poll": (-10, -1800),     # recente peiling, waarde ongewijzigd
    "recent_change": (-10, -10),     # waarde net veranderd
}


def _preset_none(ev, dev):
    pass


def _start(offset, note=True, sent=None):
    def apply(ev: EVGuard, dev: EVChargerDevice):
        g = ev.get_guard(dev.id)
        g.state, g.last_switch_state, g.turned_on_at = EVState.CHARGING, True, _t(offset)
        if note:
            ev.note_charge_start(dev, _t(offset))
        if sent is not None:
            g.last_sent_amps, g.last_current_update = float(sent), _t(-10)
    return apply


def _stop(offset, by_pg=True):
    def apply(ev: EVGuard, dev: EVChargerDevice):
        g = ev.get_guard(dev.id)
        g.state, g.last_switch_state = EVState.IDLE, False
        g.turned_off_at, g.turned_off_by_pg = _t(offset), by_pg
    return apply


def _refused(ev: EVGuard, dev: EVChargerDevice):
    """turn_on geweigerd met 'is_charging' 60 s geleden (geen note_charge_start)."""
    g = ev.get_guard(dev.id)
    g.state, g.last_switch_state, g.turned_on_at = EVState.CHARGING, True, _t(-60)
    ev.confirm_charging(dev)


PRESETS = {
    "none": _preset_none,
    "pg_start_30s": _start(-30),
    "pg_start_150s": _start(-150),
    "pg_start_5m": _start(-300),
    "pg_start_15m": _start(-900),
    "pg_start_25m": _start(-1500),
    "guard_on_no_turn_on_5m": _start(-300, note=False),
    "pg_start_5m_sent_6A": _start(-300, sent=6),
    "pg_stop_60s": _stop(-60),
    "pg_stop_10m": _stop(-600),
    "off_not_pg": _stop(-600, by_pg=False),
    "refused_is_charging": _refused,
}


CABLE = "binary_sensor.tesla_kabel"
TRACKER = "device_tracker.tesla"
WAKE = "button.tesla_wake"


def _device(cs, st, extra=None) -> EVChargerDevice:
    extra = extra or {}
    status = None
    if st is not None:
        status = ST if st in ("on", "off", "unavailable") else STT
    return EVChargerDevice(
        cable_entity=CABLE if "cable" in extra else None,
        location_tracker=TRACKER if "tracker" in extra else None,
        wake_button=WAKE if extra.get("wake") else None,
        id="ev", name="Tesla", entity_id=SW, priority=1, action_type="ev_charger",
        switch_entity=SW, current_entity=CUR, soc_entity=SOC,
        charge_state_sensor=CS if cs is not None else None,
        status_sensor=status,
        min_value=5.0, max_value=16.0, phases=1, max_soc=100,
    )


def _hass(switch, cs, st, report, current="8", extra=None) -> MockHass:
    extra = extra or {}
    h = MockHass()
    if "cable" in extra:
        h.states.set(CABLE, extra["cable"])
    if "tracker" in extra:
        h.states.set(TRACKER, extra["tracker"])
    if extra.get("wake"):
        h.states.set(WAKE, "unknown")
    if extra.get("refuse"):
        # Tesla weigert turn_on omdat hij al laadt (of vol / niet ingeplugd is).
        h.services._raise_on.add("turn_on")
        h.services.error_message = extra["refuse"]
    if switch is not None:
        h.states.set(SW, switch)
    if extra.get("cur_stale"):
        h.states.set(CUR, current, reported=_t(-3600))
    else:
        h.states.set(CUR, current)
    h.states.set(SOC, "80")
    rep, chg = REPORTS[report]
    if cs is not None:
        h.states.set(CS, cs, reported=_t(rep), changed=_t(chg))
    if st is not None:
        h.states.set(ST if st in ("on", "off", "unavailable") else STT, st,
                     reported=_t(rep), changed=_t(chg))
    return h


def _scenario(switch, cs, st, report, preset, current="8", extra=None):
    hass = _hass(switch, cs, st, report, current, extra)
    ev = EVGuard(hass=hass, config={}, iteration_actions=[])
    dev = _device(cs, st, extra)
    PRESETS[preset](ev, dev)
    return hass, ev, dev


def _sensor_values():
    for setup, values in SENSOR_SETUPS.items():
        reports = ["old"] if setup == "none" else list(REPORTS)
        for (cs, st), report in itertools.product(values, reports):
            yield setup, cs, st, report


def _calls(hass) -> list:
    out = []
    for c in hass.services.calls:
        d = c["data"]
        val = d.get("value")
        ent = str(d.get("entity_id")).split(".")[-1].replace("tesla_", "")
        out.append(f"{c['service']}:{ent}" + (f"={val:g}" if isinstance(val, (int, float)) else ""))
    return out


def _guard(ev: EVGuard, dev) -> str:
    g = ev.get_guard(dev.id)
    return (f"{g.state.name}|sw={g.last_switch_state}|pg_off={g.turned_off_by_pg}"
            f"|sent={g.last_sent_amps}")


def _snaps(snaps: dict) -> dict:
    return {k: f"{s.original_state}|{s.original_current}|{s.original_soc}" for k, s in snaps.items()}


def _key(*parts) -> str:
    return "|".join("-" if p is None else str(p) for p in parts)


# ──────────────────────────────────────────────────────────────────────────── #
#  Laag 1: leesfuncties                                                         #
# ──────────────────────────────────────────────────────────────────────────── #

def _read_layer() -> dict:
    out = {}
    for switch, (setup, cs, st, report), preset, current in itertools.product(
        SWITCH_STATES, _sensor_values(), PRESETS, ("8", "unavailable"),
    ):
        hass, ev, dev = _scenario(switch, cs, st, report, preset, current)
        out[_key("read", switch, setup, cs, st, report, preset, current)] = [
            ev._charge_state(dev),
            ev.is_charging(dev),
            ev.charging_for_control(dev, NOW),
            ev.stale_stopped_after_start(dev, NOW),
            ev.peak_is_off(dev),
            ev.charging_draw_w(dev, NOW),
            ev.charge_complete(dev),
        ]
    return out


# ──────────────────────────────────────────────────────────────────────────── #
#  Laag 2: sturing via de publieke paden                                        #
# ──────────────────────────────────────────────────────────────────────────── #

ASYNC_SWITCH = ["on", "off", "unknown"]
ASYNC_CS = ["charging", "stopped", "complete", "disconnected", None]
ASYNC_REPORTS = ["old", "recent_poll"]


async def _restore(original_state) -> dict:
    out = {}
    for switch, cs, preset, report in itertools.product(ASYNC_SWITCH, ASYNC_CS, PRESETS, ASYNC_REPORTS):
        hass, ev, dev = _scenario(switch, cs, None, report, preset)
        snap = DeviceSnapshot(SW, original_state, 16.0, 80.0)
        cascade = "solar" if original_state == "off" else "peak"
        ok = await ev.restore(dev, snap, MockPeakTracker(), MockSolarTracker(),
                              cascade_type=cascade, now=NOW)
        out[_key("restore", cascade, switch, cs, preset, report)] = {
            "ok": ok, "calls": _calls(hass), "guard": _guard(ev, dev),
        }
    return out


async def _throttle() -> dict:
    out = {}
    for switch, cs, preset, report, (cur, cons), stale in itertools.product(
        ["on", "off", "unknown", "unavailable"], ASYNC_CS, PRESETS, ASYNC_REPORTS,
        [("8", 13.0), ("8", 1000.0), ("5", 1300.0), ("5", 600.0)], [False, True],
    ):
        if stale and report != "old":
            continue
        hass, ev, dev = _scenario(switch, cs, None, report, preset, cur, {"cur_stale": True} if stale else None)
        ok = await ev.throttle_down_solar(dev, consumption=cons, now=NOW)
        hold = ev.hold_for(dev.id) or {}
        out[_key("throttle", switch, cs, preset, report, cur, cons, *(["cur_stale"] if stale else []))] = {
            "ok": ok, "calls": _calls(hass), "hold": hold.get("code"),
        }
    return out


async def _apply(cascade) -> dict:
    out = {}
    amounts = [2000.0, 500.0] if cascade == "peak" else [1500.0, 4000.0]
    # Solar: ook een tweede aanroep 25 s na de eerste, zodat de debounce
    # (EV_DEBOUNCE_STABLE_S) voorbij is en het startpad (turn_on) loopt.
    repeats = [1] if cascade == "peak" else [1, 2]
    for switch, cs, preset, report, amount, with_snap, n in itertools.product(
        ["on", "off", "unknown", "unavailable"] if cascade == "peak" else ASYNC_SWITCH,
        ASYNC_CS, PRESETS, ASYNC_REPORTS, amounts, [False, True], repeats,
    ):
        hass, ev, dev = _scenario(switch, cs, None, report, preset)
        snaps = {SW: DeviceSnapshot(SW, "off" if cascade == "solar" else "on", 16.0, 80.0)} if with_snap else {}
        if n == 2:
            await ev.apply_action(dev, amount, snaps, cascade, MockPeakTracker(), MockSolarTracker(),
                                  now=_t(-25))
        left = await ev.apply_action(dev, amount, snaps, cascade, MockPeakTracker(), MockSolarTracker(), now=NOW)
        out[_key("apply", cascade, switch, cs, preset, report, amount, with_snap, *([n] if n > 1 else []))] = {
            "left": round(left, 1), "calls": _calls(hass), "snaps": _snaps(snaps),
            "guard": _guard(ev, dev), "skip": ev.last_skip_reason,
        }
    return out


# Extra opstellingen voor de takken die enkel met een status-sensor, kabel,
# locatie, wake-knop, een weigering of een achterlopende stroomsensor lopen.
EXTRAS = {
    "st_charging": (None, "charging", {}),
    "st_complete": (None, "complete", {}),
    "st_connected": (None, "connected", {}),
    "st_stopped": (None, "stopped", {}),
    "st_bin_on": (None, "on", {}),
    "st_bin_off_wake": (None, "off", {"wake": True}),
    "st_unavail_wake": (None, "unavailable", {"wake": True}),
    "cs_charging+st_bin_on": ("charging", "on", {}),
    "cable_off": ("stopped", None, {"cable": "off"}),
    "cable_on": ("stopped", None, {"cable": "on"}),
    "away": ("stopped", None, {"tracker": "not_home"}),
    "home": ("stopped", None, {"tracker": "home"}),
    "refuse_is_charging_cs_complete": ("complete", None, {"refuse": "Command failed: is_charging"}),
    "refuse_is_charging_cs_stopped": ("stopped", None, {"refuse": "Command failed: is_charging"}),
    "refuse_complete": ("complete", None, {"refuse": "Command failed: complete"}),
    "refuse_disconnected": ("disconnected", None, {"refuse": "Command failed: disconnected"}),
    "cur_stale_charging": ("charging", None, {"cur_stale": True}),
    "cable_unknown": ("stopped", None, {"cable": "unknown"}),
    "cable_zero": ("stopped", None, {"cable": "0"}),
    "tracker_unknown": ("stopped", None, {"tracker": "unknown"}),
}


async def _extras() -> dict:
    out = {}
    for name, switch, preset in itertools.product(EXTRAS, ["on", "off", "unknown", "unavailable"], PRESETS):
        cs, st, extra = EXTRAS[name]
        # Piek
        hass, ev, dev = _scenario(switch, cs, st, "old", preset, extra=extra)
        left = await ev.apply_action(dev, 2000.0, {}, "peak", MockPeakTracker(), MockSolarTracker(), now=NOW)
        out[_key("x_apply_peak", name, switch, preset)] = {
            "left": round(left, 1), "calls": _calls(hass), "guard": _guard(ev, dev), "skip": ev.last_skip_reason,
        }
        # Solar, twee aanroepen (debounce voorbij)
        hass, ev, dev = _scenario(switch, cs, st, "old", preset, extra=extra)
        snaps: dict = {}
        await ev.apply_action(dev, 2500.0, snaps, "solar", MockPeakTracker(), MockSolarTracker(), now=_t(-25))
        left = await ev.apply_action(dev, 2500.0, snaps, "solar", MockPeakTracker(), MockSolarTracker(), now=NOW)
        out[_key("x_apply_solar", name, switch, preset)] = {
            "left": round(left, 1), "calls": _calls(hass), "snaps": _snaps(snaps),
            "guard": _guard(ev, dev), "skip": ev.last_skip_reason,
        }
        # Piekherstel (turn_on, eventueel geweigerd) en solar-herstel, ook van
        # een wagen die vóór het overschot al laadde (solar, origineel 'on'),
        # met de stroom al op de originele waarde of zonder stroommeting.
        for original, cascade, current in (
            ("on", "peak", "8"), ("on", "peak", "16"), ("on", "peak", "unavailable"),
            ("off", "solar", "8"), ("on", "solar", "8"), ("on", "solar", "16"),
        ):
            hass, ev, dev = _scenario(switch, cs, st, "old", preset, current, extra=extra)
            ok = await ev.restore(dev, DeviceSnapshot(SW, original, 16.0, 80.0), MockPeakTracker(),
                                  MockSolarTracker(), cascade_type=cascade, now=NOW)
            out[_key("x_restore", cascade, original, current, name, switch, preset)] = {
                "ok": ok, "calls": _calls(hass), "guard": _guard(ev, dev),
            }
        # Solar zonder overschot (excess ≤ 0) en zonder stroommeting
        for amount, current in ((0.0, "8"), (2500.0, "unavailable")):
            hass, ev, dev = _scenario(switch, cs, st, "old", preset, current, extra=extra)
            snaps = {SW: DeviceSnapshot(SW, "off", 16.0, 80.0)}
            left = await ev.apply_action(dev, amount, snaps, "solar", MockPeakTracker(), MockSolarTracker(),
                                         now=NOW)
            out[_key("x_apply_solar", amount, current, name, switch, preset)] = {
                "left": round(left, 1), "calls": _calls(hass), "snaps": _snaps(snaps),
                "guard": _guard(ev, dev), "skip": ev.last_skip_reason,
            }
        # Verlagen bij netafname
        hass, ev, dev = _scenario(switch, cs, st, "old", preset, extra=extra)
        ok = await ev.throttle_down_solar(dev, consumption=1000.0, now=NOW)
        out[_key("x_throttle", name, switch, preset)] = {
            "ok": ok, "calls": _calls(hass), "hold": (ev.hold_for(dev.id) or {}).get("code"),
        }
    return out


async def _all_results() -> dict:
    results = _read_layer()
    results.update(await _restore("off"))
    results.update(await _restore("on"))
    results.update(await _throttle())
    results.update(await _apply("peak"))
    results.update(await _apply("solar"))
    results.update(await _extras())
    return json.loads(json.dumps(results))   # tuples → lists, zoals in de fixture


async def test_charger_state_matches_baseline():
    results = await _all_results()
    if REGEN:
        FIXTURE.parent.mkdir(exist_ok=True)
        # Eén regel per scenario: een afwijking is in een diff meteen leesbaar.
        body = ",\n".join(
            f"{json.dumps(k)}: {json.dumps(results[k], sort_keys=True, separators=(',', ':'))}"
            for k in sorted(results)
        )
        # mtime=0: dezelfde inhoud geeft hetzelfde bestand (geen ruis in git).
        with open(FIXTURE, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as fh:
            fh.write(("{\n" + body + "\n}\n").encode("utf-8"))
        pytest.skip(f"baseline opnieuw geschreven: {len(results)} scenario's")
    with gzip.open(FIXTURE, "rt", encoding="utf-8") as fh:
        expected = json.load(fh)
    missing = sorted(set(expected) - set(results))
    extra = sorted(set(results) - set(expected))
    diffs = [k for k in sorted(expected) if k in results and results[k] != expected[k]]
    lines = [f"{k}\n   verwacht: {expected[k]}\n   nu:       {results[k]}" for k in diffs[:15]]
    assert not (missing or extra or diffs), (
        f"{len(diffs)} afwijkend, {len(missing)} ontbrekend, {len(extra)} nieuw "
        f"(van {len(expected)}):\n" + "\n".join(lines)
        + ("\nontbrekend: " + ", ".join(missing[:5]) if missing else "")
        + ("\nnieuw: " + ", ".join(extra[:5]) if extra else "")
    )


async def test_baseline_survives_clock_drift(monkeypatch):
    """De klok van de code (datetime.now() in ev_guard en charger_state) staat stil op NOW, en de
    antwoorden veranderen niet als die klok 20 s verschuift t.o.v. de scenario's.
    De meeste paden krijgen 'now' expliciet mee; dit bewaakt de paden die de
    eigen klok gebruiken (o.a. de 'is_charging'-bevestiging) en dat er niets van
    het absolute tijdstip afhangt."""
    from custom_components.peak_guard.deciders import charger_state as _cs_mod
    from custom_components.peak_guard.deciders import ev_guard as _ev_mod
    assert _ev_mod.datetime.now(timezone.utc) == NOW
    assert _cs_mod.datetime.now(timezone.utc) == NOW
    base = await _all_results()
    _freeze(monkeypatch, NOW + timedelta(seconds=20))
    assert await _all_results() == base
