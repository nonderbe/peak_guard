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
in de geschiedenis (max. 30 dagen × 96 kwartieren = 2880 entries).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

_LOGGER = logging.getLogger(__name__)


def _quarter_start(dt: datetime) -> datetime:
    """Geeft het begin van het 15-minuten kwartierblok van dt (UTC)."""
    minute_block = (dt.minute // 15) * 15
    return dt.replace(minute=minute_block, second=0, microsecond=0)


class QuarterCalculator:
    """
    Houdt de energiedelta bij binnen het lopende kwartierblok en
    berekent het huidige gemiddeld vermogen (kW).

    Gebruik:
        calc = QuarterCalculator()
        current_kw = calc.update(energy_kwh, now)
        if calc.quarter_just_finished:
            store(calc.last_finished_quarter)
    """

    def __init__(self) -> None:
        self._quarter_start_energy: Optional[float] = None
        self._current_quarter_start: Optional[datetime] = None
        self._current_kw: float = 0.0
        self._last_finished_value: Optional[float] = None
        self._last_finished_ts: Optional[datetime] = None
        self._quarter_just_finished: bool = False

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

        self._quarter_just_finished = False
        q_start = _quarter_start(now)

        # Eerste meting ooit
        if self._current_quarter_start is None:
            self._current_quarter_start = q_start
            self._quarter_start_energy = energy_kwh
            self._current_kw = 0.0
            return 0.0

        # Nieuw kwartier gestart?
        if q_start > self._current_quarter_start:
            # Sla de definitieve waarde op van het afgelopen kwartier
            self._last_finished_value = self._current_kw
            self._last_finished_ts = self._current_quarter_start
            self._quarter_just_finished = True
            _LOGGER.debug(
                "Kwartier %s afgesloten: %.3f kW",
                self._current_quarter_start.isoformat(),
                self._current_kw,
            )
            # Reset voor het nieuwe kwartier
            self._current_quarter_start = q_start
            self._quarter_start_energy = energy_kwh
            self._current_kw = 0.0
            return 0.0

        # Binnen hetzelfde kwartier: bereken lopend gemiddelde
        elapsed_minutes = (now - self._current_quarter_start).total_seconds() / 60.0
        if elapsed_minutes < 0.05:
            # Minder dan 3 seconden in het kwartier: vermijd deling door ~0
            self._current_kw = 0.0
            return 0.0

        delta_kwh = energy_kwh - self._quarter_start_energy
        if delta_kwh < 0:
            # Sensor reset of overflow: reset referentie
            _LOGGER.warning(
                "QuarterCalculator: negatieve energiedelta (%.3f kWh) — referentie gereset",
                delta_kwh,
            )
            self._quarter_start_energy = energy_kwh
            self._current_kw = 0.0
            return 0.0

        # P = delta_kWh / (elapsed_min / 60) = delta_kWh * 60 / elapsed_min
        self._current_kw = round(delta_kwh * 60.0 / elapsed_minutes, 4)
        return self._current_kw

    def restore(
        self,
        quarter_start_energy: float,
        quarter_start_dt: datetime,
        current_kw: float,
    ) -> None:
        """
        Herstel toestand na HA-herstart (vanuit opgeslagen state).
        Wordt aangeroepen door de sensor vóór de eerste update().
        """
        self._quarter_start_energy = quarter_start_energy
        self._current_quarter_start = quarter_start_dt
        self._current_kw = current_kw