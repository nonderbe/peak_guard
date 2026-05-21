"""
Peak Guard — decision_logger.py

DecisionLogger schrijft een gedetailleerde, mensleesbare beslissingslog
naar /config/peak_guard_decisions.log. Wordt alleen gebruikt als
CONF_DEBUG_DECISION_LOGGING == True.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Dict, List, Optional

from homeassistant.core import HomeAssistant

from .const import (
    ACTION_EV_CHARGER,
    CONF_BUFFER_WATTS,
    CONF_PEAK_SENSOR,
    DEFAULT_BUFFER_WATTS,
    DEFAULT_UPDATE_INTERVAL,
    CONF_UPDATE_INTERVAL,
    DOMAIN,
)
from .deciders.base import read_sensor
from .models import (
    BaseCascadeDevice,
    EVChargerDevice,
    DeviceSnapshot,
    EV_DEBOUNCE_STABLE_S,
    EV_MIN_OFF_DURATION_S,
    EV_MIN_ON_DURATION_S,
    EV_RATE_LIMIT_MAX_CALLS,
    EV_RATE_LIMIT_WINDOW_S,
    EVState,
    EV_VOLTS_1PHASE,
    EV_VOLTS_3PHASE,
)

if TYPE_CHECKING:
    from .deciders.ev_guard import EVGuard

_LOGGER = logging.getLogger(__name__)


class DecisionLogger:
    """Schrijft een gedetailleerde beslissingslog naar een bestand per iteratie."""

    def __init__(
        self,
        hass: HomeAssistant,
        config: dict,
        peak_cascade: List[BaseCascadeDevice],
        inject_cascade: List[BaseCascadeDevice],
        peak_snapshots: Dict[str, DeviceSnapshot],
        inject_snapshots: Dict[str, DeviceSnapshot],
        ev_guard: "EVGuard",
        iteration_actions: list,
    ) -> None:
        self._hass = hass
        self._config = config
        self._peak_cascade = peak_cascade
        self._inject_cascade = inject_cascade
        self._peak_snapshots = peak_snapshots
        self._inject_snapshots = inject_snapshots
        self._ev_guard = ev_guard
        self._iteration_actions = iteration_actions
        self._last_logged_day: Optional[str] = None

    async def log(self, consumption: float, pre_states: dict) -> None:
        """Schrijf één beslissings-entry naar het logbestand.

        File-I/O loopt via asyncio.to_thread zodat de event loop vrij blijft.
        """
        now_local = datetime.now()
        now_str = now_local.strftime("%Y-%m-%d %H:%M:%S")
        today_str = now_local.strftime("%Y-%m-%d")

        peak = read_sensor(self._hass, self._config.get(CONF_PEAK_SENSOR))
        buffer_w = float(self._config.get(CONF_BUFFER_WATTS, DEFAULT_BUFFER_WATTS))
        target = (peak + buffer_w) if peak is not None else None

        lines: list = []

        # ── Dagelijkse header (één keer per dag) ─────────────────────── #
        if self._last_logged_day != today_str:
            self._last_logged_day = today_str
            lines.append("")
            lines.append("=" * 68)
            lines.append(
                f"  PeakGuard Decision Log  —  integratie: {DOMAIN}"
                f"  —  {today_str}"
            )
            lines.append("=" * 68)
            lines.append("")

        # ── Header block ─────────────────────────────────────────────── #
        lines.append(f"=== PeakGuard Decision [{now_str}] ===")
        lines.append(f"Huidig verbruik:   {consumption:+.0f} W")

        if peak is not None:
            lines.append(f"Maandpiek:         {peak:.0f} W")
            lines.append(f"Buffer:            {buffer_w:.0f} W")
            lines.append(f"Target peak:       {target:.0f} W")
        else:
            lines.append("Maandpiek:         [piek-sensor niet beschikbaar]")
            lines.append(f"Buffer:            {buffer_w:.0f} W")

        if consumption < 0:
            lines.append(
                f"Netto export:      {abs(consumption):.0f} W"
                " (injectie naar net)"
            )
        else:
            lines.append(
                f"Netto import:      {consumption:.0f} W"
                " (verbruik van net)"
            )

        # ── Peak limiting status ──────────────────────────────────────── #
        if consumption > 0 and peak is not None:
            excess_w = consumption - peak + buffer_w
            if excess_w > 0:
                lines.append(
                    f"\nPeak limiting actief?    JA  → verbruik {consumption:.0f} W"
                    f" overschrijdt target {target:.0f} W"
                    f" (overschot: {excess_w:.0f} W)"
                )
            else:
                lines.append(
                    f"\nPeak limiting actief?    Nee → verbruik {consumption:.0f} W"
                    f" ligt {abs(excess_w):.0f} W onder target {target:.0f} W"
                )
        elif consumption <= 0:
            lines.append("\nPeak limiting actief?    Nee → geen import van net")
        else:
            lines.append("\nPeak limiting actief?    Nee → piek-sensor niet beschikbaar")

        # ── Injection prevention status ───────────────────────────────── #
        if consumption < 0:
            injection = abs(consumption)
            if injection > buffer_w:
                lines.append(
                    f"Injection actief?        JA  → surplus {injection:.0f} W"
                    f" > buffer {buffer_w:.0f} W"
                    f" (verschil: {injection - buffer_w:.0f} W)"
                )
            else:
                lines.append(
                    f"Injection actief?        Nee → surplus {injection:.0f} W"
                    f" ≤ buffer {buffer_w:.0f} W"
                )
        else:
            lines.append(
                "Injection actief?        Nee → geen export gedetecteerd"
            )

        # ── Peak cascade ──────────────────────────────────────────────── #
        enabled_peak  = [d for d in self._peak_cascade if d.enabled]
        disabled_peak = [d for d in self._peak_cascade if not d.enabled]
        lines.append(
            f"\n=== Peak Cascade"
            f" ({len(enabled_peak)} actief, {len(disabled_peak)} uitgeschakeld) ==="
        )
        if not self._peak_cascade:
            lines.append("  (geen apparaten geconfigureerd)")
        else:
            for d in sorted(self._peak_cascade, key=lambda x: x.priority):
                self._append_device_lines(lines, d, pre_states, self._peak_snapshots, cascade="peak")

        # ── Injection cascade ─────────────────────────────────────────── #
        enabled_inj  = [d for d in self._inject_cascade if d.enabled]
        disabled_inj = [d for d in self._inject_cascade if not d.enabled]
        lines.append(
            f"\n=== Injection Cascade"
            f" ({len(enabled_inj)} actief, {len(disabled_inj)} uitgeschakeld) ==="
        )
        if not self._inject_cascade:
            lines.append("  (geen apparaten geconfigureerd)")
        else:
            for d in sorted(self._inject_cascade, key=lambda x: x.priority):
                self._append_device_lines(lines, d, pre_states, self._inject_snapshots, cascade="inject")

        # ── Acties deze iteratie ──────────────────────────────────────── #
        if self._iteration_actions:
            lines.append(f"\nActies deze iteratie ({len(self._iteration_actions)}):")
            for act in self._iteration_actions:
                val_str = (
                    f" {act['value']}"
                    if act.get("value") is not None else ""
                )
                lines.append(f"  - {act['entity_id']} → {act['action']}{val_str}")
        else:
            lines.append("\nActies deze iteratie: geen")

        # ── Verwachte solar shift ──────────────────────────────────────── #
        managed_solar = [
            d for d in self._inject_cascade
            if d.entity_id in self._inject_snapshots
        ]
        if managed_solar:
            total_managed_w = 0.0
            for d in managed_solar:
                if d.action_type == ACTION_EV_CHARGER:
                    g = self._ev_guard.guards.get(d.id)
                    if g and g.last_sent_amps is not None:
                        phases = int(d.phases) if isinstance(d, EVChargerDevice) and d.phases else 1
                        v = EV_VOLTS_3PHASE if phases == 3 else EV_VOLTS_1PHASE
                        total_managed_w += g.last_sent_amps * v
                else:
                    total_managed_w += float(d.power_watts)
            interval_s = max(
                float(self._config.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)),
                60.0,
            )
            shift_kwh = total_managed_w / 1000.0 * (interval_s / 3600.0)
            lines.append(
                f"Verwachte solar shift (deze cyclus):"
                f" {total_managed_w:.0f} W × {interval_s:.0f}s"
                f" = +{shift_kwh:.4f} kWh"
            )

        lines.append("=== Einde decision ===")
        lines.append("")

        log_content = "\n".join(lines) + "\n"
        log_path = self._hass.config.path("peak_guard_decisions.log")

        def _write_sync() -> Optional[Exception]:
            try:
                with open(log_path, "a", encoding="utf-8") as fh:
                    fh.write(log_content)
                return None
            except OSError as err:
                return err

        write_err = await asyncio.to_thread(_write_sync)
        if write_err:
            _LOGGER.warning(
                "Peak Guard: beslissingslog schrijven mislukt naar '%s': %s",
                log_path,
                write_err,
            )

    def _append_device_lines(
        self,
        lines: list,
        d: BaseCascadeDevice,
        pre_states: dict,
        snapshots: Dict[str, DeviceSnapshot],
        cascade: str,
    ) -> None:
        pre = pre_states.get(d.entity_id, "?")
        cur_s = self._hass.states.get(d.entity_id)
        cur = cur_s.state if cur_s else "niet gevonden"
        managed = " ✓ beheerd door PeakGuard" if d.entity_id in snapshots else ""
        disabled_tag = " [UITGESCHAKELD]" if not d.enabled else ""
        changed = " ← gewijzigd" if (pre != cur and pre != "?") else ""
        lines.append(f"  [{d.priority}] {d.name} ({d.entity_id}){disabled_tag}")
        lines.append(f"       State: {pre} → {cur}{changed}{managed}")
        lines.append(
            f"       Type: {d.action_type}"
            + (f" | Vermogen: {d.power_watts} W" if d.power_watts else "")
        )
        if d.action_type != ACTION_EV_CHARGER:
            return

        guard = self._ev_guard.guards.get(d.id)
        if not guard:
            return

        lines.append(
            f"       EV State: {guard.state.value}"
            + (f" | Laadstroom: {guard.last_sent_amps:.1f} A"
               if guard.last_sent_amps is not None else "")
        )
        lines.append(
            f"       Rate limiter: "
            f"{self._ev_guard.rate_limiter.calls_in_window}"
            f"/{EV_RATE_LIMIT_MAX_CALLS} calls"
            f" in {EV_RATE_LIMIT_WINDOW_S:.0f}s"
            + (f" | Resterende ruimte: {self._ev_guard.rate_limiter.remaining}"
               if cascade == "inject" else "")
        )

        if cascade != "inject":
            return

        surplus_vals = [
            f"{w:.0f}"
            for _, w in list(guard.surplus_history)[-5:]
        ]
        lines.append(
            f"       Surplus history (laatste 5 samples): "
            f"[{', '.join(surplus_vals) or '–'}] W"
        )

        if guard.surplus_history:
            oldest_ts = guard.surplus_history[0][0]
            elapsed_s = (datetime.now(timezone.utc) - oldest_ts).total_seconds()
            remaining_s = max(0.0, EV_DEBOUNCE_STABLE_S - elapsed_s)
            if guard.state == EVState.WAITING_FOR_STABLE:
                lines.append(
                    f"       Debounce resterend: {remaining_s:.0f}s"
                    f" (van {EV_DEBOUNCE_STABLE_S:.0f}s vereist)"
                )

        now_utc = datetime.now(timezone.utc)
        if guard.turned_on_at is not None and guard.state == EVState.CHARGING:
            on_secs = (now_utc - guard.turned_on_at).total_seconds()
            min_on_rem = max(0.0, EV_MIN_ON_DURATION_S - on_secs)
            lines.append(
                f"       EV draait: {on_secs:.0f}s"
                + (f" | Min ON nog: {min_on_rem:.0f}s"
                   if min_on_rem > 0 else " | Min ON verstreken ✓")
            )
        if guard.turned_off_at is not None and guard.turned_off_by_pg:
            off_secs = (now_utc - guard.turned_off_at).total_seconds()
            min_off_rem = max(0.0, EV_MIN_OFF_DURATION_S - off_secs)
            if min_off_rem > 0:
                lines.append(f"       Min OFF resterend: {min_off_rem:.0f}s")
