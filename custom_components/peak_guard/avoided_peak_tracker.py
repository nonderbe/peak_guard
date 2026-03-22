"""
avoided_peak_tracker.py  —  v4
================================
Twee volledig gescheiden trackers in één module:

  PeakAvoidTracker   — modus 1: piekbeperking
    Drie stappen:
      1. record_pending_avoid(device_id, ...)      @ cascade uitschakeling
      2. start_measurement_on_turnon(device_id, .) @ cascade herinschakeling
      3. complete_peak_calculation(device_id, now) @ natuurlijke power-drop
    Berekening: kW-impact op kwartierblokken vanaf avoid_ts.
    Besparing : (hypo_peak − actual_peak) × tarief_eur_kw_jaar / 12

  SolarShiftTracker  — modus 2: injectiepreventie
    Twee stappen:
      1. start_solar_measurement(device_id, ...)   @ cascade inschakeling
      2. complete_solar_calculation(device_id, now)@ terug-naar-normaal
    Berekening: verschoven_kWh = duur × nominal_kW  (geen kwartier-logica)
    Besparing : verschoven_kWh × netto_besparing_per_kwh
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

_LOGGER = logging.getLogger(__name__)

_QUARTER_MIN = 15
_QUARTER_H   = _QUARTER_MIN / 60.0   # 0.25 h

MAX_EVENTS = 50   # maximale loglijst per tracker


def _quarter_start(dt: datetime) -> datetime:
    """Geeft het begin van het kwartierblok van dt (UTC)."""
    return dt.replace(minute=(dt.minute // 15) * 15, second=0, microsecond=0)


# ──────────────────────────────────────────────────────────────────── #
#  Datastructuren                                                       #
# ──────────────────────────────────────────────────────────────────── #

@dataclass
class PendingAvoid:
    device_id:   str
    device_name: str
    nominal_kw:  float
    avoid_ts:    datetime


@dataclass
class ActivePeakMeasurement:
    device_id:   str
    device_name: str
    nominal_kw:  float
    avoid_ts:    datetime   # origineel vermijdingsmoment
    turnon_ts:   datetime   # moment herinschakeling


@dataclass
class PeakEvent:
    """Afgerond piek-vermijdings-event."""
    device_id:             str
    device_name:           str
    nominal_kw:            float
    avoid_ts:              datetime
    turnon_ts:             datetime
    natural_stop_ts:       datetime
    measured_duration_min: float
    added_energy_kwh:      float
    avoided_peak_kw:       float
    savings_euro:          float


@dataclass
class ActiveSolarMeasurement:
    device_id:   str
    device_name: str
    nominal_kw:  float
    turnon_ts:   datetime   # moment geforceerde inschakeling


@dataclass
class SolarEvent:
    """Afgerond solar-shift-event."""
    device_id:             str
    device_name:           str
    nominal_kw:            float
    turnon_ts:             datetime
    restore_ts:            datetime
    measured_duration_min: float
    shifted_kwh:           float
    savings_euro:          float


# ──────────────────────────────────────────────────────────────────── #
#  Modus 1 — PeakAvoidTracker                                          #
# ──────────────────────────────────────────────────────────────────── #

class PeakAvoidTracker:
    """Berekent vermeden piekbijdrage (kW) en besparing via capaciteitstarief."""

    def __init__(self) -> None:
        self._pending: Dict[str, PendingAvoid] = {}
        self._active:  Dict[str, ActivePeakMeasurement] = {}

        # Hypothetische extra kW per kwartierstart
        self.extra_dict: Dict[datetime, float] = {}

        # Events log (max 50, FIFO)
        self.events: deque[PeakEvent] = deque(maxlen=MAX_EVENTS)

        # Maand-cumulatieven
        self.avoided_kw_this_month:   float = 0.0
        self.savings_euro_this_month:  float = 0.0
        # Jaar-cumulatief (overleeft maandwissel)
        self.savings_euro_this_year:   float = 0.0

        # Hypothetische maandpiek
        self.hypothetical_monthly_peak_kw: Optional[float] = None

        # Context (bijgewerkt door SharedCapacityState)
        self._actual_quarters:     Dict[datetime, float] = {}
        self._actual_monthly_peak: float = 0.0
        self._tarief:              float = 0.0   # €/kW/jaar

    # ── context ──────────────────────────────────────────────────── #

    def set_context(self, actual_quarters: Dict[datetime, float],
                    actual_monthly_peak: float) -> None:
        self._actual_quarters     = actual_quarters
        self._actual_monthly_peak = actual_monthly_peak

    def set_tarief(self, tarief: float) -> None:
        self._tarief = tarief

    def reset_month(self) -> None:
        self.extra_dict.clear()
        self.events.clear()
        self.avoided_kw_this_month  = 0.0
        self.savings_euro_this_month = 0.0
        self.hypothetical_monthly_peak_kw = None
        _LOGGER.info("PeakAvoidTracker: maanddata gereset")

    def reset_year(self) -> None:
        self.savings_euro_this_year = 0.0
        _LOGGER.info("PeakAvoidTracker: jaardata gereset")

    # ── Hook 1: cascade schakelt UIT ─────────────────────────────── #

    def record_pending_avoid(self, device_id: str, device_name: str,
                              nominal_kw: float,
                              ts: Optional[datetime] = None) -> None:
        if ts is None:
            ts = datetime.now(timezone.utc)
        self._pending[device_id] = PendingAvoid(
            device_id=device_id, device_name=device_name,
            nominal_kw=nominal_kw, avoid_ts=ts,
        )
        self._active.pop(device_id, None)
        _LOGGER.debug("PeakAvoidTracker: pending '%s' @ %s", device_name, ts.isoformat())

    # ── Hook 2: cascade schakelt terug IN ────────────────────────── #

    def start_measurement_on_turnon(self, device_id: str, device_name: str,
                                     ts: Optional[datetime] = None) -> None:
        if device_id not in self._pending:
            return
        if ts is None:
            ts = datetime.now(timezone.utc)
        p = self._pending[device_id]
        self._active[device_id] = ActivePeakMeasurement(
            device_id=device_id, device_name=device_name,
            nominal_kw=p.nominal_kw, avoid_ts=p.avoid_ts, turnon_ts=ts,
        )
        _LOGGER.debug("PeakAvoidTracker: meting gestart '%s' turnon @ %s",
                      device_name, ts.isoformat())

    # ── Hook 3: natuurlijke stop → bereken kW-impact ─────────────── #

    def complete_peak_calculation(self, device_id: str,
                                   now: Optional[datetime] = None) -> Optional[PeakEvent]:
        if device_id not in self._active:
            return None
        if now is None:
            now = datetime.now(timezone.utc)

        meas = self._active.pop(device_id)
        self._pending.pop(device_id, None)

        raw_min = (now - meas.turnon_ts).total_seconds() / 60.0
        duration_min = min(raw_min, float(_QUARTER_MIN))

        if duration_min < 0.5:
            _LOGGER.debug("PeakAvoidTracker: '%s' te kort (%.1f min) genegeerd",
                          meas.device_name, duration_min)
            return None

        added_kwh = (duration_min / 60.0) * meas.nominal_kw

        # Verdeel energie over kwartierblokken vanaf avoid_ts
        for q, kwh in self._distribute(meas.avoid_ts, duration_min, added_kwh).items():
            self.extra_dict[q] = self.extra_dict.get(q, 0.0) + kwh / _QUARTER_H

        self._recalc_hypo()

        hypo     = self.hypothetical_monthly_peak_kw or 0.0
        avoided  = round(max(0.0, hypo - self._actual_monthly_peak), 4)
        savings  = round(max(0.0, hypo - self._actual_monthly_peak) * self._tarief / 12.0, 4)

        self.avoided_kw_this_month   = round(self.avoided_kw_this_month + avoided, 4)
        self.savings_euro_this_month  = round(self.savings_euro_this_month + savings, 4)
        self.savings_euro_this_year   = round(self.savings_euro_this_year + savings, 4)

        event = PeakEvent(
            device_id=device_id, device_name=meas.device_name,
            nominal_kw=meas.nominal_kw,
            avoid_ts=meas.avoid_ts, turnon_ts=meas.turnon_ts,
            natural_stop_ts=now,
            measured_duration_min=round(duration_min, 2),
            added_energy_kwh=round(added_kwh, 4),
            avoided_peak_kw=avoided,
            savings_euro=savings,
        )
        self.events.append(event)
        _LOGGER.info("PeakAvoidTracker: '%s' duur=%.1fmin vermeden=%.3fkW €%.4f",
                     meas.device_name, duration_min, avoided, savings)
        return event

    # ── query-methoden ────────────────────────────────────────────── #

    def get_pending_ids(self) -> list[str]:
        return list(self._pending.keys())

    def get_active_ids(self) -> list[str]:
        return list(self._active.keys())

    # ── interne helpers ───────────────────────────────────────────── #

    def _distribute(self, start_ts: datetime, duration_min: float,
                    total_kwh: float) -> Dict[datetime, float]:
        end_ts = start_ts + timedelta(minutes=duration_min)
        result: Dict[datetime, float] = {}
        q = _quarter_start(start_ts)
        while q < end_ts:
            q_end = q + timedelta(minutes=_QUARTER_MIN)
            overlap = (min(end_ts, q_end) - max(start_ts, q)).total_seconds() / 60.0
            if overlap > 0:
                result[q] = total_kwh * (overlap / duration_min)
            q = q_end
        return result

    def _recalc_hypo(self) -> None:
        all_q = set(self._actual_quarters) | set(self.extra_dict)
        if not all_q:
            self.hypothetical_monthly_peak_kw = None
            return
        vals = [self._actual_quarters.get(q, 0.0) + self.extra_dict.get(q, 0.0)
                for q in all_q]
        self.hypothetical_monthly_peak_kw = round(max(vals), 4)


# ──────────────────────────────────────────────────────────────────── #
#  Modus 2 — SolarShiftTracker                                         #
# ──────────────────────────────────────────────────────────────────── #

class SolarShiftTracker:
    """
    Berekent verschoven energie (kWh) en besparing bij injectiepreventie.
    Geen kwartier-logica — enkel: duur × nominal_kW = verschoven_kWh.
    Besparing = verschoven_kWh × netto_besparing_per_kwh.
    """

    def __init__(self) -> None:
        self._active: Dict[str, ActiveSolarMeasurement] = {}

        # Events log (max 50, FIFO)
        self.events: deque[SolarEvent] = deque(maxlen=MAX_EVENTS)

        # Maand-cumulatieven
        self.shifted_kwh_this_month:   float = 0.0
        self.savings_euro_this_month:  float = 0.0
        # Jaar-cumulatief
        self.savings_euro_this_year:   float = 0.0

        # Tarief: netto besparing per verschoven kWh
        self._netto_eur_per_kwh: float = 0.25   # default

    # ── context ──────────────────────────────────────────────────── #

    def set_netto_eur_per_kwh(self, value: float) -> None:
        self._netto_eur_per_kwh = value

    def reset_month(self) -> None:
        self.events.clear()
        self.shifted_kwh_this_month  = 0.0
        self.savings_euro_this_month = 0.0
        _LOGGER.info("SolarShiftTracker: maanddata gereset")

    def reset_year(self) -> None:
        self.savings_euro_this_year = 0.0
        _LOGGER.info("SolarShiftTracker: jaardata gereset")

    # ── Hook A: cascade schakelt apparaat IN (injectiepreventie) ──── #

    def start_solar_measurement(self, device_id: str, device_name: str,
                                  nominal_kw: float,
                                  ts: Optional[datetime] = None) -> None:
        """
        Start timer voor solar-shift meting.
        Aanroepen in _apply_action() bij ACTION_SWITCH_ON (inject cascade),
        vlak NA de succesvolle turn_on service call.
        Overschrijft eventuele lopende meting (herhaald inschakelen).
        """
        if ts is None:
            ts = datetime.now(timezone.utc)
        self._active[device_id] = ActiveSolarMeasurement(
            device_id=device_id, device_name=device_name,
            nominal_kw=nominal_kw, turnon_ts=ts,
        )
        _LOGGER.debug("SolarShiftTracker: meting gestart '%s' @ %s",
                      device_name, ts.isoformat())

    # ── Hook B: terug-naar-normaal → bereken kWh-verschuiving ─────── #

    def complete_solar_calculation(self, device_id: str,
                                    now: Optional[datetime] = None) -> Optional[SolarEvent]:
        """
        Rondt de solar-shift berekening af.
        Aanroepen in _restore_device() bij ACTION_SWITCH_ON + turn_off,
        vlak NA de succesvolle turn_off service call.
        Geen duurlimiet van 15 min — zonnesessie kan langer duren.
        """
        if device_id not in self._active:
            return None
        if now is None:
            now = datetime.now(timezone.utc)

        meas = self._active.pop(device_id)
        duration_min = (now - meas.turnon_ts).total_seconds() / 60.0

        if duration_min < 0.5:
            _LOGGER.debug("SolarShiftTracker: '%s' te kort (%.1f min) genegeerd",
                          meas.device_name, duration_min)
            return None

        shifted_kwh = round((duration_min / 60.0) * meas.nominal_kw, 4)
        savings     = round(shifted_kwh * self._netto_eur_per_kwh, 4)

        self.shifted_kwh_this_month  = round(self.shifted_kwh_this_month + shifted_kwh, 4)
        self.savings_euro_this_month = round(self.savings_euro_this_month + savings, 4)
        self.savings_euro_this_year  = round(self.savings_euro_this_year + savings, 4)

        event = SolarEvent(
            device_id=device_id, device_name=meas.device_name,
            nominal_kw=meas.nominal_kw,
            turnon_ts=meas.turnon_ts, restore_ts=now,
            measured_duration_min=round(duration_min, 2),
            shifted_kwh=shifted_kwh,
            savings_euro=savings,
        )
        self.events.append(event)
        _LOGGER.info("SolarShiftTracker: '%s' duur=%.1fmin verschoven=%.4fkWh €%.4f",
                     meas.device_name, duration_min, shifted_kwh, savings)
        return event

    # ── query-methoden ────────────────────────────────────────────── #

    def get_active_ids(self) -> list[str]:
        return list(self._active.keys())


# ──────────────────────────────────────────────────────────────────── #
#  Backwards-compat alias (gebruikt door bestaande sensor.py-imports) #
# ──────────────────────────────────────────────────────────────────── #
AvoidedPeakTracker = PeakAvoidTracker