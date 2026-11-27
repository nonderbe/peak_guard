"""
Peak Guard — deciders/charger_state.py

Eén plek die vaststelt wat een EV-lader doet (docs/charger_state_plan.md).

- ChargerMemory: wat Peak Guard zelf met de lader deed, per schakelaar
  (gedeeld door alle kopieën van de wagen: piek, injectie, schema).
- ChargerView: alleen-lezen beeld van één wagen — leest de HA-states en
  het geheugen, stuurt niets.

EVGuard houdt dunne doorverwijzende methodes (is_charging, peak_is_off, …),
zodat de aanroepers niet in één keer moeten veranderen.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from time import monotonic
from typing import TYPE_CHECKING, Dict, Mapping, Optional

from ..const import (
    EV_CHARGING_CONFIRM_S,
    PEAK_RESTART_CONFIRM_S,
    SCHEDULE_START_CONFIRM_S,
    EV_STALE_STOPPED_S,
    EV_STALE_REPORT_GRACE_S,
    DEFAULT_EV_MIN_AMPERE,
)
from ..models import (
    EVChargerDevice,
    EVDeviceGuard,
    EVState,
    EV_SENSOR_STALE_S,
    EV_VOLTS_1PHASE,
    EV_VOLTS_3PHASE,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Tekstuele laadstatussen die als laadstatus gelden (charge_state_sensor of
# een tekstuele status_sensor); al de rest (bv. binaire online/slaap) niet.
CHARGE_STATES = (
    "charging", "starting", "stopped", "complete", "fully_charged",
    "disconnected", "no_power", "nopower",
)


def sw_key(device: EVChargerDevice) -> str:
    """Sleutel van het geheugen per schakelaar: alle kopieën van de wagen delen hem."""
    return device.switch_entity or device.entity_id


def voltage(device: EVChargerDevice) -> float:
    phases = int(device.phases) if device.phases else 1
    return EV_VOLTS_3PHASE if phases == 3 else EV_VOLTS_1PHASE


def hw_min_amps(device: EVChargerDevice) -> float:
    return float(
        device.min_current if device.min_current is not None
        else (device.min_value if device.min_value is not None else DEFAULT_EV_MIN_AMPERE)
    )


def read_float(hass: "HomeAssistant", entity_id: Optional[str]) -> Optional[float]:
    if not entity_id:
        return None
    st = hass.states.get(entity_id)
    if st is None:
        return None
    try:
        return float(st.state)
    except (TypeError, ValueError):
        return None


class ChargerMemory:
    """Wat Peak Guard zelf deed, per schakelaar-entity (enkel in het geheugen)."""

    def __init__(self) -> None:
        # Schakelaar-entity → (tijdstip, laadstatus in HA op dat moment) van een
        # turn_on die de wagen weigerde met 'is_charging'. Per entity, niet per
        # device.id, zodat alle kopieën (piek/injectie/schema) het delen; een
        # geslaagde turn_off van die schakelaar wist het (forget_on_turn_off).
        self.charging_confirmed: Dict[str, tuple] = {}
        # Laatste echte turn_on per schakelaar (alle kopieën van de wagen
        # delen hem); een geslaagde turn_off wist hem. Zie stale_stopped_after_start.
        self.charge_started: Dict[str, datetime] = {}
        # (tick-'now', monotonic()) bij het begin van de lopende iteratie: zo
        # is een startmoment 'now' + verstreken tijd (wake-up, retries, eerdere
        # apparaten in dezelfde tick tellen mee).
        self.tick_clock: Optional[tuple] = None

    def begin_tick(self, now: Optional[datetime]) -> None:
        self.tick_clock = (now, monotonic()) if now is not None else None

    def note_charge_start(self, device: EVChargerDevice, now: datetime) -> None:
        """Peak Guard stuurde net een geslaagde switch.turn_on naar deze wagen.

        Is 'now' het tijdstip van de lopende iteratie (begin_tick), dan telt
        de sindsdien verstreken tijd mee: de start is het moment van de
        geslaagde turn_on, na wake-up, retries en eerdere apparaten.
        """
        if self.tick_clock is not None and self.tick_clock[0] == now:
            now = now + timedelta(seconds=max(0.0, monotonic() - self.tick_clock[1]))
        self.charge_started[sw_key(device)] = now

    def confirm_charging(self, device: EVChargerDevice, charge_state: str) -> None:
        """De wagen meldde zelf dat hij laadt (turn_on geweigerd met 'is_charging')."""
        self.charging_confirmed[sw_key(device)] = (
            datetime.now(timezone.utc), charge_state,
        )

    def forget_on_turn_off(self, entity_id: Optional[str]) -> None:
        """Een geslaagde switch.turn_off: start en bevestiging gelden niet meer."""
        self.charging_confirmed.pop(entity_id, None)
        self.charge_started.pop(entity_id, None)


class ChargerView:
    """Alleen-lezen beeld van één wagen: wat doet de lader nu?

    Goedkoop; maak er een aan per vraag (EVGuard.view). 'guards' is de
    dict device.id → EVDeviceGuard van EVGuard (enkel gelezen, behalve
    home(), dat de laatst gekende locatie in de meegegeven guard bewaart).
    """

    __slots__ = ("hass", "memory", "guards", "device")

    def __init__(
        self,
        hass: "HomeAssistant",
        memory: ChargerMemory,
        guards: Mapping[str, EVDeviceGuard],
        device: EVChargerDevice,
    ) -> None:
        self.hass = hass
        self.memory = memory
        self.guards = guards
        self.device = device

    # ------------------------------------------------------------------ #
    #  Vaste gegevens                                                      #
    # ------------------------------------------------------------------ #

    @property
    def voltage(self) -> float:
        return voltage(self.device)

    @property
    def hw_min_a(self) -> float:
        return hw_min_amps(self.device)

    # ------------------------------------------------------------------ #
    #  Laadstatus                                                          #
    # ------------------------------------------------------------------ #

    def charge_state(self) -> str:
        """Laadstatus in kleine letters; '' als onbekend.

        Bron: charge_state_sensor, anders status_sensor. Een binaire
        status-sensor ('on'/'off' = online/slaap) levert hier niets bruikbaars
        op; enkel tekstuele laadstatussen tellen.
        """
        return self.charge_state_with_state()[0]

    def charge_state_with_state(self):
        """(laadstatus, HA-state-object waar hij vandaan komt) — ('', None) als onbekend."""
        device = self.device
        for entity_id in (device.charge_state_sensor, device.status_sensor):
            if not entity_id:
                continue
            st = self.hass.states.get(entity_id)
            if st is None:
                continue
            s = st.state.lower().strip()
            if s in CHARGE_STATES:
                return s, st
        return "", None

    def observed_charging(self) -> bool:
        """True als de EV echt laadt (EVGuard.is_charging).

        Schakelaar 'on', of een laadstatus 'charging'/'starting' (Tesla-
        schakelaars die 'unknown' melden). Bewust NIET 'connected' of
        'complete' zoals de fallback in apply_action: een ingeplugde of volle
        auto laadt niet. Zie ook charging_for_control.
        """
        device = self.device
        charge_state = self.charge_state()
        conf = self.memory.charging_confirmed.get(sw_key(device))
        if (
            conf is not None and charge_state == conf[1]
            and (datetime.now(timezone.utc) - conf[0]).total_seconds() < EV_CHARGING_CONFIRM_S
        ):
            # De wagen weigerde een turn_on met 'is_charging' en de laadstatus
            # in HA is sindsdien niet veranderd (loopt achter): hij laadt.
            return True
        if charge_state:
            # Een gekende laadstatus is leidend: een Tesla-schakelaar blijft
            # vaak 'on' na uitpluggen ('disconnected') of aan de limiet.
            return charge_state in ("charging", "starting")
        sw_state = self.hass.states.get(sw_key(device))
        return sw_state is not None and sw_state.state == "on"

    def stale_stopped_after_start(self, now: datetime) -> bool:
        """True als de laadstatus 'stopped' een overblijfsel van vóór de eigen start is.

        Peak Guard stuurde minder dan EV_STALE_STOPPED_S geleden een echte
        turn_on (note_charge_start; een turn_off wist hem) en de integratie
        meldde sindsdien geen nieuwe laadstatus: de Tesla-integratie peilt maar
        om de ~10 min (veldlog 27-11-2026: 'stopped' van een peiling tijdens
        een eigen stop bleef staan terwijl de wagen weer 8 A laadde).
        Echt is een 'stopped' die na de start veranderde (last_changed), of
        die minstens EV_STALE_REPORT_GRACE_S na de start (opnieuw) gemeld werd
        (last_reported) — een peiling vlak na de turn_on toont nog de oude
        toestand. 'complete'/'disconnected' gelden altijd.
        """
        started = self.memory.charge_started.get(sw_key(self.device))
        if started is None or not (0 <= (now - started).total_seconds() < EV_STALE_STOPPED_S):
            return False
        charge_state, st = self.charge_state_with_state()
        if charge_state != "stopped":
            return False
        reported = getattr(st, "last_reported", None) or getattr(st, "last_updated", None)
        changed = getattr(st, "last_changed", None) or reported
        if isinstance(changed, datetime) and changed > started:
            return False
        return not (
            isinstance(reported, datetime)
            and (reported - started).total_seconds() >= EV_STALE_REPORT_GRACE_S
        )

    def charging_for_control(self, now: datetime) -> bool:
        """Laadt de wagen, voor het bijsturen van een lopende lading?

        observed_charging plus een verouderde 'stopped' na de eigen start. Voor
        het verlagen, stoppen en het geschatte laadvermogen (piek, solar, einde
        laadvenster). Niet voor startbevestigingen (schema-herstart,
        stopped_by_pg): daar telt enkel wat de wagen echt meldt.
        """
        return self.observed_charging() or self.stale_stopped_after_start(now)

    def stopped_by_pg(self) -> bool:
        """Staat de lader uit voor het herstel na piekbeperking? (EVGuard.peak_is_off)"""
        if self.stopped_by_pg_not_restarted():
            # Door Peak Guard gestopt en niet herstart: de laadstatus van een
            # Tesla meldt na turn_off vaak nog een of meer cycli 'charging'.
            return True
        # Laadstatus i.p.v. de schakelaar: die van een Tesla blijft vaak 'unknown'.
        return not self.observed_charging()

    # ------------------------------------------------------------------ #
    #  Wat Peak Guard zelf deed (tijdvensters)                             #
    # ------------------------------------------------------------------ #
    #
    # Vier vensters bestaan bewust naast elkaar (zie CLAUDE.md, "Charger
    # state"): SCHEDULE_START_CONFIRM_S (3 min, just_started),
    # PEAK_RESTART_CONFIRM_S (3 min, just_stopped_by_pg),
    # EV_CHARGING_CONFIRM_S (10 min, pg_start_with_stale_stopped en de
    # 'is_charging'-bevestiging) en EV_STALE_STOPPED_S (20 min,
    # stale_stopped_after_start, met EV_STALE_REPORT_GRACE_S 2 min marge).

    def _guard(self) -> Optional[EVDeviceGuard]:
        return self.guards.get(self.device.id)

    def stopped_by_pg_not_restarted(self) -> bool:
        """Peak Guard stopte de lader en startte hem sindsdien niet opnieuw."""
        guard = self._guard()
        return (
            guard is not None and guard.last_switch_state is False
            and bool(guard.turned_off_by_pg)
        )

    def just_started(self, now: datetime) -> bool:
        """Minder dan SCHEDULE_START_CONFIRM_S sinds turned_on_at (start door
        Peak Guard of een herkende manuele start): de laadstatus loopt nog achter."""
        guard = self._guard()
        return (
            guard is not None and guard.turned_on_at is not None
            and (now - guard.turned_on_at).total_seconds() < SCHEDULE_START_CONFIRM_S
        )

    def just_stopped_by_pg(self, now: datetime) -> bool:
        """Minder dan PEAK_RESTART_CONFIRM_S sinds de eigen turn_off: een Tesla
        toont dan vaak nog 'charging'."""
        guard = self._guard()
        return (
            guard is not None and bool(guard.turned_off_by_pg)
            and guard.turned_off_at is not None
            and (now - guard.turned_off_at).total_seconds() < PEAK_RESTART_CONFIRM_S
        )

    def pg_start_with_stale_stopped(self, now: datetime) -> bool:
        """De oudere piekregel: laadstatus 'stopped', schakelaar volgens Peak
        Guard aan en turned_on_at minder dan EV_CHARGING_CONFIRM_S geleden. Dekt
        ook een manuele start en een start van vóór een herstart van HA."""
        guard = self._guard()
        return (
            self.charge_state() == "stopped"
            and guard is not None and guard.last_switch_state is True
            and guard.turned_on_at is not None
            and (now - guard.turned_on_at).total_seconds() < EV_CHARGING_CONFIRM_S
        )

    # ------------------------------------------------------------------ #
    #  Antwoorden per stuurpad (bewust verschillend, met een naam)         #
    # ------------------------------------------------------------------ #

    def charging_for_throttle(self, now: datetime) -> bool:
        """Laadt de wagen, voor de solar-verlaging (throttle_down_solar)?

        charging_for_control; zonder laadstatus en met een schakelaar zonder
        toestand (Tesla zonder charge_state_sensor) wat Peak Guard zelf weet.
        """
        charging = self.charging_for_control(now)
        if not charging and not self.charge_state():
            _sw = self.hass.states.get(sw_key(self.device))
            if _sw is None or _sw.state in ("unknown", "unavailable"):
                guard = self._guard()
                charging = (
                    guard is not None and guard.state == EVState.CHARGING
                    and guard.last_switch_state is True
                )
        return charging

    def already_stopped_for_solar_restore(self, sw_state, now: datetime) -> bool:
        """Is de wagen al gestopt, voor het solar-herstel (geen turn_off nodig)?

        Enkel een schakelaar zonder bruikbare toestand (Tesla: 'unknown')
        wordt via de laadstatus beoordeeld; een schakelaar die 'on' meldt, gaat
        altijd uit (anders laadt een wagen die later weer ingeplugd wordt van
        het net). Net gestart: de laadstatus loopt nog achter. Een gekende
        laadstatus die niet 'laden' is, telt als uit.
        """
        return sw_state.state == "off" or (
            sw_state.state in ("unknown", "unavailable")
            and not self.just_started(now)
            and self.charge_state() != "" and not self.charging_for_control(now)
        )

    def switch_on_for_apply(self, sw_state, now: datetime) -> bool:
        """Staat de lader aan, voor apply_action (piek en solar)?

        De schakelaar; bij 'unknown'/'unavailable' een status-sensor die
        on/charging/connected/complete meldt (hier tellen 'connected' en
        'complete' bewust als aan, anders dan in observed_charging), of
        charging_for_control. Zo gaat er geen onnodige turn_on naar een al
        ladende wagen en legt het snapshot geen 'unknown' als origineel vast.
        """
        device = self.device
        if sw_state.state == "on":
            return True
        if sw_state.state not in ("unknown", "unavailable"):
            return False
        if device.status_sensor:
            _st_fallback = self.hass.states.get(device.status_sensor)
            if _st_fallback is not None and _st_fallback.state not in ("unknown", "unavailable", ""):
                if _st_fallback.state in ("on", "charging", "connected", "complete"):
                    _LOGGER.debug(
                        "Peak Guard EV: schakelaar '%s' = '%s' maar status-sensor '%s' = '%s' — "
                        "behandeld als AAN (auto laadt al)",
                        sw_key(device), sw_state.state, device.status_sensor, _st_fallback.state,
                    )
                    return True
        # Zelfde fallback via de laadstatus (charge_state_sensor), een
        # bevestigde 'is_charging'-weigering, of een 'stopped' die nog van
        # vóór de eigen start dateert — ook zonder status-sensor.
        return self.charging_for_control(now)

    def peak_already_stopped(self, now: datetime) -> bool:
        """Piekpad: door Peak Guard gestopt en niet herstart — niets af te schakelen.

        Vlak na de eigen turn_off toont een Tesla vaak nog 'charging'; daarna
        telt enkel een laadstatus 'charging' (manueel herstart).
        """
        guard = self._guard()
        return guard is not None and guard.last_switch_state is False and (
            self.just_stopped_by_pg(now) or not self.observed_charging()
        )

    def peak_not_charging(self, now: datetime) -> bool:
        """Piekpad: een gekende laadstatus zegt dat de wagen niet laadt.

        Vlak na een eigen start toont een Tesla nog minuten 'stopped':
        charging_for_control telt dat als ladend, en daarnaast de oudere regel
        pg_start_with_stale_stopped — liever een call te veel dan een piek.
        'complete'/'disconnected' gelden altijd.
        """
        return (
            bool(self.charge_state()) and not self.charging_for_control(now)
            and not self.pg_start_with_stale_stopped(now)
        )

    def refusal_was_manual_start(self) -> bool:
        """Een 'is_charging'-weigering bij een solar-start: was het een
        manuele/app-start? Enkel de eigen SoC-override kan een volle wagen
        ('complete') hervat hebben; elke andere laadstatus op dat moment
        betekent dat iemand anders hem startte."""
        return self.confirmed_state() not in ("complete", "fully_charged")

    def confirmed_state(self) -> Optional[str]:
        """Laadstatus in HA bij de laatste 'is_charging'-weigering (None = geen)."""
        conf = self.memory.charging_confirmed.get(sw_key(self.device))
        return conf[1] if conf is not None else None

    def charge_complete(self) -> bool:
        """True als de laadstatus meldt dat de auto aan zijn laadlimiet zit."""
        return self.charge_state() in ("complete", "fully_charged")

    # ------------------------------------------------------------------ #
    #  Stroom en vermogen                                                  #
    # ------------------------------------------------------------------ #

    def current_a(self) -> Optional[float]:
        """Laadstroom volgens de stroom-entity (A); None als onbekend."""
        device = self.device
        if not device.current_entity:
            return None
        st = self.hass.states.get(device.current_entity)
        if st is None:
            return None
        try:
            return float(st.state)
        except (ValueError, TypeError):
            return None

    def effective_current_a(
        self,
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
                self.device.name, age_s, guard.last_sent_amps, current_a,
            )
            return guard.last_sent_amps
        return current_a

    def draw_w(self, now: Optional[datetime] = None) -> float:
        """Geschat laadvermogen (W); 0 als de EV niet laadt."""
        if not self.charging_for_control(now or datetime.now(timezone.utc)):
            return 0.0
        guard = self.guards.get(self.device.id)
        amps = self.current_a()
        if amps is None and guard is not None:
            amps = guard.last_sent_amps
        if amps is None:
            amps = self.hw_min_a
        return max(0.0, amps) * self.voltage

    # ------------------------------------------------------------------ #
    #  Kabel, wakker, locatie                                              #
    # ------------------------------------------------------------------ #

    def cable_connected(self) -> bool:
        """
        Geeft True als de laadkabel aangesloten is, of als er geen kabelentity
        expliciet geconfigureerd is.

        Truthy-states: on, true, connected, charging, complete, fully_charged,
        pending, stopped, starting, nopower, 1 — of een numerieke waarde > 0.
        "stopped"/"starting"/"nopower" zijn Tesla-laderstaten waarbij de kabel
        WEL aangesloten is maar het laden tijdelijk gestopt/gepauzeerd is.
        Bij unavailable/unknown: True (niet blokkeren bij tijdelijke storing).
        """
        device = self.device
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

    def awake(self) -> bool:
        """
        Geeft True als de EV verbonden/wakker is (EVGuard.is_connected).

        States die als "verbonden/wakker" gelden: on, true, connected, online, home,
        charging, fully_charged, pending, 1.
        "complete" is bewust weggelaten: Tesla-auto's slapen vaak in die staat.
        "unavailable"/"unknown": als een wake-button geconfigureerd is, beschouwen we
        dit als slapend (False) zodat de wake-up getriggerd wordt. Zonder wake-button
        blokkeren we niet (True).
        Bij ontbrekende sensor: True (geen blokkering).
        """
        device = self.device
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

    def home(self, guard: "Optional[EVDeviceGuard]" = None) -> bool:
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
        device = self.device
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
