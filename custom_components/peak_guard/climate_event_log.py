"""Peak Guard — climate_event_log.py

Blijvend logboek van elke ingreep van de piek-cascade op een thermostaat
(airco-bijstelling of uitschakelen), voor een analyse over een hele winter:
<config_dir>/peak_guard/climate_events.jsonl — buiten custom_components, zodat
een update het niet wist.

Twee regels per ingreep, te koppelen op (entity, start):
  {"event": "start", start, device, entity, mode, original_mode,
   original_temp, set_temp, room_start, power_w}
  {"event": "end",   start, end, duration_min, device, entity, mode,
   original_mode, original_temp, set_temp, room_start, room_end,
   end_reason, power_w}
Tijden in UTC (ISO). end_reason: restored | user_changed | unit_off |
already_restored. Een start zonder end: ingreep afgebroken (handmatige
override, opgegeven na geen bevestiging) of nog bezig.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

FILENAME = "climate_events.jsonl"


class ClimateEventLog:
    def __init__(self, hass) -> None:
        self._hass = hass
        self._path: Path = Path(hass.config.config_dir) / "peak_guard" / FILENAME

    def _write_sync(self, entry: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def write(self, entry: dict) -> None:
        """Vanuit de event loop: schrijven in de executor, niet wachten."""
        try:
            self._hass.async_add_executor_job(self._write_sync, entry)
        except Exception as exc:   # nooit de cascade laten falen op het logboek
            _LOGGER.debug("ClimateEventLog: schrijffout: %s", exc)
