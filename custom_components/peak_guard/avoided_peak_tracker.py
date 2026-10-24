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
    Besparing : (max(hypo_peak, 2,5) − max(actual_peak, 2,5)) × tarief_eur_kw_jaar / 12

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
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

from .const import CAPACITY_MIN_KW
from .utils import quarter_start as _quarter_start

_LOGGER = logging.getLogger(__name__)

_QUARTER_MIN = 15
_QUARTER_H   = _QUARTER_MIN / 60.0   # 0.25 h

MAX_EVENTS = 100   # maximale loglijst per tracker


# ──────────────────────────────────────────────────────────────────── #
#  Datastructuren                                                       #
# ──────────────────────────────────────────────────────────────────── #

@dataclass
class PendingAvoid:
    device_id:   str
    device_name: str
    nominal_kw:  float
    avoid_ts:    datetime
    # EV: de lading schuift op naar het einde van de sessie (zie
    # start_measurement_on_turnon) in plaats van na het herinschakelen in te halen.
    deferred:    bool = False


@dataclass
class ActivePeakMeasurement:
    device_id:   str
    device_name: str
    nominal_kw:  float
    avoid_ts:    datetime   # origineel vermijdingsmoment
    turnon_ts:   datetime   # moment herinschakeling


@dataclass
class PeakEvent:
    """
    Afgerond piek-vermijdings-event.

    hypothetical_peak_kw, avoided_peak_kw en savings_euro gelden voor dít
    event: de hoogste kwartierwaarde zonder Peak Guard in de kwartieren waar
    de uitgestelde energie anders zou zijn gevallen, en wat dat ten opzichte
    van de werkelijke maandpiek uitspaart. Ze worden herberekend zolang de
    maand loopt (open kwartier, latere hogere piek, P1-correctie). Events
    overlappen: hun besparingen zijn niet optelbaar tot het maandtotaal.
    """
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
    # Kwartieren waar de uitgestelde energie zonder Peak Guard was gevallen.
    # Leeg bij events van vóór v1.9.2: die houden hun opgeslagen waarden.
    avoid_quarters:        List[datetime] = field(default_factory=list)


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

        # Netto verschoven vermogen (kW) per apparaat per kwartierstart:
        # + waar de energie zonder Peak Guard zou zijn gevallen (vanaf avoid_ts),
        # − waar ze na het herinschakelen werkelijk viel (vanaf turnon_ts, en
        # dus al in de gemeten kwartierwaarde zit). Wordt gepersisteerd.
        self.extra_by_device: Dict[str, Dict[datetime, float]] = {}
        # Som over alle apparaten (afgeleid, bijgewerkt door _recalc)
        self.extra_dict: Dict[datetime, float] = {}

        # Events log (max 100, FIFO)
        self.events: deque[PeakEvent] = deque(maxlen=MAX_EVENTS)

        # Maand-cumulatieven (holistisch herberekend, niet per event opgeteld)
        self.avoided_kw_this_month:   float = 0.0
        self.savings_euro_this_month:  float = 0.0
        # Jaar-cumulatief: basis van afgesloten maanden + lopende maand
        self._savings_euro_year_base:  float = 0.0
        self.savings_euro_this_year:   float = 0.0

        # Hypothetische maandpiek
        self.hypothetical_monthly_peak_kw: Optional[float] = None
        # Vloer uit een opgeslagen maandstaat van vóór v1.9.2 (zonder
        # extra_by_device). Wordt niet meer aangevuld; vervalt bij de maandwissel.
        self.hypothetical_peaks_this_month: List[float] = []

        # Per-apparaat hypothetische maandpiek: de werkelijke kwartieren plus
        # enkel de verschuiving van dát apparaat — "wat had dit apparaat
        # alleen gekost". Afgeleid uit extra_by_device door _recalc.
        self.device_max_hypo_this_month: Dict[str, float] = {}
        self.device_names_this_month:    Dict[str, str] = {}
        # Vloer per apparaat in een maand die met een oudere versie begon: de
        # laatst opgeslagen maandrecord per apparaat (zie hypothetical_peaks_this_month).
        self.device_hypo_floor_this_month: Dict[str, float] = {}

        # Kwartieren waarin de piek-cascade uitgeput was (alle apparaten al
        # uit/verlaagd en toch nog overschot): kwartierstart → hoogste rest (W).
        # Verklaart een maandpiek die Peak Guard niet kon vermijden.
        self.exhausted_quarters: Dict[datetime, float] = {}

        # Context (bijgewerkt door SharedCapacityState)
        self._actual_quarters:     Dict[datetime, float] = {}
        self._actual_monthly_peak: float = 0.0
        self._tarief:              float = 0.0   # €/kW/jaar

    # ── context ──────────────────────────────────────────────────── #

    def set_context(self, actual_quarters: Dict[datetime, float],
                    actual_monthly_peak: float) -> None:
        self._actual_quarters     = actual_quarters
        self._actual_monthly_peak = actual_monthly_peak
        # Herbereken met de nieuwste kwartierdata: een kwartier dat afsluit,
        # een latere hogere piek of een door de P1-meter gewist kwartier
        # werken zo door in maandtotaal, per-apparaat- en per-event-waarden.
        self._recalc()
        _LOGGER.debug(
            "PeakAvoidTracker.set_context: %d kwartieren, maandpiek=%.3f kW, "
            "besparing=€%.4f",
            len(actual_quarters), actual_monthly_peak, self.savings_euro_this_month,
        )

    def set_tarief(self, tarief: float) -> None:
        self._tarief = tarief
        self._recalc()
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
        self.extra_by_device.clear()
        self.extra_dict.clear()
        self.events.clear()
        self.avoided_kw_this_month  = 0.0
        self.savings_euro_this_month = 0.0
        self.hypothetical_monthly_peak_kw = None
        self.hypothetical_peaks_this_month.clear()
        self.device_max_hypo_this_month.clear()
        self.device_names_this_month.clear()
        self.device_hypo_floor_this_month.clear()
        self.exhausted_quarters.clear()
        _LOGGER.info("PeakAvoidTracker: maanddata gereset")

    def reset_year(self) -> None:
        self._savings_euro_year_base = 0.0
        self.savings_euro_this_year  = 0.0
        _LOGGER.info("PeakAvoidTracker: jaardata gereset")

    # ── Hook 1: cascade schakelt UIT ─────────────────────────────── #

    def record_pending_avoid(self, device_id: str, device_name: str,
                              nominal_kw: float,
                              ts: Optional[datetime] = None,
                              deferred: bool = False) -> None:
        if ts is None:
            ts = datetime.now(timezone.utc)
        # Opnieuw afgeschakeld terwijl de vorige meting nog liep: rond die
        # eerst af, anders gaat de eerdere vermijding verloren.
        if device_id in self._active:
            self.complete_peak_calculation(device_id, now=ts)
        # Nog altijd uit sinds de vorige afschakeling: het oorspronkelijke
        # uitstelmoment blijft gelden.
        if device_id in self._pending:
            _LOGGER.debug("PeakAvoidTracker: '%s' al pending sinds %s",
                          device_name, self._pending[device_id].avoid_ts.isoformat())
            return
        self._pending[device_id] = PendingAvoid(
            device_id=device_id, device_name=device_name,
            nominal_kw=nominal_kw, avoid_ts=ts, deferred=deferred,
        )
        _LOGGER.debug("PeakAvoidTracker: pending '%s' @ %s", device_name, ts.isoformat())

    # ── Hook 2: cascade schakelt terug IN ────────────────────────── #

    def start_measurement_on_turnon(self, device_id: str, device_name: str,
                                     ts: Optional[datetime] = None,
                                     max_kw: Optional[float] = None) -> None:
        """
        max_kw: het apparaat trekt na herstel minder (bv. EV op lagere stroom).

        Een uitgestelde EV-lading (deferred) wordt hier meteen afgerond: zonder
        Peak Guard had de wagen de hele stilstand verder geladen, dus die
        energie valt in de kwartieren van avoid_ts tot nu. Ze wordt niet na het
        herinschakelen ingehaald maar aan het einde van de sessie, uren later
        en buiten de kwartieren die ertoe doen; dat minus-deel wordt niet
        geboekt. De daling van het huisverbruik wordt voor een EV dus niet als
        natuurlijke stop gebruikt.
        """
        if device_id not in self._pending:
            return
        if ts is None:
            ts = datetime.now(timezone.utc)
        if self._pending[device_id].deferred:
            self.complete_deferral(device_id, ts)
            return
        p = self._pending.pop(device_id)   # verwijder uit pending bij overgang → active
        nominal_kw = p.nominal_kw if max_kw is None else min(p.nominal_kw, max_kw)
        self._active[device_id] = ActivePeakMeasurement(
            device_id=device_id, device_name=device_name,
            nominal_kw=nominal_kw, avoid_ts=p.avoid_ts, turnon_ts=ts,
        )
        _LOGGER.debug("PeakAvoidTracker: meting gestart '%s' turnon @ %s",
                      device_name, ts.isoformat())

    def complete_expired(self, now: datetime) -> List[PeakEvent]:
        """
        Rond metingen af die al een kwartier lopen. De duur wordt toch op
        15 min begrensd; zonder deze stap blijft een meting waarvan de
        natuurlijke stop niet gedetecteerd wordt (modulerend verbruik,
        gelijktijdig ander verbruik) tot de maandwissel hangen. Zo ook een
        uitgestelde EV-lading die geen herstart meer ziet (kabel uit, vol,
        door iets anders herstart).
        """
        done: List[PeakEvent] = []
        limit = timedelta(minutes=_QUARTER_MIN)
        for device_id, p in list(self._pending.items()):
            if p.deferred and now - p.avoid_ts >= limit:
                event = self.complete_deferral(device_id, p.avoid_ts + limit)
                if event is not None:
                    done.append(event)
        for device_id, meas in list(self._active.items()):
            if now - meas.turnon_ts >= limit:
                event = self.complete_peak_calculation(device_id, now=meas.turnon_ts + limit)
                if event is not None:
                    done.append(event)
        return done

    def discard_pending(self, device_id: str) -> None:
        """Vergeet een afschakeling die nooit hersteld wordt (apparaat stond vóór het overschot uit)."""
        self._pending.pop(device_id, None)

    def complete_deferral(self, device_id: str, ts: datetime) -> Optional[PeakEvent]:
        """
        Rond een uitgestelde EV-lading af op ts (herstart, overname door de
        solar-cascade, of einde van het laadvenster). Geen effect als het
        apparaat geen uitgestelde lading heeft.
        """
        p = self._pending.get(device_id)
        if p is None or not p.deferred:
            return None
        del self._pending[device_id]
        # Begrensd: zonder bevestiging dat de wagen bleef laden (vol, kabel
        # uit, door iets anders herstart) is een langer uitstel geen besparing.
        off_min = min((ts - p.avoid_ts).total_seconds() / 60.0, float(_QUARTER_MIN))
        if off_min < 0.5 or p.nominal_kw <= 0:
            return None
        added_kwh = (off_min / 60.0) * p.nominal_kw
        avoid_parts = self._distribute(p.avoid_ts, off_min, added_kwh)
        dev_extra = self.extra_by_device.setdefault(device_id, {})
        for q, kwh in avoid_parts.items():
            dev_extra[q] = dev_extra.get(q, 0.0) + kwh / _QUARTER_H
        self.device_names_this_month[device_id] = p.device_name
        event = PeakEvent(
            device_id=device_id, device_name=p.device_name,
            nominal_kw=p.nominal_kw,
            avoid_ts=p.avoid_ts, turnon_ts=ts, natural_stop_ts=ts,
            measured_duration_min=round(off_min, 2),
            added_energy_kwh=round(added_kwh, 4),
            avoided_peak_kw=0.0, savings_euro=0.0,
            avoid_quarters=sorted(avoid_parts),
        )
        self.events.append(event)
        self._recalc()
        _LOGGER.info(
            "PeakAvoidTracker: '%s' lading %.1f min uitgesteld (max. 15) (%.3f kWh) "
            "hypo=%.3fkW event_vermeden=%.3fkW €%.4f",
            p.device_name, off_min, added_kwh, event.hypothetical_peak_kw,
            event.avoided_peak_kw, event.savings_euro,
        )
        return event

    def rebuild_legacy_events(self) -> int:
        """
        Bouw de verschuiving op van events van vóór v1.9.2 (zonder
        avoid_quarters), met dezelfde +/−-regel als complete_peak_calculation.
        Geeft het aantal herbouwde events terug. Geen herberekening hier
        (zie extra_from_dict).
        """
        rebuilt = 0
        for ev in self.events:
            if ev.avoid_quarters:
                continue
            if ev.measured_duration_min < 0.5 or ev.added_energy_kwh <= 0:
                continue
            dev_extra = self.extra_by_device.setdefault(ev.device_id, {})
            avoid_parts = self._distribute(
                ev.avoid_ts, ev.measured_duration_min, ev.added_energy_kwh
            )
            for q, kwh in avoid_parts.items():
                dev_extra[q] = dev_extra.get(q, 0.0) + kwh / _QUARTER_H
            for q, kwh in self._distribute(
                ev.turnon_ts, ev.measured_duration_min, ev.added_energy_kwh
            ).items():
                dev_extra[q] = dev_extra.get(q, 0.0) - kwh / _QUARTER_H
            ev.avoid_quarters = sorted(avoid_parts)
            self.device_names_this_month.setdefault(ev.device_id, ev.device_name)
            rebuilt += 1
        return rebuilt

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

        # Zonder Peak Guard was deze energie vanaf avoid_ts verbruikt; nu viel
        # ze vanaf turnon_ts en zit ze al in de gemeten kwartieren.
        dev_extra = self.extra_by_device.setdefault(device_id, {})
        avoid_parts = self._distribute(meas.avoid_ts, duration_min, added_kwh)
        for q, kwh in avoid_parts.items():
            dev_extra[q] = dev_extra.get(q, 0.0) + kwh / _QUARTER_H
        for q, kwh in self._distribute(meas.turnon_ts, duration_min, added_kwh).items():
            dev_extra[q] = dev_extra.get(q, 0.0) - kwh / _QUARTER_H
        self.device_names_this_month[device_id] = meas.device_name

        event = PeakEvent(
            device_id=device_id, device_name=meas.device_name,
            nominal_kw=meas.nominal_kw,
            avoid_ts=meas.avoid_ts, turnon_ts=meas.turnon_ts,
            natural_stop_ts=now,
            measured_duration_min=round(duration_min, 2),
            added_energy_kwh=round(added_kwh, 4),
            avoided_peak_kw=0.0,
            savings_euro=0.0,
            avoid_quarters=sorted(avoid_parts),
        )
        self.events.append(event)
        self._recalc()

        _LOGGER.info(
            "PeakAvoidTracker: '%s' duur=%.1fmin hypo=%.3fkW event_vermeden=%.3fkW €%.4f "
            "| maand totaal: hypo=%.3fkW vermeden=%.3fkW €%.4f",
            meas.device_name, duration_min, event.hypothetical_peak_kw,
            event.avoided_peak_kw, event.savings_euro,
            self.hypothetical_monthly_peak_kw or 0.0,
            self.avoided_kw_this_month, self.savings_euro_this_month,
        )
        return event

    def note_exhausted(self, ts: datetime, remaining_w: float) -> None:
        """De piek-cascade hield na alle apparaten nog remaining_w W over."""
        q = _quarter_start(ts)
        self.exhausted_quarters[q] = round(max(self.exhausted_quarters.get(q, 0.0), remaining_w), 1)

    def exhausted_to_dict(self) -> Dict[str, float]:
        return {q.isoformat(): w for q, w in sorted(self.exhausted_quarters.items())}

    def exhausted_from_dict(self, data) -> None:
        self.exhausted_quarters = {}
        for q, w in (data or {}).items():
            try:
                self.exhausted_quarters[datetime.fromisoformat(q)] = float(w)
            except (TypeError, ValueError):
                continue

    # ── query-methoden ────────────────────────────────────────────── #

    def get_pending_ids(self) -> list[str]:
        return list(self._pending.keys())

    def get_active_ids(self) -> list[str]:
        return list(self._active.keys())

    def get_active_nominal_kw(self, device_id: str) -> Optional[float]:
        """Geeft het opgeslagen nominaal vermogen (kW) voor een actieve meting, of None."""
        meas = self._active.get(device_id)
        return meas.nominal_kw if meas else None

    def get_device_monthly_savings(self) -> List[Dict[str, object]]:
        """
        Isoleert de besparing per apparaat voor de lopende maand.

        Voor elk apparaat dat deze maand minstens één piek-vermijdend event
        voltooide: de hoogste kwartierwaarde met enkel de verschuiving van
        dát apparaat erbij, vergeleken met de werkelijke maandpiek — dezelfde
        (hypo − actual) × tarief/12-methodologie als de globale maandbesparing
        (inclusief de 2,5 kW-ondergrens, zie _avoided_kw). Wat andere
        apparaten in hetzelfde kwartier uitstelden, telt hier niet mee; de
        som over apparaten is dus geen maandtotaal.

        Retourneert een lijst dicts, één per apparaat:
        device_id, device_name, hypothetical_peak_kw, actual_monthly_peak_kw,
        avoided_kw, savings_euro.
        """
        result: List[Dict[str, object]] = []
        for device_id, hypo in self.device_max_hypo_this_month.items():
            avoided_kw = self._avoided_kw(hypo)
            savings_euro = round(avoided_kw * self._tarief / 12.0, 4)
            result.append({
                "device_id": device_id,
                "device_name": self.device_names_this_month.get(device_id, device_id),
                "hypothetical_peak_kw": round(hypo, 4),
                "actual_monthly_peak_kw": round(self._actual_monthly_peak, 4),
                "avoided_kw": avoided_kw,
                "savings_euro": savings_euro,
            })
        return result

    # ── persistentie ──────────────────────────────────────────────── #

    def extra_to_dict(self) -> Dict[str, Dict[str, float]]:
        return {
            device_id: {q.isoformat(): round(kw, 4) for q, kw in quarters.items()}
            for device_id, quarters in self.extra_by_device.items()
        }

    def extra_from_dict(self, data: Dict[str, Dict[str, float]]) -> None:
        self.extra_by_device = {}
        for device_id, quarters in (data or {}).items():
            try:
                self.extra_by_device[device_id] = {
                    datetime.fromisoformat(q): float(kw) for q, kw in quarters.items()
                }
            except (AttributeError, TypeError, ValueError):
                _LOGGER.warning("PeakAvoidTracker: ongeldige verschuivingsdata voor '%s' genegeerd",
                                device_id)
        # Geen herberekening hier: bij het herstel moeten de opgeslagen maand-
        # en jaarbesparing blijven staan tot de jaarbasis gezet is. De eerste
        # set_context() rekent alles opnieuw uit.

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

    def _hypo_quarter(self, q: datetime, extra: Dict[datetime, float]) -> float:
        """Kwartierwaarde zonder (die) verschuiving(en), nooit onder 0."""
        return max(0.0, self._actual_quarters.get(q, 0.0) + extra.get(q, 0.0))

    def _max_hypo(self, extra: Dict[datetime, float]) -> float:
        quarters = set(self._actual_quarters) | set(extra)
        if not quarters:
            return 0.0
        return round(max(self._hypo_quarter(q, extra) for q in quarters), 4)

    def _recalc(self) -> None:
        totals: Dict[datetime, float] = {}
        for quarters in self.extra_by_device.values():
            for q, kw in quarters.items():
                totals[q] = totals.get(q, 0.0) + kw
        self.extra_dict = totals

        # Maandpiek zonder Peak Guard. De vloer bestaat enkel in een maand die
        # met een oudere versie begon (daar ging de verschuiving bij elke
        # herstart verloren).
        live = self._max_hypo(totals)
        floor = max(self.hypothetical_peaks_this_month) if self.hypothetical_peaks_this_month else 0.0
        combined = max(live, floor)
        self.hypothetical_monthly_peak_kw = combined if combined > 0 else None

        device_hypo = dict(self.device_hypo_floor_this_month)
        for device_id, quarters in self.extra_by_device.items():
            device_hypo[device_id] = max(
                self._max_hypo(quarters), device_hypo.get(device_id, 0.0)
            )
        self.device_max_hypo_this_month = device_hypo

        for event in self.events:
            if not event.avoid_quarters:
                continue   # event van vóór v1.9.2: opgeslagen waarden behouden
            hypo = max(self._hypo_quarter(q, totals) for q in event.avoid_quarters)
            event.hypothetical_peak_kw = round(hypo, 4)
            event.avoided_peak_kw = self._avoided_kw(hypo)
            event.savings_euro = round(event.avoided_peak_kw * self._tarief / 12.0, 4)

        self._recalc_month_savings()

    def _avoided_kw(self, hypo_kw: float) -> float:
        """
        Vermeden kW die ook echt geld oplevert: beide pieken worden eerst
        opgetrokken naar CAPACITY_MIN_KW, want onder dat minimum wordt geen
        extra capaciteitstarief aangerekend.
        """
        billed_hypo   = max(hypo_kw, CAPACITY_MIN_KW)
        billed_actual = max(self._actual_monthly_peak, CAPACITY_MIN_KW)
        return round(max(0.0, billed_hypo - billed_actual), 4)

    def _recalc_month_savings(self) -> None:
        """
        Herbereken maand- en jaarbesparing holistisch op basis van de huidige
        hypothetische maandpiek versus de werkelijke maandpiek.

        De maandbesparing is een enkelvoudige waarde:
            (max(hypo_maandpiek, 2,5) − max(werkelijke_maandpiek, 2,5)) × tarief / 12

        Dit voorkomt dat individuele events dubbel worden opgeteld, en zorgt
        dat een latere echte piek de besparing automatisch vermindert.
        """
        hypo = self.hypothetical_monthly_peak_kw or 0.0
        self.avoided_kw_this_month  = self._avoided_kw(hypo)
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
