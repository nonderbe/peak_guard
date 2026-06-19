"""
Peak Guard — deciders/ev_guard.py

Beheert de volledige EV-lader state machine, rate-limiting en debounce-logica.

Publieke API (aangeroepen vanuit BaseDecider / InjectionDecider):
  apply_action(device, excess, snapshots, cascade_type, peak_tracker, solar_tracker) -> float
  restore(device, snapshot, peak_tracker, solar_tracker) -> bool
  throttle_down_solar(device, consumption) -> bool

Properties (aangeroepen vanuit controller voor to_dict):
  guards      -> Dict[str, EVDeviceGuard]
  rate_limiter -> EVRateLimiter
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections import deque
from datetime import datetime, timedelta, timezone
from time import monotonic
from typing import TYPE_CHECKING, Dict, Optional

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from ..const import (
    DEFAULT_EV_MAX_AMPERE,
    DEFAULT_EV_MIN_AMPERE,
)
from .base import track_action
from ..models import (
    EVChargerDevice,
    DeviceSnapshot,
    EVDeviceGuard,
    EVRateLimiter,
    EVState,
    EV_DEBOUNCE_STABLE_S,
    EV_FLOOR_PERCENTILE,
    EV_HYSTERESIS_AMPS,
    EV_MIN_OFF_DURATION_S,
    EV_MIN_UPDATE_INTERVAL_S,
    EV_RATE_LIMIT_MAX_CALLS,
    EV_RATE_LIMIT_WINDOW_S,
    EV_CMD_MAX_RETRIES,
    EV_CMD_RETRY_DELAY_S,
    EV_SENSOR_STALE_S,
    EV_VOLTS_1PHASE,
    EV_VOLTS_3PHASE,
    EV_WAKE_COOLDOWN_S,
    EV_WAKE_TIMEOUT_S,
)

if TYPE_CHECKING:
    from ..avoided_peak_tracker import PeakAvoidTracker, SolarShiftTracker

_LOGGER = logging.getLogger(__name__)


class EVGuard:
    """
    Beheert alle per-apparaat EV-lader state, rate-limiting en debounce.

    Eén instantie gedeeld door PeakDecider en InjectionDecider.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        config: dict,
        iteration_actions: list,
    ) -> None:
        self.hass = hass
        self.config = config
        self._iteration_actions = iteration_actions   # gedeelde mutable lijst
        self._guards: Dict[str, EVDeviceGuard] = {}
        self._rate_limiter = EVRateLimiter()
        self._recent_warnings: deque = deque(maxlen=100)
        # Wordt gezet door apply_action; BaseDecider leest dit na de aanroep.
        self.last_skip_reason: str = ""
        # JSONL-logger — geïnjecteerd vanuit __init__.py na initialisatie.
        self._api_logger = None
        # Huidige call-context voor de JSONL-logger (gezet door publieke methoden).
        self._log_ctx: dict = {}

    # ------------------------------------------------------------------ #
    #  Properties voor controller.to_dict()                               #
    # ------------------------------------------------------------------ #

    @property
    def guards(self) -> Dict[str, EVDeviceGuard]:
        return self._guards

    @property
    def rate_limiter(self) -> EVRateLimiter:
        return self._rate_limiter

    def status_dict(self) -> dict:
        """Return serialisable guard status for the REST API / to_dict()."""
        now = datetime.now(timezone.utc)
        return {
            "ev_guards": {
                device_id: {
                    "state":               guard.state.value,
                    "history_len":         len(guard.surplus_history),
                    "history_secs":        (
                        now - guard.surplus_history[0][0]
                    ).total_seconds() if guard.surplus_history else 0.0,
                    "pending_amps":        guard.pending_amps,
                    "last_sent_amps":      int(guard.last_sent_amps) if guard.last_sent_amps is not None else None,
                    "skip_reason":         guard.skip_reason,
                    "turned_off_by_pg":    guard.turned_off_by_pg,
                    "min_off_remaining_s": max(0.0, EV_MIN_OFF_DURATION_S - (
                        now - guard.turned_off_at
                    ).total_seconds()) if (guard.turned_off_at and guard.turned_off_by_pg) else 0.0,
                    "wake_elapsed_s":      (
                        now - guard.wake_requested_at
                    ).total_seconds() if guard.wake_requested_at else 0.0,
                }
                for device_id, guard in self._guards.items()
            },
            "ev_rate_limiter": {
                "calls_in_window": self._rate_limiter.calls_in_window,
                "remaining":       self._rate_limiter.remaining,
                "window_s":        EV_RATE_LIMIT_WINDOW_S,
                "max_calls":       EV_RATE_LIMIT_MAX_CALLS,
            },
            "warnings": list(self._recent_warnings),
        }

    # ------------------------------------------------------------------ #
    #  Interne helpers                                                     #
    # ------------------------------------------------------------------ #

    def get_guard(self, device_id: str) -> EVDeviceGuard:
        """Geef de EVDeviceGuard voor device_id; maak aan als nog niet bestaat."""
        if device_id not in self._guards:
            self._guards[device_id] = EVDeviceGuard()
        return self._guards[device_id]

    def _reset_debounce(self, guard: EVDeviceGuard) -> None:
        """Wis de surplus-geschiedenis en reset de wallclock debounce-timer."""
        guard.surplus_history.clear()
        guard.debounce_start_at    = None
        guard.debounce_remaining_s = 0.0
        guard.debounce_floor_w     = 0.0

    def _rate_check(self, device_name: str, reason: str) -> bool:
        """
        Geeft True als een EV service call is toegestaan.
        Logt een warning en geeft False als de rate-limiter vol is.
        """
        if self._rate_limiter.is_allowed():
            return True
        self._warn(
            "Peak Guard EV '%s': service call OVERGESLAGEN wegens globale rate-limiter "
            "(%d/%d calls in %.0f s). Reden: %s",
            device_name,
            self._rate_limiter.calls_in_window,
            EV_RATE_LIMIT_MAX_CALLS,
            EV_RATE_LIMIT_WINDOW_S,
            reason,
        )
        return False

    def _record_call(self) -> None:
        """Registreer dat we zojuist een EV service call hebben gemaakt."""
        self._rate_limiter.record()

    def _track_action(self, entity_id: str, action: str, value=None) -> None:
        track_action(self.config, self._iteration_actions, entity_id, action, value)

    def _effective_current_amps(
        self,
        device: "EVChargerDevice",
        guard: "EVDeviceGuard",
        cur_state,
        now: datetime,
        current_a: Optional[float],
    ) -> Optional[float]:
        """Return current_a, substituting guard.last_sent_amps when the sensor is stale."""
        if cur_state is None or guard.last_sent_amps is None or current_a is None:
            return current_a
        last_upd = cur_state.last_updated
        if last_upd.tzinfo is None:
            last_upd = last_upd.replace(tzinfo=timezone.utc)
        age_s = (now - last_upd).total_seconds()
        if age_s > EV_SENSOR_STALE_S:
            _LOGGER.warning(
                "Peak Guard [SOLAR]: '%s' stroom-sensor stale (%.0f s oud) — "
                "eigen sturing (%.1f A) als referentie i.p.v. sensor (%.1f A)",
                device.name, age_s, guard.last_sent_amps, current_a,
            )
            return guard.last_sent_amps
        return current_a

    # ------------------------------------------------------------------ #
    #  GUI-waarschuwingsbuffer                                            #
    # ------------------------------------------------------------------ #

    def _warn(self, msg: str, *args) -> None:
        """Log een waarschuwing én sla hem op in de GUI-buffer."""
        _LOGGER.warning(msg, *args)
        try:
            self._recent_warnings.append({
                "ts": datetime.now(timezone.utc).isoformat(),
                "message": msg % args if args else msg,
            })
        except TypeError:
            self._recent_warnings.append({
                "ts": datetime.now(timezone.utc).isoformat(),
                "message": str(msg),
            })

    def add_warning(self, message: str) -> None:
        """Sla een waarschuwing op in de GUI-buffer (zonder opnieuw te loggen)."""
        self._recent_warnings.append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "message": message,
        })

    @property
    def recent_warnings(self) -> list:
        return list(self._recent_warnings)

    # ------------------------------------------------------------------ #
    #  JSONL API-logger                                                    #
    # ------------------------------------------------------------------ #

    def set_logger(self, logger) -> None:
        """Koppel de EVApiLogger (aangeroepen vanuit __init__.py na setup)."""
        self._api_logger = logger

    async def _svc(
        self,
        domain: str,
        service: str,
        data: dict,
        *,
        blocking: bool = True,
    ) -> None:
        """Wrapper rond hass.services.async_call die elke call naar JSONL logt."""
        start = monotonic()
        error_str: Optional[str] = None
        try:
            await self.hass.services.async_call(
                domain, service, data, blocking=blocking
            )
        except Exception as err:
            error_str = str(err)
            raise
        finally:
            if self._api_logger is not None:
                ctx = self._log_ctx
                try:
                    await self._api_logger.log(
                        device=ctx.get("device", ""),
                        service=f"{domain}.{service}",
                        entity=data.get("entity_id", ""),
                        value=data.get("value"),
                        cascade=ctx.get("cascade", ""),
                        surplus_w=ctx.get("surplus_w", 0.0),
                        success=error_str is None,
                        error=error_str,
                        duration_ms=(monotonic() - start) * 1000,
                    )
                except Exception:
                    pass  # logging mag EV-logica nooit onderbreken

    # ------------------------------------------------------------------ #
    #  Kabeldetectie                                                       #
    # ------------------------------------------------------------------ #

    def cable_connected(self, device: EVChargerDevice) -> bool:
        """
        Geeft True als de laadkabel aangesloten is, of als er geen kabelentity
        expliciet geconfigureerd is.

        Truthy-states: on, true, connected, charging, complete, fully_charged,
        pending, stopped, starting, nopower, 1 — of een numerieke waarde > 0.
        "stopped"/"starting"/"nopower" zijn Tesla-laderstaten waarbij de kabel
        WEL aangesloten is maar het laden tijdelijk gestopt/gepauzeerd is.
        Bij unavailable/unknown: True (niet blokkeren bij tijdelijke storing).
        """
        # Gebruik alleen de expliciet geconfigureerde kabelentity.
        # Geen fallback op DEFAULT_EV_CABLE_ENTITY: als de gebruiker geen kabel-
        # sensor heeft ingesteld, nemen we aan dat de kabel aangesloten is.
        cable_entity = device.cable_entity
        if not cable_entity:
            return True

        state = self.hass.states.get(cable_entity)
        if state is None:
            _LOGGER.debug(
                "Peak Guard EV: kabelentity '%s' niet gevonden voor '%s' — "
                "kabelcheck overgeslagen (aanname: aangesloten)",
                cable_entity, device.name,
            )
            return True

        s = state.state.lower().strip()
        if s in ("unavailable", "unknown", ""):
            return True

        # Truthy-states: kabel fysiek aangesloten (ook als opladen gepauzeerd is).
        # "stopped" / "starting" / "nopower" zijn Tesla-laderstaten waarbij
        # de kabel WEL aangesloten is maar het laden (tijdelijk) gestopt is.
        CABLE_ON = {"on", "true", "connected", "charging", "complete",
                    "fully_charged", "pending", "1",
                    "stopped", "starting", "nopower"}
        if s in CABLE_ON:
            return True

        try:
            return float(s) > 0
        except (ValueError, TypeError):
            pass

        return False

    # ------------------------------------------------------------------ #
    #  Verbindingsstatus (wake-up check)                                   #
    # ------------------------------------------------------------------ #

    def is_connected(self, device: EVChargerDevice) -> bool:
        """
        Geeft True als de EV verbonden/wakker is.

        States die als "verbonden/wakker" gelden: on, true, connected, online, home,
        charging, fully_charged, pending, 1.
        "complete" is bewust weggelaten: Tesla-auto's slapen vaak in die staat.
        "unavailable"/"unknown": als een wake-button geconfigureerd is, beschouwen we
        dit als slapend (False) zodat de wake-up getriggerd wordt. Zonder wake-button
        blokkeren we niet (True).
        Bij ontbrekende sensor: True (geen blokkering).
        """
        status_entity = device.status_sensor
        if not status_entity:
            return True

        state = self.hass.states.get(status_entity)
        if state is None:
            _LOGGER.debug(
                "Peak Guard EV: status-sensor '%s' niet gevonden voor '%s' — "
                "wake-up check overgeslagen (aanname: verbonden)",
                status_entity, device.name,
            )
            return True

        s = state.state.lower().strip()
        if s in ("unavailable", "unknown", ""):
            # Tesla-sensoren worden unavailable als de auto slaapt. Als er een
            # wake-button is, triggeren we de wake-up; anders blokkeren we niet.
            return not bool(device.wake_button)

        # "complete" = lading gestopt omdat limiet bereikt; auto is wakker en verbonden.
        # "off"     = Tesla binary sensor die aangeeft "niet aan het laden" — auto IS wakker.
        # Slapen → "unavailable"/"unknown" (Tesla integration gaat offline bij slaap).
        CONNECTED = {"on", "off", "true", "false", "connected", "online", "home",
                     "charging", "complete", "fully_charged", "pending", "1", "0"}
        if s in CONNECTED:
            return True

        try:
            return float(s) > 0
        except (ValueError, TypeError):
            pass

        return False

    # ------------------------------------------------------------------ #
    #  Locatie helper                                                      #
    # ------------------------------------------------------------------ #

    def is_home(
        self,
        device: EVChargerDevice,
        guard: "Optional[EVDeviceGuard]" = None,
    ) -> bool:
        """
        Geeft True als de EV thuis is, of als er geen locatie-tracker is.

        Ondersteunt device_tracker (home/not_home) en binary_sensor (on/off).

        Wanneer de tracker 'unknown'/'unavailable' rapporteert:
        - Als de guard een eerder bekende locatiestand heeft → gebruik die.
        - Als de locatie nooit werd waargenomen → return True (aanname: thuis;
          kabeldetectie is de primaire aanwezigheidscheck).
        Dit voorkomt dat 'unknown' bij een auto thuis het laden blokkeert,
        maar houdt de blokkering in stand als de auto op vakantie was en
        de API inslaapt (last_known_home=False).
        """
        tracker_entity = device.location_tracker
        if not tracker_entity:
            return True

        state = self.hass.states.get(tracker_entity)
        if state is None:
            _LOGGER.debug(
                "Peak Guard EV: locatie-tracker '%s' niet gevonden voor '%s' — "
                "locatiecheck overgeslagen (aanname: thuis)",
                tracker_entity, device.name,
            )
            return True

        s = state.state.lower().strip()

        if s not in ("unavailable", "unknown", ""):
            # Bekende staat: onthoud voor toekomstige 'unknown' cycli.
            result = s in ("home", "on", "true", "1")
            if guard is not None:
                guard.last_known_home = result
            return result

        # Locatie is onbekend (auto slaapt of integratie tijdelijk offline).
        if guard is not None and guard.last_known_home is not None:
            _LOGGER.debug(
                "Peak Guard EV: locatie-tracker '%s' rapporteert '%s' voor '%s' — "
                "terugval op laatste bekende staat: %s",
                tracker_entity, s, device.name,
                "thuis" if guard.last_known_home else "niet thuis",
            )
            return guard.last_known_home

        # Nooit een bekende locatie gezien (bv. tracker werkt nooit): aanname thuis.
        _LOGGER.debug(
            "Peak Guard EV: locatie-tracker '%s' rapporteert '%s' voor '%s' — "
            "nooit bekende locatie gezien; aanname thuis (kabeldetectie is primaire check)",
            tracker_entity, s, device.name,
        )
        return True

    # ------------------------------------------------------------------ #
    #  Dynamische ondergrens-detectie                                      #
    # ------------------------------------------------------------------ #

    def _surplus_floor(
        self,
        guard: EVDeviceGuard,
        current_surplus_w: float,
        now: datetime,
    ) -> tuple[bool, float]:
        """
        Bepaal de veilige ondergrens van het zonne-overschot uit de recente geschiedenis.

        Gebruikt een wallclock-timer (guard.debounce_start_at) in plaats van de tijdspanne
        tussen het oudste en nieuwste sample.  Voordeel: EV_DEBOUNCE_STABLE_S klopt exact
        ongeacht het loop-interval — bij een 60 s-interval wachtte de span-aanpak feitelijk
        één cyclus (60 s) in plaats van de ingestelde 20 s.

        Algoritme:
          1. Start de wallclock-timer op het eerste positieve sample.
          2. Voeg het huidige sample toe aan de ringbuffer.
          3. Bereken het EV_FLOOR_PERCENTILE-percentiel zodra er ≥ 2 samples zijn
             (ook vóór het einde van de debounce, voor GUI-preview in debounce_floor_w).
          4. Klaar als: timer ≥ EV_DEBOUNCE_STABLE_S ÉN ≥ 2 samples ÉN positieve floor.

        Bijwerkt altijd:
          guard.debounce_remaining_s  — resterende seconden (GUI-afteller)
          guard.debounce_floor_w      — tentatieve floor in W (GUI-preview)

        Returns:
            (ready, floor_w)
            ready   — True als debounce voltooid en floor positief.
            floor_w — veilige startwaarde in watt (0.0 als not ready).
        """
        # Wallclock-timer: gezet op het eerste sample, daarna ongewijzigd.
        if guard.debounce_start_at is None:
            guard.debounce_start_at = now

        guard.surplus_history.append((now, current_surplus_w))

        elapsed_s = (now - guard.debounce_start_at).total_seconds()
        guard.debounce_remaining_s = max(0.0, EV_DEBOUNCE_STABLE_S - elapsed_s)

        # Percentiel berekenen zodra er ≥ 2 samples zijn (ook als timer nog loopt).
        if len(guard.surplus_history) >= 2:
            values = [w for _, w in guard.surplus_history]
            sorted_vals = sorted(values)
            idx = max(0, min(int(len(sorted_vals) * EV_FLOOR_PERCENTILE / 100), len(sorted_vals) - 1))
            guard.debounce_floor_w = sorted_vals[idx]

        if elapsed_s < EV_DEBOUNCE_STABLE_S or len(guard.surplus_history) < 2:
            return False, 0.0

        floor_w = guard.debounce_floor_w
        if floor_w <= 0:
            return False, 0.0
        return True, floor_w

    # ------------------------------------------------------------------ #
    #  SOC-limiet override                                                 #
    # ------------------------------------------------------------------ #

    async def _set_soc_override(
        self,
        device: EVChargerDevice,
        override: bool,
        original_soc: Optional[float] = None,
    ) -> None:
        """Stel de SOC-limiet in (override=True) of herstel hem (override=False)."""
        if device.max_soc is None:
            _LOGGER.debug(
                "Peak Guard EV: '%s' SOC-limiet overgeslagen — ev_max_soc niet geconfigureerd",
                device.name,
            )
            return

        soc_entity = device.soc_entity
        if not soc_entity:
            self._warn(
                "Peak Guard EV: '%s' SOC-limiet NIET aangepast — "
                "geen soc_entity geconfigureerd in de wizard (stap 3: SoC-limiet entiteit)",
                device.name,
            )
            return

        if override:
            target_soc = float(device.max_soc)
            self._warn(
                "Peak Guard EV: '%s' SOC-limiet instellen op %.0f%% via '%s'",
                device.name, target_soc, soc_entity,
            )
        else:
            target_soc = float(original_soc) if original_soc is not None else 80.0
            if original_soc is None:
                self._warn(
                    "Peak Guard EV: '%s' originele laadlimiet was onbekend bij activering "
                    "(Tesla sliep of rapporteerde 'none') — hersteld naar standaard 80%%.",
                    device.name,
                )
            self._warn(
                "Peak Guard EV: '%s' SOC-limiet herstellen naar %.0f%% via '%s'",
                device.name, target_soc, soc_entity,
            )

        try:
            await self._svc(
                "number", "set_value",
                {"entity_id": soc_entity, "value": target_soc},
            )
        except Exception as err:
            _LOGGER.error(
                "Peak Guard EV: fout bij instellen SOC-limiet voor '%s' via '%s': %s",
                device.name, soc_entity, err,
            )

    # ------------------------------------------------------------------ #
    #  Herstel na Peak Guard ingreep                                       #
    # ------------------------------------------------------------------ #

    async def restore(
        self,
        device: EVChargerDevice,
        snapshot: DeviceSnapshot,
        peak_tracker: "PeakAvoidTracker",
        solar_tracker: "SolarShiftTracker",
        cascade_type: str = "peak",
        now: Optional[datetime] = None,
    ) -> bool:
        """
        Herstel EV Charger na een Peak Guard ingreep.

        PIEKBEPERKING (original_state == "on"):
          Zet schakelaar terug aan, herstel laadstroom, start duurmeting.

        INJECTIEPREVENTIE (original_state == "off"):
          Verwijder SOC-override, herstel laadstroom, zet schakelaar uit,
          voltooi solar duurmeting.
        """
        if now is None:
            now = datetime.now(timezone.utc)
        self._log_ctx = {"device": device.name, "cascade": cascade_type, "surplus_w": 0.0}
        try:
            sw_entity  = device.switch_entity or device.entity_id
            cur_entity = device.current_entity

            sw_state = self.hass.states.get(sw_entity)
            if sw_state is None:
                self._warn(
                    "Peak Guard EV: schakelaar '%s' niet gevonden bij herstel", sw_entity
                )
                return False

            # ---- PIEKBEPERKING: schakelaar was aan, nu uitgeschakeld ---- #
            if snapshot.original_state == "on":
                if sw_state.state != "on":
                    await self._svc("switch", "turn_on", {"entity_id": sw_entity})
                    _LOGGER.info(
                        "Peak Guard EV peak: '%s' terug ingeschakeld "
                        "(reden: herstel na piekbeperking)",
                        device.name,
                    )

                if cur_entity and snapshot.original_current is not None:
                    max_a = float(
                        device.max_value if device.max_value is not None
                        else DEFAULT_EV_MAX_AMPERE
                    )
                    orig_a = min(snapshot.original_current, max_a)
                    cur_state = self.hass.states.get(cur_entity)
                    if cur_state is not None:
                        try:
                            cur_val = float(cur_state.state)
                        except (ValueError, TypeError):
                            cur_val = None
                        if cur_val is None or round(cur_val, 0) != round(orig_a, 0):
                            await self._svc(
                                "number", "set_value",
                                {"entity_id": cur_entity, "value": round(orig_a, 1)},
                            )
                            _LOGGER.info(
                                "Peak Guard EV peak: '%s' laadstroom hersteld naar %.1f A "
                                "(reden: herstel na piekbeperking, gecapped aan max %.1f A)",
                                device.name, orig_a, max_a,
                            )

                if cascade_type == "peak":
                    peak_tracker.start_measurement_on_turnon(
                        device_id=device.id,
                        device_name=device.name,
                        ts=now,
                    )
                    guard = self.get_guard(device.id)
                    guard.state = EVState.CHARGING
                    guard.last_switch_state = True
                    guard.turned_on_at = now
                    self._reset_debounce(guard)
                elif cascade_type == "solar":
                    # Auto was al aan het laden voor PG ingreep (status-sensor fallback of
                    # schakelaar stond expliciet 'on'). SOC-override herstellen en solar
                    # tracking afsluiten, maar de auto NIET uitschakelen.
                    await self._set_soc_override(
                        device, override=False, original_soc=snapshot.original_soc
                    )
                    ev_event = solar_tracker.complete_solar_calculation(
                        device_id=device.id,
                        now=now,
                    )
                    if ev_event:
                        _LOGGER.info(
                            "Peak Guard EV solar: event afgerond voor '%s' — "
                            "duur=%.1f min, verschoven=%.4f kWh, besparing=€%.4f",
                            device.name, ev_event.measured_duration_min,
                            ev_event.shifted_kwh, ev_event.savings_euro,
                        )
                    guard = self.get_guard(device.id)
                    guard.state = EVState.IDLE
                    guard.last_switch_state = None
                    guard.soc_override_active = False
                    self._reset_debounce(guard)
                return True

            # ---- INJECTIEPREVENTIE: schakelaar was uit (of unknown), nu aangezet ---- #
            if snapshot.original_state in ("off", "unknown"):
                if sw_state.state != "off":
                    await self._set_soc_override(
                        device, override=False, original_soc=snapshot.original_soc
                    )

                    if cur_entity:
                        restore_a = (
                            snapshot.original_current
                            if snapshot.original_current is not None
                            else (device.min_value or DEFAULT_EV_MIN_AMPERE)
                        )
                        await self._svc(
                            "number", "set_value",
                            {"entity_id": cur_entity, "value": round(restore_a, 1)},
                        )
                        _LOGGER.info(
                            "Peak Guard EV solar: '%s' laadstroom hersteld naar %.1f A "
                            "(reden: herstel na injectiepreventie)",
                            device.name, restore_a,
                        )

                    await self._svc("switch", "turn_off", {"entity_id": sw_entity})
                    _LOGGER.info(
                        "Peak Guard EV solar: '%s' schakelaar uitgeschakeld "
                        "(reden: herstel na injectiepreventie)",
                        device.name,
                    )

                    ev_event = solar_tracker.complete_solar_calculation(
                        device_id=device.id,
                        now=now,
                    )
                    if ev_event:
                        _LOGGER.info(
                            "Peak Guard EV solar: event afgerond voor '%s' — "
                            "duur=%.1f min, verschoven=%.4f kWh, besparing=€%.4f",
                            device.name, ev_event.measured_duration_min,
                            ev_event.shifted_kwh, ev_event.savings_euro,
                        )

                    guard = self.get_guard(device.id)
                    guard.state = EVState.IDLE
                    guard.last_switch_state = None
                    guard.turned_off_at = now
                    guard.turned_off_by_pg = True
                    guard.soc_override_active = False
                    self._reset_debounce(guard)
                else:
                    # Schakelaar staat al uit (bijv. kabel eerder ontkoppeld).
                    # Herstel de SOC-limiet voor het geval dat nog niet gebeurde
                    # in de kabeldetectie-gate (bijv. geen ev_cable_entity geconfigureerd).
                    await self._set_soc_override(
                        device, override=False, original_soc=snapshot.original_soc
                    )
                    _LOGGER.debug(
                        "Peak Guard EV solar: '%s' schakelaar al uit bij herstel — "
                        "snapshot opgeruimd zonder service-call",
                        device.name,
                    )
                    solar_tracker.complete_solar_calculation(
                        device_id=device.id,
                        now=now,
                    )
                    guard = self.get_guard(device.id)
                    guard.state = EVState.IDLE
                    guard.last_switch_state = None
                    guard.soc_override_active = False
                    self._reset_debounce(guard)
                return True

        except HomeAssistantError as err:
            self._warn(
                "Peak Guard EV: '%s' offline of niet bereikbaar bij herstel — "
                "volgende cyclus opnieuw proberen (%s)",
                device.name, err,
            )
            return False
        except (ValueError, TypeError) as err:
            _LOGGER.error(
                "Peak Guard EV: fout bij herstellen '%s': %s",
                device.name, err,
            )
            return False

        return False

    # ------------------------------------------------------------------ #
    #  Zachte stop: laadstroom verlagen vóór volledige stop               #
    # ------------------------------------------------------------------ #

    async def throttle_down_solar(
        self,
        device: EVChargerDevice,
        consumption: float,
        now: Optional[datetime] = None,
    ) -> bool:
        """
        Verlaag de EV-laadstroom met het minimum dat nodig is om de grid-import
        te elimineren, zonder de EV te stoppen.

        Wordt aangeroepen vanuit InjectionDecider.check_restore() vóór de
        volledige restore (stop), zodat een dalend solar-overschot leidt tot
        lagere laadstroom in plaats van een abrupte stop.

        Returns:
            True  — stroom verlaagd, EV blijft laden (stop NIET uitvoeren).
            False — stroom staat al op hw-minimum of kan niet zinvol worden
                    verlaagd; aanroeper moet EV volledig stoppen.
        """
        self._log_ctx = {"device": device.name, "cascade": "solar", "surplus_w": -consumption}
        cur_entity = device.current_entity
        if not cur_entity:
            return False

        cur_state = self.hass.states.get(cur_entity)
        if cur_state is None:
            return False

        try:
            current_a = float(cur_state.state)
        except (ValueError, TypeError):
            return False

        phases  = int(device.phases) if device.phases else 1
        voltage = EV_VOLTS_3PHASE if phases == 3 else EV_VOLTS_1PHASE
        hw_min_a = float(
            device.min_current if device.min_current is not None
            else (device.min_value if device.min_value is not None else DEFAULT_EV_MIN_AMPERE)
        )
        max_a = float(device.max_value if device.max_value is not None else DEFAULT_EV_MAX_AMPERE)

        # Hoeveel ampère moeten we reduceren?
        # apply_action gebruikt ceil() bij het verhogen → EV mag tot 1 A meer trekken
        # dan het surplus dekt (bij 230 V is dat max ±230 W netimport).
        # throttle_down spiegelt dat: reduceer pas als het netverbruik méér dan 1 A
        # boven de balans uitkomt, zodat beide kanten dezelfde marge hanteren en de
        # EV niet oscilleert tussen twee waarden die elk 20 s duren.
        reduction_a = max(0, math.ceil((consumption - voltage) / voltage))
        new_a = max(int(hw_min_a), min(int(max_a), int(current_a) - reduction_a))

        if new_a >= int(current_a):
            # No meaningful reduction possible (solar still covers most of EV draw).
            # Keep the EV running as long as stopping it would cause injection again.
            # Rule: stop only when solar contributes nothing to the EV draw, i.e. when
            # consumption >= ev_draw_w — at that point even switching off the EV would
            # not create injection (the house alone already consumes at least ev_draw_w).
            ev_draw_w = current_a * voltage
            if consumption < ev_draw_w:
                return True   # solar still covers part of EV draw: keep charging
            return False      # solar gone entirely: allow restore to stop the EV

        guard = self.get_guard(device.id)
        if now is None:
            now = datetime.now(timezone.utc)

        current_a = self._effective_current_amps(device, guard, cur_state, now, current_a)

        # GATE: minimum update-interval — geef vorige commando tijd om effect te hebben.
        if guard.last_current_update is not None:
            elapsed = (now - guard.last_current_update).total_seconds()
            if elapsed < EV_MIN_UPDATE_INTERVAL_S:
                _LOGGER.debug(
                    "Peak Guard [SOLAR]: '%s' throttle_down OVERGESLAGEN wegens "
                    "update-interval (%.0f s geleden, minimum %.0f s) — EV blijft voorlopig laden",
                    device.name, elapsed, EV_MIN_UPDATE_INTERVAL_S,
                )
                return True  # interval nog actief: wacht, stop EV niet

        # GATE: global rate limiter
        if not self._rate_check(device.name, f"throttle_down {int(current_a)} → {new_a} A"):
            return True  # rate-limiter vol: probeer volgende cyclus, stop EV niet

        try:
            await self._svc(
                "number", "set_value",
                {"entity_id": cur_entity, "value": float(new_a)},
            )
        except HomeAssistantError as err:
            self._warn(
                "Peak Guard [SOLAR]: '%s' throttle_down %d A mislukt: %s — EV stoppen",
                device.name, new_a, err,
            )
            return False

        self._record_call()
        self._track_action(cur_entity, "number.set_value", float(new_a))
        guard.last_sent_amps      = float(new_a)
        guard.last_current_update = now
        guard.state               = EVState.CHARGING

        _LOGGER.info(
            "Peak Guard [SOLAR]: '%s' laadstroom verlaagd %d → %d A "
            "(grid-import %.0f W, reductie %d A, %d fase(n))",
            device.name, int(current_a), new_a, consumption, reduction_a, phases,
        )
        return True

    # ------------------------------------------------------------------ #
    #  Cascade-actie — dispatcher                                         #
    # ------------------------------------------------------------------ #

    async def apply_action(
        self,
        device: EVChargerDevice,
        excess: float,
        snapshots: dict,
        cascade_type: str,
        peak_tracker: "PeakAvoidTracker",
        solar_tracker: "SolarShiftTracker",
        now: Optional[datetime] = None,
    ) -> float:
        """Dispatch to _apply_peak or _apply_solar after shared setup."""
        self.last_skip_reason = ""
        self._log_ctx = {"device": device.name, "cascade": cascade_type, "surplus_w": excess}

        min_a   = float(device.min_value if device.min_value is not None else DEFAULT_EV_MIN_AMPERE)
        max_a   = float(device.max_value if device.max_value is not None else DEFAULT_EV_MAX_AMPERE)
        phases  = int(device.phases) if device.phases else 1
        voltage = EV_VOLTS_3PHASE if phases == 3 else EV_VOLTS_1PHASE

        sw_entity  = device.switch_entity or device.entity_id
        cur_entity = device.current_entity

        sw_state = self.hass.states.get(sw_entity)
        if sw_state is None:
            self._warn("Peak Guard EV: schakelaar '%s' niet gevonden", sw_entity)
            return excess

        sw_on = sw_state.state == "on"

        # Fallback: als de Tesla-schakelaar 'unknown'/'unavailable' rapporteert maar de
        # status-sensor aangeeft dat de wagen aan het laden is, behandel als 'aan'.
        # Dit voorkomt dat een onnodige turn_on gestuurd wordt aan een al ladende wagen
        # én dat het snapshot 'unknown' vastlegt als originele staat terwijl de auto actief was.
        if not sw_on and sw_state.state in ("unknown", "unavailable") and device.status_sensor:
            _st_fallback = self.hass.states.get(device.status_sensor)
            if _st_fallback is not None and _st_fallback.state not in ("unknown", "unavailable", ""):
                if _st_fallback.state in ("on", "charging", "connected", "complete"):
                    _LOGGER.debug(
                        "Peak Guard EV: schakelaar '%s' = '%s' maar status-sensor '%s' = '%s' — "
                        "behandeld als AAN (auto laadt al)",
                        sw_entity, sw_state.state, device.status_sensor, _st_fallback.state,
                    )
                    sw_on = True

        current_a: Optional[float] = None
        cur_state = None
        if cur_entity:
            cur_state = self.hass.states.get(cur_entity)
            if cur_state is not None:
                try:
                    current_a = float(cur_state.state)
                except (ValueError, TypeError):
                    current_a = None

        current_soc: Optional[float] = None
        if device.soc_entity:
            soc_state = self.hass.states.get(device.soc_entity)
            if soc_state is not None:
                try:
                    current_soc = float(soc_state.state)
                except (ValueError, TypeError):
                    current_soc = None

        snap_key = device.entity_id
        if snap_key not in snapshots:
            # Gebruik de effectieve staat: als de status-sensor aangeeft dat de auto
            # laadt terwijl de schakelaar 'unknown' meldt, sla 'on' op zodat restore
            # weet dat de auto al actief was en niet uitgeschakeld moet worden.
            snapshots[snap_key] = DeviceSnapshot(
                entity_id=snap_key,
                original_state="on" if sw_on else sw_state.state,
                original_current=current_a,
                original_soc=current_soc,
            )

        if now is None:
            now = datetime.now(timezone.utc)
        guard = self.get_guard(device.id)
        guard.skip_reason = ""

        if cascade_type == "peak":
            return await self._apply_peak(
                device, excess, snapshots, peak_tracker,
                sw_entity, cur_entity, sw_on, current_a,
                voltage, min_a, max_a, phases, now, guard,
            )

        # ── Solar pre-setup (before _apply_solar) ──────────────────── #
        # SOC-override: apply as soon as snapshot is created so that a
        # Tesla stopped at its charge limit (but switch still "on") will
        # resume.  Must happen before the solar path reads guard state.
        if device.max_soc is not None and not guard.soc_override_active:
            if self._rate_check(device.name, "SOC-override activeren"):
                await self._set_soc_override(device, override=True)
                self._record_call()
                guard.soc_override_active = True

        current_a = self._effective_current_amps(device, guard, cur_state, now, current_a)

        hw_min_a = float(
            device.min_current if device.min_current is not None
            else (device.min_value if device.min_value is not None else DEFAULT_EV_MIN_AMPERE)
        )
        return await self._apply_solar(
            device, excess, snapshots, peak_tracker, solar_tracker,
            sw_entity, cur_entity, sw_on, current_a, cur_state,
            voltage, hw_min_a, max_a, phases, now, guard,
        )

    # ------------------------------------------------------------------ #
    #  Peak-cascade pad                                                    #
    # ------------------------------------------------------------------ #

    async def _apply_peak(
        self,
        device: EVChargerDevice,
        excess: float,
        snapshots: dict,
        peak_tracker: "PeakAvoidTracker",
        sw_entity: str,
        cur_entity: Optional[str],
        sw_on: bool,
        current_a: Optional[float],
        voltage: float,
        min_a: float,
        max_a: float,
        phases: int,
        now: datetime,
        guard: "EVDeviceGuard",
    ) -> float:
        """Peak-limiting path: floor charge current; turn off when below minimum."""
        if not sw_on:
            _LOGGER.debug(
                "Peak Guard EV peak: '%s' overgeslagen — EV laadt niet", device.name
            )
            return excess

        eff_current_a    = current_a if current_a is not None else max_a
        current_w        = eff_current_a * voltage
        needed_reduction_w = min(excess, current_w)
        target_a_raw     = (current_w - needed_reduction_w) / voltage
        new_a            = max(0, min(int(max_a), math.floor(target_a_raw)))

        if new_a < min_a:
            # Below minimum → turn the charger off entirely.

            if guard.last_switch_state is False:
                _LOGGER.debug(
                    "Peak Guard EV peak: '%s' uitschakelen OVERGESLAGEN — "
                    "schakelaar al uit (redundant call vermeden)",
                    device.name,
                )
                return excess - current_w

            if not self._rate_check(device.name, "turn_off voor piekbeperking"):
                return excess

            turn_off_ok = False
            _last_err: Optional[HomeAssistantError] = None
            for _attempt in range(EV_CMD_MAX_RETRIES + 1):
                try:
                    await self._svc("switch", "turn_off", {"entity_id": sw_entity})
                    turn_off_ok = True
                    break
                except HomeAssistantError as ha_err:
                    _last_err = ha_err
                    if _attempt < EV_CMD_MAX_RETRIES:
                        self._warn(
                            "Peak Guard EV peak: '%s' turn_off mislukt "
                            "(poging %d/%d): %s — over %.0f s opnieuw proberen",
                            device.name, _attempt + 1, EV_CMD_MAX_RETRIES + 1,
                            ha_err, EV_CMD_RETRY_DELAY_S,
                        )
                        await asyncio.sleep(EV_CMD_RETRY_DELAY_S)

            if not turn_off_ok:
                self._warn(
                    "Peak Guard EV peak: '%s' turn_off definitief mislukt na %d pogingen: %s "
                    "— piekbeperking niet uitgevoerd, volgende cyclus opnieuw proberen",
                    device.name, EV_CMD_MAX_RETRIES + 1, _last_err,
                )
                return excess

            self._record_call()
            self._track_action(sw_entity, "switch.turn_off")

            if cur_entity and current_a is not None:
                if self._rate_check(device.name, "set_value min_a na turn_off"):
                    try:
                        await self._svc(
                            "number", "set_value",
                            {"entity_id": cur_entity, "value": min_a},
                        )
                        self._record_call()
                        self._track_action(cur_entity, "number.set_value", min_a)
                    except HomeAssistantError as err:
                        self._warn(
                            "Peak Guard EV peak: '%s' set_value min_a mislukt na turn_off: %s",
                            device.name, err,
                        )

            _LOGGER.info(
                "Peak Guard EV peak: '%s' uitgeschakeld "
                "(%.1f A < min %.1f A, verlaging %.0f W, %d fase(n), "
                "rate-limiter: %d/%d calls in %.0f s)",
                device.name, target_a_raw, min_a, current_w, phases,
                self._rate_limiter.calls_in_window,
                EV_RATE_LIMIT_MAX_CALLS, EV_RATE_LIMIT_WINDOW_S,
            )
            guard.state           = EVState.IDLE
            guard.last_switch_state = False
            guard.turned_off_at   = now
            guard.turned_off_by_pg = True
            guard.last_sent_amps  = min_a
            self._reset_debounce(guard)

            peak_tracker.record_pending_avoid(
                device_id=device.id,
                device_name=device.name,
                nominal_kw=current_w / 1000.0,
                ts=now,
            )
            return excess - current_w

        # Still above minimum → reduce charge current to floor.
        actual_reduction_w = (eff_current_a - new_a) * voltage

        if cur_entity is None:
            return excess - actual_reduction_w

        if new_a == int(eff_current_a):
            _LOGGER.debug(
                "Peak Guard EV peak: '%s' set_value OVERGESLAGEN — "
                "laadstroom al op %d A (redundant call vermeden)",
                device.name, new_a,
            )
            return excess - actual_reduction_w

        if (
            guard.last_sent_amps is not None
            and abs(new_a - guard.last_sent_amps) < EV_HYSTERESIS_AMPS
        ):
            _LOGGER.debug(
                "Peak Guard EV peak: '%s' set_value OVERGESLAGEN wegens hysteresis "
                "(%d A → %d A, delta=%.1f A < %.1f A drempel)",
                device.name, int(guard.last_sent_amps), new_a,
                abs(new_a - guard.last_sent_amps), EV_HYSTERESIS_AMPS,
            )
            return excess - actual_reduction_w

        if guard.last_current_update is not None:
            elapsed = (now - guard.last_current_update).total_seconds()
            if elapsed < EV_MIN_UPDATE_INTERVAL_S:
                _LOGGER.debug(
                    "Peak Guard EV peak: '%s' set_value OVERGESLAGEN wegens "
                    "update-interval (%.0f s geleden, minimum %.0f s)",
                    device.name, elapsed, EV_MIN_UPDATE_INTERVAL_S,
                )
                return excess - actual_reduction_w

        if not self._rate_check(
            device.name, f"set_value peak {int(eff_current_a)} → {new_a} A"
        ):
            return excess - actual_reduction_w

        try:
            await self._svc(
                "number", "set_value",
                {"entity_id": cur_entity, "value": float(new_a)},
            )
        except HomeAssistantError as err:
            self._warn(
                "Peak Guard EV peak: '%s' set_value %d A mislukt: %s",
                device.name, new_a, err,
            )
            return excess - actual_reduction_w

        self._record_call()
        self._track_action(cur_entity, "number.set_value", float(new_a))
        _LOGGER.info(
            "Peak Guard EV peak: '%s' laadstroom %d → %d A "
            "(floor van %.2f A, verlaging %.0f W, %d fase(n), "
            "rate-limiter: %d/%d calls in %.0f s)",
            device.name, int(eff_current_a), new_a,
            target_a_raw, actual_reduction_w, phases,
            self._rate_limiter.calls_in_window,
            EV_RATE_LIMIT_MAX_CALLS, EV_RATE_LIMIT_WINDOW_S,
        )
        guard.last_sent_amps      = float(new_a)
        guard.last_current_update = now
        guard.state               = EVState.CHARGING
        return excess - actual_reduction_w

    # ------------------------------------------------------------------ #
    #  Solar-cascade pad                                                   #
    # ------------------------------------------------------------------ #

    async def _apply_solar(
        self,
        device: EVChargerDevice,
        excess: float,
        snapshots: dict,
        peak_tracker: "PeakAvoidTracker",
        solar_tracker: "SolarShiftTracker",
        sw_entity: str,
        cur_entity: Optional[str],
        sw_on: bool,
        current_a: Optional[float],
        cur_state,
        voltage: float,
        hw_min_a: float,
        max_a: float,
        phases: int,
        now: datetime,
        guard: "EVDeviceGuard",
    ) -> float:
        """Injection-prevention path: set charge current to match solar surplus."""
        snap_key  = device.entity_id
        hw_min_w  = hw_min_a * voltage
        available_a_raw = excess / voltage
        new_a     = max(int(hw_min_a), min(int(max_a), math.ceil(available_a_raw)))
        guard.pending_amps = new_a

        _LOGGER.debug(
            "Peak Guard [SOLAR]: '%s' evalueren — "
            "overschot=%.0f W, spanning=%dV (%d fase(n)), "
            "beschikbaar=%.2f A, doel=%d A (hw-min=%.0f A=%.0f W, max=%d A), "
            "schakelaar=%s, laadstroom=%s A, guard-state=%s",
            device.name, excess, int(voltage), phases,
            available_a_raw, new_a, hw_min_a, hw_min_w, int(max_a),
            "AAN" if sw_on else "UIT",
            f"{current_a:.1f}" if current_a is not None else "onbekend",
            guard.state.value,
        )

        # GATE: cable detection
        cable_entity = device.cable_entity
        if not self.cable_connected(device):
            if sw_on:
                _cable_st = self.hass.states.get(cable_entity)
                self._warn(
                    "Peak Guard [SOLAR]: '%s' — laadkabel ontkoppeld TIJDENS het laden "
                    "('%s' = '%s') — schakelaar uitzetten",
                    device.name, cable_entity,
                    _cable_st.state if _cable_st else "??",
                )
                if self._rate_check(device.name, "turn_off kabel ontkoppeld"):
                    try:
                        await self._svc("switch", "turn_off", {"entity_id": sw_entity})
                        self._record_call()
                        self._track_action(sw_entity, "switch.turn_off")
                    except HomeAssistantError as err:
                        self._warn(
                            "Peak Guard [SOLAR]: '%s' — turn_off na kabelontkoppeling mislukt: %s",
                            device.name, err,
                        )
                snap = snapshots.get(snap_key)
                if snap is not None:
                    await self._set_soc_override(device, override=False, original_soc=snap.original_soc)
                guard.state             = EVState.CABLE_DISCONNECTED
                guard.last_switch_state = False
                guard.soc_override_active = False
                self._reset_debounce(guard)
                guard.skip_reason       = f"laadkabel ontkoppeld tijdens laden ({cable_entity})"
                self.last_skip_reason   = guard.skip_reason
            elif guard.state != EVState.CABLE_DISCONNECTED:
                guard.state           = EVState.CABLE_DISCONNECTED
                guard.skip_reason     = f"laadkabel niet aangesloten ({cable_entity})"
                self.last_skip_reason = guard.skip_reason
                _cable_st2 = self.hass.states.get(cable_entity)
                self._warn(
                    "Peak Guard [SOLAR]: '%s' — laadkabel NIET aangesloten "
                    "('%s' = '%s') — laden geblokkeerd",
                    device.name, cable_entity,
                    _cable_st2.state if _cable_st2 else "??",
                )
                snap = snapshots.get(snap_key)
                if snap is not None and snap.original_state == "off":
                    _LOGGER.info(
                        "Peak Guard [SOLAR]: '%s' — kabel ontkoppeld, SOC-override herstellen "
                        "naar %.0f%%",
                        device.name, snap.original_soc if snap.original_soc is not None else 100.0,
                    )
                    await self._set_soc_override(
                        device, override=False, original_soc=snap.original_soc
                    )
                    guard.soc_override_active = False
            else:
                guard.skip_reason     = f"laadkabel niet aangesloten ({cable_entity})"
                self.last_skip_reason = guard.skip_reason
                _LOGGER.debug(
                    "Peak Guard [SOLAR]: '%s' — kabel nog steeds niet aangesloten, wachten",
                    device.name,
                )
            return excess

        if guard.state == EVState.CABLE_DISCONNECTED:
            _LOGGER.info(
                "Peak Guard [SOLAR]: '%s' — laadkabel nu aangesloten — doorgang hersteld",
                device.name,
            )
            guard.state = EVState.IDLE
            self._reset_debounce(guard)

        # GATE: surplus present?
        if excess <= 0:
            if sw_on:
                _LOGGER.debug(
                    "Peak Guard [SOLAR]: '%s' draait — surplus %.0f W ≤ 0 "
                    "→ doorladen (stop enkel als geen injectie meer)",
                    device.name, excess,
                )
            else:
                self._reset_debounce(guard)
                guard.skip_reason     = f"geen injectie (surplus {excess:.0f} W)"
                self.last_skip_reason = guard.skip_reason
                _LOGGER.debug(
                    "Peak Guard [SOLAR]: '%s' staat uit — geen injectie (%.0f W) → geen actie",
                    device.name, excess,
                )
                return excess

        # GATE: EV must be home
        if not self.is_home(device, guard):
            if guard.state != EVState.IDLE:
                loc_st = self.hass.states.get(device.location_tracker) if device.location_tracker else None
                _LOGGER.info(
                    "Peak Guard [SOLAR]: '%s' niet thuis (tracker='%s', staat='%s') — "
                    "toestand gereset naar IDLE",
                    device.name, device.location_tracker or "(geen)",
                    loc_st.state if loc_st else "onbekend",
                )
                guard.state = EVState.IDLE
                self._reset_debounce(guard)
            else:
                _LOGGER.debug(
                    "Peak Guard [SOLAR]: '%s' niet thuis — laden overgeslagen", device.name,
                )
            guard.skip_reason     = "EV niet thuis"
            self.last_skip_reason = guard.skip_reason
            return excess

        # Detect manual start
        if sw_on and guard.last_switch_state is not True:
            _LOGGER.info(
                "Peak Guard [SOLAR]: '%s' — handmatige start gedetecteerd — "
                "toestand overgenomen (turned_off_at gereset, turned_off_by_pg=False)",
                device.name,
            )
            guard.state           = EVState.CHARGING
            guard.last_switch_state = True
            guard.turned_on_at    = now
            guard.turned_off_at   = None
            guard.turned_off_by_pg = False
            self._reset_debounce(guard)
            nominal_w = (current_a * voltage) if current_a is not None else (hw_min_a * voltage)
            solar_tracker.start_solar_measurement(
                device_id=device.id,
                device_name=device.name,
                nominal_kw=nominal_w / 1000.0,
                ts=now,
            )

        if not sw_on:
            # EV off → turn on at hardware minimum
            guard.pending_amps = int(hw_min_a)
            new_a = int(hw_min_a)

            if guard.turned_off_at is not None and guard.turned_off_by_pg:
                off_secs = (now - guard.turned_off_at).total_seconds()
                if off_secs < EV_MIN_OFF_DURATION_S:
                    guard.skip_reason = (
                        f"min OFF-duur: {off_secs:.0f}s < {EV_MIN_OFF_DURATION_S:.0f}s"
                    )
                    self.last_skip_reason = guard.skip_reason
                    _LOGGER.info(
                        "Peak Guard [SOLAR]: '%s' aanzetten OVERGESLAGEN — "
                        "te kort geleden automatisch uitgeschakeld "
                        "(%.0f s geleden, minimum %.0f s)",
                        device.name, off_secs, EV_MIN_OFF_DURATION_S,
                    )
                    return excess

            start_thr_w = float(device.start_threshold_w) if device.start_threshold_w is not None else hw_min_w
            if excess < start_thr_w:
                self._reset_debounce(guard)
                guard.skip_reason = f"surplus {excess:.0f} W < start-drempel {start_thr_w:.0f} W"
                self.last_skip_reason = guard.skip_reason
                return excess

            # Debounce: wacht op stabiel surplus voor EV te starten.
            _ready, _floor_w = self._surplus_floor(guard, excess, now)
            if not _ready:
                guard.state = EVState.WAITING_FOR_STABLE
                if guard.debounce_floor_w > 0:
                    floor_a = math.ceil(guard.debounce_floor_w / voltage)
                    guard.skip_reason = (
                        f"debounce wachten op stabiel surplus "
                        f"(floor: {floor_a} A, {guard.debounce_remaining_s:.0f}s resterend)"
                    )
                else:
                    guard.skip_reason = (
                        f"debounce wachten op stabiel surplus ({guard.debounce_remaining_s:.0f}s resterend)"
                    )
                self.last_skip_reason = guard.skip_reason
                return excess

            if guard.last_switch_state is True:
                _LOGGER.info(
                    "Peak Guard [SOLAR]: '%s' — schakelaar nog steeds UIT na eerder "
                    "turn_on commando (Tesla API bevestigt ontvangst maar niet uitvoering) "
                    "— opnieuw proberen",
                    device.name,
                )
                guard.last_switch_state = None
                guard.state = EVState.IDLE

            if guard.last_switch_state is not True:
                if guard.wake_cooldown_until is not None and now < guard.wake_cooldown_until:
                    remaining_s = (guard.wake_cooldown_until - now).total_seconds()
                    guard.skip_reason     = f"wake-up cooldown ({remaining_s:.0f}s resterend)"
                    self.last_skip_reason = guard.skip_reason
                    _LOGGER.debug(
                        "Peak Guard [SOLAR]: '%s' wake-up OVERGESLAGEN — cooldown actief "
                        "(%.0f s resterend na mislukte wake-poging)",
                        device.name, remaining_s,
                    )
                    return excess

                if device.wake_button and not self.is_connected(device):
                    # State-machine wake-up: press once, then poll on subsequent
                    # loop iterations (triggered early by the status-sensor
                    # state-change listener).  Never blocks the event loop.
                    status_entity = device.status_sensor or "(geen sensor)"
                    status_val    = "onbekend"
                    if device.status_sensor:
                        _st = self.hass.states.get(device.status_sensor)
                        status_val = _st.state if _st else "niet gevonden"

                    if guard.state == EVState.SLEEPING and guard.wake_requested_at is not None:
                        elapsed = (now - guard.wake_requested_at).total_seconds()
                        if elapsed <= EV_WAKE_TIMEOUT_S:
                            guard.skip_reason = (
                                f"wachten op Tesla wake-up "
                                f"({elapsed:.0f}s/{EV_WAKE_TIMEOUT_S:.0f}s)"
                            )
                            self.last_skip_reason = guard.skip_reason
                            _LOGGER.debug(
                                "Peak Guard [SOLAR]: '%s' — wachten op Tesla wake-up "
                                "(%.0f s verstreken van %.0f s timeout)",
                                device.name, elapsed, EV_WAKE_TIMEOUT_S,
                            )
                            return excess
                        # Timeout elapsed without the car waking up.
                        guard.state               = EVState.IDLE
                        guard.wake_requested_at   = None
                        guard.wake_cooldown_until = now + timedelta(seconds=EV_WAKE_COOLDOWN_S)
                        guard.skip_reason = (
                            f"Tesla niet wakker na wake-up poging "
                            f"(volgende poging over {EV_WAKE_COOLDOWN_S:.0f}s)"
                        )
                        self.last_skip_reason = guard.skip_reason
                        self._warn(
                            "Peak Guard [SOLAR]: '%s' — Tesla niet wakker na %.0f s "
                            "('%s' = '%s') — laden uitgesteld, volgende wake-poging over %.0f s",
                            device.name, EV_WAKE_TIMEOUT_S, status_entity, status_val,
                            EV_WAKE_COOLDOWN_S,
                        )
                        return excess

                    # First attempt: press wake button and defer to next iteration.
                    _LOGGER.info(
                        "Peak Guard [SOLAR]: '%s' — Tesla in slaapstand "
                        "('%s' = '%s') → wake button '%s' aanroepen",
                        device.name, status_entity, status_val, device.wake_button,
                    )
                    try:
                        await self._svc(
                            "button", "press",
                            {"entity_id": device.wake_button},
                            blocking=False,
                        )
                    except Exception as wake_err:
                        self._warn(
                            "Peak Guard [SOLAR]: '%s' — wake-up aanroep mislukt: %s",
                            device.name, wake_err,
                        )
                    guard.state             = EVState.SLEEPING
                    guard.wake_requested_at = now
                    guard.skip_reason       = "wake-up verstuurd, wachten op Tesla"
                    self.last_skip_reason   = guard.skip_reason
                    return excess

                if guard.state == EVState.SLEEPING:
                    # Car woke up (is_connected() became True) — reset state.
                    _LOGGER.info(
                        "Peak Guard [SOLAR]: '%s' — Tesla nu wakker → laden starten met %d A",
                        device.name, new_a,
                    )
                    guard.state             = EVState.IDLE
                    guard.wake_requested_at = None
                    guard.wake_cooldown_until = None

                if not self._rate_check(device.name, "turn_on voor injectiepreventie"):
                    return excess

                # If Tesla was asleep when the snapshot was made (original_soc=None),
                # force a fresh poll so we can restore correctly later.
                _snap_now = snapshots.get(snap_key)
                if device.soc_entity and _snap_now is not None and _snap_now.original_soc is None:
                    try:
                        await self._svc(
                            "homeassistant", "update_entity",
                            {"entity_id": device.soc_entity},
                        )
                    except Exception:
                        pass
                    _soc_st = self.hass.states.get(device.soc_entity)
                    if _soc_st is not None:
                        try:
                            _snap_now.original_soc = float(_soc_st.state)
                            _LOGGER.info(
                                "Peak Guard EV: '%s' originele laadlimiet uitgelezen = %.0f%%"
                                " (na herbevraging)",
                                device.name, _snap_now.original_soc,
                            )
                        except (ValueError, TypeError):
                            self._warn(
                                "Peak Guard EV: '%s' laadlimiet onleesbaar ('%s') na herbevraging"
                                " — SOC-override wordt toegepast maar kan bij herstel niet worden"
                                " hersteld. Controleer manueel de laadlimiet in de Tesla-app.",
                                device.name, _soc_st.state,
                            )

                # SOC-override fallback in case rate-limiter blocked it earlier
                if not guard.soc_override_active and device.max_soc is not None:
                    await self._set_soc_override(device, override=True)
                    guard.soc_override_active = True

                turn_on_ok = False
                _last_err: Optional[HomeAssistantError] = None
                for _attempt in range(EV_CMD_MAX_RETRIES + 1):
                    try:
                        await self._svc("switch", "turn_on", {"entity_id": sw_entity})
                        turn_on_ok = True
                        break
                    except HomeAssistantError as ha_err:
                        _last_err = ha_err
                        if _attempt < EV_CMD_MAX_RETRIES:
                            self._warn(
                                "Peak Guard [SOLAR]: '%s' turn_on mislukt "
                                "(poging %d/%d): %s — over %.0f s opnieuw proberen",
                                device.name, _attempt + 1, EV_CMD_MAX_RETRIES + 1,
                                ha_err, EV_CMD_RETRY_DELAY_S,
                            )
                            await asyncio.sleep(EV_CMD_RETRY_DELAY_S)

                if not turn_on_ok:
                    self._warn(
                        "Peak Guard [SOLAR]: '%s' turn_on definitief mislukt na %d pogingen: %s "
                        "— SOC herstellen naar originele waarde en volgende cyclus opnieuw proberen",
                        device.name, EV_CMD_MAX_RETRIES + 1, _last_err,
                    )
                    await self._set_soc_override(
                        device, override=False,
                        original_soc=_snap_now.original_soc if _snap_now else None,
                    )
                    return excess

                self._record_call()
                self._track_action(sw_entity, "switch.turn_on")
                guard.state           = EVState.CHARGING
                guard.skip_reason     = ""
                guard.last_switch_state = True
                guard.turned_on_at    = now
                self._reset_debounce(guard)

                if cur_entity:
                    if self._rate_check(device.name, f"set_value {new_a} A bij turn_on"):
                        try:
                            await self._svc(
                                "number", "set_value",
                                {"entity_id": cur_entity, "value": float(new_a)},
                            )
                            self._record_call()
                            self._track_action(cur_entity, "number.set_value", float(new_a))
                            guard.last_sent_amps      = float(new_a)
                            guard.last_current_update = now
                        except HomeAssistantError as err:
                            self._warn(
                                "Peak Guard [SOLAR]: '%s' set_value %d A mislukt bij turn_on: %s "
                                "— laden gestart, stroominstelling volgende cyclus opnieuw",
                                device.name, new_a, err,
                            )

                actual_consumption_w = new_a * voltage
                _LOGGER.info(
                    "Peak Guard [SOLAR]: → '%s' gestart met %d A (%.0f W) "
                    "omdat injectie %.0f W > 0 "
                    "(hw-min=%.0f A, %d fase(n), SOC-override: %s%%, "
                    "rate-limiter: %d/%d calls in %.0f s)",
                    device.name, new_a, actual_consumption_w,
                    excess, hw_min_a, phases,
                    device.max_soc if device.max_soc is not None else "n.v.t.",
                    self._rate_limiter.calls_in_window,
                    EV_RATE_LIMIT_MAX_CALLS, EV_RATE_LIMIT_WINDOW_S,
                )
                solar_tracker.start_solar_measurement(
                    device_id=device.id,
                    device_name=device.name,
                    nominal_kw=actual_consumption_w / 1000.0,
                    ts=now,
                )
                return excess - actual_consumption_w

        # EV already on → adjust current to match available surplus
        if current_a is not None:
            total_for_ev_w  = current_a * voltage + excess
            available_a_raw = total_for_ev_w / voltage
            new_a = max(int(hw_min_a), min(int(max_a), math.ceil(available_a_raw)))
            _LOGGER.debug(
                "Peak Guard [SOLAR]: '%s' (EV aan) herberekend — "
                "huidig=%.1f A, overschot=%.0f W, totaal=%.0f W → doel=%d A",
                device.name, current_a, excess, total_for_ev_w, new_a,
            )

        actual_consumption_w = new_a * voltage

        if cur_entity is None or current_a is None:
            return excess - actual_consumption_w

        if new_a == math.ceil(current_a):
            _LOGGER.debug(
                "Peak Guard [SOLAR]: '%s' set_value OVERGESLAGEN — "
                "laadstroom al op %d A (redundant call vermeden)",
                device.name, new_a,
            )
            return excess - actual_consumption_w

        if (
            guard.last_sent_amps is not None
            and abs(new_a - guard.last_sent_amps) < EV_HYSTERESIS_AMPS
        ):
            _LOGGER.debug(
                "Peak Guard [SOLAR]: '%s' set_value OVERGESLAGEN wegens hysteresis "
                "(%d A → %d A, delta=%.1f A < %.1f A drempel)",
                device.name, int(guard.last_sent_amps), new_a,
                abs(new_a - guard.last_sent_amps), EV_HYSTERESIS_AMPS,
            )
            return excess - actual_consumption_w

        if guard.last_current_update is not None:
            elapsed = (now - guard.last_current_update).total_seconds()
            if elapsed < EV_MIN_UPDATE_INTERVAL_S:
                _LOGGER.debug(
                    "Peak Guard [SOLAR]: '%s' set_value OVERGESLAGEN wegens update-interval "
                    "(%.0f s geleden, minimum %.0f s)",
                    device.name, elapsed, EV_MIN_UPDATE_INTERVAL_S,
                )
                return excess - actual_consumption_w

        if not self._rate_check(
            device.name, f"set_value solar {int(current_a)} → {new_a} A"
        ):
            return excess - actual_consumption_w

        try:
            await self._svc(
                "number", "set_value",
                {"entity_id": cur_entity, "value": float(new_a)},
            )
        except HomeAssistantError as err:
            self._warn(
                "Peak Guard [SOLAR]: '%s' set_value %d A mislukt: %s "
                "— stroomaanpassing volgende cyclus opnieuw",
                device.name, new_a, err,
            )
            return excess - actual_consumption_w

        self._record_call()
        self._track_action(cur_entity, "number.set_value", float(new_a))
        _LOGGER.info(
            "Peak Guard [SOLAR]: '%s' laadstroom %d → %d A "
            "(ceil van %.2f A, hw-min=%.0f A, verbruik %.0f W, %d fase(n), "
            "rate-limiter: %d/%d calls in %.0f s)",
            device.name, int(current_a), new_a,
            available_a_raw, hw_min_a, actual_consumption_w, phases,
            self._rate_limiter.calls_in_window,
            EV_RATE_LIMIT_MAX_CALLS, EV_RATE_LIMIT_WINDOW_S,
        )
        guard.last_sent_amps      = float(new_a)
        guard.last_current_update = now
        guard.state               = EVState.CHARGING
        return excess - actual_consumption_w
