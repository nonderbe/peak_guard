import asyncio
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Dict, List, Optional

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .avoided_peak_tracker import PeakAvoidTracker, SolarShiftTracker
from .deciders import EVGuard, InjectionDecider, PeakDecider
from .models import (
    CascadeDevice,
    DeviceSnapshot,
    EVState,
    EV_VOLTS_1PHASE,
    EV_VOLTS_3PHASE,
    EV_DEBOUNCE_STABLE_S,
    EV_MIN_ON_DURATION_S,
    EV_MIN_OFF_DURATION_S,
    EV_RATE_LIMIT_MAX_CALLS,
    EV_RATE_LIMIT_WINDOW_S,
)
from .const import (
    DOMAIN,
    STORAGE_KEY,
    STORAGE_VERSION,
    CONF_CONSUMPTION_SENSOR,
    CONF_PEAK_SENSOR,
    CONF_BUFFER_WATTS,
    CONF_UPDATE_INTERVAL,
    CONF_POWER_DETECTION_TOLERANCE_PERCENT,
    CONF_DEBUG_DECISION_LOGGING,
    DEFAULT_BUFFER_WATTS,
    DEFAULT_UPDATE_INTERVAL,
    DEFAULT_POWER_DETECTION_TOLERANCE_PERCENT,
    ACTION_EV_CHARGER,
)

_LOGGER = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────── #
#  Data classes                                                                 #
# ──────────────────────────────────────────────────────────────────────────── #

# CascadeDevice en DeviceSnapshot worden geïmporteerd uit models.py
# en zijn hierdoor ook beschikbaar via 'from .controller import CascadeDevice'.


# ──────────────────────────────────────────────────────────────────────────── #
#  Controller                                                                  #
# ──────────────────────────────────────────────────────────────────────────── #

