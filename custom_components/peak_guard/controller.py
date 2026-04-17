import asyncio
import logging
import math
from collections import deque
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Deque, Dict, List, Optional

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store

from .avoided_peak_tracker import PeakAvoidTracker, SolarShiftTracker
from .deciders import EVGuard, InjectionDecider, PeakDecider

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
    DEFAULT_EV_MIN_AMPERE,
    DEFAULT_EV_MAX_AMPERE,
    DEFAULT_EV_CABLE_ENTITY,
    DEFAULT_EV_SOLAR_START_THRESHOLD_W,
    DEFAULT_EV_SOLAR_STOP_THRESHOLD_W,
    ACTION_SWITCH_OFF,
    ACTION_SWITCH_ON,
    ACTION_THROTTLE,
    ACTION_EV_CHARGER,
)

# EV: spanning afhankelijk van het aantal fasen.
# 1-fase: U = 230 V  →  P = A × 230
# 3-fasen: U = 400 V  →  P = A × 400
EV_VOLTS_1PHASE: float = 230.0
EV_VOLTS_3PHASE: float = 400.0

_LOGGER = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────── #
#  EV Rate-limiting & hysteresis constants                                     #
#  (all tunable without touching logic)                                        #
# ──────────────────────────────────────────────────────────────────────────── #

# Minimum amp-delta before we bother sending a set_value call.
# Tesla ignores sub-1A steps anyway; 1 A == 230–400 W.
EV_HYSTERESIS_AMPS: float = 1.0

# Minimum seconds between ANY current-adjustment calls for a single EV device.
# Prevents the Tesla API from seeing a call every 5 s when the loop runs fast.
EV_MIN_UPDATE_INTERVAL_S: float = 90.0

# Solar surplus must remain stable (within EV_DEBOUNCE_TOLERANCE_W watts) for
# at least this many seconds before we act on it.  Avoids chasing clouds.
EV_DEBOUNCE_STABLE_S: float = 45.0
EV_DEBOUNCE_TOLERANCE_W: float = 150.0   # ±150 W counts as "stable"

# When surplus exceeds this threshold the debounce is skipped entirely — a very
# large surplus signals solid solar production, not a brief cloud break.
EV_DEBOUNCE_BYPASS_SURPLUS_W: float = 1500.0

# After turning the charger ON we refuse to turn it back OFF for this long.
# Prevents rapid ON/OFF cycling that hammers the Tesla API.
EV_MIN_ON_DURATION_S: float = 360.0     # 6 minutes

# After turning the charger OFF we refuse to turn it ON again for this long.
EV_MIN_OFF_DURATION_S: float = 300.0    # 5 minutes

# Maximale wachttijd (seconden) om te wachten tot de EV wakker is na wake-up.
# De controller pollt elke seconde en start laden zodra de status "on" is.
EV_WAKE_TIMEOUT_S: float = 15.0

# Global rate limiter: maximum EV-related service calls per rolling window.
EV_RATE_LIMIT_MAX_CALLS: int = 12
EV_RATE_LIMIT_WINDOW_S: float = 600.0   # 10 minutes


# ──────────────────────────────────────────────────────────────────────────── #
#  EV State machine                                                            #
# ──────────────────────────────────────────────────────────────────────────── #

class EVState(Enum):
    IDLE                    = "idle"
    CHARGING                = "charging"
    WAITING_FOR_STABLE      = "waiting_for_stable_surplus"
    CABLE_DISCONNECTED      = "cable_disconnected"   # laadkabel niet aangesloten
    SLEEPING                = "sleeping"              # EV in slaapstand, wake-up bezig


# ──────────────────────────────────────────────────────────────────────────── #
#  Per-device EV rate-limit / debounce state                                  #
# ──────────────────────────────────────────────────────────────────────────── #

@dataclass
class EVDeviceGuard:
    """
    All per-device rate-limiting & debounce state for one EV charger.

    Lives inside PeakGuardController._ev_guards[device.id].
    Reset on HA restart; intentionally NOT persisted (safe defaults on boot).
    """
    # ---- state machine ------------------------------------------------ #
    state: EVState = EVState.IDLE

    # ---- last-sent values (to detect redundant calls) ----------------- #
    last_sent_amps: Optional[float] = None   # amps actually sent to HA
    last_switch_state: Optional[bool] = None  # True=on, False=off

    # ---- timestamps --------------------------------------------------- #
    last_current_update: Optional[datetime] = None   # last set_value call
    turned_on_at:      Optional[datetime] = None     # when we last turned ON
    turned_off_at:     Optional[datetime] = None     # when we last turned OFF
    wake_requested_at: Optional[datetime] = None     # wanneer wake-up button is aangeroepen

    # ---- debounce ring buffer ----------------------------------------- #
    # Stores (timestamp, surplus_W) tuples for stability check
    surplus_history: Deque = field(default_factory=lambda: deque(maxlen=60))

    # True when Peak Guard itself issued the last turn_off (not the user).
    # Used to decide whether the min-OFF-duration gate applies.
    turned_off_by_pg: bool = False

    # ---- global rate limiter (shared across all devices) -------------- #
    # NOTE: the actual limiter lives on the controller; this is a back-ref
    # placeholder kept here for potential per-device limiting in the future.


# ──────────────────────────────────────────────────────────────────────────── #
#  Global EV rate limiter                                                      #
# ──────────────────────────────────────────────────────────────────────────── #

class EVRateLimiter:
    """
    Sliding-window rate limiter shared across ALL EV charger service calls.

    Tracks timestamps of recent calls; refuses new ones when the window is full.
    """

    def __init__(
        self,
        max_calls: int = EV_RATE_LIMIT_MAX_CALLS,
        window_s: float = EV_RATE_LIMIT_WINDOW_S,
    ):
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
#  Data classes (unchanged from original)                                      #
# ──────────────────────────────────────────────────────────────────────────── #

