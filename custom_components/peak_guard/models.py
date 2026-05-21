"""
Peak Guard — models.py

Alle dataclasses, enums en waarde-objecten die door meerdere modules
worden gebruikt.  Geen HA-afhankelijkheden; puur Python.
"""

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Deque, Optional


# ──────────────────────────────────────────────────────────────────────────── #
#  EV spanning-constanten                                                       #
# ──────────────────────────────────────────────────────────────────────────── #

# 1-fase: U = 230 V  →  P = A × 230
# 3-fasen: U = 400 V  →  P = A × 400
EV_VOLTS_1PHASE: float = 230.0
EV_VOLTS_3PHASE: float = 400.0


# ──────────────────────────────────────────────────────────────────────────── #
#  EV Rate-limiting & hysteresis constanten                                     #
#  (allemaal instelbaar zonder logica aan te raken)                             #
# ──────────────────────────────────────────────────────────────────────────── #

# Minimale amp-delta voor een set_value call.
# Tesla negeert sub-1A stappen; 1 A ≈ 230–400 W.
EV_HYSTERESIS_AMPS: float = 1.0

# Minimale seconden tussen opeenvolgende stroom-aanpassingen voor één EV-apparaat.
EV_MIN_UPDATE_INTERVAL_S: float = 20.0

# Tijdvenster (seconden) waarbinnen de surplus-geschiedenis wordt opgebouwd.
# De EV start pas nadat dit venster volledig gevuld is met positieve waarden.
EV_DEBOUNCE_STABLE_S: float = 20.0

# Percentiel van de surplus-geschiedenis dat als "veilige ondergrens" (floor) wordt gebruikt.
# 10 % betekent: de laadstroom wordt bepaald op de waarde die 90 % van de tijd gehaald wordt.
# Lager = conservatiever (minder oscillatierisico), hoger = agressiever.
EV_FLOOR_PERCENTILE: int = 10

# Na het AAN-zetten van de lader weigeren we hem gedurende deze tijd UIT te zetten.
EV_MIN_ON_DURATION_S: float = 360.0     # 6 minuten

# Na het UIT-zetten van de lader weigeren we hem gedurende deze tijd AAN te zetten.
EV_MIN_OFF_DURATION_S: float = 300.0    # 5 minuten

# Maximale wachttijd (seconden) om te wachten tot de EV wakker is na wake-up.
EV_WAKE_TIMEOUT_S: float = 15.0

# Retry-gedrag voor EV schakelaarcommando's (bv. "Command was unsuccessful" van Tesla API).
EV_CMD_MAX_RETRIES: int = 2        # 2 extra pogingen = 3 totaal
EV_CMD_RETRY_DELAY_S: float = 3.0  # seconden wachten tussen pogingen

# Globale rate-limiter: maximale EV-service calls per rollend venster.
EV_RATE_LIMIT_MAX_CALLS: int = 12
EV_RATE_LIMIT_WINDOW_S: float = 600.0   # 10 minuten

# Stroom-sensor ouder dan dit (seconden) wordt als stale beschouwd.
# PG gebruikt dan last_sent_amps als referentie i.p.v. de (verouderde) sensorwaarde.
# Bedoeld als workaround voor traag-updatende integraties zoals Tesla Fleet.
EV_SENSOR_STALE_S: float = 180.0

# Na een mislukte wake-up poging: wachttijd (seconden) vóór de volgende poging.
# 900 s = 15 minuten. Voorkomt dat een niet-thuis of slapende auto tientallen
# API-calls per uur genereert.
EV_WAKE_COOLDOWN_S: float = 900.0


# ──────────────────────────────────────────────────────────────────────────── #
#  EV State machine                                                             #
# ──────────────────────────────────────────────────────────────────────────── #

class EVState(Enum):
    IDLE                = "idle"
    CHARGING            = "charging"
    WAITING_FOR_STABLE  = "waiting_for_stable_surplus"
    CABLE_DISCONNECTED  = "cable_disconnected"   # laadkabel niet aangesloten
    SLEEPING            = "sleeping"              # EV in slaapstand, wake-up bezig


# ──────────────────────────────────────────────────────────────────────────── #
#  Per-apparaat EV rate-limit / debounce toestand                               #
# ──────────────────────────────────────────────────────────────────────────── #

