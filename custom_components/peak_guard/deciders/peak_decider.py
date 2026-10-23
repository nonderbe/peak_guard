"""
Peak Guard — deciders/peak_decider.py

PeakDecider: bewaakt het kwartiervermogen en schakelt apparaten uit
als het verbruik de maandpiek (min. 2,5 kW) − buffer dreigt te overschrijden.
Herstelt apparaten zodra er weer voldoende headroom is.
"""
from __future__ import annotations

import logging
import math
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Tuple

from homeassistant.core import HomeAssistant

from ..const import (
    ACTION_EV_CHARGER,
    CONF_BUFFER_WATTS,
    CONF_PEAK_SENSOR,
    DEFAULT_BUFFER_WATTS,
    PEAK_RESTART_CONFIRM_S,
)
from ..models import EV_MIN_OFF_DURATION_S, BaseCascadeDevice, DeviceSnapshot
from ..utils import effective_peak_w
from .base import BaseDecider, read_power_w

if TYPE_CHECKING:
    from ..avoided_peak_tracker import PeakAvoidTracker, SolarShiftTracker
    from .ev_guard import EVGuard

_LOGGER = logging.getLogger(__name__)


class PeakDecider(BaseDecider):
    """
    Beslisser voor piekbeperking (Modus 1).

    check()         — overschrijdt het verbruik de piekgrens − buffer?
                      Zo ja, start de cascade om apparaten uit te schakelen.
    check_restore() — is er genoeg headroom om eerder uitgeschakelde
                      apparaten te herstellen?
    """

    def __init__(
        self,
        hass: HomeAssistant,
        config: dict,
        peak_tracker: "PeakAvoidTracker",
        solar_tracker: "SolarShiftTracker",
        ev_guard: "EVGuard",
        iteration_actions: list,
        save_fn: Callable,
        cascade: List[BaseCascadeDevice],
        snapshots: Dict[str, DeviceSnapshot],
    ) -> None:
        super().__init__(
            hass=hass,
            config=config,
            peak_tracker=peak_tracker,
            solar_tracker=solar_tracker,
            ev_guard=ev_guard,
            iteration_actions=iteration_actions,
            save_fn=save_fn,
        )
        self._cascade = cascade
        self._snapshots = snapshots
        # Laadschema: apparaten die het schema nu beheert, overslaan.
        self._skip_fn: Optional[Callable[[BaseCascadeDevice], bool]] = None
        # EV's herstart op een lagere stroom: entity_id → (tijdstip, stroom A).
        self._ev_restarts: Dict[str, Tuple[datetime, int]] = {}

    def set_skip_fn(self, skip_fn: Optional[Callable[[BaseCascadeDevice], bool]]) -> None:
        self._skip_fn = skip_fn

    # ------------------------------------------------------------------ #
    #  Publieke interface                                                  #
    # ------------------------------------------------------------------ #

    async def check(self, consumption: float, now: Optional[datetime] = None) -> None:
        """
        Controleer of het verbruik de maandpiek − buffer overschrijdt.
        Zo ja, start de piek-cascade.
        """
        raw_peak = read_power_w(self.hass, self.config.get(CONF_PEAK_SENSOR))
        if raw_peak is None:
            _LOGGER.warning(
                "Peak Guard: piek-sensor '%s' niet beschikbaar — piekcheck overgeslagen",
                self.config.get(CONF_PEAK_SENSOR),
            )
            return
        # Onder 2,5 kW is geen capaciteitstarief verschuldigd: stuur nooit
        # op een lagere piek dan dat minimum, ook al meldt de P1-meter minder.
        peak = effective_peak_w(raw_peak)
        buffer = float(self.config.get(CONF_BUFFER_WATTS, DEFAULT_BUFFER_WATTS))
        excess = consumption - peak + buffer
        _LOGGER.debug(
            "Peak Guard _check_peak: verbruik=%.0f W, piek=%.0f W (P1: %.0f W), "
            "buffer=%.0f W, overschot=%.0f W",
            consumption, peak, raw_peak, buffer, excess,
        )
        if excess > 0:
            enabled_devices = [d for d in self._cascade if d.enabled]
            _LOGGER.warning(
                "Peak Guard [PIEK cascade]: gestart — piek overschreden met %.0f W "
                "(verbruik=%.0f W, piekgrens=%.0f W, buffer=%.0f W, "
                "%d apparaat/apparaten: %s)",
                excess, consumption, peak, buffer, len(enabled_devices),
                ", ".join(f"'{d.name}'" for d in enabled_devices) or "–",
            )
            if not enabled_devices:
                _LOGGER.warning(
                    "Peak Guard: geen actieve apparaten in piek-cascade — niets te doen!"
                )
            await self._run_cascade(self._cascade, excess, self._snapshots, "peak", now)

    async def check_restore(self, consumption: float, now: Optional[datetime] = None) -> None:
        """
        Controleer of eerder uitgeschakelde apparaten veilig hersteld kunnen
        worden zonder de piekgrens opnieuw te overschrijden.
        """
        if not self._snapshots:
            return
        raw_peak = read_power_w(self.hass, self.config.get(CONF_PEAK_SENSOR))
        if raw_peak is None:
            return
        peak = effective_peak_w(raw_peak)
        buffer = float(self.config.get(CONF_BUFFER_WATTS, DEFAULT_BUFFER_WATTS))
        headroom = peak - buffer - consumption
        if now is None:
            now = datetime.now(timezone.utc)
        for entity_id in list(self._ev_restarts):
            if entity_id not in self._snapshots:
                del self._ev_restarts[entity_id]
        if headroom <= 0:
            _LOGGER.debug(
                "Peak Guard [PIEK] herstel geblokkeerd: verbruik=%.0f W, "
                "piekgrens=%.0f W, buffer=%.0f W → headroom=%.0f W (≤ 0)",
                consumption, peak, buffer, headroom,
            )
            for device in self._cascade:
                if device.entity_id not in self._snapshots:
                    continue
                if self._skip_fn is not None and self._skip_fn(device):
                    continue
                reduced_ev = (
                    getattr(device, "action_type", None) == ACTION_EV_CHARGER
                    and not self.ev_guard.peak_is_off(device)
                )
                self.ev_guard.set_hold(
                    device.id, "peak_no_headroom",
                    ("Laadstroom blijft verlaagd" if reduced_ev else "Blijft afgeschakeld")
                    + f": het verbruik ({consumption:.0f} W) ligt niet "
                    f"onder de piekgrens min buffer ({peak - buffer:.0f} W).",
                )
            return

        snapshots_to_restore = self._get_restore_candidates(
            self._cascade, self._snapshots, reverse=True, skip_fn=self._skip_fn
        )
        if not snapshots_to_restore:
            return

        # Herstel kandidaten in omgekeerde prioriteitsvolgorde
        # (laagste prioriteit eerst = laatste uitgeschakeld).
        # Reduceer de beschikbare headroom per hersteld apparaat. Een apparaat
        # dat niet past, houdt de kleinere apparaten erna niet tegen.
        remaining_headroom = headroom
        any_restored = False
        for device, snapshot in snapshots_to_restore:
            is_ev = getattr(device, "action_type", None) == ACTION_EV_CHARGER
            if is_ev:
                ev_off = self.ev_guard.peak_is_off(device)
                if self._ev_restart_pending(device, ev_off, now):
                    _ts, restart_a = self._ev_restarts[device.entity_id]
                    remaining_headroom -= restart_a * self.ev_guard.voltage(device)
                    continue
                if device.entity_id not in self._snapshots:
                    any_restored = True   # snapshot vervallen na mislukte herstart
                    continue
                min_off_s = self.ev_guard.min_off_remaining_s(device, now)
                if min_off_s > 0 and ev_off:
                    self.ev_guard.set_hold(
                        device.id, "min_off",
                        f"Wacht nog {min_off_s:.0f} s voor het herstarten: na een stop door "
                        f"Peak Guard blijft de lader minstens "
                        f"{EV_MIN_OFF_DURATION_S / 60:.0f} min uit, om de schakelaar te sparen.",
                        until=now + timedelta(seconds=min_off_s),
                    )
                    continue
                need_w = self.ev_guard.peak_restore_need_w(device, snapshot, now)
                if not ev_off:
                    if need_w > remaining_headroom:
                        # Aan maar beperkt: verhoog stapsgewijs, snapshot blijft liggen.
                        remaining_headroom -= await self.ev_guard.peak_raise_current(
                            device, snapshot, remaining_headroom, now
                        )
                        continue
                else:
                    start_w = self.ev_guard.hw_min_amps(device) * self.ev_guard.voltage(device)
                    cap_a = self._ev_start_cap_a(device, snapshot, remaining_headroom)
                    if start_w > remaining_headroom:
                        need_w = max(need_w, start_w)   # zelfs het startminimum past niet
                    elif cap_a is not None:
                        # Uit, en de originele stroom past niet: herstart op wat past.
                        # Het snapshot blijft liggen; daarna gaat het stapsgewijs omhoog.
                        if await self._restore_device(
                            device, replace(snapshot, original_current=float(cap_a)),
                            cascade_type="peak", now=now,
                        ):
                            self._ev_restarts[device.entity_id] = (now, cap_a)
                            remaining_headroom -= cap_a * self.ev_guard.voltage(device)
                            self.ev_guard.set_hold(
                                device.id, "peak_stepwise",
                                f"Na piekbeperking herstart op {cap_a} A (zoveel als de piekruimte "
                                f"toelaat); de stroom gaat stapsgewijs terug naar "
                                f"{snapshot.original_current:.0f} A.",
                            )
                        continue
            else:
                need_w = float(device.power_watts) if device.power_watts else 0.0
            if need_w > 0 and remaining_headroom < need_w:
                _LOGGER.info(
                    "Peak Guard [PIEK]:   · '%s' herstel GEBLOKKEERD — "
                    "headroom %.0f W < nodig %.0f W (te weinig marge)",
                    device.name, remaining_headroom, need_w,
                )
                self.ev_guard.set_hold(
                    device.id, "peak_headroom",
                    f"Blijft afgeschakeld: herstel vraagt {need_w:.0f} W, maar er is maar "
                    f"{max(0.0, remaining_headroom):.0f} W ruimte onder de piekgrens "
                    f"(piek {peak:.0f} W − buffer {buffer:.0f} W − verbruik {consumption:.0f} W"
                    + (", min. wat eerder herstelde apparaten vragen" if remaining_headroom < headroom else "")
                    + ").",
                )
                continue

            restored = await self._restore_device(device, snapshot, cascade_type="peak", now=now)
            if restored:
                del self._snapshots[device.entity_id]
                remaining_headroom -= need_w
                any_restored = True
                _LOGGER.info(
                    "Peak Guard [PIEK]: '%s' terug AAN — headroom was %.0f W "
                    "(piek=%.0f W, buffer=%.0f W, verbruik=%.0f W)",
                    device.name, headroom, peak, buffer, consumption,
                )
            else:
                self.ev_guard.set_hold(
                    device.id, "restore_failed",
                    "Herstel mislukt (apparaat niet bereikbaar?) — volgende cyclus opnieuw.",
                )
        if any_restored:
            await self._save_fn()

    def hand_over_to_solar(
        self, inject_snapshots: Dict[str, DeviceSnapshot], now: Optional[datetime] = None
    ) -> None:
        """
        Een EV die de piek verlaagde of stopte en die de solar-cascade nu
        stuurt, laat het piekherstel los. Anders zet het herstel in dezelfde
        cyclus de laadstroom terug naar de originele waarde, ook als die lager
        is dan wat het overschot toelaat (injectie) of hoger (netafname,
        waarna de solar-cascade weer verlaagt). Het solar-snapshot neemt de
        toestand van vóór de piek over, zodat de lading na het overschot naar
        die toestand terugkeert en niet naar de verlaagde stroom of 'uit'.
        """
        if now is None:
            now = datetime.now(timezone.utc)
        for device in self._cascade:
            if getattr(device, "action_type", None) != ACTION_EV_CHARGER:
                continue
            peak_snap = self._snapshots.get(device.entity_id)
            inject_snap = inject_snapshots.get(device.entity_id)
            if peak_snap is None or inject_snap is None:
                continue
            inject_snapshots[device.entity_id] = replace(
                inject_snap,
                original_state=peak_snap.original_state,
                original_current=(
                    peak_snap.original_current
                    if peak_snap.original_current is not None
                    else inject_snap.original_current
                ),
            )
            del self._snapshots[device.entity_id]
            self._ev_restarts.pop(device.entity_id, None)
            # Een stop door de piek wacht op de herinschakeling: die is er nu.
            self.peak_tracker.start_measurement_on_turnon(
                device_id=device.id, device_name=device.name, ts=now,
            )
            _LOGGER.info(
                "Peak Guard [PIEK]: '%s' overgenomen door de solar-cascade — "
                "piekherstel afgesloten (na het overschot terug naar %s)",
                device.name,
                f"{peak_snap.original_current:.0f} A" if peak_snap.original_current is not None
                else peak_snap.original_state,
            )

    def _ev_restart_pending(self, device: BaseCascadeDevice, ev_off: bool, now: datetime) -> bool:
        """
        Wacht een herstart op lagere stroom nog op het laden? Laadt de wagen na
        PEAK_RESTART_CONFIRM_S nog niet (vol, kabel uit), dan vervalt het
        snapshot: anders gaat elke cyclus opnieuw turn_on/set_value naar de wagen.
        """
        entry = self._ev_restarts.get(device.entity_id)
        if entry is None:
            return False
        ts, restart_a = entry
        turned_off_at = self.ev_guard.get_guard(device.id).turned_off_at
        if not ev_off or (turned_off_at is not None and turned_off_at > ts):
            # Laadt (verder stapsgewijs), of opnieuw gestopt door de piek.
            del self._ev_restarts[device.entity_id]
            return False
        waited = (now - ts).total_seconds()
        if waited < PEAK_RESTART_CONFIRM_S:
            self.ev_guard.set_hold(
                device.id, "peak_stepwise",
                f"Herstart op {restart_a} A na piekbeperking; wacht tot de wagen laadt "
                f"(nog {PEAK_RESTART_CONFIRM_S - waited:.0f} s).",
                until=ts + timedelta(seconds=PEAK_RESTART_CONFIRM_S),
            )
            return True
        _LOGGER.info(
            "Peak Guard [PIEK]: '%s' laadt %.0f s na de herstart nog niet "
            "(vol of kabel uit?) — herstel na piekbeperking afgesloten",
            device.name, waited,
        )
        del self._ev_restarts[device.entity_id]
        self._snapshots.pop(device.entity_id, None)
        return False

    def _ev_start_cap_a(
        self, device: BaseCascadeDevice, snapshot: DeviceSnapshot, headroom_w: float
    ) -> Optional[int]:
        """Lagere startstroom als de originele niet in de piekruimte past, anders None."""
        if not self.ev_guard.peak_is_off(device) or snapshot.original_current is None:
            return None
        if not getattr(device, "current_entity", None):
            return None
        fit_a = math.floor(headroom_w / self.ev_guard.voltage(device))
        if fit_a >= snapshot.original_current:
            return None
        return max(int(self.ev_guard.hw_min_amps(device)), fit_a)