@dataclass
class CascadeDevice:
    """
    Beschrijft een apparaat in een cascade.

    Velden voor EV Charger (action_type == 'ev_charger'):
      ev_switch_entity  : entity_id van de oplaadschakelaar (switch)
      ev_current_entity : entity_id van de laadstroom-number entity
      ev_soc_entity     : entity_id van de SOC-limiet-number entity (optioneel)
      ev_battery_entity : entity_id van de sensor die het huidig batterijniveau toont (optioneel)
      ev_max_soc        : gewenst maximumpercentage bij zonne-overschot (0-100)
      ev_phases         : aantal fasen (1 of 3), default 1
      ev_min_current    : hardware-minimum laadstroom (A) — de Tesla accepteert NOOIT minder.
                          Verschilt van min_value (die ook als floor voor peak-cascade dient).
                          Standaard gelijk aan DEFAULT_EV_MIN_AMPERE (6 A) als niet ingesteld.
      ev_cable_entity   : sensor die aangeeft of de laadkabel aangesloten is.
      ev_wake_button    : button-entity om de EV uit slaapstand te halen (bijv. button.tesla_wakker).
                          Optioneel; als niet ingesteld wordt wake-up overgeslagen.
      ev_status_sensor  : sensor die verbindingsstatus toont (bijv. binary_sensor.tesla_status).
                          "connected"/"online"/"on" = verbonden, anders = slapend.
                          Optioneel; als niet ingesteld wordt wake-up check overgeslagen.
      ev_location_tracker : device_tracker of sensor die aangeeft of de EV thuis is.
                          "home" / "on" = thuis; alles anders = niet thuis → laden overgeslagen.
                          Optioneel; als niet ingesteld wordt locatie niet gecontroleerd.
      min_value         : minimale laadstroom (A), default 6
      max_value         : maximale laadstroom (A), default 32

      Vermogenformule EV:
        1-fase: P = A × 230 V  (bv. 32 A → 7 360 W)
        3-fasen: P = A × 400 V  (bv. 16 A → 6 400 W)

    Velden voor throttle (legacy, backwards-compat):
      min_value, max_value, power_per_unit
    """
    id:                 str
    name:               str
    entity_id:          str       # primaire entity (switch voor ev_charger)
    priority:           int
    action_type:        str
    power_watts:        int = 0
    min_value:          Optional[float] = None
    max_value:          Optional[float] = None
    power_per_unit:     Optional[float] = None
    enabled:            bool = True
    # EV-specifieke velden
    ev_switch_entity:   Optional[str] = None
    ev_current_entity:  Optional[str] = None
    ev_soc_entity:      Optional[str] = None
    ev_battery_entity:  Optional[str] = None
    ev_max_soc:         Optional[int] = None
    ev_phases:          int = 1
    ev_min_current:     Optional[float] = None   # hardware-minimum laadstroom (A)
    start_threshold_w:  Optional[float] = None   # solar start-drempel (W), default 230
    ev_cable_entity:    Optional[str]   = None   # sensor die kabelaansluiting detecteert
    ev_wake_button:       Optional[str]   = None   # button.* om EV wakker te maken
    ev_status_sensor:     Optional[str]   = None   # sensor verbindingsstatus EV
    ev_location_tracker:  Optional[str]   = None   # device_tracker.* of sensor — thuis = home/on

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DeviceSnapshot:
    """Oorspronkelijke staat van een apparaat voor een Peak Guard ingreep."""
    entity_id: str
    original_state: str
    original_current: Optional[float] = None
    original_soc: Optional[float] = None


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

        # ── NEW: EV rate-limiting & debounce state ────────────────────── #
        # One EVDeviceGuard per device.id, lazily created.
        self._ev_guards: Dict[str, EVDeviceGuard] = {}
        # One shared global rate limiter for all EV service calls.
        self._ev_rate_limiter = EVRateLimiter()

        # ── Entity listeners: callbacks die worden aangeroepen na elke
        # update_cascade() aanroep, zodat switch/number platforms
        # dynamisch nieuwe entities kunnen aanmaken.
        self._entity_listeners: list = []

        # Laatste reden waarom een apparaat geen actie ondernam (per cascade-run).
        # Wordt gezet door _apply_action en gebruikt door _run_cascade voor logging.
        self._last_skip_reason: str = ""

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
    #  EV guard helpers                                                    #
    # ------------------------------------------------------------------ #

    def _ev_guard(self, device_id: str) -> EVDeviceGuard:
        """Return (creating if needed) the EVDeviceGuard for device_id."""
        if device_id not in self._ev_guards:
            self._ev_guards[device_id] = EVDeviceGuard()
        return self._ev_guards[device_id]

    def _ev_rate_check(self, device_name: str, reason: str) -> bool:
        """
        Return True if we are ALLOWED to make an EV service call right now.
        Logs a warning and returns False when the rate limit would be exceeded.
        """
        if self._ev_rate_limiter.is_allowed():
            return True
        _LOGGER.warning(
            "Peak Guard EV '%s': service call OVERGESLAGEN wegens globale rate-limiter "
            "(%d/%d calls in %.0f s). Reden: %s",
            device_name,
            self._ev_rate_limiter.calls_in_window,
            EV_RATE_LIMIT_MAX_CALLS,
            EV_RATE_LIMIT_WINDOW_S,
            reason,
        )
        return False

    def _ev_record_call(self) -> None:
        """Register that we just made an EV service call."""
        self._ev_rate_limiter.record()

    # ------------------------------------------------------------------ #
    #  Beslissingslog — actie tracking                                     #
    # ------------------------------------------------------------------ #

    def _track_action(
        self,
        entity_id: str,
        action: str,
        value=None,
    ) -> None:
        """Registreer een uitgevoerde service-call voor de beslissingslog.

        Wordt alleen actief gebruikt als CONF_DEBUG_DECISION_LOGGING == True;
        anders is dit een no-op zodat er geen overhead is in productie.
        """
        if not self.config.get(CONF_DEBUG_DECISION_LOGGING, False):
            return
        entry: dict = {"entity_id": entity_id, "action": action}
        if value is not None:
            entry["value"] = value
        self._iteration_actions.append(entry)

    # ------------------------------------------------------------------ #
    #  Kabeldetectie helper                                                 #
    # ------------------------------------------------------------------ #

    def _ev_cable_connected(self, device: "CascadeDevice") -> bool:
        """
        Geeft True als de laadkabel aangesloten is (of als er geen kabelentity is geconfigureerd).

        Een sensor wordt als "kabel aangesloten" beschouwd als de state een van de
        volgende truthy-waarden heeft: "on", "true", "connected", "charging",
        "complete", "1", of een numerieke waarde > 0.
        Bij "off", "false", "disconnected", "unavailable", "unknown" of een lege
        state wordt False teruggegeven.
        """
        cable_entity = device.ev_cable_entity or DEFAULT_EV_CABLE_ENTITY
        if not cable_entity:
            return True  # geen entiteit geconfigureerd → neem aan dat kabel ok is

        state = self.hass.states.get(cable_entity)
        if state is None:
            _LOGGER.debug(
                "Peak Guard EV: kabelentity '%s' niet gevonden voor '%s' — "
                "kabelcheck overgeslagen (aanname: aangesloten)",
                cable_entity, device.name,
            )
            return True  # entity onbekend → niet blokkeren

        s = state.state.lower().strip()
        if s in ("unavailable", "unknown", ""):
            return True  # tijdelijk onbeschikbaar → niet blokkeren

        # Truthy-states: kabel is aangesloten / laden loopt / volledig geladen
        CABLE_ON = {"on", "true", "connected", "charging", "complete",
                    "fully_charged", "pending", "1"}
        if s in CABLE_ON:
            return True

        # Numerieke waarde > 0 → ook aangesloten
        try:
            return float(s) > 0
        except (ValueError, TypeError):
            pass

        # Alles anders (off, false, disconnected, …) → kabel los
        return False

    # ------------------------------------------------------------------ #
    #  EV verbindingsstatus helper (wake-up check)                         #
    # ------------------------------------------------------------------ #

    def _ev_is_connected(self, device: "CascadeDevice") -> bool:
        """
        Geeft True als de EV verbonden/online is, of als er geen status-sensor is.

        Gebruikt om te bepalen of de auto wakker is voor het starten van laden.
        States die als "verbonden" worden beschouwd:
          "on", "true", "connected", "online", "home", "charging", "1"
        Bij ontbrekende of unavailable sensor: True (geen blokkering).
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

        return False  # off, false, disconnected, offline, asleep, …

    # ------------------------------------------------------------------ #
    #  Debounce helper                                                     #
    # ------------------------------------------------------------------ #

    def _ev_surplus_is_stable(
        self,
        guard: EVDeviceGuard,
        current_surplus_w: float,
        now: datetime,
    ) -> bool:
        """
        Return True when the surplus has been above zero and within
        EV_DEBOUNCE_TOLERANCE_W watts (min-to-max spread) for at least
        EV_DEBOUNCE_STABLE_S seconds.

        Vergelijkt samples onderling (spread = max - min), niet allemaal
        met de huidige waarde. Zo wordt een geleidelijk dalend surplus
        (bijv. 1586 → 1403 W) als stabiel beschouwd zolang de spread
        binnen de tolerantie blijft.

        Side-effect: appends (now, surplus) to guard.surplus_history.
        """
        guard.surplus_history.append((now, current_surplus_w))

        cutoff = now - timedelta(seconds=EV_DEBOUNCE_STABLE_S)
        # Houd alleen recente samples (binnen 2× de debounce-window zodat
        # ook bij een 60s loop-interval voldoende samples beschikbaar zijn)
        cutoff2x = now - timedelta(seconds=EV_DEBOUNCE_STABLE_S * 2)
        relevant = [(ts, w) for ts, w in guard.surplus_history if ts >= cutoff2x]

        if not relevant:
            return False

        # Minimaal 2 samples vereist (voorkomt starten op 1 meting)
        if len(relevant) < 2:
            return False

        oldest_ts = relevant[0][0]
        elapsed = (now - oldest_ts).total_seconds()
        if elapsed < EV_DEBOUNCE_STABLE_S:
            # Nog niet genoeg tijd verstreken
            return False

        # Alle samples moeten positief zijn (surplus, niet import)
        values = [w for _, w in relevant]
        if min(values) <= 0:
            return False

        # Spread (max - min) moet binnen tolerantie blijven.
        # Dit accepteert geleidelijk dalende of stijgende surplussen
        # (bijv. 1586 → 1403 W: spread=183 W > 150 W tolerantie).
        # We verdubbelen de tolerantie voor de spread-check om realistische
        # zonne-surplus variaties te accommoderen.
        spread = max(values) - min(values)
        return spread <= EV_DEBOUNCE_TOLERANCE_W * 2

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
                        "state": guard.state.value,
                        "history_len": len(guard.surplus_history),
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

    # ------------------------------------------------------------------ #
    #  Cascade logica — ingreep                                            #
    # ------------------------------------------------------------------ #

    async def _check_peak(self, consumption: float):
        peak = self._sensor_value(self.config.get(CONF_PEAK_SENSOR))
        if peak is None:
            _LOGGER.warning(
                "Peak Guard: piek-sensor '%s' niet beschikbaar — piekcheck overgeslagen",
                self.config.get(CONF_PEAK_SENSOR),
            )
            return
        buffer = float(self.config.get(CONF_BUFFER_WATTS, DEFAULT_BUFFER_WATTS))
        excess = consumption - peak + buffer
        _LOGGER.debug(
            "Peak Guard _check_peak: verbruik=%.0f W, piek=%.0f W, buffer=%.0f W, overschot=%.0f W",
            consumption, peak, buffer, excess,
        )
        if excess > 0:
            enabled_devices = [d for d in self.peak_cascade if d.enabled]
            _LOGGER.warning(
                "Peak Guard [PIEK cascade]: gestart — piek overschreden met %.0f W "
                "(verbruik=%.0f W, piekgrens=%.0f W, buffer=%.0f W, "
                "%d apparaat/apparaten: %s)",
                excess, consumption, peak, buffer, len(enabled_devices),
                ", ".join(f"'{d.name}'" for d in enabled_devices) or "–",
            )
            if not enabled_devices:
                _LOGGER.warning("Peak Guard: geen actieve apparaten in piek-cascade — niets te doen!")
            await self._run_cascade(self.peak_cascade, excess, self._peak_snapshots, "peak")

    async def _check_injection(self, consumption: float):
        injection = abs(consumption)
        buffer = float(self.config.get(CONF_BUFFER_WATTS, DEFAULT_BUFFER_WATTS))
        _LOGGER.debug(
            "Peak Guard _check_injection: injectie=%.0f W, buffer=%.0f W, actief=%d snapshot(s)",
            injection, buffer, len(self._inject_snapshots),
        )
        if injection > buffer:
            enabled_devices = [d for d in self.inject_cascade if d.enabled]
            _LOGGER.warning(
                "Peak Guard [SOLAR cascade]: gestart — overschot = %.0f W "
                "(buffer=%.0f W, %d apparaat/apparaten: %s)",
                injection, buffer, len(enabled_devices),
                ", ".join(f"'{d.name}'" for d in enabled_devices) or "–",
            )
            if not enabled_devices:
                _LOGGER.warning(
                    "Peak Guard: geen actieve apparaten in inject-cascade — "
                    "%.0f W wordt teruggeleverd aan het net zonder actie!",
                    injection,
                )
            await self._run_cascade(self.inject_cascade, injection, self._inject_snapshots, "solar")
        else:
            _LOGGER.debug(
                "Peak Guard: injectie %.0f W ≤ buffer %.0f W — geen actie vereist",
                injection, buffer,
            )

    # ------------------------------------------------------------------ #
    #  Cascade logica — herstel                                            #
    # ------------------------------------------------------------------ #

    async def _check_peak_restore(self, consumption: float):
        if not self._peak_snapshots:
            return
        peak = self._sensor_value(self.config.get(CONF_PEAK_SENSOR))
        if peak is None:
            return
        buffer = float(self.config.get(CONF_BUFFER_WATTS, DEFAULT_BUFFER_WATTS))
        headroom = peak - buffer - consumption
        if headroom <= 0:
            _LOGGER.debug(
                "Peak Guard [PIEK] herstel geblokkeerd: verbruik=%.0f W, "
                "piekgrens=%.0f W, buffer=%.0f W → headroom=%.0f W (≤ 0)",
                consumption, peak, buffer, headroom,
            )
            return

        snapshots_to_restore = self._get_restore_candidates(
            self.peak_cascade, self._peak_snapshots, reverse=True
        )
        if not snapshots_to_restore:
            return

        # Herstel ALLE kandidaten in omgekeerde prioriteitsvolgorde (laagste
        # prioriteit eerst = het apparaat dat als laatste werd uitgeschakeld).
        # Per apparaat reduceren we de beschikbare headroom met zijn vermogen
        # zodat volgende apparaten alleen worden ingeschakeld als er nog marge is.
        remaining_headroom = headroom
        any_restored = False
        for device, snapshot in snapshots_to_restore:
            nominal_w = float(device.power_watts) if device.power_watts else 0.0
            if nominal_w > 0 and remaining_headroom < nominal_w:
                _LOGGER.info(
                    "Peak Guard [PIEK]:   · '%s' herstel GEBLOKKEERD — "
                    "headroom %.0f W < nominaal %.0f W (te weinig marge)",
                    device.name, remaining_headroom, nominal_w,
                )
                # Sla dit apparaat over maar ga door met de rest: een kleiner
                # apparaat (lagere prioriteit) kan mogelijk wél nog passen.
                continue

            restored = await self._restore_device(device, snapshot)
            if restored:
                del self._peak_snapshots[device.entity_id]
                remaining_headroom -= nominal_w   # reserveer vermogen voor volgende check
                any_restored = True
                _LOGGER.info(
                    "Peak Guard [PIEK]: '%s' terug AAN — headroom was %.0f W "
                    "(piek=%.0f W, buffer=%.0f W, verbruik=%.0f W)",
                    device.name, headroom, peak, buffer, consumption,
                )
        if any_restored:
            await self.async_save()

    async def _check_inject_restore(self, consumption: float):
        if not self._inject_snapshots:
            return
        _LOGGER.debug(
            "Peak Guard _check_inject_restore: verbruik=%.0f W, %d snapshot(s) actief",
            consumption, len(self._inject_snapshots),
        )
        if consumption < 0:
            _LOGGER.debug(
                "Peak Guard: inject-herstel geblokkeerd — verbruik nog negatief (%.0f W)",
                consumption,
            )
            return
        snapshots_to_restore = self._get_restore_candidates(
            self.inject_cascade, self._inject_snapshots, reverse=True
        )
        if not snapshots_to_restore:
            return
        device, snapshot = snapshots_to_restore[0]
        restored = await self._restore_device(device, snapshot)
        if restored:
            del self._inject_snapshots[device.entity_id]
            _LOGGER.info("Peak Guard: '%s' hersteld", device.name)
            await self.async_save()

    def _get_restore_candidates(
        self,
        cascade: List[CascadeDevice],
        snapshots: Dict[str, DeviceSnapshot],
        reverse: bool = True,
    ) -> List[tuple]:
        candidates = []
        for device in cascade:
            if device.entity_id in snapshots:
                candidates.append((device, snapshots[device.entity_id]))
        candidates.sort(key=lambda x: x[0].priority, reverse=reverse)
        return candidates

    async def _restore_device(self, device: CascadeDevice, snapshot: DeviceSnapshot) -> bool:
        state = self.hass.states.get(device.entity_id)
        if state is None:
            _LOGGER.warning(
                "Peak Guard: kan '%s' niet herstellen — entity niet gevonden", device.name
            )
            return False

        try:
            if device.action_type == ACTION_SWITCH_OFF:
                if snapshot.original_state == "on" and state.state != "on":
                    await self.hass.services.async_call(
                        "switch", "turn_on", {"entity_id": device.entity_id}, blocking=True
                    )
                    _LOGGER.info(
                        "Peak Guard: '%s' terug ingeschakeld na piekbeperking "
                        "(originele staat: %s)",
                        device.name, snapshot.original_state,
                    )
                    self.peak_tracker.start_measurement_on_turnon(
                        device_id=device.id,
                        device_name=device.name,
                        ts=datetime.now(timezone.utc),
                    )
                elif snapshot.original_state == "on" and state.state == "on":
                    # Apparaat staat al terug aan (bijv. manueel ingeschakeld vóór
                    # Peak Guard het kon herstellen). Transiteer expliciet via
                    # pending → active → direct afgerond (0 duur → geen event),
                    # zodat _pending correct opgeruimd wordt en er geen stale state
                    # overblijft tot aan de maandwissel.
                    now_ts = datetime.now(timezone.utc)
                    self.peak_tracker.start_measurement_on_turnon(
                        device_id=device.id, device_name=device.name, ts=now_ts
                    )
                    self.peak_tracker.complete_peak_calculation(
                        device_id=device.id, now=now_ts
                    )
                    _LOGGER.debug(
                        "Peak Guard: '%s' stond al aan bij herstel — "
                        "meting opgeruimd (0 duur, geen event)",
                        device.name,
                    )
                return True

            if device.action_type == ACTION_SWITCH_ON:
                if snapshot.original_state == "off" and state.state != "off":
                    await self.hass.services.async_call(
                        "switch", "turn_off", {"entity_id": device.entity_id}, blocking=True
                    )
                    _LOGGER.info(
                        "Peak Guard: '%s' terug uitgeschakeld na injectiepreventie",
                        device.name,
                    )
                    event = self.solar_tracker.complete_solar_calculation(
                        device_id=device.id,
                        now=datetime.now(timezone.utc),
                    )
                    if event:
                        _LOGGER.info(
                            "Peak Guard: solar-event afgerond voor '%s' — "
                            "duur=%.1f min, verschoven=%.4f kWh, besparing=€%.4f",
                            device.name, event.measured_duration_min,
                            event.shifted_kwh, event.savings_euro,
                        )
                return True

            if device.action_type == ACTION_THROTTLE:
                original = float(snapshot.original_state)
                current  = float(state.state)
                new_value = round(original, 1)
                if new_value != round(current, 1):
                    await self.hass.services.async_call(
                        "number", "set_value",
                        {"entity_id": device.entity_id, "value": new_value},
                        blocking=True,
                    )
                    _LOGGER.info(
                        "Peak Guard: '%s' hersteld %s → %s", device.name, current, new_value
                    )
                return True

            if device.action_type == ACTION_EV_CHARGER:
                return await self._restore_ev(device, snapshot)

        except HomeAssistantError as err:
            _LOGGER.warning(
                "Peak Guard: '%s' niet bereikbaar bij herstel — volgende cyclus opnieuw proberen (%s)",
                device.name, err,
            )
        except (ValueError, TypeError) as err:
            _LOGGER.error("Peak Guard: fout bij herstellen '%s': %s", device.name, err)

        return False

    async def _restore_ev(self, device: CascadeDevice, snapshot: DeviceSnapshot) -> bool:
        """
        Herstel EV Charger na een Peak Guard ingreep.

        PIEKBEPERKING (orig. state = "on"):
          - Zet schakelaar terug aan
          - Herstel laadstroom naar originele waarde
          - Start duurmeting in peak_tracker

        INJECTIEPREVENTIE (orig. state = "off"):
          - Verwijder SOC-override
          - Zet laadstroom terug
          - Zet schakelaar uit
          - Voltooi duurmeting in solar_tracker
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
                now_ts = datetime.now(timezone.utc)
                already_on = sw_state.state == "on"

                if not already_on:
                    await self.hass.services.async_call(
                        "switch", "turn_on", {"entity_id": sw_entity}, blocking=True
                    )
                    _LOGGER.info(
                        "Peak Guard EV peak: '%s' terug ingeschakeld (reden: herstel na piekbeperking)",
                        device.name,
                    )

                if cur_entity and snapshot.original_current is not None:
                    max_a = float(device.max_value if device.max_value is not None else DEFAULT_EV_MAX_AMPERE)
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

                guard = self._ev_guard(device.id)
                if already_on:
                    # EV stond al aan (bijv. manueel ingeschakeld vóór Peak Guard).
                    # Meting opruimen: pending → active → direct afgerond (0 duur, geen event).
                    self.peak_tracker.start_measurement_on_turnon(
                        device_id=device.id, device_name=device.name, ts=now_ts
                    )
                    self.peak_tracker.complete_peak_calculation(
                        device_id=device.id, now=now_ts
                    )
                    _LOGGER.debug(
                        "Peak Guard EV peak: '%s' stond al aan bij herstel — "
                        "meting opgeruimd (0 duur, geen event)",
                        device.name,
                    )
                else:
                    # Normaal herstelpad: meting start nu, wordt afgerond bij
                    # power-drop detectie (via get_active_nominal_kw).
                    self.peak_tracker.start_measurement_on_turnon(
                        device_id=device.id, device_name=device.name, ts=now_ts
                    )
                    guard.turned_on_at = now_ts

                guard.state = EVState.CHARGING
                guard.last_switch_state = True
                guard.surplus_history.clear()
                return True

            # ---- INJECTIEPREVENTIE: schakelaar was uit, nu aangezet ---- #
            if snapshot.original_state == "off":
                if sw_state.state != "off":
                    await self._set_ev_soc_override(
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

                    ev_event = self.solar_tracker.complete_solar_calculation(
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

                    # Update guard: we just turned OFF
                    guard = self._ev_guard(device.id)
                    guard.state = EVState.IDLE
                    guard.turned_off_at = datetime.now(timezone.utc)
                    guard.turned_off_by_pg = True
                    guard.surplus_history.clear()
                else:
                    # Schakelaar staat al uit — snapshot opruimen zonder extra actie.
                    # (Kan optreden na HA-herstart of als de lader zelf al gestopt is.)
                    _LOGGER.debug(
                        "Peak Guard EV solar: '%s' schakelaar al uit bij herstel — "
                        "snapshot opgeruimd zonder service-call",
                        device.name,
                    )
                    self.solar_tracker.complete_solar_calculation(
                        device_id=device.id,
                        now=datetime.now(timezone.utc),
                    )
                    guard = self._ev_guard(device.id)
                    guard.state = EVState.IDLE
                    guard.surplus_history.clear()
                return True

        except HomeAssistantError as err:
            _LOGGER.warning(
                "Peak Guard EV: '%s' offline of niet bereikbaar bij herstel — volgende cyclus opnieuw proberen (%s)",
                device.name, err,
            )
            return False
        except (ValueError, TypeError) as err:
            _LOGGER.error("Peak Guard EV: fout bij herstellen '%s': %s", device.name, err)
            return False

        return False

    async def _set_ev_soc_override(
        self,
        device: CascadeDevice,
        override: bool,
        original_soc: Optional[float] = None,
    ) -> None:
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
    #  Cascade uitvoering                                                  #
    # ------------------------------------------------------------------ #

    async def _run_cascade(
        self,
        cascade: List[CascadeDevice],
        excess: float,
        snapshots: Dict[str, DeviceSnapshot],
        cascade_type: str = "peak",
    ):
        """
        cascade_type: "peak" (piekbeperking) of "solar" (injectiepreventie).
        Wordt doorgegeven aan _apply_action voor EV-specifieke logica.
        """
        sorted_devices = sorted(
            [d for d in cascade if d.enabled], key=lambda x: x.priority
        )
        label = "PIEK" if cascade_type == "peak" else "SOLAR"
        _LOGGER.warning(
            "Peak Guard [%s cascade]: start — overschot=%.0f W, %d apparaat/apparaten (prioriteitsvolgorde: %s)",
            label, excess,
            len(sorted_devices),
            ", ".join(f"'{d.name}'[{d.action_type}]" for d in sorted_devices) or "–",
        )
        remaining = excess
        for device in sorted_devices:
            if remaining <= 0:
                _LOGGER.warning(
                    "Peak Guard [%s cascade]: overschot opgelost (0 W resterend) — "
                    "verdere apparaten niet verwerkt",
                    label,
                )
                break
            before = remaining
            self._last_skip_reason = ""
            remaining = await self._apply_action(device, remaining, snapshots, cascade_type)
            handled = before - remaining
            if handled > 0:
                _LOGGER.info(
                    "Peak Guard [%s cascade]:   ✓ '%s' — %.0f W verwerkt, resterend: %.0f W",
                    label, device.name, handled, remaining,
                )
            elif handled < 0:
                _LOGGER.info(
                    "Peak Guard [%s cascade]:   ✓ '%s' — gestart (%.0f W > surplus), resterend: %.0f W",
                    label, device.name, abs(handled), remaining,
                )
            else:
                _LOGGER.warning(
                    "Peak Guard [%s cascade]:   · '%s' — geen actie: %s",
                    label, device.name, self._last_skip_reason or "zie detail-logs",
                )
        if remaining > 0:
            _LOGGER.warning(
                "Peak Guard [%s cascade]: klaar — nog %.0f W overschot onverwerkt "
                "(alle apparaten doorlopen)",
                label, remaining,
            )
        else:
            _LOGGER.warning(
                "Peak Guard [%s cascade]: klaar — overschot volledig verwerkt ✓",
                label,
            )
        await self.async_save()

    async def _apply_action(
        self,
        device: CascadeDevice,
        excess: float,
        snapshots: Dict[str, DeviceSnapshot],
        cascade_type: str = "peak",
    ) -> float:
        state = self.hass.states.get(device.entity_id)
        if state is None:
            _LOGGER.warning(
                "Peak Guard: entity '%s' ('%s') niet gevonden in HA — apparaat overgeslagen",
                device.entity_id, device.name,
            )
            return excess

        # ---- Switch OFF (piekbeperking) -------------------------------- #
        if device.action_type == ACTION_SWITCH_OFF:
            if state.state == "on":
                if device.entity_id not in snapshots:
                    snapshots[device.entity_id] = DeviceSnapshot(
                        entity_id=device.entity_id,
                        original_state=state.state,
                    )
                await self.hass.services.async_call(
                    "switch", "turn_off", {"entity_id": device.entity_id}, blocking=True
                )
                self._track_action(device.entity_id, "switch.turn_off")
                _LOGGER.info(
                    "Peak Guard: → '%s' UITgeschakeld (piekbeperking, -%d W, overschot was %.0f W)",
                    device.name, device.power_watts, excess,
                )
                self.peak_tracker.record_pending_avoid(
                    device_id=device.id,
                    device_name=device.name,
                    nominal_kw=device.power_watts / 1000.0,
                    ts=datetime.now(timezone.utc),
                )
                return excess - device.power_watts
            else:
                _LOGGER.info(
                    "Peak Guard: → '%s' al UIT — overgeslagen (piekbeperking, staat=%s)",
                    device.name, state.state,
                )
                return excess

        # ---- Switch ON (injectiepreventie) ----------------------------- #
        if device.action_type == ACTION_SWITCH_ON:
            if state.state == "off":
                if device.entity_id not in snapshots:
                    snapshots[device.entity_id] = DeviceSnapshot(
                        entity_id=device.entity_id,
                        original_state=state.state,
                    )
                await self.hass.services.async_call(
                    "switch", "turn_on", {"entity_id": device.entity_id}, blocking=True
                )
                self._track_action(device.entity_id, "switch.turn_on")
                _LOGGER.info(
                    "Peak Guard: → '%s' AANgeschakeld (injectiepreventie, +%d W, overschot was %.0f W)",
                    device.name, device.power_watts, excess,
                )
                self.solar_tracker.start_solar_measurement(
                    device_id=device.id,
                    device_name=device.name,
                    nominal_kw=device.power_watts / 1000.0,
                    ts=datetime.now(timezone.utc),
                )
                return excess - device.power_watts
            else:
                _LOGGER.info(
                    "Peak Guard: → '%s' al AAN — overgeslagen (injectiepreventie, staat=%s)",
                    device.name, state.state,
                )
                return excess

        # ---- Throttle (legacy) ----------------------------------------- #
        if device.action_type == ACTION_THROTTLE:
            try:
                current = float(state.state)
                ppu = device.power_per_unit or 690.0
                new_value = max(device.min_value or 0, current - (excess / ppu))
                new_value = round(new_value, 1)
                reduction = (current - new_value) * ppu
                if new_value < current:
                    if device.entity_id not in snapshots:
                        snapshots[device.entity_id] = DeviceSnapshot(
                            entity_id=device.entity_id,
                            original_state=str(current),
                        )
                    await self.hass.services.async_call(
                        "number", "set_value",
                        {"entity_id": device.entity_id, "value": new_value},
                        blocking=True,
                    )
                    self._track_action(device.entity_id, "number.set_value", new_value)
                    _LOGGER.info(
                        "Peak Guard: '%s' teruggeschroefd %.1f → %.1f (-%d W)",
                        device.name, current, new_value, reduction,
                    )
                    return excess - reduction
            except (ValueError, TypeError) as err:
                _LOGGER.error("Peak Guard throttle '%s': %s", device.name, err)

        # ---- EV Charger ------------------------------------------------ #
        if device.action_type == ACTION_EV_CHARGER:
            return await self._apply_ev_action(device, excess, snapshots, cascade_type)

        return excess

    # ──────────────────────────────────────────────────────────────────── #
    #  EV action — the main refactored method                              #
    # ──────────────────────────────────────────────────────────────────── #

    async def _apply_ev_action(
        self,
        device: CascadeDevice,
        excess: float,
        snapshots: Dict[str, DeviceSnapshot],
        cascade_type: str = "peak",
    ) -> float:
        """
        EV Charger cascade-ingreep — met volledige rate-limiting.

        Vermogenformule: P = A × U
          1-fase: U = 230 V  →  W = A × 230
          3-fasen: U = 400 V  →  W = A × 400

        Piekbeperking  (cascade_type == "peak"):
          Laadstroom naar BENEDEN afronden (floor).
          Als new_a < min_a → schakelaar volledig uit.

        Injectiepreventie (cascade_type == "solar"):
          Start-drempel (start_threshold_w, standaard 230 W) is LOSGEKOPPELD van
          het hardware-minimum (hw_min_a / ev_min_current). De lader mag starten
          zodra surplus > start_threshold_w; de werkelijke laadstroom wordt dan
          meteen op max(hw_min_a, ceil(surplus_a)) gezet — nooit lager dan hw-min.
          Stoplogica met hysteresis: stop alleen als surplus − EV-verbruik ≤ 0 W.

        RATE-LIMITING GATES (in volgorde):
          1. Global rate limiter        — max calls per 10-min window
          2. Minimum update interval    — ≥ 90 s between current adjustments
          3. Hysteresis                 — ignore changes < 1 A
          4. Redundancy check           — skip if value already set
          5. Debounce                   — surplus must be stable for 45 s
          6. Min ON/OFF duration        — no rapid switching
          7. State machine              — only act on valid state transitions
        """
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

        snap_key = device.entity_id
        if snap_key not in snapshots:
            snapshots[snap_key] = DeviceSnapshot(
                entity_id=snap_key,
                original_state=sw_state.state,
                original_current=current_a,
                original_soc=current_soc,
            )

        now = datetime.now(timezone.utc)
        guard = self._ev_guard(device.id)

        # ================================================================ #
        #  PIEKBEPERKING — floor, laadstroom verlagen                      #
        #  (peak path: act immediately, but still avoid redundant calls)   #
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
                # ── GATE: redundancy check — don't turn off if already off #
                if guard.last_switch_state is False:
                    _LOGGER.debug(
                        "Peak Guard EV peak: '%s' uitschakelen OVERGESLAGEN — "
                        "schakelaar al uit (redundant call vermeden)",
                        device.name,
                    )
                    return excess - current_w

                # ── GATE: global rate limiter ───────────────────────────── #
                if not self._ev_rate_check(device.name, "turn_off voor piekbeperking"):
                    return excess  # rate-limited; don't reduce excess (conservative)

                await self.hass.services.async_call(
                    "switch", "turn_off", {"entity_id": sw_entity}, blocking=True
                )
                self._ev_record_call()
                self._track_action(sw_entity, "switch.turn_off")
                if cur_entity and current_a is not None:
                    if not self._ev_rate_check(device.name, "set_value min_a na turn_off"):
                        pass  # skip amps reset if rate-limited; not critical
                    else:
                        await self.hass.services.async_call(
                            "number", "set_value",
                            {"entity_id": cur_entity, "value": min_a},
                            blocking=True,
                        )
                        self._ev_record_call()
                        self._track_action(cur_entity, "number.set_value", min_a)

                _LOGGER.info(
                    "Peak Guard EV peak: '%s' uitgeschakeld "
                    "(%.1f A < min %.1f A, verlaging %.0f W, %d fase(n), "
                    "rate-limiter: %d/%d calls in %.0f s)",
                    device.name, target_a_raw, min_a, current_w, phases,
                    self._ev_rate_limiter.calls_in_window,
                    EV_RATE_LIMIT_MAX_CALLS,
                    EV_RATE_LIMIT_WINDOW_S,
                )
                guard.state = EVState.IDLE
                guard.last_switch_state = False
                guard.turned_off_at = now
                guard.turned_off_by_pg = True
                guard.last_sent_amps = min_a
                guard.surplus_history.clear()

                self.peak_tracker.record_pending_avoid(
                    device_id=device.id,
                    device_name=device.name,
                    nominal_kw=current_w / 1000.0,
                    ts=now,
                )
                return excess - current_w

            else:
                actual_reduction_w = (eff_current_a - new_a) * voltage

                # ── GATE: redundancy check ──────────────────────────────── #
                if cur_entity is None:
                    return excess - actual_reduction_w

                if new_a == int(eff_current_a):
                    _LOGGER.debug(
                        "Peak Guard EV peak: '%s' set_value OVERGESLAGEN — "
                        "laadstroom al op %d A (redundant call vermeden)",
                        device.name, new_a,
                    )
                    return excess - actual_reduction_w

                # ── GATE: hysteresis ────────────────────────────────────── #
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

                # ── GATE: minimum update interval ──────────────────────── #
                if guard.last_current_update is not None:
                    elapsed = (now - guard.last_current_update).total_seconds()
                    if elapsed < EV_MIN_UPDATE_INTERVAL_S:
                        _LOGGER.debug(
                            "Peak Guard EV peak: '%s' set_value OVERGESLAGEN wegens update-interval "
                            "(%.0f s geleden, minimum %.0f s)",
                            device.name, elapsed, EV_MIN_UPDATE_INTERVAL_S,
                        )
                        return excess - actual_reduction_w

                # ── GATE: global rate limiter ───────────────────────────── #
                if not self._ev_rate_check(
                    device.name, f"set_value peak {int(eff_current_a)} → {new_a} A"
                ):
                    return excess - actual_reduction_w

                await self.hass.services.async_call(
                    "number", "set_value",
                    {"entity_id": cur_entity, "value": float(new_a)},
                    blocking=True,
                )
                self._ev_record_call()
                self._track_action(cur_entity, "number.set_value", float(new_a))
                _LOGGER.info(
                    "Peak Guard EV peak: '%s' laadstroom %d → %d A "
                    "(floor van %.2f A, verlaging %.0f W, %d fase(n), "
                    "rate-limiter: %d/%d calls in %.0f s)",
                    device.name, int(eff_current_a), new_a,
                    target_a_raw, actual_reduction_w, phases,
                    self._ev_rate_limiter.calls_in_window,
                    EV_RATE_LIMIT_MAX_CALLS,
                    EV_RATE_LIMIT_WINDOW_S,
                )
                guard.last_sent_amps = float(new_a)
                guard.last_current_update = now
                guard.state = EVState.CHARGING
                return excess - actual_reduction_w

        # ================================================================ #
        #  INJECTIEPREVENTIE — laadstroom instellen op beschikbaar surplus  #
        # ================================================================ #
        # cascade_type == "solar"
        #
        # Twee afzonderlijke drempelwaarden voor EV (hardwarematige nuance Tesla):
        #
        #   hw_min_a (ev_min_current)  : hardware-minimum laadstroom — de Tesla
        #                                accepteert NOOIT minder. Dit is de minimale
        #                                stroom die we werkelijk sturen bij opstarten.
        #                                Losgekoppeld van de start-drempel!
        #
        #   start_threshold_w          : minimale injectie (W) om de lader te STARTEN.
        #                                Standaard 230 W ≈ 1 A. Bewust lager dan hw_min_w
        #                                zodat het systeem toestemming krijgt om op te
        #                                starten; de werkelijke stroom wordt dan meteen
        #                                op max(hw_min_a, ceil(surplus_a)) gezet.
        #
        # Stoplogica (hysteresis — voorkomt constant aan/uit):
        #   Stop alleen als surplus − EV-verbruik ≤ DEFAULT_EV_SOLAR_STOP_THRESHOLD_W (0 W).
        #   M.a.w.: stop pas als de EV meer verbruikt dan er injectie is.

        hw_min_a = float(
            device.ev_min_current if device.ev_min_current is not None
            else (device.min_value if device.min_value is not None else DEFAULT_EV_MIN_AMPERE)
        )
        start_threshold_w = float(
            device.start_threshold_w if device.start_threshold_w is not None
            else DEFAULT_EV_SOLAR_START_THRESHOLD_W
        )
        hw_min_w = hw_min_a * voltage

        available_a_raw = excess / voltage
        # Werkelijke laadstroom: altijd ≥ hardware-minimum, ≤ max
        new_a = max(int(hw_min_a), min(int(max_a), math.ceil(available_a_raw)))

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

        # ── GATE: kabeldetectie ─────────────────────────────────────────── #
        # Als de laadkabel niet aangesloten is, kan het laden niet starten.
        # We blokkeren alleen het STARTEN, niet het stoppen van een lopende sessie
        # (de kabel kan niet losgaan terwijl er geladen wordt).
        cable_entity = device.ev_cable_entity or DEFAULT_EV_CABLE_ENTITY
        if not sw_on and not self._ev_cable_connected(device):
            if guard.state != EVState.CABLE_DISCONNECTED:
                guard.state = EVState.CABLE_DISCONNECTED
                self._last_skip_reason = f"laadkabel niet aangesloten ({cable_entity})"
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

        # ── GATE: start-drempel check ───────────────────────────────────── #
        # Twee scenario's:
        #   A) EV staat UIT  → alleen starten als excess ≥ start_threshold_w
        #   B) EV staat AAN  → stoplogica met hysteresis (zie hieronder)
        if excess < start_threshold_w:
            if sw_on:
                # EV draait al — controleer of we moeten stoppen (hysteresis).
                # surplus_without_ev = wat overblijft als we de EV zouden uitzetten.
                # Omdat de EV al meeloopt in 'consumption', is 'excess' het netto
                # injectiesurplus BOVENOP wat de EV al verbruikt.
                # Formule: stop als (excess - ev_verbruik) ≤ stop_drempel
                ev_current_w = (current_a if current_a is not None else hw_min_a) * voltage
                surplus_after_stop = excess - ev_current_w
                if surplus_after_stop > DEFAULT_EV_SOLAR_STOP_THRESHOLD_W:
                    # Er blijft nog voldoende surplus over na uitschakelen → doorladen.
                    # Houd de surplus_history bij zodat de debounce-buffer actueel blijft.
                    self._ev_surplus_is_stable(guard, excess, now)
                    _LOGGER.debug(
                        "Peak Guard [SOLAR]: '%s' draait — surplus %.0f W < start-drempel %.0f W "
                        "maar surplus na stop zou %.0f W zijn (> stop-drempel %.0f W) → doorladen",
                        device.name, excess, start_threshold_w,
                        surplus_after_stop, DEFAULT_EV_SOLAR_STOP_THRESHOLD_W,
                    )
                    # Val door naar stroom-aanpassing hieronder (EV staat al aan)
                else:
                    # Surplus is echt weg → stoppen
                    _LOGGER.info(
                        "Peak Guard [SOLAR]: '%s' GESTOPT — surplus %.0f W < start-drempel %.0f W "
                        "én surplus na stop zou %.0f W zijn (≤ stop-drempel %.0f W)",
                        device.name, excess, start_threshold_w,
                        surplus_after_stop, DEFAULT_EV_SOLAR_STOP_THRESHOLD_W,
                    )
                    # ── GATE: minimum ON duration ──────────────────────────── #
                    if guard.turned_on_at is not None:
                        on_secs = (now - guard.turned_on_at).total_seconds()
                        if on_secs < EV_MIN_ON_DURATION_S:
                            _LOGGER.info(
                                "Peak Guard [SOLAR]: '%s' uitschakelen OVERGESLAGEN — "
                                "te kort geleden ingeschakeld (%.0f s geleden, minimum %.0f s)",
                                device.name, on_secs, EV_MIN_ON_DURATION_S,
                            )
                            return excess
                    if not self._ev_rate_check(device.name, "turn_off wegens geen surplus"):
                        return excess
                    try:
                        await self.hass.services.async_call(
                            "switch", "turn_off", {"entity_id": sw_entity}, blocking=True
                        )
                    except HomeAssistantError as ha_err:
                        _LOGGER.warning(
                            "Peak Guard [SOLAR]: '%s' niet bereikbaar voor turn_off — volgende cyclus opnieuw proberen (%s)",
                            device.name, ha_err,
                        )
                        return excess
                    self._ev_record_call()
                    self._track_action(sw_entity, "switch.turn_off")
                    guard.state = EVState.IDLE
                    guard.last_switch_state = False
                    guard.turned_off_at = now
                    guard.turned_off_by_pg = True
                    guard.surplus_history.clear()
                    event = self.solar_tracker.complete_solar_calculation(
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
                # EV staat uit en surplus < start-drempel → geen actie
                self._last_skip_reason = f"surplus {excess:.0f} W < start-drempel {start_threshold_w:.0f} W"
                _LOGGER.info(
                    "Peak Guard [SOLAR]: '%s' staat uit — surplus %.0f W < "
                    "start-drempel %.0f W → geen actie",
                    device.name, excess, start_threshold_w,
                )
                return excess

        # surplus ≥ start_threshold_w — EV mag (of blijft) laden

        # ── GATE: locatie — EV moet thuis zijn om te laden ──────────────── #
        if device.ev_location_tracker:
            loc_state = self.hass.states.get(device.ev_location_tracker)
            loc_val = loc_state.state.lower().strip() if loc_state else ""
            ev_is_home = loc_val in ("home", "on", "true", "1") or loc_val in ("unavailable", "unknown", "")
            if not ev_is_home:
                if guard.state != EVState.IDLE:
                    _LOGGER.info(
                        "Peak Guard [SOLAR]: '%s' niet thuis (tracker='%s', staat='%s') — "
                        "toestand gereset naar IDLE",
                        device.name, device.ev_location_tracker,
                        loc_state.state if loc_state else "onbekend",
                    )
                    guard.state = EVState.IDLE
                    guard.surplus_history.clear()
                else:
                    _LOGGER.debug(
                        "Peak Guard [SOLAR]: '%s' niet thuis — laden overgeslagen",
                        device.name,
                    )
                return excess

        # ── Detecteer handmatige start ──────────────────────────────────── #
        # Als de schakelaar aan staat maar de guard dat niet verwacht, heeft de
        # gebruiker de EV handmatig gestart.  Neem de toestand over zodat PG
        # de EV niet direct weer uitzet of blokkeert via turned_off_at.
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

        # ── GATE: debounce — surplus must be stable before we act ──────── #
        # Bypass volledig als het overschot zeer groot is (solide zonne-opwekking).
        large_surplus = excess >= EV_DEBOUNCE_BYPASS_SURPLUS_W
        if large_surplus:
            guard.surplus_history.append((now, excess))  # bijhouden voor historiek
            if guard.state == EVState.WAITING_FOR_STABLE:
                _LOGGER.info(
                    "Peak Guard [SOLAR]: '%s' — debounce overgeslagen "
                    "(overschot %.0f W ≥ bypass-drempel %.0f W)",
                    device.name, excess, EV_DEBOUNCE_BYPASS_SURPLUS_W,
                )
            surplus_stable = True
        else:
            surplus_stable = self._ev_surplus_is_stable(guard, excess, now)

        if not surplus_stable:
            history_secs = 0.0
            if guard.surplus_history:
                oldest = guard.surplus_history[0][0]
                history_secs = (now - oldest).total_seconds()
            if guard.state != EVState.WAITING_FOR_STABLE:
                guard.state = EVState.WAITING_FOR_STABLE
                self._last_skip_reason = f"debounce: surplus {excess:.0f} W nog niet {EV_DEBOUNCE_STABLE_S:.0f}s stabiel"
                _LOGGER.warning(
                    "Peak Guard [SOLAR]: '%s' NIET geactiveerd — wacht op stabiel overschot "
                    "(huidig=%.0f W, debounce=%.0f s vereist, tot nu toe=%.0f s, "
                    "tolerantie=±%.0f W)",
                    device.name, excess, EV_DEBOUNCE_STABLE_S, history_secs,
                    EV_DEBOUNCE_TOLERANCE_W,
                )
            else:
                self._last_skip_reason = f"debounce: wachten op stabiliteit ({history_secs:.0f}/{EV_DEBOUNCE_STABLE_S:.0f}s)"
                _LOGGER.info(
                    "Peak Guard [SOLAR]: '%s' nog wachtend op stabiel overschot "
                    "(huidig=%.0f W, %.0f/%.0f s verstreken)",
                    device.name, excess, history_secs, EV_DEBOUNCE_STABLE_S,
                )
            return excess

        # Surplus is stable (of groot genoeg) — state machine bijwerken
        if guard.state == EVState.WAITING_FOR_STABLE:
            _LOGGER.info(
                "Peak Guard [SOLAR]: '%s' overschot stabiel — actie toegestaan "
                "(%.0f W stabiel gedurende ≥ %.0f s)",
                device.name, excess, EV_DEBOUNCE_STABLE_S,
            )

        if not sw_on:
            # ──────────────────────────────────────────────────────────── #
            #  EV staat uit → aanzetten op hardware-minimum laadstroom     #
            # ──────────────────────────────────────────────────────────── #

            # ── GATE: minimum OFF duration ──────────────────────────────── #
            # Alleen van toepassing als PG zelf heeft uitgeschakeld.
            # Bij een handmatige stop (turned_off_by_pg=False) wordt de wachttijd
            # niet opgelegd zodat PG gewoon kan herstarten bij voldoende overschot.
            if guard.turned_off_at is not None and guard.turned_off_by_pg:
                off_secs = (now - guard.turned_off_at).total_seconds()
                if off_secs < EV_MIN_OFF_DURATION_S:
                    self._last_skip_reason = f"min OFF-duur: {off_secs:.0f}s < {EV_MIN_OFF_DURATION_S:.0f}s"
                    _LOGGER.info(
                        "Peak Guard [SOLAR]: '%s' aanzetten OVERGESLAGEN — "
                        "te kort geleden automatisch uitgeschakeld (%.0f s geleden, minimum %.0f s)",
                        device.name, off_secs, EV_MIN_OFF_DURATION_S,
                    )
                    return excess

            # ── GATE: redundancy check ──────────────────────────────────── #
            if guard.last_switch_state is True:
                # We sent turn_on previously but the HA entity still reports off.
                # The Tesla API acknowledged our command but didn't execute it yet
                # (or the command was silently dropped). Reset guard state so the
                # turn_on is retried this cycle instead of being skipped forever.
                _LOGGER.info(
                    "Peak Guard [SOLAR]: '%s' — schakelaar nog steeds UIT na eerder turn_on commando "
                    "(Tesla API bevestigt ontvangst maar niet uitvoering) — opnieuw proberen",
                    device.name,
                )
                guard.last_switch_state = None
                guard.state = EVState.IDLE
            if guard.last_switch_state is not True:
                # ── GATE: wake-up check ───────────────────────────────── #
                # Alleen hier: we hebben besloten te starten (surplus OK,
                # debounce stabiel, alle gates gepasseerd). Nu pas checken
                # of de EV wakker is. Als niet → wekken en wachten.
                if device.ev_wake_button and not self._ev_is_connected(device):
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
                    # Wacht maximaal EV_WAKE_TIMEOUT_S seconden tot auto wakker is
                    wake_ok = False
                    for _ in range(int(EV_WAKE_TIMEOUT_S)):
                        await asyncio.sleep(1.0)
                        if self._ev_is_connected(device):
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
                        return excess  # volgende loop-iteratie opnieuw proberen

                # ── GATE: global rate limiter ─────────────────────────── #
                if not self._ev_rate_check(device.name, "turn_on voor injectiepreventie"):
                    return excess

                try:
                    await self.hass.services.async_call(
                        "switch", "turn_on", {"entity_id": sw_entity}, blocking=True
                    )
                except HomeAssistantError as ha_err:
                    _LOGGER.warning(
                        "Peak Guard [SOLAR]: '%s' niet bereikbaar voor turn_on — volgende cyclus opnieuw proberen (%s)",
                        device.name, ha_err,
                    )
                    return excess
                self._ev_record_call()
                self._track_action(sw_entity, "switch.turn_on")
                guard.state = EVState.CHARGING
                guard.last_switch_state = True
                guard.turned_on_at = now
                guard.surplus_history.clear()  # fresh start after switching

                if cur_entity:
                    if not self._ev_rate_check(
                        device.name, f"set_value {new_a} A bij turn_on"
                    ):
                        pass  # rate-limited; charger will use its own default
                    else:
                        await self.hass.services.async_call(
                            "number", "set_value",
                            {"entity_id": cur_entity, "value": float(new_a)},
                            blocking=True,
                        )
                        self._ev_record_call()
                        self._track_action(cur_entity, "number.set_value", float(new_a))
                        guard.last_sent_amps = float(new_a)
                        guard.last_current_update = now

                await self._set_ev_soc_override(device, override=True)

                actual_consumption_w = new_a * voltage
                _LOGGER.info(
                    "Peak Guard [SOLAR]: → '%s' gestart met %d A (%.0f W) "
                    "omdat injectie %.0f W > start-drempel %.0f W "
                    "(hw-min=%.0f A, %d fase(n), SOC-override: %s%%, "
                    "rate-limiter: %d/%d calls in %.0f s)",
                    device.name, new_a, actual_consumption_w,
                    excess, start_threshold_w,
                    hw_min_a, phases,
                    device.ev_max_soc if device.ev_max_soc is not None else "n.v.t.",
                    self._ev_rate_limiter.calls_in_window,
                    EV_RATE_LIMIT_MAX_CALLS,
                    EV_RATE_LIMIT_WINDOW_S,
                )
                self.solar_tracker.start_solar_measurement(
                    device_id=device.id,
                    device_name=device.name,
                    nominal_kw=actual_consumption_w / 1000.0,
                    ts=now,
                )
                return excess - actual_consumption_w

        # ──────────────────────────────────────────────────────────────── #
        #  EV staat al aan → alleen stroom aanpassen als echt nodig        #
        # ──────────────────────────────────────────────────────────────── #

        # Herbereken new_a rekening houdend met de lopende EV-consumptie.
        # excess = netto export BOVENOP de huidige EV-consumptie (de P1-meter
        # meet al het verbruik inclusief de EV). Totaal beschikbaar voor de EV:
        #   huidige EV-stroom × spanning  +  netto overschot
        # Zonder deze correctie zou een overschot van 777 W bij een EV die al op
        # 10 A laadt (2300 W) leiden tot new_a = ceil(777/230) = 4 A → hw_min 5 A,
        # wat de lader onterecht verlaagt van 10 A naar 5 A.
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

        # ── GATE: redundancy check ──────────────────────────────────────── #
        if new_a == math.ceil(current_a):
            _LOGGER.debug(
                "Peak Guard [SOLAR]: '%s' set_value OVERGESLAGEN — "
                "laadstroom al op %d A (redundant call vermeden)",
                device.name, new_a,
            )
            return excess - actual_consumption_w

        # ── GATE: hysteresis ────────────────────────────────────────────── #
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

        # ── GATE: minimum update interval ──────────────────────────────── #
        if guard.last_current_update is not None:
            elapsed = (now - guard.last_current_update).total_seconds()
            if elapsed < EV_MIN_UPDATE_INTERVAL_S:
                _LOGGER.debug(
                    "Peak Guard [SOLAR]: '%s' set_value OVERGESLAGEN wegens update-interval "
                    "(%.0f s geleden, minimum %.0f s)",
                    device.name, elapsed, EV_MIN_UPDATE_INTERVAL_S,
                )
                return excess - actual_consumption_w

        # ── GATE: global rate limiter ─────────────────────────────────── #
        if not self._ev_rate_check(
            device.name, f"set_value solar {int(current_a)} → {new_a} A"
        ):
            return excess - actual_consumption_w

        await self.hass.services.async_call(
            "number", "set_value",
            {"entity_id": cur_entity, "value": float(new_a)},
            blocking=True,
        )
        self._ev_record_call()
        self._track_action(cur_entity, "number.set_value", float(new_a))
        _LOGGER.info(
            "Peak Guard [SOLAR]: '%s' laadstroom %d → %d A "
            "(ceil van %.2f A, hw-min=%.0f A, verbruik %.0f W, %d fase(n), "
            "rate-limiter: %d/%d calls in %.0f s)",
            device.name, int(current_a), new_a,
            available_a_raw, hw_min_a, actual_consumption_w, phases,
            self._ev_rate_limiter.calls_in_window,
            EV_RATE_LIMIT_MAX_CALLS,
            EV_RATE_LIMIT_WINDOW_S,
        )
        guard.last_sent_amps = float(new_a)
        guard.last_current_update = now
        guard.state = EVState.CHARGING
        return excess - actual_consumption_w

    # ------------------------------------------------------------------ #
    #  Beslissingslog                                                      #
    # ------------------------------------------------------------------ #

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
                    guard = self._ev_guards.get(d.id)
                    if guard:
                        lines.append(
                            f"       EV State: {guard.state.value}"
                            + (f" | Laadstroom: {guard.last_sent_amps:.1f} A"
                               if guard.last_sent_amps is not None else "")
                        )
                        lines.append(
                            f"       Rate limiter: "
                            f"{self._ev_rate_limiter.calls_in_window}"
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
                    guard = self._ev_guards.get(d.id)
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
                            f"{self._ev_rate_limiter.calls_in_window}"
                            f"/{EV_RATE_LIMIT_MAX_CALLS} calls"
                            f" in {EV_RATE_LIMIT_WINDOW_S:.0f}s"
                            f" | Resterende ruimte: {self._ev_rate_limiter.remaining}"
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
                    g = self._ev_guards.get(d.id)
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
