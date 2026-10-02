"""
quarter_store.py
----------------
Beheert de opslag en bevraging van historische kwartierpiek-waarden.

Slaat QUARTER_HISTORY_DAYS × 96 kwartieren op in HA's persistente opslag
(homeassistant.helpers.storage), plus één maandpiek-record per kalendermaand
voor de laatste MONTHLY_PEAK_HISTORY_MONTHS maanden.

Kwartier-entry:  {"ts": "2026-03-01T00:00:00+00:00", "kw": 3.141}
Maandpiek-record: {"year": 2026, "month": 3, "ts": "...", "kw": 4.2}

Tijdstempels worden in UTC opgeslagen. De maand waartoe een kwartier behoort
wordt in lokale tijd bepaald (utils.local_year_month): het capaciteitstarief
loopt per kalendermaand in Belgische tijd.
"""

from __future__ import annotations

import logging
from collections import deque
from functools import lru_cache
import math
from datetime import datetime, timedelta
from typing import Optional

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CAPACITY_MIN_KW,
    MAX_PLAUSIBLE_QUARTER_KW,
    MONTHLY_PEAK_HISTORY_MONTHS,
    PEAK_VERIFY_GRACE_MINUTES,
    PEAK_VERIFY_MARGIN_KW,
    PEAK_VERIFY_TOLERANCE,
    QUARTER_SECONDS,
    STORAGE_KEY_QUARTERS,
    STORAGE_VERSION_QUARTERS,
    QUARTER_HISTORY_DAYS,
)
from .utils import local_year_month

_LOGGER = logging.getLogger(__name__)

# Maximale entries = QUARTER_HISTORY_DAYS × 96 kwartieren per dag
_MAX_ENTRIES = QUARTER_HISTORY_DAYS * 96


@lru_cache(maxsize=2 * _MAX_ENTRIES)
def _local_month_of(ts: str, time_zone: str) -> tuple[int, int]:
    """(year, month) in lokale tijd voor een ISO-tijdstempel.

    Gecachet: elke entry wordt per sensorupdate tientallen keren aan een
    maand getoetst, en een tijdstempel verandert nooit van maand. De tijdzone
    zit in de sleutel zodat een gewijzigde HA-tijdzone meteen doorwerkt.
    """
    return local_year_month(datetime.fromisoformat(ts))


# Is meer dan dit aandeel van de opgeslagen kwartieren onmogelijk hoog, dan
# wordt de hele reeks als "verkeerde eenheid" beschouwd en niet overgenomen.
_WRONG_UNIT_SHARE = 0.10


def _is_plausible_kw(kw) -> bool:
    """
    True als kw een geloofwaardig kwartiergemiddelde is.

    Een maandpiek-record kan alleen stijgen en blijft 36 maanden staan, dus
    een meetfout mag er nooit in belanden.
    """
    return (
        isinstance(kw, (int, float))
        and not isinstance(kw, bool)
        and math.isfinite(kw)
        and 0.0 <= kw <= MAX_PLAUSIBLE_QUARTER_KW
    )