@dataclass
class EVDeviceGuard:
    """
    Alle per-apparaat rate-limiting & debounce-toestand voor één EV-lader.

    Leeft in EVGuard._guards[device.id].
    Reset bij HA-herstart; bewust NIET opgeslagen (veilige standaarden bij boot).
    """
    # ---- state machine ------------------------------------------------ #
    state: EVState = EVState.IDLE

    # ---- laatste verzonden waarden (voor redundante calls) ------------ #
    last_sent_amps:    Optional[float] = None   # ampère werkelijk verzonden
    last_switch_state: Optional[bool]  = None   # True=aan, False=uit

    # ---- tijdstempels ------------------------------------------------- #
    last_current_update: Optional[datetime] = None   # laatste set_value call
    turned_on_at:        Optional[datetime] = None   # wanneer laatste keer AAN gezet
    turned_off_at:       Optional[datetime] = None   # wanneer laatste keer UIT gezet
    wake_requested_at:   Optional[datetime] = None   # wake-up button aangeroepen

    # ---- debounce ringbuffer ------------------------------------------ #
    # Slaat (tijdstempel, surplus_W) tuples op voor stabiliteitscheck
    surplus_history: Deque = field(default_factory=lambda: deque(maxlen=60))

    # ---- wallclock debounce-timer ------------------------------------- #
    # Gezet op het eerste moment waarop het surplus boven de start-drempel
    # uitkwam. Gereset via EVGuard._reset_debounce() zodra het surplus
    # wegvalt of de EV start. Ontkoppelt de debounce-timing volledig van
    # het loop-interval zodat EV_DEBOUNCE_STABLE_S altijd klopt.
    debounce_start_at:    Optional[datetime] = None
    debounce_remaining_s: float             = 0.0   # seconden tot debounce klaar (GUI)
    debounce_floor_w:     float             = 0.0   # tentatieve floor-waarde in W (GUI)

    # ---- debounce doelwaarde ------------------------------------------ #
    # Laadstroom (A) die PG wil instellen zodra het surplus stabiel is.
    # Ingesteld bij elke evaluatie zodat de GUI de gewenste actie kan tonen.
    pending_amps: Optional[int] = None

    # True als Peak Guard zelf de laatste turn_off heeft gegeven (niet de gebruiker).
    # Wordt gebruikt om te bepalen of de min-OFF-duur gate van toepassing is.
    turned_off_by_pg: bool = False

    # Meest recente reden waarom de solar-evaluatie werd overgeslagen.
    # Leeg als er geen skip was of als laden actief is.
    skip_reason: str = ""

    # True als PG de SOC-laadlimiet heeft verhoogd via _set_soc_override(override=True).
    # Voorkomt herhaalde API-calls per loop-iteratie.
    soc_override_active: bool = False

    # Tijdstip tot wanneer wake-up pogingen worden geblokkeerd na een mislukte poging.
    # Reset bij succesvolle wake-up of als de EV zichzelf aanmeldt.
    wake_cooldown_until: Optional[datetime] = None


# ──────────────────────────────────────────────────────────────────────────── #
#  Globale EV rate-limiter                                                      #
# ──────────────────────────────────────────────────────────────────────────── #

class EVRateLimiter:
    """
    Sliding-window rate-limiter gedeeld door ALLE EV-lader service calls.

    Bijgehouden tijdstempels van recente calls; weigert nieuwe als het venster vol is.
    """

    def __init__(
        self,
        max_calls: int = EV_RATE_LIMIT_MAX_CALLS,
        window_s: float = EV_RATE_LIMIT_WINDOW_S,
    ) -> None:
        self._max_calls = max_calls
        self._window_s = window_s
        self._call_times: Deque[datetime] = deque()

    def _purge_old(self, now: datetime) -> None:
        cutoff = now - timedelta(seconds=self._window_s)
        while self._call_times and self._call_times[0] < cutoff:
            self._call_times.popleft()

    def is_allowed(self, now: Optional[datetime] = None) -> bool:
        now = now or datetime.now(timezone.utc)
        self._purge_old(now)
        return len(self._call_times) < self._max_calls

    def record(self, now: Optional[datetime] = None) -> None:
        now = now or datetime.now(timezone.utc)
        self._purge_old(now)
        self._call_times.append(now)

    @property
    def calls_in_window(self) -> int:
        self._purge_old(datetime.now(timezone.utc))
        return len(self._call_times)

    @property
    def remaining(self) -> int:
        return max(0, self._max_calls - self.calls_in_window)


# ──────────────────────────────────────────────────────────────────────────── #
#  Cascade dataclasses                                                           #
# ──────────────────────────────────────────────────────────────────────────── #

