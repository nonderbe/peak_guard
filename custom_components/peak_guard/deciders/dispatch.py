"""
Peak Guard — deciders/dispatch.py

Volgorde van de deciders binnen één loop-iteratie. Apart van controller.py
zodat de wisselwerking piek > schema > injectie testbaar is.
"""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .injection_decider import InjectionDecider
    from .peak_decider import PeakDecider
    from .schedule_decider import ScheduleDecider


async def run_tick(
    consumption: Optional[float],
    now: datetime,
    peak: "PeakDecider",
    schedule: "ScheduleDecider",
    injection: "InjectionDecider",
) -> None:
    """Voer één iteratie uit.

    - Piekbeperking schakelt eerst af (absolute prioriteit).
    - Het laadschema beslist vóór de inject-cascade, zodat die apparaten die
      het schema beheert, overslaat.
    - consumption=None (verbruiksensor onbeschikbaar): enkel het schema loopt,
      om vensters af te sluiten en limieten te herstellen.
    """
    if consumption is None:
        await schedule.check(None, now)
        return
    if consumption > 0:
        await peak.check(consumption, now)
        await peak.check_restore(consumption, now)
        await schedule.check(consumption, now)
        await injection.check_restore(consumption, now)
    elif consumption < 0:
        await schedule.check(consumption, now)
        await injection.check(consumption, now)
        await peak.check_restore(consumption, now)
        await injection.check_restore(consumption, now)
    else:
        await schedule.check(0.0, now)
        await peak.check_restore(0.0, now)
        await injection.check_restore(0.0, now)
