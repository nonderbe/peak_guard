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
from typing import TYPE_CHECKING, Dict, Optional

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from ..const import (
    CONF_DEBUG_DECISION_LOGGING,
    DEFAULT_EV_CABLE_ENTITY,
    DEFAULT_EV_MAX_AMPERE,
    DEFAULT_EV_MIN_AMPERE,
)
from ..models import (
    CascadeDevice,
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
        self._recent_warnings: deque = deque(maxlen=20)
        # Wordt gezet door apply_action; BaseDecider leest dit na de aanroep.
        self.last_skip_reason: str = ""

    # ------------------------------------------------------------------ #
    #  Properties voor controller.to_dict()                               #
    # ------------------------------------------------------------------ #

    @property
    def guards(self) -> Dict[str, EVDeviceGuard]:
        return self._guards

    @property
    def rate_limiter(self) -> EVRateLimiter:
        return self._rate_limiter

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
        """Registreer een service-call in de gedeelde iteratie-actielijst."""
        if not self.config.get(CONF_DEBUG_DECISION_LOGGING, False):
            return
        entry: dict = {"entity_id": entity_id, "action": action}
        if value is not None:
            entry["value"] = value
        self._iteration_actions.append(entry)

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
    #  Kabeldetectie                                                       #
    # ------------------------------------------------------------------ #

    def cable_connected(self, device: CascadeDevice) -> bool:
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
        cable_entity = device.ev_cable_entity
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

    def is_connected(self, device: CascadeDevice) -> bool:
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
        status_entity = device.ev_status_sensor
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
            return not bool(device.ev_wake_button)

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

    def is_home(self, device: CascadeDevice) -> bool:
        """
        Geeft True als de EV thuis is, of als er geen locatie-tracker is.

        Ondersteunt device_tracker (home/not_home) en binary_sensor (on/off).
        Bij ontbrekende, unavailable of onbekende sensor: True (geen blokkering).
        """
        tracker_entity = device.ev_location_tracker
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
        if s in ("unavailable", "unknown", ""):
            return True

        return s in ("home", "on", "true", "1")

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
        device: CascadeDevice,
        override: bool,
        original_soc: Optional[float] = None,
    ) -> None:
        """Stel de SOC-limiet in (override=True) of herstel hem (override=False)."""
        if device.ev_max_soc is None:
            _LOGGER.debug(
                "Peak Guard EV: '%s' SOC-limiet overgeslagen — ev_max_soc niet geconfigureerd",
                device.name,
            )
            return

        soc_entity = device.ev_soc_entity
        if not soc_entity:
            self._warn(
                "Peak Guard EV: '%s' SOC-limiet NIET aangepast — "
                "geen soc_entity geconfigureerd in de wizard (stap 3: SoC-limiet entiteit)",
                device.name,
            )
            return

        if override:
            target_soc = float(device.ev_max_soc)
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
            await self.hass.services.async_call(
                "number", "set_value",
                {"entity_id": soc_entity, "value": target_soc},
                blocking=True,
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
        device: CascadeDevice,
        snapshot: DeviceSnapshot,
        peak_tracker: "PeakAvoidTracker",
        solar_tracker: "SolarShiftTracker",
        cascade_type: str = "peak",
    ) -> bool:
        """
        Herstel EV Charger na een Peak Guard ingreep.

        PIEKBEPERKING (original_state == "on"):
          Zet schakelaar terug aan, herstel laadstroom, start duurmeting.

        INJECTIEPREVENTIE (original_state == "off"):
          Verwijder SOC-override, herstel laadstroom, zet schakelaar uit,
          voltooi solar duurmeting.
        """
        try:
            sw_entity  = device.ev_switch_entity or device.entity_id
            cur_entity = device.ev_current_entity

            sw_state = self.hass.states.get(sw_entity)
            if sw_state is None:
                self._warn(
                    "Peak Guard EV: schakelaar '%s' niet gevonden bij herstel", sw_entity
                )
                return False

            # ---- PIEKBEPERKING: schakelaar was aan, nu uitgeschakeld ---- #
            if snapshot.original_state == "on":
                if sw_state.state != "on":
                    await self.hass.services.async_call(
                        "switch", "turn_on", {"entity_id": sw_entity}, blocking=True
                    )
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
                            await self.hass.services.async_call(
                                "number", "set_value",
                                {"entity_id": cur_entity, "value": round(orig_a, 1)},
                                blocking=True,
                            )
                            _LOGGER.info(
                                "Peak Guard EV peak: '%s' laadstroom hersteld naar %.1f A "
                                "(reden: herstel na piekbeperking, gecapped aan max %.1f A)",
                                device.name, orig_a, max_a,
                            )

                # peak_tracker alleen aanroepen in piekbeperkings-context.
                # In solar-context met original_state="on" was het apparaat al aan
                # vóór PG ingreep (gebruiker schakelde handmatig in) — geen piek-event.
                if cascade_type == "peak":
                    peak_tracker.start_measurement_on_turnon(
                        device_id=device.id,
                        device_name=device.name,
                        ts=datetime.now(timezone.utc),
                    )
                guard = self.get_guard(device.id)
                guard.state = EVState.CHARGING
                guard.last_switch_state = True
                guard.turned_on_at = datetime.now(timezone.utc)
                self._reset_debounce(guard)
                return True

            # ---- INJECTIEPREVENTIE: schakelaar was uit, nu aangezet ---- #
            if snapshot.original_state == "off":
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
                        await self.hass.services.async_call(
                            "number", "set_value",
                            {"entity_id": cur_entity, "value": round(restore_a, 1)},
                            blocking=True,
                        )
                        _LOGGER.info(
                            "Peak Guard EV solar: '%s' laadstroom hersteld naar %.1f A "
                            "(reden: herstel na injectiepreventie)",
                            device.name, restore_a,
                        )

                    await self.hass.services.async_call(
                        "switch", "turn_off", {"entity_id": sw_entity}, blocking=True
                    )
                    _LOGGER.info(
                        "Peak Guard EV solar: '%s' schakelaar uitgeschakeld "
                        "(reden: herstel na injectiepreventie)",
                        device.name,
                    )

                    ev_event = solar_tracker.complete_solar_calculation(
                        device_id=device.id,
                        now=datetime.now(timezone.utc),
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
                    guard.turned_off_at = datetime.now(timezone.utc)
                    guard.turned_off_by_pg = True
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
                        now=datetime.now(timezone.utc),
                    )
                    guard = self.get_guard(device.id)
                    guard.state = EVState.IDLE
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
        device: CascadeDevice,
        consumption: float,
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
        cur_entity = device.ev_current_entity
        if not cur_entity:
            return False

        cur_state = self.hass.states.get(cur_entity)
        if cur_state is None:
            return False

        try:
            current_a = float(cur_state.state)
        except (ValueError, TypeError):
            return False

        phases  = int(device.ev_phases) if device.ev_phases else 1
        voltage = EV_VOLTS_3PHASE if phases == 3 else EV_VOLTS_1PHASE
        hw_min_a = float(
            device.ev_min_current if device.ev_min_current is not None
            else (device.min_value if device.min_value is not None else DEFAULT_EV_MIN_AMPERE)
        )
        max_a = float(device.max_value if device.max_value is not None else DEFAULT_EV_MAX_AMPERE)

        # Hoeveel ampère moeten we reduceren om de grid-import weg te werken?
        reduction_a = math.ceil(consumption / voltage)
        new_a = max(int(hw_min_a), min(int(max_a), int(current_a) - reduction_a))

        if new_a >= int(current_a):
            # Al op hw-minimum of reductie niet mogelijk → aanroeper stopt EV.
            return False

        guard = self.get_guard(device.id)
        now   = datetime.now(timezone.utc)

        # OPTIE C: stale sensor-check
        if guard.last_sent_amps is not None:
            _last_upd = cur_state.last_updated
            if _last_upd.tzinfo is None:
                _last_upd = _last_upd.replace(tzinfo=timezone.utc)
            _sensor_age_s = (now - _last_upd).total_seconds()
            if _sensor_age_s > EV_SENSOR_STALE_S:
                _LOGGER.warning(
                    "Peak Guard [SOLAR]: '%s' stroom-sensor stale (%.0f s oud) — "
                    "eigen sturing (%.1f A) als referentie i.p.v. sensor (%.1f A)",
                    device.name, _sensor_age_s, guard.last_sent_amps, current_a,
                )
                current_a = guard.last_sent_amps

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
            await self.hass.services.async_call(
                "number", "set_value",
                {"entity_id": cur_entity, "value": float(new_a)},
                blocking=True,
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
    #  Cascade-actie (peak-pad + solar-pad)                               #
    # ------------------------------------------------------------------ #

    async def apply_action(
        self,
        device: CascadeDevice,
        excess: float,
        snapshots: dict,
        cascade_type: str,
        peak_tracker: "PeakAvoidTracker",
        solar_tracker: "SolarShiftTracker",
    ) -> float:
        """
        EV Charger cascade-ingreep met volledige rate-limiting.

        cascade_type == "peak"  : laadstroom floor, schakelaar uit als < min_a.
        cascade_type == "solar" : laadstroom instellen op beschikbaar surplus,
                                  schakelaar aan/uit met debounce en hysteresis.

        Snapshot-key is altijd device.entity_id (niet device.id).
        """
        self.last_skip_reason = ""

        min_a = float(device.min_value if device.min_value is not None else DEFAULT_EV_MIN_AMPERE)
        max_a = float(device.max_value if device.max_value is not None else DEFAULT_EV_MAX_AMPERE)
        phases = int(device.ev_phases) if device.ev_phases else 1
        voltage = EV_VOLTS_3PHASE if phases == 3 else EV_VOLTS_1PHASE

        sw_entity  = device.ev_switch_entity or device.entity_id
        cur_entity = device.ev_current_entity

        sw_state = self.hass.states.get(sw_entity)
        if sw_state is None:
            self._warn("Peak Guard EV: schakelaar '%s' niet gevonden", sw_entity)
            return excess

        sw_on = sw_state.state == "on"

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
        if device.ev_soc_entity:
            soc_state = self.hass.states.get(device.ev_soc_entity)
            if soc_state is not None:
                try:
                    current_soc = float(soc_state.state)
                except (ValueError, TypeError):
                    current_soc = None

        # Snapshot-key: device.entity_id (kritisch voor correcte restore-lookup)
        snap_key = device.entity_id
        if snap_key not in snapshots:
            snapshots[snap_key] = DeviceSnapshot(
                entity_id=snap_key,
                original_state=sw_state.state,
                original_current=current_a,
                original_soc=current_soc,
            )

        now = datetime.now(timezone.utc)
        guard = self.get_guard(device.id)
        guard.skip_reason = ""

        # OPTIE C: stale sensor-check (solar-pad)
        # Tesla Fleet rapporteert soms verouderde waarden. Als de sensor ouder is dan
        # EV_SENSOR_STALE_S en PG zelf een bekende waarde heeft, gebruik die als referentie.
        if cascade_type == "solar" and cur_state is not None and guard.last_sent_amps is not None:
            _last_upd = cur_state.last_updated
            if _last_upd.tzinfo is None:
                _last_upd = _last_upd.replace(tzinfo=timezone.utc)
            _sensor_age_s = (now - _last_upd).total_seconds()
            if _sensor_age_s > EV_SENSOR_STALE_S:
                _LOGGER.warning(
                    "Peak Guard [SOLAR]: '%s' stroom-sensor stale (%.0f s oud) — "
                    "eigen sturing (%.1f A) als referentie i.p.v. sensor (%.1f A)",
                    device.name, _sensor_age_s, guard.last_sent_amps, current_a,
                )
                current_a = guard.last_sent_amps

        # ================================================================ #
        #  PIEKBEPERKING — floor, laadstroom verlagen                      #
        # ================================================================ #
        if cascade_type == "peak":
            if not sw_on:
                _LOGGER.debug(
                    "Peak Guard EV peak: '%s' overgeslagen — EV laadt niet", device.name
                )
                return excess

            eff_current_a = current_a if current_a is not None else max_a
            current_w = eff_current_a * voltage
            needed_reduction_w = min(excess, current_w)
            target_a_raw = (current_w - needed_reduction_w) / voltage
            new_a = math.floor(target_a_raw)
            new_a = max(0, min(int(max_a), new_a))

            if new_a < min_a:
                # GATE: redundancy — niet uitschakelen als al uit
                if guard.last_switch_state is False:
                    _LOGGER.debug(
                        "Peak Guard EV peak: '%s' uitschakelen OVERGESLAGEN — "
                        "schakelaar al uit (redundant call vermeden)",
                        device.name,
                    )
                    return excess - current_w

                # GATE: global rate limiter (conservatief bij turn_off)
                if not self._rate_check(device.name, "turn_off voor piekbeperking"):
                    return excess

                turn_off_ok = False
                _last_turn_off_err: Optional[HomeAssistantError] = None
                for _attempt in range(EV_CMD_MAX_RETRIES + 1):
                    try:
                        await self.hass.services.async_call(
                            "switch", "turn_off", {"entity_id": sw_entity}, blocking=True
                        )
                        turn_off_ok = True
                        break
                    except HomeAssistantError as ha_err:
                        _last_turn_off_err = ha_err
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
                        device.name, EV_CMD_MAX_RETRIES + 1, _last_turn_off_err,
                    )
                    return excess

                self._record_call()
                self._track_action(sw_entity, "switch.turn_off")

                if cur_entity and current_a is not None:
                    if self._rate_check(device.name, "set_value min_a na turn_off"):
                        try:
                            await self.hass.services.async_call(
                                "number", "set_value",
                                {"entity_id": cur_entity, "value": min_a},
                                blocking=True,
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
                guard.state = EVState.IDLE
                guard.last_switch_state = False
                guard.turned_off_at = now
                guard.turned_off_by_pg = True
                guard.last_sent_amps = min_a
                self._reset_debounce(guard)

                peak_tracker.record_pending_avoid(
                    device_id=device.id,
                    device_name=device.name,
                    nominal_kw=current_w / 1000.0,
                    ts=now,
                )
                return excess - current_w

            else:
                actual_reduction_w = (eff_current_a - new_a) * voltage

                if cur_entity is None:
                    return excess - actual_reduction_w

                # GATE: redundancy
                if new_a == int(eff_current_a):
                    _LOGGER.debug(
                        "Peak Guard EV peak: '%s' set_value OVERGESLAGEN — "
                        "laadstroom al op %d A (redundant call vermeden)",
                        device.name, new_a,
                    )
                    return excess - actual_reduction_w

                # GATE: hysteresis
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

                # GATE: minimum update interval
                if guard.last_current_update is not None:
                    elapsed = (now - guard.last_current_update).total_seconds()
                    if elapsed < EV_MIN_UPDATE_INTERVAL_S:
                        _LOGGER.debug(
                            "Peak Guard EV peak: '%s' set_value OVERGESLAGEN wegens "
                            "update-interval (%.0f s geleden, minimum %.0f s)",
                            device.name, elapsed, EV_MIN_UPDATE_INTERVAL_S,
                        )
                        return excess - actual_reduction_w

                # GATE: global rate limiter
                if not self._rate_check(
                    device.name, f"set_value peak {int(eff_current_a)} → {new_a} A"
                ):
                    return excess - actual_reduction_w

                try:
                    await self.hass.services.async_call(
                        "number", "set_value",
                        {"entity_id": cur_entity, "value": float(new_a)},
                        blocking=True,
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
                guard.last_sent_amps = float(new_a)
                guard.last_current_update = now
                guard.state = EVState.CHARGING
                return excess - actual_reduction_w

        # ================================================================ #
        #  INJECTIEPREVENTIE — laadstroom instellen op beschikbaar surplus #
        # ================================================================ #
        # hw_min_a is losgekoppeld van start_threshold_w (zie klasdocstring).
        hw_min_a = float(
            device.ev_min_current if device.ev_min_current is not None
            else (device.min_value if device.min_value is not None else DEFAULT_EV_MIN_AMPERE)
        )
        from ..const import DEFAULT_EV_SOLAR_START_THRESHOLD_W
        start_threshold_w = float(
            device.start_threshold_w if device.start_threshold_w is not None
            else DEFAULT_EV_SOLAR_START_THRESHOLD_W
        )
        hw_min_w = hw_min_a * voltage

        available_a_raw = excess / voltage
        new_a = max(int(hw_min_a), min(int(max_a), math.ceil(available_a_raw)))
        guard.pending_amps = new_a

        _LOGGER.debug(
            "Peak Guard [SOLAR]: '%s' evalueren — "
            "overschot=%.0f W, spanning=%dV (%d fase(n)), "
            "beschikbaar=%.2f A, doel=%d A (hw-min=%.0f A=%.0f W, max=%d A), "
            "start-drempel=%.0f W, schakelaar=%s, laadstroom=%s A, guard-state=%s",
            device.name, excess, int(voltage), phases,
            available_a_raw, new_a, hw_min_a, hw_min_w, int(max_a),
            start_threshold_w,
            "AAN" if sw_on else "UIT",
            f"{current_a:.1f}" if current_a is not None else "onbekend",
            guard.state.value,
        )

        # GATE: kabeldetectie — blokkeert starten én zet EV uit als kabel mid-charge ontkoppeld
        cable_entity = device.ev_cable_entity or DEFAULT_EV_CABLE_ENTITY
        if not self.cable_connected(device):
            if sw_on:
                # Kabel ontkoppeld terwijl EV aan het laden was: zet schakelaar uit en
                # meld het overschot als onverwerkt. Zonder deze check berekent de code
                # een fictief verbruik (new_a × voltage) en denkt de cascade ten onrechte
                # dat het overschot volledig verwerkt is.
                self._warn(
                    "Peak Guard [SOLAR]: '%s' — laadkabel ontkoppeld TIJDENS het laden "
                    "('%s' = '%s') — schakelaar uitzetten",
                    device.name, cable_entity,
                    (self.hass.states.get(cable_entity) or type("", (), {"state": "??"})()).state,
                )
                if self._rate_check(device.name, "turn_off kabel ontkoppeld"):
                    try:
                        await self.hass.services.async_call(
                            "switch", "turn_off", {"entity_id": sw_entity}, blocking=True
                        )
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
                guard.state = EVState.CABLE_DISCONNECTED
                guard.last_switch_state = False
                self._reset_debounce(guard)
                guard.skip_reason = f"laadkabel ontkoppeld tijdens laden ({cable_entity})"
                self.last_skip_reason = guard.skip_reason
            elif guard.state != EVState.CABLE_DISCONNECTED:
                guard.state = EVState.CABLE_DISCONNECTED
                guard.skip_reason = f"laadkabel niet aangesloten ({cable_entity})"
                self.last_skip_reason = guard.skip_reason
                self._warn(
                    "Peak Guard [SOLAR]: '%s' — laadkabel NIET aangesloten "
                    "('%s' = '%s') — laden geblokkeerd",
                    device.name, cable_entity,
                    (self.hass.states.get(cable_entity) or type("", (), {"state": "??"})()).state,
                )
                # Als PG de laadlimiet had verhoogd (SOC-override actief), herstel die nu.
                # De normale restore()-flow triggert pas als de injectie stopt, wat niet
                # gebeurt als de kabel wordt ontkoppeld terwijl de zon nog schijnt.
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
            else:
                guard.skip_reason = f"laadkabel niet aangesloten ({cable_entity})"
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

        # GATE: start-drempel
        # EV aan: doorladen zolang er injectie is; stoppen enkel via check_restore (consumption ≥ 0).
        # EV uit: alleen starten als excess ≥ start_threshold_w.
        if excess < start_threshold_w:
            if sw_on:
                # EV draait — doorladen zolang er injectie is.
                # Geen ondergrens-check nodig; stroomaanpassing hieronder.
                _LOGGER.debug(
                    "Peak Guard [SOLAR]: '%s' draait — surplus %.0f W < start-drempel %.0f W "
                    "→ doorladen (stop enkel als geen injectie meer)",
                    device.name, excess, start_threshold_w,
                )
                # Val door naar stroom-aanpassing hieronder
            else:
                # EV uit, surplus < drempel → debounce-timer wissen en geen actie.
                # Tijdstempelinformatie is ongeldig zodra surplus de drempel verlaat,
                # zodat de 20 s opnieuw geteld wordt vanaf het volgende positieve surplus.
                self._reset_debounce(guard)
                guard.skip_reason = (
                    f"surplus {excess:.0f} W < start-drempel {start_threshold_w:.0f} W"
                )
                self.last_skip_reason = guard.skip_reason
                _LOGGER.info(
                    "Peak Guard [SOLAR]: '%s' staat uit — surplus %.0f W < "
                    "start-drempel %.0f W → geen actie",
                    device.name, excess, start_threshold_w,
                )
                return excess

        # surplus ≥ start_threshold_w — EV mag (of blijft) laden

        # GATE: locatie — EV moet thuis zijn om te laden
        if not self.is_home(device):
            if guard.state != EVState.IDLE:
                loc_st = self.hass.states.get(device.ev_location_tracker) if device.ev_location_tracker else None
                _LOGGER.info(
                    "Peak Guard [SOLAR]: '%s' niet thuis (tracker='%s', staat='%s') — "
                    "toestand gereset naar IDLE",
                    device.name, device.ev_location_tracker or "(geen)",
                    loc_st.state if loc_st else "onbekend",
                )
                guard.state = EVState.IDLE
                self._reset_debounce(guard)
            else:
                _LOGGER.debug(
                    "Peak Guard [SOLAR]: '%s' niet thuis — laden overgeslagen",
                    device.name,
                )
            guard.skip_reason = "EV niet thuis"
            self.last_skip_reason = guard.skip_reason
            return excess

        # Detecteer handmatige start
        if sw_on and guard.last_switch_state is not True:
            _LOGGER.info(
                "Peak Guard [SOLAR]: '%s' — handmatige start gedetecteerd — "
                "toestand overgenomen (turned_off_at gereset, turned_off_by_pg=False)",
                device.name,
            )
            guard.state = EVState.CHARGING
            guard.last_switch_state = True
            guard.turned_on_at = now
            guard.turned_off_at = None
            guard.turned_off_by_pg = False
            self._reset_debounce(guard)

        # ── Dynamische ondergrens-detectie ───────────────────────────
        # Alleen nodig als de EV nog niet aan het laden is.
        # Bouwt de surplus-geschiedenis op en berekent het EV_FLOOR_PERCENTILE-
        # percentiel als "veilige startwaarde". Geen vaste watt-drempel.
        if not sw_on:
            surplus_ready, floor_w = self._surplus_floor(guard, excess, now)

            if not surplus_ready:
                elapsed_s = EV_DEBOUNCE_STABLE_S - guard.debounce_remaining_s
                floor_hint = ""
                if guard.debounce_floor_w > 0:
                    floor_a = max(
                        int(hw_min_a),
                        min(int(max_a), math.floor(guard.debounce_floor_w / voltage)),
                    )
                    floor_hint = f", floor ≈ {floor_a} A"
                skip = (
                    f"opbouwen ({elapsed_s:.0f}/{EV_DEBOUNCE_STABLE_S:.0f}s"
                    f"{floor_hint})"
                )
                guard.skip_reason = skip
                self.last_skip_reason = skip
                if guard.state != EVState.WAITING_FOR_STABLE:
                    guard.state = EVState.WAITING_FOR_STABLE
                    self._warn(
                        "Peak Guard [SOLAR]: '%s' NIET geactiveerd — "
                        "debounce loopt (%.0f/%.0f s, huidig=%.0f W%s, percentiel=%d%%)",
                        device.name, elapsed_s, EV_DEBOUNCE_STABLE_S,
                        excess, floor_hint, EV_FLOOR_PERCENTILE,
                    )
                else:
                    _LOGGER.info(
                        "Peak Guard [SOLAR]: '%s' debounce loopt "
                        "(%.0f/%.0f s, huidig=%.0f W%s)",
                        device.name, elapsed_s, EV_DEBOUNCE_STABLE_S,
                        excess, floor_hint,
                    )
                return excess

            if guard.state == EVState.WAITING_FOR_STABLE:
                _LOGGER.info(
                    "Peak Guard [SOLAR]: '%s' ondergrens vastgesteld op %.0f W "
                    "(%d-percentiel over %.0f s) — EV starten",
                    device.name, floor_w, EV_FLOOR_PERCENTILE, EV_DEBOUNCE_STABLE_S,
                )
                guard.state = EVState.IDLE

            # Overschrijf new_a: gebruik de veilige ondergrens als startlaadstroom.
            # floor() garandeert dat het setpoint nooit boven de ondergrens uitkomt,
            # ook niet als het surplus precies op de grens schommelt.
            floor_a = max(int(hw_min_a), min(int(max_a), math.floor(floor_w / voltage)))
            guard.pending_amps = floor_a
            new_a = floor_a

        if not sw_on:
            # EV staat uit → aanzetten op hardware-minimum laadstroom

            # GATE: minimum OFF duration (alleen als PG zelf uitschakelde)
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

            # GATE: redundancy (turn_on eerder gezonden maar EV nog uit)
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
                # GATE: wake-up check
                if device.ev_wake_button and not self.is_connected(device):
                    status_entity = device.ev_status_sensor or "(geen sensor)"
                    status_val = "onbekend"
                    if device.ev_status_sensor:
                        _st = self.hass.states.get(device.ev_status_sensor)
                        status_val = _st.state if _st else "niet gevonden"
                    _LOGGER.info(
                        "Peak Guard [SOLAR]: '%s' — Tesla in slaapstand "
                        "('%s' = '%s') → wake button '%s' aanroepen",
                        device.name, status_entity, status_val, device.ev_wake_button,
                    )
                    try:
                        await self.hass.services.async_call(
                            "button", "press",
                            {"entity_id": device.ev_wake_button},
                            blocking=False,
                        )
                    except Exception as wake_err:
                        self._warn(
                            "Peak Guard [SOLAR]: '%s' — wake-up aanroep mislukt: %s",
                            device.name, wake_err,
                        )
                    guard.state = EVState.SLEEPING
                    guard.wake_requested_at = now
                    wake_ok = False
                    for _ in range(int(EV_WAKE_TIMEOUT_S)):
                        await asyncio.sleep(1.0)
                        if self.is_connected(device):
                            wake_ok = True
                            break
                    if wake_ok:
                        guard.state = EVState.IDLE
                        guard.wake_requested_at = None
                        if device.ev_status_sensor:
                            _st2 = self.hass.states.get(device.ev_status_sensor)
                            status_val = _st2.state if _st2 else "verbonden"
                        _LOGGER.info(
                            "Peak Guard [SOLAR]: '%s' — Tesla nu wakker "
                            "('%s' = '%s') → laden starten met %d A",
                            device.name, status_entity, status_val, new_a,
                        )
                    else:
                        guard.state = EVState.IDLE
                        guard.wake_requested_at = None
                        guard.skip_reason = "Tesla niet wakker na wake-up poging"
                        self.last_skip_reason = guard.skip_reason
                        self._warn(
                            "Peak Guard [SOLAR]: '%s' — Tesla niet wakker na %.0f s "
                            "('%s' = '%s') — laden uitgesteld tot volgende cyclus",
                            device.name, EV_WAKE_TIMEOUT_S, status_entity, status_val,
                        )
                        return excess

                # GATE: global rate limiter
                if not self._rate_check(device.name, "turn_on voor injectiepreventie"):
                    return excess

                # SOC-override vóór turn_on: Tesla weigert turn_on als huidige SOC boven de
                # geconfigureerde laadlimiet ligt ("Command was unsuccessful: complete").
                # Als de Tesla sliep bij snapshot-aanmaak (original_soc=None), forceer nu een
                # verse bevraging zodat we de originele waarde kunnen opslaan voor correct herstel.
                _snap_now = snapshots.get(snap_key)
                if device.ev_soc_entity and _snap_now is not None and _snap_now.original_soc is None:
                    try:
                        await self.hass.services.async_call(
                            "homeassistant", "update_entity",
                            {"entity_id": device.ev_soc_entity},
                            blocking=True,
                        )
                    except Exception:
                        pass
                    _soc_st = self.hass.states.get(device.ev_soc_entity)
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
                await self._set_soc_override(device, override=True)

                turn_on_ok = False
                _last_turn_on_err: Optional[HomeAssistantError] = None
                for _attempt in range(EV_CMD_MAX_RETRIES + 1):
                    try:
                        await self.hass.services.async_call(
                            "switch", "turn_on", {"entity_id": sw_entity}, blocking=True
                        )
                        turn_on_ok = True
                        break
                    except HomeAssistantError as ha_err:
                        _last_turn_on_err = ha_err
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
                        device.name, EV_CMD_MAX_RETRIES + 1, _last_turn_on_err,
                    )
                    await self._set_soc_override(
                        device, override=False,
                        original_soc=_snap_now.original_soc if _snap_now else None,
                    )
                    return excess

                self._record_call()
                self._track_action(sw_entity, "switch.turn_on")
                guard.state = EVState.CHARGING
                guard.skip_reason = ""
                guard.last_switch_state = True
                guard.turned_on_at = now
                self._reset_debounce(guard)

                if cur_entity:
                    if self._rate_check(device.name, f"set_value {new_a} A bij turn_on"):
                        try:
                            await self.hass.services.async_call(
                                "number", "set_value",
                                {"entity_id": cur_entity, "value": float(new_a)},
                                blocking=True,
                            )
                            self._record_call()
                            self._track_action(cur_entity, "number.set_value", float(new_a))
                            guard.last_sent_amps = float(new_a)
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
                    "omdat injectie %.0f W > start-drempel %.0f W "
                    "(hw-min=%.0f A, %d fase(n), SOC-override: %s%%, "
                    "rate-limiter: %d/%d calls in %.0f s)",
                    device.name, new_a, actual_consumption_w,
                    excess, start_threshold_w, hw_min_a, phases,
                    device.ev_max_soc if device.ev_max_soc is not None else "n.v.t.",
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

        # EV staat al aan → stroom aanpassen indien nodig
        # Herbereken new_a: totaal beschikbaar = huidige EV-stroom + netto surplus
        if current_a is not None:
            total_for_ev_w = current_a * voltage + excess
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

        # GATE: redundancy
        if new_a == math.ceil(current_a):
            _LOGGER.debug(
                "Peak Guard [SOLAR]: '%s' set_value OVERGESLAGEN — "
                "laadstroom al op %d A (redundant call vermeden)",
                device.name, new_a,
            )
            return excess - actual_consumption_w

        # GATE: hysteresis
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

        # GATE: minimum update interval
        if guard.last_current_update is not None:
            elapsed = (now - guard.last_current_update).total_seconds()
            if elapsed < EV_MIN_UPDATE_INTERVAL_S:
                _LOGGER.debug(
                    "Peak Guard [SOLAR]: '%s' set_value OVERGESLAGEN wegens update-interval "
                    "(%.0f s geleden, minimum %.0f s)",
                    device.name, elapsed, EV_MIN_UPDATE_INTERVAL_S,
                )
                return excess - actual_consumption_w

        # GATE: global rate limiter
        if not self._rate_check(
            device.name, f"set_value solar {int(current_a)} → {new_a} A"
        ):
            return excess - actual_consumption_w

        try:
            await self.hass.services.async_call(
                "number", "set_value",
                {"entity_id": cur_entity, "value": float(new_a)},
                blocking=True,
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
        guard.last_sent_amps = float(new_a)
        guard.last_current_update = now
        guard.state = EVState.CHARGING
        return excess - actual_consumption_w