class QuarterStore:
    """
    Persistente opslag voor 15-minuten kwartierpiek-waarden en maandpieken.

    Twee lagen:
      - kwartieren: QUARTER_HISTORY_DAYS dagen, nodig voor de lopende maand;
      - maandpieken: één record per kalendermaand (lokale tijd), bewaard voor
        MONTHLY_PEAK_HISTORY_MONTHS maanden. Zo hangen de historiek en het
        12-maandsgemiddelde niet af van kwartieren die al gewist zijn.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        self._store = Store(hass, STORAGE_VERSION_QUARTERS, STORAGE_KEY_QUARTERS)
        # deque met max. _MAX_ENTRIES items: (timestamp_str, kw_float)
        self._entries: deque[dict] = deque(maxlen=_MAX_ENTRIES)
        # (year, month) in lokale tijd → {"ts": str, "kw": float}
        self._monthly_peaks: dict[tuple[int, int], dict] = {}

    # ---------------------------------------------------------------- #
    #  Laden en opslaan                                                 #
    # ---------------------------------------------------------------- #

    async def async_load(self) -> None:
        """Laad opgeslagen kwartierpiek-waarden en maandpieken vanuit HA-opslag."""
        data = await self._store.async_load()
        if not data:
            return
        if isinstance(data.get("monthly_peaks"), list):
            for record in data["monthly_peaks"]:
                parsed = self._parse_month_record(record)
                if parsed is not None:
                    key, value = parsed
                    self._monthly_peaks[key] = value
        quarters: list[dict] = []
        skipped = 0
        if isinstance(data.get("quarters"), list):
            cutoff = dt_util.utcnow() - timedelta(days=QUARTER_HISTORY_DAYS)
            for entry in data["quarters"]:
                try:
                    ts = datetime.fromisoformat(entry["ts"])
                    kw = float(entry["kw"])
                except (KeyError, ValueError, TypeError):
                    continue
                if ts < cutoff:
                    continue
                if not _is_plausible_kw(kw):
                    skipped += 1
                    continue
                quarters.append({"ts": entry["ts"], "kw": kw})
        if skipped > _WRONG_UNIT_SHARE * (skipped + len(quarters)):
            # Geen losse meetfout maar een verkeerde eenheid (energiesensor in
            # Wh onder een oudere versie): dan zijn ook de waarden die
            # toevallig onder de bovengrens blijven een factor 1000 fout.
            _LOGGER.warning(
                "QuarterStore: %d van %d opgeslagen kwartieren zijn onmogelijk "
                "hoog — de volledige kwartierhistoriek wordt genegeerd "
                "(verkeerde eenheid van de energiesensor)",
                skipped, skipped + len(quarters),
            )
            quarters = []
        elif skipped:
            _LOGGER.warning(
                "QuarterStore: %d opgeslagen kwartier(en) boven %.0f kW of met "
                "een ongeldige waarde genegeerd (meetfout)",
                skipped, MAX_PLAUSIBLE_QUARTER_KW,
            )
        for entry in quarters:
            self._entries.append(entry)
            # Ook bij een opslag van vóór de maandpiek-records: leid de
            # maandpieken af uit de nog aanwezige kwartieren.
            self._record_month_peak(entry["ts"], entry["kw"])
        self._prune_month_peaks()
        _LOGGER.debug(
            "QuarterStore: %d kwartieren en %d maandpieken geladen",
            len(self._entries), len(self._monthly_peaks),
        )

    @staticmethod
    def _parse_month_record(record) -> Optional[tuple[tuple[int, int], dict]]:
        """Valideer één opgeslagen maandpiek-record; None als het ongeldig is."""
        if not isinstance(record, dict):
            return None
        year, month, ts, kw = (
            record.get("year"), record.get("month"), record.get("ts"), record.get("kw"),
        )
        if isinstance(year, bool) or not isinstance(year, int):
            return None
        if isinstance(month, bool) or not isinstance(month, int) or not 1 <= month <= 12:
            return None
        if ts is not None and not isinstance(ts, str):
            return None
        if not _is_plausible_kw(kw):
            return None
        return (year, month), {"ts": ts, "kw": float(kw)}

    async def async_save(self) -> None:
        """Bewaar kwartierpiek-waarden en maandpieken naar HA-opslag."""
        await self._store.async_save({
            "quarters": list(self._entries),
            "monthly_peaks": [
                {"year": year, "month": month, "ts": rec["ts"], "kw": rec["kw"]}
                for (year, month), rec in sorted(self._monthly_peaks.items())
            ],
        })

    # ---------------------------------------------------------------- #
    #  Schrijven                                                        #
    # ---------------------------------------------------------------- #

    async def add_quarter(self, ts: datetime, kw: float) -> None:
        """
        Voeg een afgesloten kwartierpiek toe en sla op.

        Parameters
        ----------
        ts  : datetime  Start-tijdstip van het kwartier (UTC).
        kw  : float     Gemiddeld vermogen gedurende het kwartier (kW).
        """
        if not _is_plausible_kw(kw):
            _LOGGER.warning(
                "QuarterStore: kwartier %s met %s kW geweigerd — geen "
                "geloofwaardige meting (maximum %.0f kW)",
                ts.isoformat(), kw, MAX_PLAUSIBLE_QUARTER_KW,
            )
            return
        entry = {"ts": ts.isoformat(), "kw": round(kw, 4)}
        self._entries.append(entry)
        self._record_month_peak(entry["ts"], entry["kw"])
        self._prune_month_peaks()
        await self.async_save()

    async def async_remove_unconfirmed_peaks(
        self, meter_peak_kw: float, now: datetime,
    ) -> list[dict]:
        """
        Verwijder kwartieren van de lopende maand die de P1-meter tegenspreekt.

        meter_peak_kw is de maandpiek die de meter zelf rapporteert. Geen
        enkel afgesloten kwartier van deze maand kan daar boven liggen, dus
        een eigen waarde boven
            meter_peak_kw × PEAK_VERIFY_TOLERANCE + PEAK_VERIFY_MARGIN_KW
        is een meetfout. Zulke kwartieren worden gewist en het maandpiek-
        record wordt opnieuw opgebouwd uit de kwartieren die overblijven.

        Een kwartier wordt pas beoordeeld PEAK_VERIFY_GRACE_MINUTES na zijn
        einde. Vorige maanden blijven ongemoeid: de meter kent alleen de
        lopende maand. Geeft de verwijderde entries terug ({"ts", "kw"}).
        """
        limit = meter_peak_kw * PEAK_VERIFY_TOLERANCE + PEAK_VERIFY_MARGIN_KW
        month = local_year_month(now)
        settled_before = now - timedelta(
            seconds=QUARTER_SECONDS, minutes=PEAK_VERIFY_GRACE_MINUTES,
        )

        def is_wrong(entry: dict) -> bool:
            if entry["kw"] <= limit:
                return False
            try:
                return datetime.fromisoformat(entry["ts"]) <= settled_before
            except (ValueError, TypeError):
                # Record zonder bruikbaar tijdstip: niet te dateren, dus ook
                # niet te vertrouwen als het de meter tegenspreekt.
                return True

        removed = [
            e for e in self._entries
            if self._entry_month(e) == month and is_wrong(e)
        ]
        record = self._monthly_peaks.get(month)
        record_wrong = record is not None and is_wrong(record)
        if not removed and not record_wrong:
            return []

        if record_wrong and not any(e["ts"] == record["ts"] for e in removed):
            # Het record verwijst naar een kwartier dat niet (meer) in de
            # historiek zit.
            removed.append(dict(record))
        removed_ts = {e["ts"] for e in removed}
        self._entries = deque(
            (e for e in self._entries if e["ts"] not in removed_ts),
            maxlen=_MAX_ENTRIES,
        )
        # Record van de lopende maand opnieuw opbouwen uit wat overblijft.
        self._monthly_peaks.pop(month, None)
        for e in self._entries:
            if self._entry_month(e) == month:
                self._record_month_peak(e["ts"], e["kw"])
        await self.async_save()

        new_peak = self._monthly_peaks.get(month)
        _LOGGER.warning(
            "QuarterStore: %d kwartier(en) verwijderd uit maand %04d-%02d omdat "
            "ze boven de maandpiek van de P1-meter liggen (meter: %.2f kW, "
            "grens: %.2f kW): %s. Maandpiek is nu %s.",
            len(removed), month[0], month[1], meter_peak_kw, limit,
            ", ".join(f"{e['ts']} = {e['kw']:.2f} kW" for e in removed),
            f"{new_peak['kw']:.2f} kW" if new_peak else "onbekend",
        )
        return removed

    def _record_month_peak(self, ts: str, kw: float) -> None:
        """Verhoog het maandpiek-record van de maand van dit kwartier, indien hoger."""
        key = self._entry_month({"ts": ts})
        if key == (0, 0):
            return
        current = self._monthly_peaks.get(key)
        if current is None or kw > current["kw"]:
            self._monthly_peaks[key] = {"ts": ts, "kw": kw}

    def _prune_month_peaks(self) -> None:
        """
        Verwijder records ouder dan MONTHLY_PEAK_HISTORY_MONTHS maanden,
        geteld vanaf de huidige maand. Records met een datum in de toekomst
        (foute klok) blijven staan maar tellen niet mee als ijkpunt, zodat
        één zo'n record de echte historiek niet kan wegsnoeien.
        """
        year, month = local_year_month(dt_util.utcnow())
        oldest_kept = year * 12 + month - MONTHLY_PEAK_HISTORY_MONTHS + 1
        for year, month in list(self._monthly_peaks):
            if year * 12 + month < oldest_kept:
                del self._monthly_peaks[(year, month)]

    # ---------------------------------------------------------------- #
    #  Bevragen                                                         #
    # ---------------------------------------------------------------- #

    def _peaks_by_month(self) -> dict[tuple[int, int], dict]:
        """
        Hoogste kwartier per (year, month): de maandpiek-records, aangevuld
        met de kwartieren die nog in de historiek zitten.
        """
        best = dict(self._monthly_peaks)
        for e in self._entries:
            key = self._entry_month(e)
            current = best.get(key)
            if current is None or e["kw"] > current["kw"]:
                best[key] = e
        return best

    def get_month_peak(self, year: int, month: int) -> Optional[float]:
        """Hoogste kwartierpiek-waarde voor de gegeven maand (kW), of None."""
        peak = self._peaks_by_month().get((year, month))
        return peak["kw"] if peak else None

    def get_current_month_peak(self) -> Optional[float]:
        """Hoogste kwartierpiek-waarde voor de huidige maand (kW), of None."""
        return self.get_month_peak(*local_year_month(dt_util.utcnow()))

    def get_monthly_peaks(self, months: int = MONTHLY_PEAK_HISTORY_MONTHS) -> list[dict]:
        """
        Lijst van de maandpieken van de laatste `months` maanden, de lopende
        maand inbegrepen (oudste eerst). Maanden zonder data ontbreken.

        Elke entry: {"year": int, "month": int, "ts": str, "kw": float}
        """
        year, month = local_year_month(dt_util.utcnow())
        best = self._peaks_by_month()
        results = []
        for _ in range(months):
            peak = best.get((year, month))
            if peak is not None:
                results.append({
                    "year": year,
                    "month": month,
                    "ts": peak["ts"],
                    "kw": peak["kw"],
                })
            month -= 1
            if month == 0:
                year, month = year - 1, 12
        results.reverse()   # Oudste eerst
        return results

    def get_monthly_peaks_last_12(self) -> list[dict]:
        """De laatste 12 maandpieken (oudste eerst) — basis voor de facturatie."""
        return self.get_monthly_peaks(12)

    def get_rolling_12_month_avg(self) -> Optional[float]:
        """Gemiddelde van de laatste 12 maandpieken (kW), of None."""
        peaks = self.get_monthly_peaks_last_12()
        if not peaks:
            return None
        return round(sum(p["kw"] for p in peaks) / len(peaks), 4)

    def get_billed_avg_kw(self) -> float:
        """
        Aangerekende piek (kW): gemiddelde van de laatste 12 maandpieken,
        waarbij elke maandpiek eerst wordt opgetrokken naar CAPACITY_MIN_KW —
        Fluvius past het minimum per maand toe, niet op het gemiddelde.
        """
        peaks = self.get_monthly_peaks_last_12()
        if not peaks:
            return CAPACITY_MIN_KW
        return round(
            sum(max(p["kw"], CAPACITY_MIN_KW) for p in peaks) / len(peaks), 4
        )

    def get_all_entries(self) -> list[dict]:
        """Alle opgeslagen entries (voor debugging/diagnostics)."""
        return list(self._entries)

    # ---------------------------------------------------------------- #
    #  Hulpfuncties                                                     #
    # ---------------------------------------------------------------- #

    @staticmethod
    def _entry_month(entry: dict) -> tuple[int, int]:
        """Geeft (year, month) in lokale tijd voor een entry-dict."""
        try:
            return _local_month_of(entry["ts"], str(dt_util.DEFAULT_TIME_ZONE))
        except (KeyError, ValueError, TypeError):
            return (0, 0)
