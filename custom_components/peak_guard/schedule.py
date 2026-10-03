"""
Peak Guard — schedule.py

Pure logica van het laadschema: (de)serialisatie van ScheduleEntry en
bepalen of een venster actief is. Geen service-calls; de sturing zit in
deciders/schedule_decider.py.

Tijdvensters worden in lokale tijd (HA-tijdzone) beoordeeld op wandkloktijd,
zodat een venster 22:00–06:00 ook op de dagen van de zomertijdwissel klopt.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Iterable, Optional

from homeassistant.util import dt as dt_util

from .models import (
    SCHEDULE_KIND_SENSOR,
    SCHEDULE_KIND_TIME,
    ScheduleEntry,
    ScheduleWindow,
    from_dict as cascade_from_dict,
)

_LOGGER = logging.getLogger(__name__)

DAY_LABELS = ("ma", "di", "wo", "do", "vr", "za", "zo")
SCHEDULABLE_ACTION_TYPES = ("ev_charger", "switch_on", "switch_off")


# ──────────────────────────────────────────────────────────────────────────── #
#  Parsing                                                                      #
# ──────────────────────────────────────────────────────────────────────────── #

def parse_hhmm(value) -> Optional[int]:
    """'HH:MM' → minuten sinds middernacht (0–1440); None bij ongeldige invoer."""
    try:
        hh, mm = str(value).strip().split(":")
        h, m = int(hh), int(mm)
    except (ValueError, TypeError, AttributeError):
        return None
    if not (0 <= h <= 24 and 0 <= m < 60) or (h == 24 and m != 0):
        return None
    return h * 60 + m


def normalize_state(value) -> str:
    """Vergelijkbare vorm van een sensorstaat: '2', '2.0' en 2 → '2'."""
    s = str(value).strip().lower()
    try:
        f = float(s)
    except ValueError:
        return s
    if f != f or f in (float("inf"), float("-inf")):
        return s
    return str(int(f)) if f.is_integer() else repr(f)


def _opt_int(value) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(round(float(value)))
    except (ValueError, TypeError):
        return None


def _opt_float(value) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def window_from_dict(d: dict) -> Optional[ScheduleWindow]:
    """Bouw een ScheduleWindow; None (met waarschuwing) bij ongeldige invoer."""
    if not isinstance(d, dict):
        return None
    kind = d.get("kind") or SCHEDULE_KIND_TIME
    target = _opt_int(d.get("target_soc"))
    if target is not None:
        target = max(1, min(100, target))
    enabled = bool(d.get("enabled", True))
    if kind == SCHEDULE_KIND_SENSOR:
        entity_id = str(d.get("entity_id") or "").strip()
        active_state = d.get("active_state")
        if not entity_id or active_state is None or str(active_state).strip() == "":
            _LOGGER.warning("Peak Guard schema: sensorvenster zonder sensor of staat genegeerd: %s", d)
            return None
        return ScheduleWindow(
            kind=SCHEDULE_KIND_SENSOR, entity_id=entity_id,
            active_state=str(active_state).strip(), target_soc=target, enabled=enabled,
        )
    if kind != SCHEDULE_KIND_TIME:
        _LOGGER.warning("Peak Guard schema: onbekend venstertype '%s' genegeerd", kind)
        return None
    try:
        days = sorted({int(x) for x in d.get("days", []) if 0 <= int(x) <= 6})
    except (ValueError, TypeError):
        days = []
    start, end = d.get("start", ""), d.get("end", "")
    if not days or parse_hhmm(start) is None or parse_hhmm(end) is None:
        _LOGGER.warning("Peak Guard schema: ongeldig tijdvenster genegeerd: %s", d)
        return None
    return ScheduleWindow(
        kind=SCHEDULE_KIND_TIME, days=days, start=str(start).strip(), end=str(end).strip(),
        target_soc=target, enabled=enabled,
    )


def entry_from_dict(d: dict) -> Optional[ScheduleEntry]:
    """Bouw een ScheduleEntry; None (met waarschuwing) als het apparaat ontbreekt."""
    try:
        device = cascade_from_dict(d["device"])
    except (KeyError, TypeError, ValueError) as err:
        _LOGGER.warning("Peak Guard schema: item zonder geldig apparaat genegeerd (%s)", err)
        return None
    if device.action_type not in SCHEDULABLE_ACTION_TYPES:
        _LOGGER.warning(
            "Peak Guard schema: apparaattype '%s' van '%s' kan niet gepland worden",
            device.action_type, device.name,
        )
        return None
    windows = [w for w in (window_from_dict(x) for x in d.get("windows", [])) if w is not None]
    rest_soc = _opt_int(d.get("rest_soc"))
    if rest_soc is not None:
        rest_soc = max(1, min(100, rest_soc))
    return ScheduleEntry(
        id=str(d.get("id") or device.id),
        device=device,
        enabled=bool(d.get("enabled", True)),
        windows=windows,
        max_current=_opt_float(d.get("max_current")),
        rest_soc=rest_soc,
        block_unplanned=bool(d.get("block_unplanned", True)),
    )


# ──────────────────────────────────────────────────────────────────────────── #
#  Venster-evaluatie                                                            #
# ──────────────────────────────────────────────────────────────────────────── #

def time_window_covers(window: ScheduleWindow, local_dt: datetime) -> bool:
    """True als het tijdvenster het (lokale) tijdstip bevat."""
    start = parse_hhmm(window.start)
    end = parse_hhmm(window.end)
    if start is None or end is None:
        return False
    minute = local_dt.hour * 60 + local_dt.minute
    weekday = local_dt.weekday()
    days = window.days or []
    if end > start:
        return weekday in days and start <= minute < end
    # end <= start: loopt over middernacht (start == end: 24 u).
    if weekday in days and minute >= start:
        return True
    return (weekday - 1) % 7 in days and minute < end


def window_active(
    window: ScheduleWindow,
    local_dt: datetime,
    sensor_states: Dict[str, Optional[str]],
) -> bool:
    """True als het venster nu actief is.

    sensor_states: effectieve (genormaliseerde) staat per tariefsensor; None =
    onbekend, en dan is een sensorvenster niet actief.
    """
    if not window.enabled:
        return False
    if window.kind == SCHEDULE_KIND_SENSOR:
        state = sensor_states.get(window.entity_id or "")
        return state is not None and state == normalize_state(window.active_state)
    return time_window_covers(window, local_dt)


@dataclass
class ActiveResult:
    active: bool
    target_soc: Optional[int] = None
    label: str = ""


def window_label(window: ScheduleWindow) -> str:
    """Leesbare omschrijving, bv. 'ma–vr 22:00–06:00' of 'sensor.p1_meter_tarief = 2'."""
    if window.kind == SCHEDULE_KIND_SENSOR:
        return f"{window.entity_id} = {window.active_state}"
    return f"{days_label(window.days)} {window.start}–{window.end}"


def days_label(days: Iterable[int]) -> str:
    ds = sorted(set(days))
    if len(ds) == 7:
        return "elke dag"
    parts, i = [], 0
    while i < len(ds):
        j = i
        while j + 1 < len(ds) and ds[j + 1] == ds[j] + 1:
            j += 1
        if j - i >= 2:
            parts.append(f"{DAY_LABELS[ds[i]]}–{DAY_LABELS[ds[j]]}")
        else:
            parts.extend(DAY_LABELS[k] for k in ds[i:j + 1])
        i = j + 1
    return ", ".join(parts)


def evaluate_entry(
    entry: ScheduleEntry,
    now: datetime,
    sensor_states: Dict[str, Optional[str]],
) -> ActiveResult:
    """Samengevoegde toestand van alle vensters van een item.

    Actief zodra één venster actief is; bij overlap geldt het hoogste doel.
    """
    local_dt = dt_util.as_local(now)
    active = [w for w in entry.windows if window_active(w, local_dt, sensor_states)]
    if not active:
        return ActiveResult(active=False)
    targets = [w.target_soc for w in active if w.target_soc is not None]
    best = max(active, key=lambda w: w.target_soc if w.target_soc is not None else -1)
    return ActiveResult(
        active=True,
        target_soc=max(targets) if targets else None,
        label=window_label(best),
    )


def sensor_entities(entries: Iterable[ScheduleEntry]) -> set:
    """Alle tariefsensoren die in (ingeschakelde) sensorvensters gebruikt worden."""
    return {
        w.entity_id
        for e in entries if e.enabled
        for w in e.windows
        if w.kind == SCHEDULE_KIND_SENSOR and w.enabled and w.entity_id
    }
