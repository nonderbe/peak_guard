"""
quarter_calculator.py
---------------------
Berekent de lopende kwartierpiek (kW) op basis van een kWh-energiesensor.

Kwartierblokken zijn vaste blokken zoals Fluvius/DSMR ze gebruikt:
  00:00–00:15, 00:15–00:30, 00:30–00:45, 00:45–01:00, …

Elk kwartier wordt de energiedelta (kWh) bijgehouden t.o.v. het begin
van het blok. Het lopende gemiddeld vermogen is:
  P (kW) = delta_kWh / (verstreken_minuten / 60)
           = delta_kWh * 60 / verstreken_minuten

Aan het einde van een kwartier wordt de definitieve waarde opgeslagen
in de geschiedenis (QUARTER_HISTORY_DAYS × 96 kwartieren).
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Optional

from .const import MAX_PLAUSIBLE_QUARTER_KW
from .utils import quarter_start as _quarter_start

_LOGGER = logging.getLogger(__name__)

# Zolang een kwartier korter dan dit loopt, kan één stap van een grove sensor
# het lopende gemiddelde nog boven MAX_PLAUSIBLE_QUARTER_KW duwen.
_SETTLE_MINUTES = 1.0

# Een eerste meting tot zoveel seconden na de grens geldt als de stand op de grens.
_EXACT_START_S = 5.0
# Langer gat rond de grens: niet interpoleren, het kwartier is niet betrouwbaar.
_MAX_INTERPOLATION_GAP_S = 180.0


class QuarterCalculator:
    """
    Houdt de energiedelta bij binnen het lopende kwartierblok en
    berekent het huidige gemiddeld vermogen (kW).

    Gebruik:
        calc = QuarterCalculator()
        current_kw = calc.update(energy_kwh, now)
        if calc.quarter_just_finished:
            store(calc.last_finished_quarter)

    De meterstand op de kwartiergrens wordt lineair geïnterpoleerd tussen de
    laatste meting vóór en de eerste meting na de grens. Zonder interpolatie
    ging de energie tussen de grens en de eerste meting (tot een minuut, want
    de timer loopt per minuut op een willekeurige seconde) verloren en lag
    elk kwartier tot 6 % te laag.

    Een kwartier waarvan het begin niet gemeten werd (eerste meting na een
    herstart zonder bruikbare opgeslagen toestand, of een te lang gat rond de
    grens) is onvolledig: het wordt niet afgesloten, want de waarde is enkel
    een ondergrens.
    """

    def __init__(self) -> None:
        self._quarter_start_energy: Optional[float] = None
        self._current_quarter_start: Optional[datetime] = None
        # Tijdstip waarop _quarter_start_energy geldt: de kwartiergrens zelf
        # (volledig kwartier) of een latere eerste meting (onvolledig).
        self._measure_start: Optional[datetime] = None
        self._current_kw: float = 0.0
        self._last_finished_value: Optional[float] = None
        self._last_finished_ts: Optional[datetime] = None
        self._quarter_just_finished: bool = False
        # True zodra de meterstand in het lopende kwartier terugsprong of een
        # onmogelijk vermogen opleverde: het kwartier is dan onbetrouwbaar en
        # wordt niet afgesloten.
        self._quarter_invalid: bool = False
        # Laatste meting (voor de interpolatie op de kwartiergrens).
        self._prev_ts: Optional[datetime] = None
        self._prev_energy: Optional[float] = None

    # ---------------------------------------------------------------- #
    #  Publieke interface                                                #
    # ---------------------------------------------------------------- #

    @property
    def current_kw(self) -> float:
        """Lopend kwartiergemiddeld vermogen (kW)."""
        return self._current_kw

    @property
    def quarter_just_finished(self) -> bool:
        """True als er bij de laatste update() een kwartier afliep."""
        return self._quarter_just_finished

    @property
    def last_finished_value(self) -> Optional[float]:
        """Definitieve kW-waarde van het zojuist afgelopen kwartier."""
        return self._last_finished_value

    @property
    def last_finished_ts(self) -> Optional[datetime]:
        """Timestamp (start) van het zojuist afgelopen kwartier."""
        return self._last_finished_ts

    @property
    def current_quarter_start(self) -> Optional[datetime]:
        """Start van het lopende kwartier."""
        return self._current_quarter_start

    @property
    def quarter_complete(self) -> bool:
        """True als het lopende kwartier vanaf de grens gemeten wordt en geldig is."""
        return (
            self._current_quarter_start is not None
            and self._measure_start == self._current_quarter_start
            and not self._quarter_invalid
        )

    def baseline(self) -> Optional[tuple]:
        """
        (kwartierstart, meterstand op de grens) van het lopende kwartier, of
        None als het niet volledig gemeten wordt. De piek-beslisser rekent
        er de energie tot nu mee uit met een verse meterstand.
        """
        if not self.quarter_complete:
            return None
        return self._current_quarter_start, self._quarter_start_energy

    def energy_so_far_kwh(self) -> Optional[float]:
        """
        Energie (kWh) sinds het begin van het lopende kwartier, tot de laatste
        meting. None als het kwartier niet volledig gemeten wordt.
        """
        if not self.quarter_complete or self._prev_energy is None:
            return None
        if self._prev_ts is None or self._prev_ts < self._current_quarter_start:
            return None
        return max(0.0, self._prev_energy - self._quarter_start_energy)

    def update(self, energy_kwh: float, now: Optional[datetime] = None) -> float:
        """
        Verwerk een nieuwe energiemeting.

        Parameters
        ----------
        energy_kwh : float
            Huidige cumulatieve energie van de sensor (kWh).
            Moet een stijgende waarde zijn (zoals een P1-meter geeft).
        now : datetime, optional
            Tijdstip van de meting (UTC). Standaard: datetime.now(UTC).

        Returns
        -------
        float
            Huidig lopend kwartiergemiddeld vermogen (kW).
        """
        if now is None:
            now = datetime.now(timezone.utc)
        # Lokale tijd vergelijkt fout rond de zomer-/wintertijdwissel (fold).
        now = now.astimezone(timezone.utc)

        self._quarter_just_finished = False
        q_start = _quarter_start(now)

        if self._current_quarter_start is None:
            # Eerste meting ooit: enkel volledig als ze op de grens valt.
            self._start_quarter(q_start, energy_kwh, now)
        elif q_start > self._current_quarter_start:
            self._roll_over(q_start, energy_kwh, now)

        self._prev_ts = now
        self._prev_energy = energy_kwh
        return self._running_value(energy_kwh, now)

    # ---------------------------------------------------------------- #
    #  Persistentie (herstart midden in een kwartier)                   #
    # ---------------------------------------------------------------- #

    def to_dict(self) -> dict:
        """Toestand om een herstart te overbruggen (zie from_dict)."""
        def iso(dt: Optional[datetime]) -> Optional[str]:
            return dt.isoformat() if dt is not None else None
        return {
            "quarter_start": iso(self._current_quarter_start),
            "measure_start": iso(self._measure_start),
            "start_energy": self._quarter_start_energy,
            "invalid": self._quarter_invalid,
            "prev_ts": iso(self._prev_ts),
            "prev_energy": self._prev_energy,
        }

    def restore(self, data: Optional[dict]) -> bool:
        """
        Herstel de toestand van vóór een herstart. De volgende update() werkt
        dan alsof er geen herstart was: zelfde kwartier → exact verder meten,
        grens gepasseerd met een kort gat → geïnterpoleerd afsluiten, anders
        wordt het onderbroken kwartier niet afgesloten.
        Geeft True terug als er iets hersteld werd.
        """
        if not isinstance(data, dict):
            return False
        try:
            q = datetime.fromisoformat(data["quarter_start"])
            m = datetime.fromisoformat(data["measure_start"])
            start = float(data["start_energy"])
            prev_ts = datetime.fromisoformat(data["prev_ts"])
            prev_e = float(data["prev_energy"])
        except (KeyError, TypeError, ValueError):
            return False
        if not (math.isfinite(start) and math.isfinite(prev_e)):
            return False
        if q.tzinfo is None or m.tzinfo is None or prev_ts.tzinfo is None:
            return False
        q, m, prev_ts = (dt.astimezone(timezone.utc) for dt in (q, m, prev_ts))
        self._current_quarter_start = q
        self._measure_start = m
        self._quarter_start_energy = start
        self._quarter_invalid = bool(data.get("invalid", False))
        self._prev_ts = prev_ts
        self._prev_energy = prev_e
        return True

    # ---------------------------------------------------------------- #
    #  Intern                                                            #
    # ---------------------------------------------------------------- #

    def _start_quarter(self, q_start: datetime, energy_kwh: float, at: datetime) -> None:
        self._current_quarter_start = q_start
        self._quarter_start_energy = energy_kwh
        # Valt de meting (bijna) op de grens, dan is het kwartier volledig.
        if (at - q_start).total_seconds() <= _EXACT_START_S:
            self._measure_start = q_start
        else:
            self._measure_start = at
        self._quarter_invalid = False
        self._current_kw = 0.0

    def _roll_over(self, q_start: datetime, energy_kwh: float, now: datetime) -> None:
        old_start = self._current_quarter_start
        boundary = old_start + timedelta(minutes=15)
        e_boundary: Optional[float] = None
        if (
            self._prev_ts is not None
            and self._prev_energy is not None
            and q_start == boundary                 # precies één grens gepasseerd
            and self._prev_ts <= boundary <= now
            and (now - self._prev_ts).total_seconds() <= _MAX_INTERPOLATION_GAP_S
            and energy_kwh >= self._prev_energy
        ):
            span = (now - self._prev_ts).total_seconds()
            frac = 1.0 if span <= 0 else (boundary - self._prev_ts).total_seconds() / span
            e_boundary = self._prev_energy + (energy_kwh - self._prev_energy) * frac

        complete = self._measure_start == old_start
        if self._quarter_invalid:
            _LOGGER.warning(
                "QuarterCalculator: kwartier %s niet afgesloten — de "
                "meterstand sprong terug tijdens dit kwartier",
                old_start.isoformat(),
            )
        elif not complete:
            _LOGGER.info(
                "QuarterCalculator: kwartier %s niet afgesloten — het begin "
                "ervan werd niet gemeten (herstart)",
                old_start.isoformat(),
            )
        elif e_boundary is None:
            _LOGGER.info(
                "QuarterCalculator: kwartier %s niet afgesloten — geen meting "
                "rond het einde (gat van %.0f s)",
                old_start.isoformat(),
                (now - self._prev_ts).total_seconds() if self._prev_ts else -1,
            )
        else:
            value = round((e_boundary - self._quarter_start_energy) * 4.0, 4)
            if 0.0 <= value <= MAX_PLAUSIBLE_QUARTER_KW:
                self._last_finished_value = value
                self._last_finished_ts = old_start
                self._quarter_just_finished = True
                _LOGGER.debug(
                    "Kwartier %s afgesloten: %.3f kW", old_start.isoformat(), value,
                )
            else:
                _LOGGER.warning(
                    "QuarterCalculator: kwartier %s niet afgesloten — "
                    "onmogelijke waarde %.1f kW",
                    old_start.isoformat(), value,
                )

        if e_boundary is not None:
            self._start_quarter(q_start, e_boundary, q_start)
        else:
            self._start_quarter(q_start, energy_kwh, now)

    def _running_value(self, energy_kwh: float, now: datetime) -> float:
        elapsed_minutes = (now - self._measure_start).total_seconds() / 60.0
        if elapsed_minutes < 0.05:
            # Minder dan 3 seconden gemeten: vermijd deling door ~0
            self._current_kw = 0.0
            return 0.0

        if self._quarter_invalid:
            # Rest van een onbetrouwbaar kwartier: niets meten tot het volgende.
            self._current_kw = 0.0
            return 0.0

        delta_kwh = energy_kwh - self._quarter_start_energy
        if delta_kwh < 0:
            # Meterstand sprong terug (sensor-reset, of een register dat kort
            # wegviel). Springt hij daarna weer omhoog, dan zou die sprong als
            # verbruik tellen en een reusachtige piek geven. Het kwartier is
            # dus onbetrouwbaar: meet niet verder en sluit het niet af.
            _LOGGER.warning(
                "QuarterCalculator: negatieve energiedelta (%.3f kWh) — "
                "lopend kwartier ongeldig verklaard",
                delta_kwh,
            )
            self._quarter_invalid = True
            self._current_kw = 0.0
            return 0.0

        # P = delta_kWh / (elapsed_min / 60) = delta_kWh * 60 / elapsed_min
        current_kw = round(delta_kwh * 60.0 / elapsed_minutes, 4)
        if current_kw > MAX_PLAUSIBLE_QUARTER_KW:
            # Onmogelijk hoog. In de eerste minuut kan dat nog een gewone
            # stap van een grove sensor zijn (0,1 kWh na 5 s = 72 kW): toon
            # dan voorlopig 0. Daarna is het een meetfout — typisch een te
            # lage eerste meting van het kwartier gevolgd door een sprong
            # terug omhoog — en is het hele kwartier onbetrouwbaar.
            if elapsed_minutes >= _SETTLE_MINUTES:
                _LOGGER.warning(
                    "QuarterCalculator: onmogelijk kwartiervermogen (%.0f kW) — "
                    "lopend kwartier ongeldig verklaard",
                    current_kw,
                )
                self._quarter_invalid = True
            self._current_kw = 0.0
            return 0.0
        self._current_kw = current_kw
        return self._current_kw
