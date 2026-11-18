"""Peak Guard — event_archive.py

Archive of all peak and solar events, 12 full months plus the running month,
in <config_dir>/peak_guard/events/ (outside custom_components, so an update
does not remove it):

  YYYY-MM.jsonl  running month: one line per completed event, appended as the
                 event completes. Every event is kept, unlike the trackers'
                 in-memory log of 100 per month.
  YYYY-MM.json   closed month: {"year", "month", "final", "late", "totals",
                 "maandpiek", "peak": [...], "solar": [...]}

Solar events are final when they complete. A peak event's hypothetical
quarter, avoided kW and euro keep changing until the month ends (a later
higher peak, a quarter removed by the P1 check), so its line carries its
avoid_quarters and the values are recalculated from the tracker: live for the
running month, and once more at the month change, just before the tracker is
reset. A month whose change was missed while HA was down is closed with the
values as last written (late=true).

Records use the field names of the event sensor attributes, so the panel
reads both the same way.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

_LOGGER = logging.getLogger(__name__)

ARCHIVE_SUBDIR = ("peak_guard", "events")
# Running month + 12 closed months: at least 365 days of history.
EVENT_ARCHIVE_MONTHS = 13

_NAME_RE = re.compile(r"^(\d{4})-(\d{2})\.(jsonl|json)$")


def month_name(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def _month_index(year: int, month: int) -> int:
    return year * 12 + (month - 1)


def peak_record(ev) -> dict:
    """Archive record of a PeakEvent."""
    return {
        "modus": "peak",
        "key": f"p|{ev.device_id}|{ev.avoid_ts.isoformat()}",
        "device_id": ev.device_id,
        "apparaat": ev.device_name,
        "timestamp_start_uitstel": ev.avoid_ts.isoformat(),
        "timestamp_herstart": ev.turnon_ts.isoformat(),
        "gemeten_duur_min": ev.measured_duration_min,
        "nominal_kw": ev.nominal_kw,
        "added_energy_kwh": ev.added_energy_kwh,
        "vermeden_piek_kw": ev.avoided_peak_kw,
        "hypothetische_piek_kw": ev.hypothetical_peak_kw,
        "besparing_eur": ev.savings_euro,
        "avoid_quarters": [q.isoformat() for q in ev.avoid_quarters],
    }


def solar_record(ev) -> dict:
    """Archive record of a SolarEvent."""
    return {
        "modus": "solar",
        "key": f"s|{ev.device_id}|{ev.turnon_ts.isoformat()}",
        "device_id": ev.device_id,
        "apparaat": ev.device_name,
        "timestamp_start_inschakeling": ev.turnon_ts.isoformat(),
        "timestamp_einde": ev.restore_ts.isoformat(),
        "gemeten_duur_min": ev.measured_duration_min,
        "nominal_kw": ev.nominal_kw,
        "verschoven_kwh": ev.shifted_kwh,
        "besparing_eur": ev.savings_euro,
    }


def refresh_peak_values(records: Iterable[dict], peak_tracker) -> None:
    """Recalculate the per-event values of peak records with the tracker's
    current month state. Records without avoid_quarters (pre-v1.9.2 events)
    keep their stored values."""
    for rec in records:
        if rec.get("modus") != "peak" or not rec.get("avoid_quarters"):
            continue
        try:
            quarters = [datetime.fromisoformat(q) for q in rec["avoid_quarters"]]
        except (TypeError, ValueError):
            continue
        hypo, avoided, eur = peak_tracker.evaluate_quarters(quarters)
        rec["hypothetische_piek_kw"] = hypo
        rec["vermeden_piek_kw"] = avoided
        rec["besparing_eur"] = eur


def split_records(records: Iterable[dict]) -> Tuple[List[dict], List[dict]]:
    """Peak and solar records, each newest first."""
    peak = [r for r in records if r.get("modus") == "peak"]
    solar = [r for r in records if r.get("modus") == "solar"]
    peak.sort(key=lambda r: r.get("timestamp_start_uitstel", ""), reverse=True)
    solar.sort(key=lambda r: r.get("timestamp_start_inschakeling", ""), reverse=True)
    return peak, solar


class EventArchive:
    """Month files of events. The running month is also kept in memory."""

    def __init__(self, hass, base_dir: Optional[Path] = None) -> None:
        self._hass = hass
        self._dir: Path = base_dir if base_dir is not None else Path(
            hass.config.config_dir, *ARCHIVE_SUBDIR
        )
        self._lock = threading.Lock()
        self._ym: Optional[Tuple[int, int]] = None
        self._records: List[dict] = []
        self._keys: set = set()

    # ── sync file helpers (executor) ──────────────────────────────── #

    def _path(self, year: int, month: int, ext: str) -> Path:
        return self._dir / f"{month_name(year, month)}.{ext}"

    @staticmethod
    def _read_jsonl(path: Path) -> List[dict]:
        out: List[dict] = []
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue   # a line cut off by a crash
                    if isinstance(rec, dict) and rec.get("key"):
                        out.append(rec)
        except FileNotFoundError:
            pass
        return out

    def _append_sync(self, year: int, month: int, records: List[dict]) -> None:
        with self._lock:
            self._dir.mkdir(parents=True, exist_ok=True)
            with open(self._path(year, month, "jsonl"), "a", encoding="utf-8") as fh:
                for rec in records:
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def _write_month_sync(self, year: int, month: int, records: List[dict],
                          totals: Optional[dict], maandpiek: Optional[dict],
                          late: bool) -> None:
        """Write the closed month file and remove its running-month file."""
        with self._lock:
            self._dir.mkdir(parents=True, exist_ok=True)
            peak, solar = split_records(records)
            data = {
                "year": year, "month": month, "final": True, "late": late,
                "totals": totals, "maandpiek": maandpiek,
                "peak": peak, "solar": solar,
            }
            final = self._path(year, month, "json")
            tmp = final.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False)
            tmp.replace(final)
            self._path(year, month, "jsonl").unlink(missing_ok=True)

    def _list_files(self) -> List[Tuple[int, int, str, Path]]:
        out = []
        if not self._dir.is_dir():
            return out
        for p in self._dir.iterdir():
            m = _NAME_RE.match(p.name)
            if m:
                out.append((int(m.group(1)), int(m.group(2)), m.group(3), p))
        return out

    def _load_sync(self, year: int, month: int) -> List[dict]:
        """Close running-month files of earlier months (missed month change),
        prune old months, return the current month's records."""
        self._dir.mkdir(parents=True, exist_ok=True)
        cur = _month_index(year, month)
        for y, m, ext, path in self._list_files():
            idx = _month_index(y, m)
            if ext == "jsonl" and idx < cur:
                records = self._read_jsonl(path)
                existing = self._path(y, m, "json")
                if existing.exists():
                    # Should not happen; keep the closed file, add what's missing.
                    try:
                        old = json.loads(existing.read_text(encoding="utf-8"))
                        keys = {r.get("key") for r in old.get("peak", []) + old.get("solar", [])}
                        records = (old.get("peak", []) + old.get("solar", [])
                                   + [r for r in records if r.get("key") not in keys])
                    except (OSError, ValueError):
                        pass
                self._write_month_sync(y, m, records, None, None, late=True)
                _LOGGER.info(
                    "Peak Guard: event archive %s closed late (%d events)",
                    month_name(y, m), len(records),
                )
        self._prune_sync(year, month)
        return self._read_jsonl(self._path(year, month, "jsonl"))

    def _prune_sync(self, year: int, month: int) -> None:
        cur = _month_index(year, month)
        for y, m, _ext, path in self._list_files():
            # Future-dated files (wrong clock) are left alone.
            if cur - _month_index(y, m) >= EVENT_ARCHIVE_MONTHS:
                try:
                    path.unlink()
                    _LOGGER.info("Peak Guard: event archive %s removed (older than 12 months)", path.name)
                except OSError as exc:
                    _LOGGER.debug("Peak Guard: cannot remove %s: %s", path, exc)

    def _read_month_sync(self, year: int, month: int) -> Optional[dict]:
        final = self._path(year, month, "json")
        if final.exists():
            try:
                return json.loads(final.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                _LOGGER.warning("Peak Guard: event archive %s unreadable: %s", final.name, exc)
                return None
        running = self._path(year, month, "jsonl")
        if running.exists():
            peak, solar = split_records(self._read_jsonl(running))
            return {"year": year, "month": month, "final": False, "late": False,
                    "totals": None, "maandpiek": None, "peak": peak, "solar": solar}
        return None

    def _months_sync(self) -> List[str]:
        names = {month_name(y, m) for y, m, _e, _p in self._list_files()}
        return sorted(names, reverse=True)

    # ── async API (event loop) ────────────────────────────────────── #

    async def async_load(self, year: int, month: int,
                         peak_events: Iterable = (), solar_events: Iterable = ()) -> None:
        """Load the running month; add tracker events that are not archived
        yet (the month before this version, or events completed before the
        archive was loaded)."""
        records = await self._hass.async_add_executor_job(self._load_sync, year, month)
        self._ym = (year, month)
        self._records = records
        self._keys = {r["key"] for r in records}
        missing = [peak_record(e) for e in peak_events]
        missing += [solar_record(e) for e in solar_events]
        missing = [r for r in missing if r["key"] not in self._keys]
        if missing:
            self._add(missing)
            _LOGGER.info(
                "Peak Guard: %d event(s) of %s added to the event archive",
                len(missing), month_name(year, month),
            )

    def _add(self, records: List[dict]) -> None:
        if self._ym is None:
            return
        for r in records:
            self._keys.add(r["key"])
            self._records.append(r)
        try:
            self._hass.async_add_executor_job(self._append_sync, *self._ym, records)
        except Exception as exc:   # the archive never breaks the cascade
            _LOGGER.debug("Peak Guard: event archive write error: %s", exc)

    def record(self, event) -> None:
        """Tracker hook: an event completed. Before async_load the event is
        still in the tracker and is added by the load."""
        if self._ym is None:
            return
        try:
            rec = solar_record(event) if hasattr(event, "shifted_kwh") else peak_record(event)
        except Exception as exc:
            _LOGGER.debug("Peak Guard: event archive record error: %s", exc)
            return
        if rec["key"] in self._keys:
            return
        self._add([rec])

    async def async_close_month(self, peak_tracker, new_year: int, new_month: int,
                                totals: Optional[dict] = None,
                                maandpiek: Optional[dict] = None) -> None:
        """Month change: freeze the running month with the final per-event
        values. Call before the trackers are reset."""
        if self._ym is None:
            return
        year, month = self._ym
        records = self._records
        refresh_peak_values(records, peak_tracker)
        # Events from here on belong to the new month.
        self._ym = (new_year, new_month)
        self._records = []
        self._keys = set()
        try:
            await self._hass.async_add_executor_job(
                self._write_month_sync, year, month, records, totals, maandpiek, False
            )
            await self._hass.async_add_executor_job(self._prune_sync, new_year, new_month)
            _LOGGER.info(
                "Peak Guard: event archive %s closed (%d events)",
                month_name(year, month), len(records),
            )
        except Exception as exc:
            _LOGGER.warning("Peak Guard: event archive %s not closed: %s",
                            month_name(year, month), exc)

    async def async_month(self, year: int, month: int, peak_tracker=None,
                          totals: Optional[dict] = None,
                          maandpiek: Optional[dict] = None) -> Optional[dict]:
        """Events of one month, newest first. The running month comes from
        memory with live peak values."""
        if self._ym == (year, month):
            records = [dict(r) for r in self._records]
            if peak_tracker is not None:
                refresh_peak_values(records, peak_tracker)
            peak, solar = split_records(records)
            return {"year": year, "month": month, "final": False, "late": False,
                    "totals": totals, "maandpiek": maandpiek,
                    "peak": peak, "solar": solar}
        return await self._hass.async_add_executor_job(self._read_month_sync, year, month)

    async def async_months(self) -> List[str]:
        names = set(await self._hass.async_add_executor_job(self._months_sync))
        if self._ym is not None:
            names.add(month_name(*self._ym))
        return sorted(names, reverse=True)

    @property
    def current_month(self) -> Optional[Tuple[int, int]]:
        return self._ym
