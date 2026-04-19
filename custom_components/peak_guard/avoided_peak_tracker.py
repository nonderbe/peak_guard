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

MAX_EVENTS = 100   # maximale loglijst per tracker


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
    hypothetical_peak_kw:  float = 0.0


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

        # Maand-cumulatieven (holistisch herberekend, niet per event opgeteld)
        self.avoided_kw_this_month:   float = 0.0
        self.savings_euro_this_month:  float = 0.0
        # Jaar-cumulatief: basis van afgesloten maanden + lopende maand
        self._savings_euro_year_base:  float = 0.0
        self.savings_euro_this_year:   float = 0.0

        # Hypothetische maandpiek
        self.hypothetical_monthly_peak_kw: Optional[float] = None
        # Lijst van alle berekende hypothetische pieken deze maand (één per event).
        # Wordt gepersisteerd zodat de hoogste waarde na een herstart beschikbaar blijft.
        self.hypothetical_peaks_this_month: List[float] = []

        # Context (bijgewerkt door SharedCapacityState)
        self._actual_quarters:     Dict[datetime, float] = {}
        self._actual_monthly_peak: float = 0.0
        self._tarief:              float = 0.0   # €/kW/jaar

    # ── context ──────────────────────────────────────────────────── #

    def set_context(self, actual_quarters: Dict[datetime, float],
                    actual_monthly_peak: float) -> None:
        self._actual_quarters     = actual_quarters
        self._actual_monthly_peak = actual_monthly_peak
        # Herbereken hypothetische piek met de nieuwste kwartierdata, daarna besparing.
        # Zonder deze recalc-stap was de hypothetische piek verouderd: complete_peak_calculation
        # vuurde terwijl het kwartier nog niet afgesloten was (_actual_quarters leeg),
        # waardoor hypo = enkel extra_dict (zonder basisbelasting) → te laag → besparing = 0.
        self._recalc_hypo()
        self._recalc_month_savings()
        _LOGGER.debug(
            "PeakAvoidTracker.set_context: %d kwartieren, maandpiek=%.3f kW, "
            "besparing=€%.4f",
            len(actual_quarters), actual_monthly_peak, self.savings_euro_this_month,
        )

    def set_tarief(self, tarief: float) -> None:
        self._tarief = tarief
        self._recalc_month_savings()
        _LOGGER.debug("PeakAvoidTracker.set_tarief: %.4f €/kW/jaar", tarief)

    def reset_month(self) -> None:
        # Sla de definitieve maandbesparing op in de jaarbasis vóór het resetten
        self._savings_euro_year_base = round(
            self._savings_euro_year_base + self.savings_euro_this_month, 4
        )
        # Waarschuw als er nog actieve metingen lopen bij de maandwissel
        if self._pending:
            _LOGGER.warning(
                "PeakAvoidTracker reset_month: %d pending meting(en) weggegooid bij maandwissel: %s",
                len(self._pending),
                list(self._pending.keys()),
            )
        if self._active:
            _LOGGER.warning(
                "PeakAvoidTracker reset_month: %d actieve meting(en) weggegooid bij maandwissel: %s",
                len(self._active),
                list(self._active.keys()),
            )
        self._pending.clear()
        self._active.clear()
        self.extra_dict.clear()
        self.events.clear()
        self.avoided_kw_this_month  = 0.0
        self.savings_euro_this_month = 0.0
        self.hypothetical_monthly_peak_kw = None
        self.hypothetical_peaks_this_month.clear()
        _LOGGER.info("PeakAvoidTracker: maanddata gereset")

    def reset_year(self) -> None:
        self._savings_euro_year_base = 0.0
        self.savings_euro_this_year  = 0.0
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
        p = self._pending.pop(device_id)   # verwijder uit pending bij overgang → active
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

        # Snapshot maandbesparing vóór dit event (voor marginale bijdrage)
        savings_before = self.savings_euro_this_month
        avoided_before = self.avoided_kw_this_month

        # Verdeel energie over kwartierblokken vanaf avoid_ts
        for q, kwh in self._distribute(meas.avoid_ts, duration_min, added_kwh).items():
            self.extra_dict[q] = self.extra_dict.get(q, 0.0) + kwh / _QUARTER_H

        self._recalc_hypo()
        if self.hypothetical_monthly_peak_kw is not None:
            self.hypothetical_peaks_this_month.append(self.hypothetical_monthly_peak_kw)
        # Herbereken maandtotalen holistisch: niet accumuleren maar opnieuw berekenen
        self._recalc_month_savings()

        hypo    = self.hypothetical_monthly_peak_kw or 0.0
        # Marginale bijdrage van dit event aan de maandbesparing
        event_avoided = round(max(0.0, self.avoided_kw_this_month - avoided_before), 4)
        event_savings = round(max(0.0, self.savings_euro_this_month - savings_before), 4)

        _LOGGER.debug(
            "PeakAvoidTracker.complete_peak_calculation '%s': "
            "hypo=%.3f kW, actual=%.3f kW, maand_vermeden=%.4f kW, "
            "event_vermeden=%.4f kW, maand_besparing=€%.4f",
            meas.device_name, hypo, self._actual_monthly_peak,
            self.avoided_kw_this_month, event_avoided, self.savings_euro_this_month,
        )

        event = PeakEvent(
            device_id=device_id, device_name=meas.device_name,
            nominal_kw=meas.nominal_kw,
            avoid_ts=meas.avoid_ts, turnon_ts=meas.turnon_ts,
            natural_stop_ts=now,
            measured_duration_min=round(duration_min, 2),
            added_energy_kwh=round(added_kwh, 4),
            avoided_peak_kw=event_avoided,
            savings_euro=event_savings,
            hypothetical_peak_kw=round(hypo, 4),
        )
        self.events.append(event)
        _LOGGER.info(
            "PeakAvoidTracker: '%s' duur=%.1fmin event_vermeden=%.3fkW €%.4f "
            "| maand totaal: vermeden=%.3fkW €%.4f",
            meas.device_name, duration_min, event_avoided, event_savings,
            self.avoided_kw_this_month, self.savings_euro_this_month,
        )
        return event

    # ── query-methoden ────────────────────────────────────────────── #

    def get_pending_ids(self) -> list[str]:
        return list(self._pending.keys())

    def get_active_ids(self) -> list[str]:
        return list(self._active.keys())

    def get_active_nominal_kw(self, device_id: str) -> Optional[float]:
        """Geeft het opgeslagen nominaal vermogen (kW) voor een actieve meting, of None."""
        meas = self._active.get(device_id)
        return meas.nominal_kw if meas else None

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
        if all_q:
            vals = [self._actual_quarters.get(q, 0.0) + self.extra_dict.get(q, 0.0)
                    for q in all_q]
            live = round(max(vals), 4)
        else:
            live = 0.0
        # Neem de hoogste eerder berekende hypo van deze maand mee als vloer.
        # Na een herstart is extra_dict leeg, maar de lijst overleeft via persistentie.
        floor = max(self.hypothetical_peaks_this_month) if self.hypothetical_peaks_this_month else 0.0
        combined = max(live, floor)
        self.hypothetical_monthly_peak_kw = combined if combined > 0 else None

    def _recalc_month_savings(self) -> None:
        """
        Herbereken maand- en jaarbesparing holistisch op basis van de huidige
        hypothetische maandpiek versus de werkelijke maandpiek.

        De maandbesparing is een enkelvoudige waarde:
            (hypo_maandpiek − werkelijke_maandpiek) × tarief / 12

        Dit voorkomt dat individuele events dubbel worden opgeteld, en zorgt
        dat een latere echte piek de besparing automatisch vermindert.
        """
        hypo = self.hypothetical_monthly_peak_kw or 0.0
        self.avoided_kw_this_month  = round(max(0.0, hypo - self._actual_monthly_peak), 4)
        self.savings_euro_this_month = round(
            self.avoided_kw_this_month * self._tarief / 12.0, 4
        )
        self.savings_euro_this_year  = round(
            self._savings_euro_year_base + self.savings_euro_this_month, 4
        )


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
        _LOGGER.debug("SolarShiftTracker.set_netto_eur_per_kwh: %.4f €/kWh", value)

    def reset_month(self) -> None:
        if self._active:
            _LOGGER.warning(
                "SolarShiftTracker reset_month: %d actieve meting(en) weggegooid bij maandwissel: %s",
                len(self._active),
                list(self._active.keys()),
            )
        self._active.clear()
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