class PeakGuardController:

    def __init__(self, hass: HomeAssistant, config: dict):
        self.hass = hass
        self.config = config
        self.peak_cascade:   List[CascadeDevice] = []
        self.inject_cascade: List[CascadeDevice] = []
        self._monitoring = False
        self._task: Optional[asyncio.Task] = None
        self._store = Store(hass, STORAGE_VERSION, STORAGE_KEY)

        self._peak_snapshots:   Dict[str, DeviceSnapshot] = {}
        self._inject_snapshots: Dict[str, DeviceSnapshot] = {}

        # Trackers
        self.peak_tracker  = PeakAvoidTracker()
        self.solar_tracker = SolarShiftTracker()

        # Power-drop detectie
        self._prev_consumption: Optional[float] = None

        # ── Entity listeners: callbacks die worden aangeroepen na elke
        # update_cascade() aanroep, zodat switch/number platforms
        # dynamisch nieuwe entities kunnen aanmaken.
        self._entity_listeners: list = []

        # Tijdstip van de laatste loop-iteratie (UTC ISO-string, voor de GUI).
        self._last_loop_at: Optional[str] = None

        # Force-check flag: als True wordt de volgende loop-iteratie onmiddellijk
        # uitgevoerd zonder te wachten op het interval.
        self._force_check: bool = False

        # ── Decision logging ──────────────────────────────────────────── #
        # Acties uitgevoerd in de huidige iteratie (reset elk begin loop).
        self._iteration_actions: list = []
        # Datum van de laatste dag waarvoor een header is geschreven.
        self._last_logged_day: Optional[str] = None

        # ── Deciders ─────────────────────────────────────────────────── #
        self._ev_guard_decider = EVGuard(hass, config, self._iteration_actions)
        self._peak_decider = PeakDecider(
            hass=hass,
            config=config,
            peak_tracker=self.peak_tracker,
            solar_tracker=self.solar_tracker,
            ev_guard=self._ev_guard_decider,
            iteration_actions=self._iteration_actions,
            save_fn=self.async_save,
            cascade=self.peak_cascade,
            snapshots=self._peak_snapshots,
        )
        self._injection_decider = InjectionDecider(
            hass=hass,
            config=config,
            peak_tracker=self.peak_tracker,
            solar_tracker=self.solar_tracker,
            ev_guard=self._ev_guard_decider,
            iteration_actions=self._iteration_actions,
            save_fn=self.async_save,
            cascade=self.inject_cascade,
            snapshots=self._inject_snapshots,
        )

    # ------------------------------------------------------------------ #
    #  Opslaan en laden                                                    #
    # ------------------------------------------------------------------ #

    async def async_load(self):
        data = await self._store.async_load()
        if data:
            self.peak_cascade   = [CascadeDevice(**d) for d in data.get("peak", [])]
            self.inject_cascade = [CascadeDevice(**d) for d in data.get("inject", [])]
            # Houd decider-cascades gesynchroniseerd na herladen vanuit opslag.
            self._peak_decider._cascade      = self.peak_cascade
            self._injection_decider._cascade = self.inject_cascade
            for k, v in data.get("peak_snapshots", {}).items():
                self._peak_snapshots[k] = DeviceSnapshot(**v)
            for k, v in data.get("inject_snapshots", {}).items():
                self._inject_snapshots[k] = DeviceSnapshot(**v)
            if self._peak_snapshots:
                _LOGGER.warning(
                    "Peak Guard: %d apparaat/apparaten nog uitgeschakeld uit vorige sessie — "
                    "worden hersteld zodra piekmarges het toelaten: %s",
                    len(self._peak_snapshots),
                    list(self._peak_snapshots.keys()),
                )

    async def async_save(self):
        await self._store.async_save({
            "peak":   [d.to_dict() for d in self.peak_cascade],
            "inject": [d.to_dict() for d in self.inject_cascade],
            "peak_snapshots":   {k: asdict(v) for k, v in self._peak_snapshots.items()},
            "inject_snapshots": {k: asdict(v) for k, v in self._inject_snapshots.items()},
        })

    # ------------------------------------------------------------------ #
    #  API data                                                            #
    # ------------------------------------------------------------------ #

    def to_dict(self) -> dict:
        return {
            "peak": [
                d.to_dict()
                for d in sorted(self.peak_cascade, key=lambda x: x.priority)
            ],
            "inject": [
                d.to_dict()
                for d in sorted(self.inject_cascade, key=lambda x: x.priority)
            ],
            "config": {
                "consumption_sensor": self.config.get(CONF_CONSUMPTION_SENSOR),
                "peak_sensor":        self.config.get(CONF_PEAK_SENSOR),
                "buffer_watts":       self.config.get(CONF_BUFFER_WATTS, DEFAULT_BUFFER_WATTS),
                "update_interval":    self.config.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL),
            },
            "status": {
                "monitoring":    self._monitoring,
                "last_loop_at":  self._last_loop_at,
                "interval_s":    max(float(self.config.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)), 60.0),
                "ev_guards": {
                    device_id: {
                        "state":          guard.state.value,
                        "history_len":    len(guard.surplus_history),
                        "pending_amps":   guard.pending_amps,
                        "last_sent_amps": int(guard.last_sent_amps) if guard.last_sent_amps is not None else None,
                    }
                    for device_id, guard in self._ev_guard_decider.guards.items()
                },
                "ev_rate_limiter": {
                    "calls_in_window": self._ev_guard_decider.rate_limiter.calls_in_window,
                    "remaining":       self._ev_guard_decider.rate_limiter.remaining,
                    "window_s":        EV_RATE_LIMIT_WINDOW_S,
                    "max_calls":       EV_RATE_LIMIT_MAX_CALLS,
                },
            },
        }

    def update_cascade(self, cascade_type: str, devices: list):
        parsed = [CascadeDevice(**d) for d in devices]
        if cascade_type == "peak":
            self.peak_cascade = parsed
            self._peak_decider._cascade = self.peak_cascade
        elif cascade_type == "inject":
            self.inject_cascade = parsed
            self._injection_decider._cascade = self.inject_cascade
        # Notificeer entity-platforms zodat nieuwe apparaten een entity krijgen
        for cb in self._entity_listeners:
            try:
                cb()
            except Exception:
                pass

    def register_entity_listener(self, callback) -> None:
        """Registreer een callback die wordt aangeroepen na elke cascade-update.

        Gebruikt door switch.py en number.py om dynamisch nieuwe entities
        aan te maken wanneer apparaten worden toegevoegd via de UI.
        """
        self._entity_listeners.append(callback)

    # ------------------------------------------------------------------ #
    #  Monitoring loop                                                     #
    # ------------------------------------------------------------------ #

    async def start_monitoring(self):
        self._monitoring = True
        self._task = self.hass.loop.create_task(self._monitor_loop())
        # Startup diagnostic: alleen configuratie loggen.
        # Sensorwaarden worden NIET gecontroleerd bij opstarten omdat HA-entities
        # bij het laden van de integratie nog in staat unknown/unavailable kunnen
        # zijn — ook al zijn ze in de UI al zichtbaar. De monitor-loop handelt
        # sensor-beschikbaarheid zelf af na de initiële opstart-vertraging.
        consumption_id = self.config.get(CONF_CONSUMPTION_SENSOR)
        peak_id = self.config.get(CONF_PEAK_SENSOR)
        _LOGGER.warning(
            "Peak Guard: monitoring gestart — "
            "verbruikssensor='%s', piek-sensor='%s', "
            "piek-cascade: %d apparaat/apparaten, inject-cascade: %d apparaat/apparaten",
            consumption_id,
            peak_id,
            len([d for d in self.peak_cascade if d.enabled]),
            len([d for d in self.inject_cascade if d.enabled]),
        )

    async def stop_monitoring(self):
        self._monitoring = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        _LOGGER.info("Peak Guard: monitoring gestopt")

    async def _monitor_loop(self):
        # ── CHANGED: default raised from 5 s → 60 s ──────────────────── #
        # The original DEFAULT_UPDATE_INTERVAL = 5 caused up to 12 loop
        # iterations per minute, each potentially generating EV API calls.
        # At 60 s we get at most 1 loop/min → ~98 % fewer potential calls
        # before any other guard kicks in.
        raw_interval = float(self.config.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL))
        interval = max(raw_interval, 60.0)   # never run faster than 60 s
        if raw_interval < 60.0:
            _LOGGER.warning(
                "Peak Guard: geconfigureerd update_interval (%.0f s) is te laag voor EV-beveiliging. "
                "Verhoogd naar 60 s.",
                raw_interval,
            )

        # Opstart-vertraging: HA-entities zijn bij het laden van de integratie
        # soms nog in staat unknown/unavailable. Na 10 s zijn ze normaal gezien
        # beschikbaar. Zo vermijden we valse "sensor niet beschikbaar"-warnings
        # in het logboek direct na het opstarten.
        _LOGGER.debug("Peak Guard: wacht 10 s op HA-opstart vóór eerste loop-iteratie")
        await asyncio.sleep(10.0)

        _sensor_unavailable_count = 0   # teller voor herhaalde warnings

        while self._monitoring:
            try:
                self._last_loop_at = datetime.now(timezone.utc).isoformat()
                # Reset iteration tracking voor beslissingslog.
                self._iteration_actions.clear()
                consumption = self._sensor_value(self.config.get(CONF_CONSUMPTION_SENSOR))
                if consumption is not None:
                    _sensor_unavailable_count = 0   # reset bij succesvolle lezing

                    # ── Pre-states voor beslissingslog ────────────────── #
                    _debug_logging = self.config.get(CONF_DEBUG_DECISION_LOGGING, False)
                    _pre_states: dict = {}
                    if _debug_logging:
                        for _d in self.peak_cascade + self.inject_cascade:
                            _s = self.hass.states.get(_d.entity_id)
                            _pre_states[_d.entity_id] = _s.state if _s else "?"

                    _LOGGER.debug(
                        "Peak Guard loop: verbruik=%.0f W — %s",
                        consumption,
                        "EXPORT (solar cascade actief)" if consumption < 0
                        else "import (piek-cascade actief)" if consumption > 0
                        else "nul",
                    )
                    await self._check_power_drop(consumption)
                    if consumption > 0:
                        await self._peak_decider.check(consumption)
                        await self._peak_decider.check_restore(consumption)
                        await self._injection_decider.check_restore(consumption)
                    elif consumption < 0:
                        # Negatief verbruik = export naar net (zonne-overschot)
                        _LOGGER.debug(
                            "Peak Guard: zonne-overschot gedetecteerd — sensor=%.0f W "
                            "(export %.0f W) — solar cascade wordt gecontroleerd",
                            consumption, abs(consumption),
                        )
                        await self._injection_decider.check(consumption)
                        await self._peak_decider.check_restore(consumption)
                        await self._injection_decider.check_restore(consumption)
                    else:
                        await self._peak_decider.check_restore(0.0)
                        await self._injection_decider.check_restore(0.0)

                    # ── Beslissingslog schrijven ──────────────────────── #
                    if _debug_logging:
                        await self._log_decision(consumption, _pre_states)

                    self._prev_consumption = consumption
                else:
                    sensor_id = self.config.get(CONF_CONSUMPTION_SENSOR)
                    _sensor_unavailable_count += 1
                    # Eerste keer: debug (kan normaal zijn bij opstart of korte onderbreking).
                    # Herhaaldelijk: warning zodat echte problemen zichtbaar blijven.
                    if _sensor_unavailable_count == 1:
                        _LOGGER.debug(
                            "Peak Guard: verbruikssensor '%s' nog niet beschikbaar — "
                            "loop overgeslagen (kan normaal zijn bij opstart)",
                            sensor_id,
                        )
                    elif _sensor_unavailable_count % 5 == 0:
                        _LOGGER.warning(
                            "Peak Guard: verbruikssensor '%s' al %d loop-iteraties niet beschikbaar — "
                            "controleer de sensor-configuratie",
                            sensor_id, _sensor_unavailable_count,
                        )
                    self._prev_consumption = None
            except Exception:
                _LOGGER.exception("Peak Guard: fout in monitoring loop")
            # Normaal wachten op interval, maar breek vroeg af als force_check gezet is
            self._force_check = False
            elapsed = 0.0
            while elapsed < interval and self._monitoring:
                await asyncio.sleep(1.0)
                elapsed += 1.0
                if self._force_check:
                    self._force_check = False
                    break


    async def _log_decision(
        self,
        consumption: float,
        pre_states: dict,
    ) -> None:
        """Schrijf een uitgebreide, mensleesbare beslissingslog naar
        /config/peak_guard_decisions.log.

        Wordt alleen aangeroepen als CONF_DEBUG_DECISION_LOGGING == True.
        File-I/O loopt via asyncio.to_thread zodat de event loop vrij blijft.
        """
        now_local = datetime.now()
        now_str = now_local.strftime("%Y-%m-%d %H:%M:%S")
        today_str = now_local.strftime("%Y-%m-%d")

        peak = self._sensor_value(self.config.get(CONF_PEAK_SENSOR))
        buffer_w = float(self.config.get(CONF_BUFFER_WATTS, DEFAULT_BUFFER_WATTS))
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
        enabled_peak = [d for d in self.peak_cascade if d.enabled]
        disabled_peak = [d for d in self.peak_cascade if not d.enabled]
        lines.append(
            f"\n=== Peak Cascade"
            f" ({len(enabled_peak)} actief, {len(disabled_peak)} uitgeschakeld) ==="
        )
        if not self.peak_cascade:
            lines.append("  (geen apparaten geconfigureerd)")
        else:
            for d in sorted(self.peak_cascade, key=lambda x: x.priority):
                pre = pre_states.get(d.entity_id, "?")
                cur_s = self.hass.states.get(d.entity_id)
                cur = cur_s.state if cur_s else "niet gevonden"
                managed = " ✓ beheerd door PeakGuard" if d.entity_id in self._peak_snapshots else ""
                disabled_tag = " [UITGESCHAKELD]" if not d.enabled else ""
                changed = " ← gewijzigd" if (pre != cur and pre != "?") else ""
                lines.append(
                    f"  [{d.priority}] {d.name} ({d.entity_id}){disabled_tag}"
                )
                lines.append(
                    f"       State: {pre} → {cur}{changed}{managed}"
                )
                lines.append(
                    f"       Type: {d.action_type}"
                    + (f" | Vermogen: {d.power_watts} W" if d.power_watts else "")
                )
                if d.action_type == ACTION_EV_CHARGER:
                    guard = self._ev_guard_decider.guards.get(d.id)
                    if guard:
                        lines.append(
                            f"       EV State: {guard.state.value}"
                            + (f" | Laadstroom: {guard.last_sent_amps:.1f} A"
                               if guard.last_sent_amps is not None else "")
                        )
                        lines.append(
                            f"       Rate limiter: "
                            f"{self._ev_guard_decider.rate_limiter.calls_in_window}"
                            f"/{EV_RATE_LIMIT_MAX_CALLS} calls"
                            f" in {EV_RATE_LIMIT_WINDOW_S:.0f}s"
                        )

        # ── Injection cascade ─────────────────────────────────────────── #
        enabled_inj = [d for d in self.inject_cascade if d.enabled]
        disabled_inj = [d for d in self.inject_cascade if not d.enabled]
        lines.append(
            f"\n=== Injection Cascade"
            f" ({len(enabled_inj)} actief, {len(disabled_inj)} uitgeschakeld) ==="
        )
        if not self.inject_cascade:
            lines.append("  (geen apparaten geconfigureerd)")
        else:
            for d in sorted(self.inject_cascade, key=lambda x: x.priority):
                pre = pre_states.get(d.entity_id, "?")
                cur_s = self.hass.states.get(d.entity_id)
                cur = cur_s.state if cur_s else "niet gevonden"
                managed = " ✓ beheerd door PeakGuard" if d.entity_id in self._inject_snapshots else ""
                disabled_tag = " [UITGESCHAKELD]" if not d.enabled else ""
                changed = " ← gewijzigd" if (pre != cur and pre != "?") else ""
                lines.append(
                    f"  [{d.priority}] {d.name} ({d.entity_id}){disabled_tag}"
                )
                lines.append(
                    f"       State: {pre} → {cur}{changed}{managed}"
                )
                lines.append(
                    f"       Type: {d.action_type}"
                    + (f" | Vermogen: {d.power_watts} W" if d.power_watts else "")
                )
                if d.action_type == ACTION_EV_CHARGER:
                    guard = self._ev_guard_decider.guards.get(d.id)
                    if guard:
                        surplus_vals = [
                            f"{w:.0f}"
                            for _, w in list(guard.surplus_history)[-5:]
                        ]
                        lines.append(
                            f"       EV State: {guard.state.value}"
                            + (f" | Laadstroom: {guard.last_sent_amps:.1f} A"
                               if guard.last_sent_amps is not None else "")
                        )
                        lines.append(
                            f"       Surplus history (laatste 5 samples): "
                            f"[{', '.join(surplus_vals) or '–'}] W"
                        )
                        # Debounce resterend
                        if guard.surplus_history:
                            oldest_ts = guard.surplus_history[0][0]
                            elapsed_s = (
                                datetime.now(timezone.utc) - oldest_ts
                            ).total_seconds()
                            remaining_s = max(0.0, EV_DEBOUNCE_STABLE_S - elapsed_s)
                            if guard.state == EVState.WAITING_FOR_STABLE:
                                lines.append(
                                    f"       Debounce resterend: {remaining_s:.0f}s"
                                    f" (van {EV_DEBOUNCE_STABLE_S:.0f}s vereist)"
                                )
                        # Min ON / OFF resterend
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
                                lines.append(
                                    f"       Min OFF resterend: {min_off_rem:.0f}s"
                                )
                        lines.append(
                            f"       Rate limiter: "
                            f"{self._ev_guard_decider.rate_limiter.calls_in_window}"
                            f"/{EV_RATE_LIMIT_MAX_CALLS} calls"
                            f" in {EV_RATE_LIMIT_WINDOW_S:.0f}s"
                            f" | Resterende ruimte: {self._ev_guard_decider.rate_limiter.remaining}"
                        )

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
            d for d in self.inject_cascade
            if d.entity_id in self._inject_snapshots
        ]
        if managed_solar:
            total_managed_w = 0.0
            for d in managed_solar:
                if d.action_type == ACTION_EV_CHARGER:
                    g = self._ev_guard_decider.guards.get(d.id)
                    if g and g.last_sent_amps is not None:
                        phases = int(d.ev_phases) if d.ev_phases else 1
                        v = EV_VOLTS_3PHASE if phases == 3 else EV_VOLTS_1PHASE
                        total_managed_w += g.last_sent_amps * v
                else:
                    total_managed_w += float(d.power_watts)
            interval_s = max(
                float(self.config.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)),
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
        log_path = self.hass.config.path("peak_guard_decisions.log")

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

    # ------------------------------------------------------------------ #
    #  Power-drop detectie — Hook 3                                        #
    # ------------------------------------------------------------------ #

    async def _check_power_drop(self, consumption: float) -> None:
        if self._prev_consumption is None:
            return

        active_ids = self.peak_tracker.get_active_ids()
        if not active_ids:
            return

        drop = self._prev_consumption - consumption
        all_peak_devices = {d.id: d for d in self.peak_cascade}

        now = datetime.now(timezone.utc)
        for device_id in list(active_ids):
            device = all_peak_devices.get(device_id)
            if device is None:
                continue

            nominal_w = float(device.power_watts)
            if nominal_w <= 0:
                # EV-laders hebben power_watts=0 omdat hun vermogen dynamisch is.
                # Het werkelijke vermogen bij uitschakelen is opgeslagen in de tracker.
                nominal_kw = self.peak_tracker.get_active_nominal_kw(device_id)
                if nominal_kw is None or nominal_kw <= 0:
                    continue
                nominal_w = nominal_kw * 1000.0

            tol_pct = float(self.config.get(
                CONF_POWER_DETECTION_TOLERANCE_PERCENT,
                DEFAULT_POWER_DETECTION_TOLERANCE_PERCENT,
            )) / 100.0
            tolerance = nominal_w * tol_pct
            if drop >= (nominal_w - tolerance):
                _LOGGER.info(
                    "Peak Guard: power-drop %.0f W gedetecteerd — naturlijke stop '%s' "
                    "(nominaal %.0f W, tolerantie %.0f W)",
                    drop, device.name, nominal_w, tolerance,
                )
                event = self.peak_tracker.complete_peak_calculation(
                    device_id=device_id, now=now
                )
                if event:
                    _LOGGER.info(
                        "Peak Guard: piek-event afgerond voor '%s' via power-drop — "
                        "duur=%.1f min, vermeden=%.3f kW, besparing=€%.4f",
                        device.name, event.measured_duration_min,
                        event.avoided_peak_kw, event.savings_euro,
                    )
                break

    # ------------------------------------------------------------------ #
    #  Hulpfuncties                                                        #
    # ------------------------------------------------------------------ #

    def _sensor_value(self, entity_id: Optional[str]) -> Optional[float]:
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable", ""):
            return None
        try:
            return float(state.state)
        except (ValueError, TypeError):
            return None
