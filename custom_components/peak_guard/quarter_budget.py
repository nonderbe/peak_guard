"""
quarter_budget.py
-----------------
Piekbeperking op het kwartierbudget in plaats van op het momentane vermogen.

Fluvius rekent het gemiddelde vermogen per kwartier aan. Een korte piek
(een waterkoker, een inductieplaat die bijschakelt) overschrijdt de grens
niet zolang het kwartiergemiddelde eronder blijft. Het vermogen dat de rest
van het kwartier mag lopen:

    toegelaten = (grens × 15 min − energie tot nu) / resterende tijd

Afschakelen gebeurt op het gladgestreken vermogen (het laagste van momentaan
en het gemiddelde van de laatste minuut: een korte uitschieter telt niet, en
een last die net wegviel ook niet). Herstellen op het hoogste van beide en
nooit boven de grens zelf, want een hersteld apparaat loopt het volgende
kwartier in.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .const import (
    PEAK_ALLOWANCE_CAP_FACTOR,
    PEAK_AVG_MAX_WINDOW_S,
    PEAK_AVG_MIN_WINDOW_S,
    QUARTER_SECONDS,
)

# Onder deze resterende tijd extrapoleren we niet verder: anders schiet het
# toegelaten vermogen in de laatste seconden alle kanten op.
_MIN_REMAINING_S = 60.0


def quarter_allowance_w(limit_w: float, energy_kwh: float, elapsed_s: float) -> float:
    """
    Vermogen (W) dat de rest van het kwartier mag lopen zonder dat het
    kwartiergemiddelde limit_w overschrijdt. Negatief als het budget al op is.
    Begrensd op PEAK_ALLOWANCE_CAP_FACTOR × limit_w.
    """
    remaining_s = max(_MIN_REMAINING_S, QUARTER_SECONDS - elapsed_s)
    budget_kwh = limit_w * QUARTER_SECONDS / 3600.0 / 1000.0
    allowed = (budget_kwh - energy_kwh) * 3600.0 * 1000.0 / remaining_s
    return min(allowed, limit_w * PEAK_ALLOWANCE_CAP_FACTOR)


@dataclass
class PeakBudget:
    """Wat de piek-beslisser deze cyclus ziet."""
    limit_w: float              # piekgrens − buffer
    allowed_w: float            # toegelaten vermogen rest van het kwartier
    shed_power_w: float         # vermogen waarop afgeschakeld wordt
    restore_power_w: float      # vermogen waarop hersteld wordt
    avg_w: Optional[float]      # gemiddelde laatste minuut (None: onbekend)
    on_budget: bool             # False: terugval op de momentane regel

    @property
    def excess_w(self) -> float:
        return self.shed_power_w - self.allowed_w

    @property
    def restore_headroom_w(self) -> float:
        return min(self.allowed_w, self.limit_w) - self.restore_power_w


class PowerAverager:
    """Gemiddeld vermogen uit opeenvolgende standen van de energiesensor."""

    def __init__(self) -> None:
        self._samples: deque = deque(maxlen=64)

    def add(self, ts: datetime, energy_kwh: Optional[float]) -> Optional[float]:
        """Voeg een stand toe en geef het gemiddelde vermogen (W) terug, of None."""
        if energy_kwh is None:
            return None
        while self._samples and (ts - self._samples[0][0]).total_seconds() > PEAK_AVG_MAX_WINDOW_S:
            self._samples.popleft()
        if self._samples and energy_kwh < self._samples[-1][1]:
            self._samples.clear()      # meter sprong terug: opnieuw beginnen
        avg = None
        # De jongste stand die minstens PEAK_AVG_MIN_WINDOW_S oud is.
        for s_ts, s_e in reversed(self._samples):
            age = (ts - s_ts).total_seconds()
            if age >= PEAK_AVG_MIN_WINDOW_S:
                avg = (energy_kwh - s_e) * 3600.0 * 1000.0 / age
                break
        self._samples.append((ts, energy_kwh))
        return avg


def make_budget(
    limit_w: float,
    consumption_w: float,
    now: datetime,
    avg_w: Optional[float],
    quarter_start: Optional[datetime],
    energy_so_far_kwh: Optional[float],
) -> PeakBudget:
    """
    Zonder bruikbaar kwartierbegin (herstart, onvolledig kwartier, simulatie)
    valt dit terug op de momentane regel: toegelaten = grens, vermogen = momentaan.
    """
    elapsed_s = (now - quarter_start).total_seconds() if quarter_start else None
    if (
        quarter_start is None
        or energy_so_far_kwh is None
        or elapsed_s is None
        or not 0 <= elapsed_s < QUARTER_SECONDS
    ):
        return PeakBudget(limit_w, limit_w, consumption_w, consumption_w, avg_w, False)
    allowed = quarter_allowance_w(limit_w, energy_so_far_kwh, elapsed_s)
    if avg_w is None:
        shed, restore = consumption_w, consumption_w
    else:
        shed, restore = min(consumption_w, avg_w), max(consumption_w, avg_w)
    return PeakBudget(limit_w, allowed, shed, restore, avg_w, True)