@dataclass
class EVChargerConfig:
    """EV-lader instellingen — onderdeel van CascadeDevice voor action_type == 'ev_charger'."""
    switch_entity:     Optional[str]   = None   # schakelaar-entity (valt terug op entity_id)
    current_entity:    Optional[str]   = None   # laadstroom-number entity
    soc_entity:        Optional[str]   = None   # SOC-limiet-number entity
    battery_entity:    Optional[str]   = None   # huidig batterijniveau sensor
    max_soc:           Optional[int]   = None   # gewenst maximum SOC % bij zonne-overschot
    phases:            int             = 1      # aantal fasen (1 of 3)
    min_current:       Optional[float] = None   # hardware-minimum laadstroom (A)
    start_threshold_w: Optional[float] = None   # solar start-drempel (W)
    cable_entity:      Optional[str]   = None   # kabelaansluiting-sensor
    wake_button:       Optional[str]   = None   # button.* om EV wakker te maken
    status_sensor:     Optional[str]   = None   # verbindingsstatus-sensor
    location_tracker:  Optional[str]   = None   # device_tracker.* — thuis = home/on


@dataclass
class CascadeDevice:
    """
    Beschrijft een apparaat in een cascade.

    EV-laders (action_type == 'ev_charger') hebben hun configuratie in het ev-veld.
    Alle andere action_types laten ev op None.

    Velden voor throttle (legacy): min_value, max_value, power_per_unit.

    Serialisatie: to_dict() geeft een plat dict (backward compat met opgeslagen JSON
    en het frontend-paneel). from_dict() herstelt vanuit datzelfde platte formaat.
    """
    id:             str
    name:           str
    entity_id:      str
    priority:       int
    action_type:    str
    power_watts:    int = 0
    min_value:      Optional[float] = None
    max_value:      Optional[float] = None
    power_per_unit: Optional[float] = None
    enabled:        bool = True
    ev:             Optional[EVChargerConfig] = None

    def to_dict(self) -> dict:
        d: dict = {
            "id":             self.id,
            "name":           self.name,
            "entity_id":      self.entity_id,
            "priority":       self.priority,
            "action_type":    self.action_type,
            "power_watts":    self.power_watts,
            "min_value":      self.min_value,
            "max_value":      self.max_value,
            "power_per_unit": self.power_per_unit,
            "enabled":        self.enabled,
        }
        if self.ev is not None:
            d.update({
                "ev_switch_entity":    self.ev.switch_entity,
                "ev_current_entity":   self.ev.current_entity,
                "ev_soc_entity":       self.ev.soc_entity,
                "ev_battery_entity":   self.ev.battery_entity,
                "ev_max_soc":          self.ev.max_soc,
                "ev_phases":           self.ev.phases,
                "ev_min_current":      self.ev.min_current,
                "start_threshold_w":   self.ev.start_threshold_w,
                "ev_cable_entity":     self.ev.cable_entity,
                "ev_wake_button":      self.ev.wake_button,
                "ev_status_sensor":    self.ev.status_sensor,
                "ev_location_tracker": self.ev.location_tracker,
            })
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "CascadeDevice":
        """Herstel vanuit een plat dict (opgeslagen formaat of API-payload)."""
        ev: Optional[EVChargerConfig] = None
        if d.get("action_type") == "ev_charger":
            ev = EVChargerConfig(
                switch_entity     = d.get("ev_switch_entity"),
                current_entity    = d.get("ev_current_entity"),
                soc_entity        = d.get("ev_soc_entity"),
                battery_entity    = d.get("ev_battery_entity"),
                max_soc           = d.get("ev_max_soc"),
                phases            = d.get("ev_phases", 1),
                min_current       = d.get("ev_min_current"),
                start_threshold_w = d.get("start_threshold_w"),
                cable_entity      = d.get("ev_cable_entity"),
                wake_button       = d.get("ev_wake_button"),
                status_sensor     = d.get("ev_status_sensor"),
                location_tracker  = d.get("ev_location_tracker"),
            )
        return cls(
            id            = d["id"],
            name          = d["name"],
            entity_id     = d["entity_id"],
            priority      = d["priority"],
            action_type   = d["action_type"],
            power_watts   = d.get("power_watts", 0),
            min_value     = d.get("min_value"),
            max_value     = d.get("max_value"),
            power_per_unit = d.get("power_per_unit"),
            enabled       = d.get("enabled", True),
            ev            = ev,
        )


@dataclass
class DeviceSnapshot:
    """Oorspronkelijke staat van een apparaat voor een Peak Guard ingreep."""
    entity_id:        str
    original_state:   str
    original_current: Optional[float] = None
    original_soc:     Optional[float] = None
