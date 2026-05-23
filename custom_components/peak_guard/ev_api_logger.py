"""Peak Guard — ev_api_logger.py

Logt elke Tesla API service-call naar een dagelijks JSONL-bestand in
<config_dir>/custom_components/peak_guard/logs/ev_api_YYYY-MM-DD.jsonl

Elke regel is een JSON-object:
  {ts, device, service, entity, value, cascade, surplus_w,
   success, error, duration_ms}
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_LOGGER = logging.getLogger(__name__)


class EVApiLogger:
    """Schrijft Tesla API service-calls naar dagelijkse JSONL-bestanden."""

    def __init__(self, hass) -> None:
        self._hass = hass
        self._log_dir: Path = (
            Path(hass.config.config_dir)
            / "custom_components"
            / "peak_guard"
            / "logs"
        )

    # ------------------------------------------------------------------ #

    def _log_path(self, date_str: Optional[str] = None) -> Path:
        if date_str is None:
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return self._log_dir / f"ev_api_{date_str}.jsonl"

    def _write_sync(self, entry: dict) -> None:
        self._log_dir.mkdir(parents=True, exist_ok=True)
        with open(self._log_path(), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _read_sync(self, date_str: Optional[str]) -> list:
        path = self._log_path(date_str)
        if not path.exists():
            return []
        entries: list = []
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        return entries

    # ------------------------------------------------------------------ #

    async def log(
        self,
        *,
        device: str,
        service: str,
        entity: str,
        value,
        cascade: str,
        surplus_w: float,
        success: bool,
        error: Optional[str],
        duration_ms: float,
    ) -> None:
        entry = {
            "ts":          datetime.now(timezone.utc).isoformat(),
            "device":      device,
            "service":     service,
            "entity":      entity,
            "value":       value,
            "cascade":     cascade,
            "surplus_w":   round(surplus_w),
            "success":     success,
            "error":       error,
            "duration_ms": round(duration_ms),
        }
        try:
            await self._hass.async_add_executor_job(self._write_sync, entry)
        except Exception as exc:
            _LOGGER.debug("EVApiLogger: schrijffout: %s", exc)

    async def read_entries(self, date_str: Optional[str] = None) -> list:
        try:
            return await self._hass.async_add_executor_job(
                self._read_sync, date_str
            )
        except Exception as exc:
            _LOGGER.debug("EVApiLogger: leesfout: %s", exc)
            return []
