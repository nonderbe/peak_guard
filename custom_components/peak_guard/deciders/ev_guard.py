"""
Peak Guard — deciders/ev_guard.py

Beheert de volledige EV-lader state machine, rate-limiting en debounce-logica.

Publieke API (aangeroepen vanuit BaseDecider):
  apply_action(device, excess, snapshots, cascade_type, peak_tracker, solar_tracker) -> float
  restore(device, snapshot, peak_tracker, solar_tracker) -> bool

Properties (aangeroepen vanuit controller voor to_dict):
  guards      -> Dict[str, EVDeviceGuard]
  rate_limiter -> EVRateLimiter
"""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Dict, Optional

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from ..const import (
    CONF_DEBUG_DECISION_LOGGING,
    DEFAULT_EV_CABLE_ENTITY,
    DEFAULT_EV_MAX_AMPERE,
    DEFAULT_EV_MIN_AMPERE,
    DEFAULT_EV_SOLAR_STOP_THRESHOLD_W,
)
from ..models import (
    CascadeDevice,
    DeviceSnapshot,
    EVDeviceGuard,
    EVRateLimiter,
    EVState,
    EV_DEBOUNCE_BYPASS_SURPLUS_W,
    EV_DEBOUNCE_STABLE_S,
    EV_DEBOUNCE_TOLERANCE_W,
    EV_HYSTERESIS_AMPS,
    EV_MIN_OFF_DURATION_S,
    EV_MIN_ON_DURATION_S,
    EV_MIN_UPDATE_INTERVAL_S,
    EV_RATE_LIMIT_MAX_CALLS,
    EV_RATE_LIMIT_WINDOW_S,
    EV_VOLTS_1PHASE,
    EV_VOLTS_3PHASE,
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

    def _rate_check(self, device_name: str, reason: str) -> bool:
        """
        Geeft True als een EV service call is toegestaan.
        Logt een warning en geeft False als de rate-limiter vol is.
        """
        if self._rate_limiter.is_allowed():
            return True
        _LOGGER.warning(
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
    #  Kabeldetectie                                                       #
    # ------------------------------------------------------------------ #

    def cable_connected(self, device: CascadeDevice) -> bool:
        """
        Geeft True als de laadkabel aangesloten is, of als er geen kabelentity
        geconfigureerd is.

        Truthy-states: on, true, connected, charging, complete, fully_charged,
        pending, 1 — of een numerieke waarde > 0.
        Bij unavailable/unknown: True (niet blokkeren bij tijdelijke storing).
        """
        cable_entity = device.ev_cable_entity or DEFAULT_EV_CABLE_ENTITY
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

        CABLE_ON = {"on", "true", "connected", "charging", "complete",
                    "fully_charged", "pending", "1"}
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

        CONNECTED = {"on", "true", "connected", "online", "home",
                     "charging", "fully_charged", "pending", "1"}
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
    #  Debounce helper                                                     #
    # ------------------------------------------------------------------ #

    def surplus_is_stable(
        self,
        guard: EVDeviceGuard,
        current_surplus_w: float,
        now: datetime,
    ) -> bool:
        """
        Geeft True als het surplus stabiel is geweest (spread ≤ 2× tolerantie)
        gedurende minstens EV_DEBOUNCE_STABLE_S seconden.

        Vergelijkt samples onderling (spread = max − min), niet allemaal met
        de huidige waarde. Zo wordt een geleidelijk dalend surplus als stabiel
        beschouwd zolang de spread binnen de tolerantie blijft.

        Side-effect: voegt (now, current_surplus_w) toe aan guard.surplus_history.
        """
        guard.surplus_history.append((now, current_surplus_w))

        cutoff2x = now - timedelta(seconds=EV_DEBOUNCE_STABLE_S * 2)
        relevant = [(ts, w) for ts, w in guard.surplus_history if ts >= cutoff2x]

        if len(relevant) < 2:
            return False

        oldest_ts = relevant[0][0]
        if (now - oldest_ts).total_seconds() < EV_DEBOUNCE_STABLE_S:
            return False

        values = [w for _, w in relevant]
        if min(values) <= 0:
            return False

        spread = max(values) - min(values)
        return spread <= EV_DEBOUNCE_TOLERANCE_W * 2

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
            _LOGGER.warning(
                "Peak Guard EV: '%s' SOC-limiet NIET aangepast — "
                "ev_max_soc niet geconfigureerd",
                device.name,
            )
            return

        soc_entity = device.ev_soc_entity
        if not soc_entity:
            _LOGGER.warning(
                "Peak Guard EV: '%s' SOC-limiet NIET aangepast — "
                "geen soc_entity geconfigureerd in de wizard (stap 3: SoC-limiet entiteit)",
                device.name,
            )
            return

        if override:
            target_soc = float(device.ev_max_soc)
            _LOGGER.warning(
                "Peak Guard EV: '%s' SOC-limiet instellen op %.0f%% via '%s'",
                device.name, target_soc, soc_entity,
            )
        else:
            target_soc = float(original_soc) if original_soc is not None else 100.0
            _LOGGER.warning(
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
                _LOGGER.warning(
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

                peak_tracker.start_measurement_on_turnon(
                    device_id=device.id,
                    device_name=device.name,
                    ts=datetime.now(timezone.utc),
                )
                guard = self.get_guard(device.id)
                guard.state = EVState.CHARGING
                guard.last_switch_state = True
                guard.turned_on_at = datetime.now(timezone.utc)
                guard.surplus_history.clear()
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
                    guard.surplus_history.clear()
                else:
                    # Schakelaar staat al uit — snapshot opruimen zonder service-call.
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
                    guard.surplus_history.clear()
                return True

        except HomeAssistantError as err:
            _LOGGER.warning(
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
            _LOGGER.warning("Peak Guard EV: schakelaar '%s' niet gevonden", sw_entity)
            return excess

        sw_on = sw_state.state == "on"

        current_a: Optional[float] = None
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

                await self.hass.services.async_call(
                    "switch", "turn_off", {"entity_id": sw_entity}, blocking=True
                )
                self._record_call()
                self._track_action(sw_entity, "switch.turn_off")

                if cur_entity and current_a is not None:
                    if self._rate_check(device.name, "set_value min_a na turn_off"):
                        await self.hass.services.async_call(
                            "number", "set_value",
                            {"entity_id": cur_entity, "value": min_a},
                            blocking=True,
                        )
                        self._record_call()
                        self._track_action(cur_entity, "number.set_value", min_a)

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
                guard.surplus_history.clear()

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

                await self.hass.services.async_call(
                    "number", "set_value",
                    {"entity_id": cur_entity, "value": float(new_a)},
                    blocking=True,
                )
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

        # GATE: kabeldetectie — blokkeert alleen starten, niet stoppen
        cable_entity = device.ev_cable_entity or DEFAULT_EV_CABLE_ENTITY
        if not sw_on and not self.cable_connected(device):
            if guard.state != EVState.CABLE_DISCONNECTED:
                guard.state = EVState.CABLE_DISCONNECTED
                self.last_skip_reason = f"laadkabel niet aangesloten ({cable_entity})"
                _LOGGER.warning(
                    "Peak Guard [SOLAR]: '%s' — laadkabel NIET aangesloten "
                    "('%s' = '%s') — laden geblokkeerd",
                    device.name, cable_entity,
                    (self.hass.states.get(cable_entity) or type("", (), {"state": "??"})()).state,
                )
            else:
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
            guard.surplus_history.clear()

        # GATE: start-drempel / stop-hysteresis
        if excess < start_threshold_w:
            if sw_on:
                ev_current_w = (current_a if current_a is not None else hw_min_a) * voltage
                surplus_after_stop = excess - ev_current_w
                if surplus_after_stop > DEFAULT_EV_SOLAR_STOP_THRESHOLD_W:
                    # Nog genoeg surplus na stop → doorladen, history bijhouden
                    self.surplus_is_stable(guard, excess, now)
                    _LOGGER.debug(
                        "Peak Guard [SOLAR]: '%s' draait — surplus %.0f W < start-drempel %.0f W "
                        "maar surplus na stop zou %.0f W zijn (> stop-drempel %.0f W) → doorladen",
                        device.name, excess, start_threshold_w,
                        surplus_after_stop, DEFAULT_EV_SOLAR_STOP_THRESHOLD_W,
                    )
                    # Val door naar stroom-aanpassing hieronder
                else:
                    # Surplus echt weg → stoppen
                    _LOGGER.info(
                        "Peak Guard [SOLAR]: '%s' GESTOPT — surplus %.0f W < start-drempel %.0f W "
                        "én surplus na stop zou %.0f W zijn (≤ stop-drempel %.0f W)",
                        device.name, excess, start_threshold_w,
                        surplus_after_stop, DEFAULT_EV_SOLAR_STOP_THRESHOLD_W,
                    )
                    # GATE: minimum ON duration
                    if guard.turned_on_at is not None:
                        on_secs = (now - guard.turned_on_at).total_seconds()
                        if on_secs < EV_MIN_ON_DURATION_S:
                            _LOGGER.info(
                                "Peak Guard [SOLAR]: '%s' uitschakelen OVERGESLAGEN — "
                                "te kort geleden ingeschakeld (%.0f s geleden, minimum %.0f s)",
                                device.name, on_secs, EV_MIN_ON_DURATION_S,
                            )
                            return excess
                    if not self._rate_check(device.name, "turn_off wegens geen surplus"):
                        return excess
                    try:
                        await self.hass.services.async_call(
                            "switch", "turn_off", {"entity_id": sw_entity}, blocking=True
                        )
                    except HomeAssistantError as ha_err:
                        _LOGGER.warning(
                            "Peak Guard [SOLAR]: '%s' niet bereikbaar voor turn_off — "
                            "volgende cyclus opnieuw proberen (%s)",
                            device.name, ha_err,
                        )
                        return excess
                    self._record_call()
                    self._track_action(sw_entity, "switch.turn_off")
                    guard.state = EVState.IDLE
                    guard.last_switch_state = False
                    guard.turned_off_at = now
                    guard.turned_off_by_pg = True
                    guard.surplus_history.clear()
                    event = solar_tracker.complete_solar_calculation(
                        device_id=device.id, now=now,
                    )
                    if event:
                        _LOGGER.info(
                            "Peak Guard: solar-event afgerond voor '%s' — "
                            "duur=%.1f min, verschoven=%.4f kWh, besparing=€%.4f",
                            device.name, event.measured_duration_min,
                            event.shifted_kwh, event.savings_euro,
                        )
                    return excess
            else:
                # EV uit, surplus < drempel → geen actie
                self.last_skip_reason = (
                    f"surplus {excess:.0f} W < start-drempel {start_threshold_w:.0f} W"
                )
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
                guard.surplus_history.clear()
            else:
                _LOGGER.debug(
                    "Peak Guard [SOLAR]: '%s' niet thuis — laden overgeslagen",
                    device.name,
                )
            self.last_skip_reason = "EV niet thuis"
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
            guard.surplus_history.clear()

        # GATE: debounce (bypass bij heel groot surplus)
        large_surplus = excess >= EV_DEBOUNCE_BYPASS_SURPLUS_W
        if large_surplus:
            guard.surplus_history.append((now, excess))
            if guard.state == EVState.WAITING_FOR_STABLE:
                _LOGGER.info(
                    "Peak Guard [SOLAR]: '%s' — debounce overgeslagen "
                    "(overschot %.0f W ≥ bypass-drempel %.0f W)",
                    device.name, excess, EV_DEBOUNCE_BYPASS_SURPLUS_W,
                )
            surplus_stable = True
        else:
            surplus_stable = self.surplus_is_stable(guard, excess, now)

        if not surplus_stable:
            history_secs = 0.0
            if guard.surplus_history:
                history_secs = (now - guard.surplus_history[0][0]).total_seconds()
            if guard.state != EVState.WAITING_FOR_STABLE:
                guard.state = EVState.WAITING_FOR_STABLE
                self.last_skip_reason = (
                    f"debounce: surplus {excess:.0f} W nog niet "
                    f"{EV_DEBOUNCE_STABLE_S:.0f}s stabiel"
                )
                _LOGGER.warning(
                    "Peak Guard [SOLAR]: '%s' NIET geactiveerd — wacht op stabiel overschot "
                    "(huidig=%.0f W, debounce=%.0f s vereist, tot nu toe=%.0f s, "
                    "tolerantie=±%.0f W)",
                    device.name, excess, EV_DEBOUNCE_STABLE_S, history_secs,
                    EV_DEBOUNCE_TOLERANCE_W,
                )
            else:
                self.last_skip_reason = (
                    f"debounce: wachten op stabiliteit "
                    f"({history_secs:.0f}/{EV_DEBOUNCE_STABLE_S:.0f}s)"
                )
                _LOGGER.info(
                    "Peak Guard [SOLAR]: '%s' nog wachtend op stabiel overschot "
                    "(huidig=%.0f W, %.0f/%.0f s verstreken)",
                    device.name, excess, history_secs, EV_DEBOUNCE_STABLE_S,
                )
            return excess

        if guard.state == EVState.WAITING_FOR_STABLE:
            _LOGGER.info(
                "Peak Guard [SOLAR]: '%s' overschot stabiel — actie toegestaan "
                "(%.0f W stabiel gedurende ≥ %.0f s)",
                device.name, excess, EV_DEBOUNCE_STABLE_S,
            )

        if not sw_on:
            # EV staat uit → aanzetten op hardware-minimum laadstroom

            # GATE: minimum OFF duration (alleen als PG zelf uitschakelde)
            if guard.turned_off_at is not None and guard.turned_off_by_pg:
                off_secs = (now - guard.turned_off_at).total_seconds()
                if off_secs < EV_MIN_OFF_DURATION_S:
                    self.last_skip_reason = (
                        f"min OFF-duur: {off_secs:.0f}s < {EV_MIN_OFF_DURATION_S:.0f}s"
                    )
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
                        _LOGGER.warning(
                            "Peak Guard [SOLAR]: '%s' — wake-up aanroep mislukt: %s",
                            device.name, wake_err,
                        )
                    wake_ok = False
                    for _ in range(int(EV_WAKE_TIMEOUT_S)):
                        await asyncio.sleep(1.0)
                        if self.is_connected(device):
                            wake_ok = True
                            break
                    if wake_ok:
                        if device.ev_status_sensor:
                            _st2 = self.hass.states.get(device.ev_status_sensor)
                            status_val = _st2.state if _st2 else "verbonden"
                        _LOGGER.info(
                            "Peak Guard [SOLAR]: '%s' — Tesla nu wakker "
                            "('%s' = '%s') → laden starten met %d A",
                            device.name, status_entity, status_val, new_a,
                        )
                    else:
                        _LOGGER.warning(
                            "Peak Guard [SOLAR]: '%s' — Tesla niet wakker na %.0f s "
                            "('%s' = '%s') — laden uitgesteld tot volgende cyclus",
                            device.name, EV_WAKE_TIMEOUT_S, status_entity, status_val,
                        )
                        return excess

                # GATE: global rate limiter
                if not self._rate_check(device.name, "turn_on voor injectiepreventie"):
                    return excess

                try:
                    await self.hass.services.async_call(
                        "switch", "turn_on", {"entity_id": sw_entity}, blocking=True
                    )
                except HomeAssistantError as ha_err:
                    _LOGGER.warning(
                        "Peak Guard [SOLAR]: '%s' niet bereikbaar voor turn_on — "
                        "volgende cyclus opnieuw proberen (%s)",
                        device.name, ha_err,
                    )
                    return excess
                self._record_call()
                self._track_action(sw_entity, "switch.turn_on")
                guard.state = EVState.CHARGING
                guard.last_switch_state = True
                guard.turned_on_at = now
                guard.surplus_history.clear()

                if cur_entity:
                    if self._rate_check(device.name, f"set_value {new_a} A bij turn_on"):
                        await self.hass.services.async_call(
                            "number", "set_value",
                            {"entity_id": cur_entity, "value": float(new_a)},
                            blocking=True,
                        )
                        self._record_call()
                        self._track_action(cur_entity, "number.set_value", float(new_a))
                        guard.last_sent_amps = float(new_a)
                        guard.last_current_update = now

                await self._set_soc_override(device, override=True)

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

        await self.hass.services.async_call(
            "number", "set_value",
            {"entity_id": cur_entity, "value": float(new_a)},
            blocking=True,
        )
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
