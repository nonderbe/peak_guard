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
    CONF_ENERGY_SENSOR,
    CONF_PEAK_SENSOR,
    DEFAULT_BUFFER_WATTS,
    PEAK_RESTART_CONFIRM_S,
    PEAK_SETTLE_S,
    PEAK_ENERGY_MAX_STALE_S,
    PEAK_ENERGY_STALE_S,
    PEAK_SWITCH_MIN_OFF_S,
)
from ..models import (
    EV_MIN_OFF_DURATION_S,
    BaseCascadeDevice,
    DeviceSnapshot,
    age_s,
    str_to_ts,
    ts_to_str,
)
from ..quarter_budget import PeakBudget, PowerAverager, PowerHistory, make_budget
from ..utils import effective_peak_w
from .base import BaseDecider, read_energy_kwh, read_power_w, restore_need_w

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
        # Staat dit apparaat (ook) in de inject-cascade? Een door de piek
        # gestopte EV wordt dan tijdens injectie niet door het piekherstel
        # herstart, maar door de solar-cascade overgenomen.
        self._solar_owner_fn: Optional[Callable[[BaseCascadeDevice], bool]] = None
        # EV's herstart op een lagere stroom: entity_id → (tijdstip, stroom A).
        self._ev_restarts: Dict[str, Tuple[datetime, int]] = {}
        # Kwartierbudget: geeft (kwartierstart, meterstand bij de start) van
        # het lopende, volledig gemeten kwartier, of None.
        self._baseline_fn: Optional[Callable[[], Optional[Tuple[datetime, float]]]] = None
        self._averager = PowerAverager()
        self._power_history = PowerHistory()
        self._inject_snapshots: Optional[Dict[str, DeviceSnapshot]] = None
        # Snapshots gewijzigd buiten een async pad (take_over_from_solar):
        # bewaren bij de volgende check_restore.
        self._snapshots_dirty = False
        self._budget_cache: Optional[Tuple[datetime, float, PeakBudget]] = None
        # Laatste afschakeling door de cascade (PEAK_SETTLE_S).
        self._last_shed_at: Optional[datetime] = None
        # Per entity: wanneer het herstel bevestigd werd (min_on_s). Gepersisteerd.
        self._restored_at: Dict[str, datetime] = {}

    def set_skip_fn(self, skip_fn: Optional[Callable[[BaseCascadeDevice], bool]]) -> None:
        self._skip_fn = skip_fn

    def set_solar_owner_fn(self, fn: Optional[Callable[[BaseCascadeDevice], bool]]) -> None:
        self._solar_owner_fn = fn

    def set_inject_snapshots(self, snapshots: Optional[Dict[str, DeviceSnapshot]]) -> None:
        """De inject-snapshots (zelfde dict als de InjectionDecider), voor solar_started."""
        self._inject_snapshots = snapshots

    def note_consumption(self, now: datetime, consumption: Optional[float]) -> None:
        """Verbruik van elke cyclus, voor de schatting bij een trage energiesensor.
        Een afnameregister daalt niet: injectie telt als 0."""
        if consumption is not None:
            self._power_history.add(now, max(0.0, consumption))

    def clear_power_history(self) -> None:
        self._power_history = PowerHistory()

    def _solar_may_act(self, device) -> bool:
        return self._solar_owner_fn is None or self._solar_owner_fn(device)

    def _mark_solar_started(self) -> None:
        """Markeer piek-snapshots van EV's die de solar-cascade had gestart."""
        inject = self._inject_snapshots
        if not inject:
            return
        for device in self._cascade:
            if getattr(device, "action_type", None) != ACTION_EV_CHARGER:
                continue
            peak_snap = self._snapshots.get(device.entity_id)
            inject_snap = inject.get(device.entity_id)
            if (
                peak_snap is not None and not getattr(peak_snap, "solar_started", True)
                and inject_snap is not None
                and inject_snap.original_state in ("off", "unavailable", "unknown")
                and self._solar_may_act(device)
            ):
                peak_snap.solar_started = True
                self._snapshots_dirty = True

    def _release_solar_started(self) -> bool:
        """
        Een door solar gestarte EV waarvan de solar-cascade intussen klaar is
        (inject-snapshot weg: overschot voorbij, gestopt en opgeruimd): het
        piekherstel herstart hem niet — de zon is weg, hij zou van het net
        laden. True als er iets wegviel.
        """
        inject = self._inject_snapshots if self._inject_snapshots is not None else {}
        changed = False
        for device in self._cascade:
            snap = self._snapshots.get(device.entity_id)
            if snap is None or not getattr(snap, "solar_started", False) or device.entity_id in inject:
                continue
            del self._snapshots[device.entity_id]
            self._ev_restarts.pop(device.entity_id, None)
            self.peak_tracker.discard_pending(device.id)
            changed = True
            _LOGGER.info(
                "Peak Guard [PIEK]: '%s' was door de solar-cascade gestart en die is klaar — "
                "na de piek niet herstarten", device.name,
            )
        return changed

    # ------------------------------------------------------------------ #
    #  Runtime-toestand (persistentie) en bevestigings-hooks              #
    # ------------------------------------------------------------------ #

    def runtime_to_dict(self) -> dict:
        return {"restored_at": {k: ts_to_str(v) for k, v in self._restored_at.items()}}

    def load_runtime(self, data) -> None:
        self._restored_at.clear()
        if not isinstance(data, dict):
            return
        for eid, raw in (data.get("restored_at") or {}).items():
            ts = str_to_ts(raw)
            if ts is not None:
                self._restored_at[eid] = ts

    def _on_apply_acked(self, device: BaseCascadeDevice, now: datetime) -> None:
        # Het verbruik daalt pas nu: de volgende metingen zijn weer verouderd.
        self._last_shed_at = now

    def _on_restore_acked(self, device: BaseCascadeDevice, now: datetime) -> None:
        self._restored_at[device.entity_id] = now

    def _on_restore_given_up(self, device: BaseCascadeDevice) -> None:
        self.peak_tracker.discard_pending(device.id)

    def _on_snapshot_dropped(self, device: BaseCascadeDevice) -> None:
        """Snapshot weg zonder bevestigd herstel (manuele bediening): niets boeken."""
        if getattr(device, "action_type", None) != ACTION_EV_CHARGER:
            self.peak_tracker.discard_pending(device.id)

    @staticmethod
    def _effective_min_on_s(device: BaseCascadeDevice) -> Optional[float]:
        """min_on_s; met enkel max_off_s minstens PEAK_SWITCH_MIN_OFF_S (geen
        20 min uit / 1 min aan bij een aanhoudende piek)."""
        min_on_s = getattr(device, "min_on_s", None)
        if min_on_s is None and getattr(device, "max_off_s", None) is not None:
            return PEAK_SWITCH_MIN_OFF_S
        if min_on_s is None and getattr(device, "peak_setback", None):
            # Thermostaat-bijstelling (cloud-airco met dagquotum): geen
            # bijstellen/herstellen om de paar minuten bij een randpiek.
            return PEAK_SWITCH_MIN_OFF_S
        return min_on_s

    async def enforce_limits(self, now: datetime) -> None:
        """
        Zonder verbruiksmeting (run_tick(None)): bevestigingen verwerken en
        max_off_s afdwingen, zodat een gepauzeerd apparaat niet blijft wachten
        op een sensor die terugkomt.
        """
        if not self._snapshots:
            return
        await self._poll_acks(self._cascade, self._snapshots, "peak", now)
        if await self._restore_max_off(now):
            await self._save_fn()

    def _min_on_blocks(self, device: BaseCascadeDevice, now: datetime) -> bool:
        """min_on_s: na een herstel nog niet opnieuw afschakelen (de volgende apparaten wel)."""
        min_on_s = self._effective_min_on_s(device)
        if min_on_s is None or getattr(device, "action_type", None) == ACTION_EV_CHARGER:
            return False
        if device.entity_id in self._snapshots:
            return False
        restored_at = self._restored_at.get(device.entity_id)
        if restored_at is None:
            return False
        elapsed = (now - restored_at).total_seconds()
        if elapsed >= min_on_s:
            return False
        wait_s = min_on_s - elapsed
        self.ev_guard.set_hold(
            device.id, "min_on",
            f"Wordt niet opnieuw afgeschakeld: na een herstel blijft dit apparaat minstens "
            f"{min_on_s / 60:.0f} min aan (nog {wait_s:.0f} s). De piek-cascade "
            f"schakelt intussen de volgende apparaten af.",
            until=now + timedelta(seconds=wait_s),
        )
        return True

    def set_quarter_baseline_fn(
        self, fn: Optional[Callable[[], Optional[Tuple[datetime, float]]]]
    ) -> None:
        self._baseline_fn = fn

    def _budget(self, consumption: float, peak: float, buffer: float, now: datetime) -> PeakBudget:
        """Kwartierbudget voor deze cyclus (één keer per tijdstip berekend)."""
        cached = self._budget_cache
        if cached is not None and cached[0] == now and cached[1] == consumption:
            return cached[2]
        energy_entity = self.config.get(CONF_ENERGY_SENSOR)
        energy = read_energy_kwh(self.hass, energy_entity)
        reading_ts = self._energy_reading_ts(energy_entity)
        age_s = (now - reading_ts).total_seconds() if reading_ts is not None else 0.0
        if energy is not None and age_s > PEAK_ENERGY_STALE_S:
            # Trage energiesensor: zijn stand mist het verbruik sinds de
            # laatste update, het budget zou te ruim lijken (bug 6). Het
            # minuutgemiddelde uit die standen is dan ook te laag.
            avg_w = None
            if age_s > PEAK_ENERGY_MAX_STALE_S:
                energy = None       # te oud om op te schatten: momentane regel
            else:
                energy += self._power_history.energy_kwh_since(reading_ts, now)
        else:
            avg_w = self._averager.add(now, energy)
        baseline = self._baseline_fn() if self._baseline_fn is not None else None
        q_start, so_far = None, None
        if baseline is not None and energy is not None:
            q_start, start_energy = baseline
            so_far = max(0.0, energy - start_energy)
        budget = make_budget(peak - buffer, consumption, now, avg_w, q_start, so_far)
        self._budget_cache = (now, consumption, budget)
        return budget

    def _energy_reading_ts(self, entity_id: Optional[str]) -> Optional[datetime]:
        """Tijdstip van de laatste melding van de energiesensor (ook zonder
        nieuwe waarde: last_reported), of None als onbekend."""
        if not entity_id:
            return None
        st = self.hass.states.get(entity_id)
        if st is None:
            return None
        ts = getattr(st, "last_reported", None) or getattr(st, "last_updated", None)
        return ts if isinstance(ts, datetime) and ts.tzinfo is not None else None

    # ------------------------------------------------------------------ #
    #  Publieke interface                                                  #
    # ------------------------------------------------------------------ #

    async def check(self, consumption: float, now: Optional[datetime] = None) -> None:
        """
        Controleer of het verbruik de maandpiek − buffer overschrijdt.
        Zo ja, start de piek-cascade.
        """
        if now is None:
            now = datetime.now(timezone.utc)
        await self._poll_acks(self._cascade, self._snapshots, "peak", now)
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
        budget = self._budget(consumption, peak, buffer, now)
        excess = budget.excess_w
        _LOGGER.debug(
            "Peak Guard _check_peak: verbruik=%.0f W (gem. %s), piek=%.0f W (P1: %.0f W), "
            "buffer=%.0f W, toegelaten=%.0f W%s, overschot=%.0f W",
            consumption, f"{budget.avg_w:.0f} W" if budget.avg_w is not None else "–",
            peak, raw_peak, buffer, budget.allowed_w,
            "" if budget.on_budget else " (momentaan)", excess,
        )
        if excess <= 0:
            return
        if self._last_shed_at is not None and (now - self._last_shed_at).total_seconds() < PEAK_SETTLE_S:
            # Vlak na de eigen afschakeling: het verbruik is nog niet bijgewerkt.
            _LOGGER.debug(
                "Peak Guard [PIEK]: overschot %.0f W genegeerd — %.0f s na de vorige "
                "afschakeling is het verbruik nog niet bijgewerkt",
                excess, (now - self._last_shed_at).total_seconds(),
            )
            return
        enabled_devices = [d for d in self._cascade if d.enabled]
        _LOGGER.warning(
            "Peak Guard [PIEK cascade]: gestart — piek overschreden met %.0f W "
            "(verbruik=%.0f W, gem. laatste minuut=%s, toegelaten rest kwartier=%.0f W, "
            "piekgrens=%.0f W, buffer=%.0f W, %d apparaat/apparaten: %s)",
            excess, consumption,
            f"{budget.avg_w:.0f} W" if budget.avg_w is not None else "–",
            budget.allowed_w, peak, buffer, len(enabled_devices),
            ", ".join(f"'{d.name}'" for d in enabled_devices) or "–",
        )
        if not enabled_devices:
            _LOGGER.warning(
                "Peak Guard: geen actieve apparaten in piek-cascade — niets te doen!"
            )
        before = set(self._snapshots)
        remaining = await self._run_cascade(
            self._cascade, excess, self._snapshots, "peak", now,
            skip_fn=lambda d: self._min_on_blocks(d, now),
        )
        for entity_id in set(self._snapshots) - before:
            # Typen zonder bevestiging (legacy throttle): afgeschakeld = nu.
            snap = self._snapshots[entity_id]
            if getattr(snap, "pending", None) is None and getattr(snap, "off_since", "x") is None:
                snap.off_since = ts_to_str(now)
        if remaining < excess:
            self._last_shed_at = now
        if remaining > 0:
            # Alles wat kon, staat al uit: een piek in dit kwartier was niet te vermijden.
            self.peak_tracker.note_exhausted(now, remaining)

    async def check_restore(self, consumption: float, now: Optional[datetime] = None) -> None:
        """
        Controleer of eerder uitgeschakelde apparaten veilig hersteld kunnen
        worden zonder de piekgrens opnieuw te overschrijden.
        """
        if now is None:
            now = datetime.now(timezone.utc)
        self._prune_restored_at(now)
        if not self._snapshots:
            if self._snapshots_dirty:
                self._snapshots_dirty = False
                await self._save_fn()
            return
        await self._poll_acks(self._cascade, self._snapshots, "peak", now)
        forced = await self._restore_max_off(now)
        # Vóór de headroom-check: een vrijgegeven EV hoort geen 'blijft
        # afgeschakeld' te tonen. Ook een nog niet bewaarde solar_started-
        # markering of overname (take_over_from_solar) wordt hier bewaard.
        if self._release_solar_started():
            forced = True
        if self._snapshots_dirty:
            self._snapshots_dirty = False
            forced = True
        if not self._snapshots:
            if forced:
                await self._save_fn()
            return
        raw_peak = read_power_w(self.hass, self.config.get(CONF_PEAK_SENSOR))
        if raw_peak is None:
            if forced:
                await self._save_fn()
            return
        peak = effective_peak_w(raw_peak)
        buffer = float(self.config.get(CONF_BUFFER_WATTS, DEFAULT_BUFFER_WATTS))
        budget = self._budget(consumption, peak, buffer, now)
        headroom = budget.restore_headroom_w
        for entity_id in list(self._ev_restarts):
            if entity_id not in self._snapshots:
                del self._ev_restarts[entity_id]
        if headroom <= 0:
            _LOGGER.debug(
                "Peak Guard [PIEK] herstel geblokkeerd: verbruik=%.0f W (herstel rekent "
                "met %.0f W), toegelaten=%.0f W, piekgrens=%.0f W, buffer=%.0f W "
                "→ headroom=%.0f W (≤ 0)",
                consumption, budget.restore_power_w, budget.allowed_w, peak, buffer, headroom,
            )
            for device in self._cascade:
                if device.entity_id not in self._snapshots:
                    continue
                if self._skip_fn is not None and self._skip_fn(device):
                    continue
                if getattr(self._snapshots[device.entity_id], "pending", None) is not None:
                    continue   # eigen verklaring (bevestiging)
                reduced_ev = (
                    getattr(device, "action_type", None) == ACTION_EV_CHARGER
                    and not self.ev_guard.peak_is_off(device)
                )
                self.ev_guard.set_hold(
                    device.id, "peak_no_headroom",
                    ("Laadstroom blijft verlaagd" if reduced_ev else "Blijft afgeschakeld")
                    + f": het verbruik ({budget.restore_power_w:.0f} W) ligt niet "
                    f"onder de piekgrens min buffer"
                    + (f" en wat het kwartier nog toelaat ({min(budget.allowed_w, budget.limit_w):.0f} W)."
                       if budget.on_budget else f" ({peak - buffer:.0f} W)."),
                )
            if forced:
                await self._save_fn()
            return

        snapshots_to_restore = self._get_restore_candidates(
            self._cascade, self._snapshots, reverse=True, skip_fn=self._skip_fn,
            on_drop=self._on_snapshot_dropped,
        )
        if not snapshots_to_restore:
            if forced:
                await self._save_fn()
            return

        # Herstel kandidaten in omgekeerde prioriteitsvolgorde
        # (laagste prioriteit eerst = laatste uitgeschakeld).
        # Reduceer de beschikbare headroom per hersteld apparaat. Een apparaat
        # dat niet past, houdt de kleinere apparaten erna niet tegen.
        remaining_headroom = headroom
        any_restored = forced
        for device, snapshot in snapshots_to_restore:
            is_ev = getattr(device, "action_type", None) == ACTION_EV_CHARGER
            if is_ev and getattr(snapshot, "solar_started", False):
                # Verlaagd, en de solar-cascade (die hem startte) stuurt hem
                # nog: geen stroomverhoging door het piekherstel, anders
                # verhoogt die en verlaagt de solar-cascade om beurten.
                self.ev_guard.set_hold(
                    device.id, "peak_solar_handover",
                    "Gestart door de solar-cascade; die beslist over de laadstroom en "
                    "stopt hem na het overschot — het piekherstel verhoogt niet.",
                )
                continue
            if is_ev:
                ev_off = self.ev_guard.peak_is_off(device)
                if (
                    ev_off and consumption < 0
                    and self._solar_owner_fn is not None and self._solar_owner_fn(device)
                    and device.entity_id not in self._ev_restarts
                ):
                    # Injectie: de solar-cascade start deze EV op het overschot
                    # (hand_over_to_solar neemt dan het snapshot over). Het
                    # piekherstel zou hem op de originele stroom herstarten.
                    self.ev_guard.set_hold(
                        device.id, "peak_solar_handover",
                        "Gestopt door de piekbeperking; er is nu injectie, dus de solar-cascade "
                        "start de lader op het overschot in plaats van het piekherstel.",
                    )
                    continue
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
                if getattr(snapshot, "pending", None) == "apply":
                    continue   # afschakeling nog niet bevestigd
                need_w = restore_need_w(device, snapshot)
                if getattr(snapshot, "pending", None) == "restore":
                    # Herstel verstuurd, nog niet bevestigd: zijn vermogen komt terug.
                    remaining_headroom -= need_w
                    continue
                off_s = age_s(getattr(snapshot, "off_since", None), now)
                if off_s < PEAK_SWITCH_MIN_OFF_S:
                    wait_s = PEAK_SWITCH_MIN_OFF_S - off_s
                    self.ev_guard.set_hold(
                        device.id, "min_off",
                        f"Wacht nog {wait_s:.0f} s voor het herstel: na een afschakeling "
                        f"blijft dit apparaat minstens {PEAK_SWITCH_MIN_OFF_S / 60:.0f} min "
                        f"uit, zodat het niet aan en uit pendelt op een wisselend huisverbruik.",
                        until=now + timedelta(seconds=wait_s),
                    )
                    continue
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
                    f"(piek {peak:.0f} W − buffer {buffer:.0f} W − verbruik {budget.restore_power_w:.0f} W"
                    + (", min. wat eerder herstelde apparaten vragen" if remaining_headroom < headroom else "")
                    + ").",
                )
                continue

            restored = await self._restore_device(device, snapshot, cascade_type="peak", now=now)
            if restored:
                self._snapshots.pop(device.entity_id, None)
                if not is_ev and getattr(snapshot, "pending", None) == "done":
                    self._restored_at[device.entity_id] = now
                remaining_headroom -= need_w
                any_restored = True
                _LOGGER.info(
                    "Peak Guard [PIEK]: '%s' terug AAN — headroom was %.0f W "
                    "(piek=%.0f W, buffer=%.0f W, verbruik=%.0f W)",
                    device.name, headroom, peak, buffer, consumption,
                )
            elif not is_ev and getattr(snapshot, "pending", None) == "restore":
                remaining_headroom -= need_w
                any_restored = True   # snapshot gewijzigd: opslaan
                self.ev_guard.set_hold(
                    device.id, "awaiting_ack",
                    "Herstel verstuurd; wacht tot het apparaat de oorspronkelijke toestand meldt.",
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
            peak_snap = self._snapshots.get(device.entity_id)
            inject_snap = inject_snapshots.get(device.entity_id)
            if peak_snap is None or inject_snap is None:
                continue
            if getattr(peak_snap, "solar_started", False):
                # De solar-cascade had hem gestart: zijn snapshot ('uit' vóór
                # het overschot) blijft gelden, niet de 'aan' van de piek —
                # anders laadt hij na het overschot door van het net.
                del self._snapshots[device.entity_id]
                self._snapshots_dirty = True
                self._ev_restarts.pop(device.entity_id, None)
                self.peak_tracker.discard_pending(device.id)
                _LOGGER.info(
                    "Peak Guard [PIEK]: '%s' terug naar de solar-cascade die hem startte — "
                    "piekherstel afgesloten", device.name,
                )
                continue
            # Ook voor een schakelaar of thermostaat in beide cascades: anders
            # zet het piekherstel tijdens het overschot de oude instelling
            # terug, en zet het solar-herstel daarna de toestand van tijdens
            # de piek (uit) terug — de vloerverwarming bleef dan uit.
            inject_snapshots[device.entity_id] = replace(
                inject_snap,
                original_state=peak_snap.original_state,
                original_current=(
                    peak_snap.original_current
                    if peak_snap.original_current is not None
                    else inject_snap.original_current
                ),
                original_temp=(
                    peak_snap.original_temp
                    if peak_snap.original_temp is not None
                    else inject_snap.original_temp
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

    def take_over_from_solar(
        self, inject_snapshots: Dict[str, DeviceSnapshot], now: Optional[datetime] = None
    ) -> None:
        """
        Omgekeerde richting van hand_over_to_solar: de piek-cascade schakelde
        een apparaat af dat de solar-cascade had aangezet. De piek is nu
        eigenaar; anders zet het solar-herstel (verbruik ≥ 0) het in dezelfde
        cyclus weer aan en pendelt het elke minuut. Het piek-snapshot neemt
        de toestand van vóór het overschot over, zodat het herstel na de piek
        naar de instelling van de gebruiker terugkeert, niet naar die van het
        overschot. Niet voor EV's: daar regelen de EV-paden de overdracht.
        """
        if now is None:
            now = datetime.now(timezone.utc)
        self._inject_snapshots = inject_snapshots
        self._mark_solar_started()
        for device in self._cascade:
            peak_snap = self._snapshots.get(device.entity_id)
            inject_snap = inject_snapshots.get(device.entity_id)
            if peak_snap is None or inject_snap is None:
                continue
            if getattr(device, "action_type", None) == ACTION_EV_CHARGER:
                self._take_over_ev_from_solar(device, peak_snap, inject_snap)
                continue
            self._snapshots[device.entity_id] = replace(
                peak_snap,
                original_state=inject_snap.original_state,
                original_temp=inject_snap.original_temp,
            )
            del inject_snapshots[device.entity_id]
            self.solar_tracker.complete_solar_calculation(device_id=device.id, now=now)
            if inject_snap.original_state in ("off", "unavailable", "unknown"):
                # Stond uit vóór het overschot: na de piek wordt er niets
                # herstart, dus de afschakeling vermeed ook niets.
                self.peak_tracker.discard_pending(device.id)
            _LOGGER.info(
                "Peak Guard [PIEK]: '%s' overgenomen van de solar-cascade — na de piek "
                "terug naar %s", device.name, inject_snap.original_state,
            )

    def _take_over_ev_from_solar(
        self, device, peak_snap: DeviceSnapshot, inject_snap: DeviceSnapshot,
    ) -> None:
        """
        De piek stopte een EV die de solar-cascade had gestart (laadde niet
        vóór het overschot). Het piek-herstel mag hem dan niet herstarten —
        de zon kan intussen weg zijn, hij zou van het net laden. Het
        piek-snapshot vervalt; het inject-snapshot blijft, zodat het solar-
        herstel de SoC-limiet terugzet en de meting afsluit, en de solar-
        cascade hem bij nieuw overschot zelf herstart. Enkel verlaagd (laadt
        nog): niets doen, de solar-cascade blijft eigenaar van de lading.
        """
        if not peak_snap.solar_started:
            return
        if peak_snap.pending or not self.ev_guard.peak_is_off(device):
            return
        del self._snapshots[device.entity_id]
        self._snapshots_dirty = True
        # Na de piek wordt niets herstart: de stop vermeed ook niets.
        self.peak_tracker.discard_pending(device.id)
        _LOGGER.info(
            "Peak Guard [PIEK]: '%s' was door de solar-cascade gestart — "
            "na de piek niet herstarten (de solar-cascade beslist)", device.name,
        )

    def prune_restored_measurements(self) -> None:
        """Na het herstel van de tracker bij de start (sensor.py); de
        piek-snapshots zijn dan al geladen (controller.async_load). Zie
        PeakAvoidTracker.prune_restored_pendings."""
        fn = getattr(self.peak_tracker, "prune_restored_pendings", None)
        if fn is None:
            return
        fn({d.id for d in self._cascade if d.entity_id in self._snapshots})

    def _prune_restored_at(self, now: datetime) -> None:
        longest = max(
            (self._effective_min_on_s(d) or 0 for d in self._cascade), default=0
        )
        for eid, ts in list(self._restored_at.items()):
            if (now - ts).total_seconds() > max(longest, 0):
                del self._restored_at[eid]

    async def _restore_max_off(self, now: datetime) -> bool:
        """
        max_off_s: een apparaat dat zo lang (bevestigd) afgeschakeld is, gaat
        terug aan, ook zonder piekruimte. Bewuste afweging van de gebruiker
        (bv. een gepauzeerde wasmachine): dit kan een nieuwe maandpiek zetten.
        """
        changed = False
        for device in sorted(self._cascade, key=lambda d: d.priority, reverse=True):
            snap = self._snapshots.get(device.entity_id)
            max_off_s = getattr(device, "max_off_s", None)
            if snap is None or getattr(snap, "pending", None) is not None or max_off_s is None:
                continue
            if getattr(device, "action_type", None) == ACTION_EV_CHARGER:
                continue
            if self._skip_fn is not None and self._skip_fn(device):
                continue
            if device.manual_override:
                continue
            off_s = age_s(getattr(snap, "off_since", None), now)
            if off_s == float("inf") or off_s < max_off_s:
                continue
            self.ev_guard._warn(
                "Peak Guard [PIEK]: '%s' %.0f min afgeschakeld — maximum (%.0f min) bereikt, "
                "hersteld ongeacht de piekruimte",
                device.name, off_s / 60, max_off_s / 60,
            )
            restored = await self._restore_device(device, snap, cascade_type="peak", now=now)
            changed = True
            if restored:
                self._snapshots.pop(device.entity_id, None)
                if getattr(snap, "pending", None) == "done":
                    self._restored_at[device.entity_id] = now
            self.ev_guard.set_hold(
                device.id, "max_off",
                f"Na {off_s / 60:.0f} min afgeschakeld hersteld: dit apparaat mag hoogstens "
                f"{max_off_s / 60:.0f} min uit blijven, ook als dat de piek verhoogt."
                + ("" if restored else " Wacht op bevestiging van het apparaat."),
            )
        return changed

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
        turned_off_at = self.ev_guard.guard_for(device).turned_off_at
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
