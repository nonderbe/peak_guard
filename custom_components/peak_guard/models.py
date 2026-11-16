"""
Peak Guard — models.py

Alle dataclasses, enums en waarde-objecten die door meerdere modules
worden gebruikt.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections import deque
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any, Callable, ClassVar, Deque, Optional

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from . import const as _const

if TYPE_CHECKING:
    from .avoided_peak_tracker import PeakAvoidTracker, SolarShiftTracker
    from .deciders.ev_guard import EVGuard

_LOGGER = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────── #
#  EV spanning-constanten                                                       #
# ──────────────────────────────────────────────────────────────────────────── #

EV_VOLTS_1PHASE: float = 230.0
EV_VOLTS_3PHASE: float = 400.0


# ──────────────────────────────────────────────────────────────────────────── #
#  EV Rate-limiting & hysteresis constanten                                     #
# ──────────────────────────────────────────────────────────────────────────── #

EV_HYSTERESIS_AMPS: float = 1.0
EV_MIN_UPDATE_INTERVAL_S: float = 20.0
EV_DEBOUNCE_STABLE_S: float = 20.0
EV_FLOOR_PERCENTILE: int = 10
EV_MIN_OFF_DURATION_S: float = 300.0
EV_WAKE_TIMEOUT_S: float = 15.0
EV_CMD_MAX_RETRIES: int = 2
EV_CMD_RETRY_DELAY_S: float = 3.0
EV_RATE_LIMIT_MAX_CALLS: int = 12
EV_RATE_LIMIT_WINDOW_S: float = 600.0
# Zoveel plaatsen van het korte venster blijven voor commando's die het
# verbruik verlagen (piekbeperking, stoppen), zodat verhogen ze niet verdringt.
EV_RATE_LIMIT_RESERVE: int = 2
EV_SENSOR_STALE_S: float = 180.0
EV_WAKE_COOLDOWN_S: float = 900.0


# ──────────────────────────────────────────────────────────────────────────── #
#  EV State machine                                                             #
# ──────────────────────────────────────────────────────────────────────────── #

class EVState(Enum):
    IDLE                = "idle"
    CHARGING            = "charging"
    WAITING_FOR_STABLE  = "waiting_for_stable_surplus"
    CABLE_DISCONNECTED  = "cable_disconnected"
    SLEEPING            = "sleeping"


# ──────────────────────────────────────────────────────────────────────────── #
#  Per-apparaat EV rate-limit / debounce toestand                               #
# ──────────────────────────────────────────────────────────────────────────── #

@dataclass
class EVDeviceGuard:
    state: EVState = EVState.IDLE
    last_sent_amps:    Optional[float] = None
    last_switch_state: Optional[bool]  = None
    last_current_update: Optional[datetime] = None
    turned_on_at:        Optional[datetime] = None
    turned_off_at:       Optional[datetime] = None
    wake_requested_at:   Optional[datetime] = None
    surplus_history: Deque = field(default_factory=lambda: deque(maxlen=60))
    debounce_start_at:    Optional[datetime] = None
    debounce_remaining_s: float             = 0.0
    debounce_floor_w:     float             = 0.0
    pending_amps: Optional[int] = None
    turned_off_by_pg: bool = False
    skip_reason: str = ""
    soc_override_active: bool = False
    wake_cooldown_until: Optional[datetime] = None
    last_known_home: Optional[bool] = None  # True/False zodra locatie ooit gekend was; None = nooit gezien
    scheduled: bool = False  # True zolang het laadschema de lader aanstuurt


# ──────────────────────────────────────────────────────────────────────────── #
#  Globale EV rate-limiter                                                      #
# ──────────────────────────────────────────────────────────────────────────── #

class EVRateLimiter:
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
#  DeviceSnapshot                                                               #
# ──────────────────────────────────────────────────────────────────────────── #

@dataclass
class DeviceSnapshot:
    entity_id:        str
    original_state:   str
    original_current: Optional[float] = None
    original_soc:     Optional[float] = None
    # Thermostaat: doeltemperatuur vóór de ingreep (original_state = hvac-modus).
    original_temp:    Optional[float] = None
    # Bevestiging (schakelaars en thermostaten, niet EV):
    #   None      — geen open actie (ingreep bevestigd)
    #   "apply"   — ingreep verstuurd, wacht op de nieuwe toestand
    #   "noack"   — ingreep na ACK_TIMEOUT_S nog niet bevestigd
    #   "restore" — herstel verstuurd, wacht op de oorspronkelijke toestand
    pending:          Optional[str] = None
    # Tijdstippen als ISO-tekst (de snapshot wordt als JSON opgeslagen).
    cmd_at:           Optional[str] = None   # laatst verstuurde commando
    pending_since:    Optional[str] = None   # eerste commando van de open actie
    off_since:        Optional[str] = None   # afschakeling bevestigd (piek)
    # Vermogen (W) op het moment van de ingreep: wat bij herstel terugkomt.
    shed_w:           Optional[float] = None
    # EV (piek-snapshot): de solar-cascade had hem gestart (laadde niet vóór
    # het overschot). Het piekherstel herstart hem dan niet; de solar-cascade
    # beslist (v1.9.8).
    solar_started:    bool = False
    # Thermostaat met peak_setback: de doeltemperatuur die Peak Guard zette
    # (verlaagd bij verwarmen, verhoogd bij koelen) in plaats van 'off'.
    set_temp:         Optional[float] = None


def ts_to_str(ts: datetime) -> str:
    return ts.isoformat()


def str_to_ts(value: Optional[str]) -> Optional[datetime]:
    """ISO-tekst → datetime (UTC als er geen tijdzone bij staat); None bij onzin."""
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)


def age_s(value: Optional[str], now: datetime) -> float:
    """Seconden sinds een ISO-tijdstip; oneindig als het ontbreekt."""
    ts = str_to_ts(value)
    return math.inf if ts is None else (now - ts).total_seconds()


def read_power_state_w(hass, entity_id: Optional[str]) -> Optional[float]:
    """Vermogenssensor in W (kW omgerekend); None als onbruikbaar."""
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None or state.state in ("unknown", "unavailable", ""):
        return None
    try:
        value = float(state.state)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    unit = str((getattr(state, "attributes", None) or {}).get("unit_of_measurement") or "")
    return value * 1000.0 if unit.strip().lower() == "kw" else value


async def wait_for_state(hass, entity_id: str, predicate: Callable[[Any], bool]) -> bool:
    """
    Wacht tot de entiteit predicate vervult, hoogstens ACK_WAIT_S. Lokale
    schakelaars melden de nieuwe toestand meestal al als de blocking call
    terugkeert; cloud-apparaten (SmartThings) later — dan volgt de
    bevestiging in een volgende cyclus.
    """
    waited = 0.0
    while True:
        state = hass.states.get(entity_id)
        if state is not None and predicate(state):
            return True
        if waited >= _const.ACK_WAIT_S:
            return False
        await asyncio.sleep(_const.ACK_POLL_S)
        waited += _const.ACK_POLL_S


# ──────────────────────────────────────────────────────────────────────────── #
#  Cascade context (dependency bundle for apply/restore methods)                #
# ──────────────────────────────────────────────────────────────────────────── #

@dataclass
class CascadeContext:
    hass: HomeAssistant
    cascade_type: str
    peak_tracker: "PeakAvoidTracker"
    solar_tracker: "SolarShiftTracker"
    ev_guard: "EVGuard"
    track_action: Callable
    warn: Callable
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_skip_reason: str = ""
    # Piek-cascade: de inject-snapshots (zelfde apparaat in beide cascades),
    # zodat een thermostaat-bijstelling vertrekt van de instelling van vóór
    # het overschot.
    inject_snapshots: Optional[dict] = None


# ──────────────────────────────────────────────────────────────────────────── #
#  Cascade device hierarchy                                                     #
# ──────────────────────────────────────────────────────────────────────────── #

@dataclass
class BaseCascadeDevice:
    """Base class for all cascade device entries."""
    _action_type: ClassVar[str] = ""   # overridden by each concrete subclass

    id:              str
    name:            str
    entity_id:       str
    priority:        int
    action_type:     str
    power_watts:     int  = 0
    enabled:         bool = True
    manual_override: bool = False
    # Optioneel (niet voor EV): sensor met het actuele verbruik (W of kW). Bij
    # het afschakelen geldt die waarde i.p.v. power_watts; onder
    # POWER_SENSOR_MIN_W verbruikt het apparaat niets en blijft het aan.
    power_sensor:    str  = ""
    # Optioneel (piek-cascade, niet voor EV): hoogstens zo lang afgeschakeld
    # (daarna hersteld, ook zonder piekruimte), en na een herstel minstens zo
    # lang niet opnieuw afgeschakeld. None = geen grens.
    max_off_s:       Optional[int] = None
    min_on_s:        Optional[int] = None

    # Ondersteunt dit type bevestiging via de entiteitstoestand?
    supports_ack: ClassVar[bool] = False

    # ---- factory ----------------------------------------------------- #

    @classmethod
    def from_dict(cls, d: dict) -> "BaseCascadeDevice":
        return from_dict(d)

    @classmethod
    def _from_dict(cls, base: dict, d: dict) -> "BaseCascadeDevice":
        """Construct this subclass from pre-extracted base fields + raw dict."""
        return cls(**base)

    # ---- polymorphic apply / restore --------------------------------- #

    async def apply(
        self,
        excess: float,
        snapshots: dict,
        ctx: CascadeContext,
    ) -> float:
        raise NotImplementedError(f"{type(self).__name__}.apply not implemented")

    async def restore(
        self,
        snapshot: DeviceSnapshot,
        ctx: CascadeContext,
    ) -> bool:
        raise NotImplementedError(f"{type(self).__name__}.restore not implemented")

    # ---- vermogen ------------------------------------------------------ #

    def live_power_w(self, hass) -> Optional[float]:
        """Actueel verbruik uit power_sensor, of None."""
        return read_power_state_w(hass, self.power_sensor) if self.power_sensor else None

    def shed_power_w(self, hass) -> float:
        """Wat afschakelen nu oplevert: de vermogenssensor, anders power_watts."""
        live = self.live_power_w(hass)
        return float(live) if live is not None else float(self.power_watts or 0)

    def restore_need_w(self, snapshot: DeviceSnapshot) -> float:
        """Wat het herstel terugbrengt: het vermogen bij het afschakelen."""
        if snapshot.shed_w is not None:
            return float(snapshot.shed_w)
        return float(self.power_watts or 0)

    def draws_nothing(self, hass) -> bool:
        """Met een vermogenssensor: verbruikt het apparaat nu (bijna) niets?"""
        live = self.live_power_w(hass)
        return live is not None and live < _const.POWER_SENSOR_MIN_W

    # ---- bevestiging (ack) ------------------------------------------- #

    def applied(self, state, snapshot: DeviceSnapshot, cascade_type: str) -> bool:
        """Meldt de entiteit de toestand die de ingreep beoogde?"""
        return False

    def restored(self, state, snapshot: DeviceSnapshot, cascade_type: str) -> bool:
        """Meldt de entiteit (weer) de toestand van vóór de ingreep?"""
        return False

    def _counts_avoid(self, snapshot: DeviceSnapshot) -> bool:
        """Vermeed de afschakeling verbruik (stond het apparaat vóór de piek aan)?"""
        return snapshot.original_state == "on"

    def on_apply_ack(
        self, snapshot: DeviceSnapshot, ctx: "CascadeContext", ts: datetime, book: bool = True
    ) -> None:
        """
        Ingreep bevestigd: nu pas boeken. Piek: de vermeden belasting vanaf het
        commando (het verbruik stopte tussen commando en bevestiging), en het
        afschakelmoment voor min-OFF / max_off_s. Solar: start de meting.
        """
        snapshot.pending = None
        snapshot.pending_since = None
        power_w = snapshot.shed_w if snapshot.shed_w is not None else float(self.power_watts or 0)
        if not book:
            # Bevestiging van een te oud commando (na een herstart): de toestand
            # telt, maar wanneer het verbruik stopte is onbekend — niets boeken.
            if ctx.cascade_type == "peak":
                snapshot.off_since = ts_to_str(ts)
            return
        if ctx.cascade_type == "peak":
            snapshot.off_since = ts_to_str(ts)
            if self._counts_avoid(snapshot):
                ctx.peak_tracker.record_pending_avoid(
                    device_id=self.id, device_name=self.name,
                    nominal_kw=power_w / 1000.0,
                    ts=str_to_ts(snapshot.cmd_at) or ts, fresh=True,
                )
        else:
            ctx.solar_tracker.start_solar_measurement(
                device_id=self.id, device_name=self.name,
                nominal_kw=power_w / 1000.0, ts=ts,
            )

    def on_restore_ack(self, snapshot: DeviceSnapshot, ctx: "CascadeContext", ts: datetime) -> None:
        """Herstel bevestigd: piek → meting van het inhaalverbruik; solar → meting afsluiten."""
        # "done": het herstel is echt uitgevoerd (niet al-aan of niets te doen).
        # Het snapshot wordt meteen daarna verwijderd; de decider leest dit om
        # min_on_s te starten.
        snapshot.pending = "done"
        snapshot.pending_since = None
        if ctx.cascade_type == "peak":
            if self._counts_avoid(snapshot):
                ctx.peak_tracker.start_measurement_on_turnon(
                    device_id=self.id, device_name=self.name, ts=ts)
            return
        event = ctx.solar_tracker.complete_solar_calculation(device_id=self.id, now=ts)
        if event:
            _LOGGER.info(
                "Peak Guard: solar-event afgerond voor '%s' — "
                "duur=%.1f min, verschoven=%.4f kWh, besparing=€%.4f",
                self.name, event.measured_duration_min,
                event.shifted_kwh, event.savings_euro,
            )

    def _open_action_gate(
        self, snapshots: dict, ctx: "CascadeContext", power_w: float
    ) -> Optional[float]:
        """
        Een eerdere ingreep wacht nog op bevestiging. Geeft het vermogen dat
        de cascade als verwerkt telt (de reservering; 0 = overslaan), of None
        als er niets openstaat of het commando opnieuw verstuurd mag worden.

        Binnen ACK_TIMEOUT_S blijft het vermogen gereserveerd: anders schakelt
        de cascade bij een trage bevestiging ook alles erachter af. Daarna
        telt het niet meer (volgende apparaten nemen het over) en volgt na
        ACK_RESEND_S een nieuw commando.
        """
        snap = snapshots.get(self.entity_id)
        if snap is None or snap.pending not in ("apply", "noack"):
            return None
        since = age_s(snap.cmd_at, ctx.now)
        if snap.pending == "apply" and since < _const.ACK_TIMEOUT_S:
            reserve = snap.shed_w if snap.shed_w is not None else power_w
            ctx.last_skip_reason = f"wacht op bevestiging van het apparaat ({since:.0f} s)"
            ctx.ev_guard.set_hold(
                self.id, "awaiting_ack",
                f"Commando verstuurd {since:.0f} s geleden; wacht tot het apparaat de "
                f"nieuwe toestand meldt. Tot dan telt het als {reserve:.0f} W, zodat de "
                f"apparaten erachter niet onnodig mee afgeschakeld worden.",
            )
            return reserve
        if since < _const.ACK_RESEND_S:
            wait_s = _const.ACK_RESEND_S - since
            ctx.last_skip_reason = "niet bevestigd door het apparaat"
            ctx.ev_guard.set_hold(
                self.id, "no_ack",
                f"Het apparaat bevestigde het commando niet (geen nieuwe toestand na "
                f"{_const.ACK_TIMEOUT_S:.0f} s); het telt niet mee en de volgende apparaten "
                f"worden aangesproken. Nieuwe poging over {wait_s:.0f} s.",
                until=ctx.now + timedelta(seconds=wait_s),
            )
            return 0.0
        return None

    async def _confirm_apply(
        self, snapshot: DeviceSnapshot, ctx: "CascadeContext", power_w: float
    ) -> None:
        """Na het commando: bevestigd → boeken; anders open laten ('apply')."""
        now_s = ts_to_str(ctx.now)
        if snapshot.pending != "apply":
            # Nieuw commando, of opnieuw na 'noack': de bevestigingstermijn
            # begint opnieuw (anders vervalt een herhaling vlak voor de
            # 30 min meteen en blijft een laat uitgevoerde pauze zonder eigenaar).
            snapshot.pending_since = now_s
        snapshot.cmd_at = now_s
        snapshot.shed_w = power_w
        snapshot.pending = "apply"
        if await wait_for_state(
            ctx.hass, self.entity_id,
            lambda st: self.applied(st, snapshot, ctx.cascade_type),
        ):
            self.on_apply_ack(snapshot, ctx, ctx.now)
        else:
            _LOGGER.info(
                "Peak Guard: '%s' — commando verstuurd, wacht op bevestiging van het apparaat",
                self.name,
            )

    async def _confirm_restore(self, snapshot: DeviceSnapshot, ctx: "CascadeContext") -> bool:
        """Na het herstelcommando: True als bevestigd, anders open ('restore')."""
        now_s = ts_to_str(ctx.now)
        if snapshot.pending != "restore":
            snapshot.pending_since = now_s
        snapshot.cmd_at = now_s
        snapshot.pending = "restore"
        if await wait_for_state(
            ctx.hass, self.entity_id,
            lambda st: self.restored(st, snapshot, ctx.cascade_type),
        ):
            self.on_restore_ack(snapshot, ctx, ctx.now)
            return True
        _LOGGER.info(
            "Peak Guard: '%s' — herstel verstuurd, wacht op bevestiging van het apparaat",
            self.name,
        )
        return False


@dataclass
class SwitchOffDevice(BaseCascadeDevice):
    """Peak-limiting switch: turns a device OFF to reduce peak demand."""
    _action_type: ClassVar[str] = "switch_off"
    supports_ack: ClassVar[bool] = True

    def applied(self, state, snapshot: DeviceSnapshot, cascade_type: str) -> bool:
        return state.state == "off"

    def restored(self, state, snapshot: DeviceSnapshot, cascade_type: str) -> bool:
        return state.state == "on"

    async def apply(self, excess: float, snapshots: dict, ctx: CascadeContext) -> float:
        state = ctx.hass.states.get(self.entity_id)
        if state is None:
            ctx.warn(
                "Peak Guard: entity '%s' ('%s') niet gevonden in HA — apparaat overgeslagen",
                self.entity_id, self.name,
            )
            return excess
        gate = self._open_action_gate(snapshots, ctx, self.shed_power_w(ctx.hass))
        if gate is not None:
            return excess - gate
        if state.state != "on":
            _LOGGER.info(
                "Peak Guard: → '%s' al UIT — overgeslagen (piekbeperking, staat=%s)",
                self.name, state.state,
            )
            ctx.last_skip_reason = f"staat al uit ({state.state})"
            return excess
        if self.draws_nothing(ctx.hass):
            ctx.last_skip_reason = (
                f"verbruikt nu niets ({self.live_power_w(ctx.hass):.0f} W volgens "
                f"{self.power_sensor}) — afschakelen levert niets op"
            )
            return excess
        power_w = self.shed_power_w(ctx.hass)
        created = self.entity_id not in snapshots
        if created:
            snapshots[self.entity_id] = DeviceSnapshot(
                entity_id=self.entity_id, original_state=state.state)
        snapshot = snapshots[self.entity_id]
        try:
            await ctx.hass.services.async_call(
                "switch", "turn_off", {"entity_id": self.entity_id}, blocking=True)
        except HomeAssistantError as err:
            ctx.warn(
                "Peak Guard: '%s' niet bereikbaar voor turn_off — "
                "volgende cyclus opnieuw (%s)", self.name, err,
            )
            if created:
                snapshots.pop(self.entity_id, None)
            return excess
        ctx.track_action(self.entity_id, "switch.turn_off")
        _LOGGER.info(
            "Peak Guard: → '%s' UITgeschakeld (piekbeperking, -%.0f W, overschot was %.0f W)",
            self.name, power_w, excess,
        )
        await self._confirm_apply(snapshot, ctx, power_w)
        return excess - power_w

    async def restore(self, snapshot: DeviceSnapshot, ctx: CascadeContext) -> bool:
        state = ctx.hass.states.get(self.entity_id)
        if state is None:
            ctx.warn("Peak Guard: kan '%s' niet herstellen — entity niet gevonden", self.name)
            return False
        if snapshot.original_state != "on":
            return True
        if state.state == "on":
            if snapshot.pending == "restore":
                # Het eerdere herstelcommando is intussen uitgevoerd.
                self.on_restore_ack(snapshot, ctx, ctx.now)
                return True
            # Al aan zonder herstel door Peak Guard (manueel, of de afschakeling
            # werd nooit uitgevoerd): het inhaalmoment is onbekend, dus geen
            # vermijding boeken — ook geen oude die later in een verkeerd
            # kwartier terechtkomt.
            ctx.peak_tracker.complete_peak_calculation(device_id=self.id, now=ctx.now)
            ctx.peak_tracker.discard_pending(self.id)
            return True
        try:
            await ctx.hass.services.async_call(
                "switch", "turn_on", {"entity_id": self.entity_id}, blocking=True)
        except HomeAssistantError as err:
            ctx.warn(
                "Peak Guard: '%s' niet bereikbaar bij herstel — "
                "volgende cyclus opnieuw proberen (%s)", self.name, err)
            return False
        ctx.track_action(self.entity_id, "switch.turn_on")
        _LOGGER.info(
            "Peak Guard: '%s' terug ingeschakeld na piekbeperking (originele staat: %s)",
            self.name, snapshot.original_state,
        )
        return await self._confirm_restore(snapshot, ctx)


@dataclass
class SwitchOnDevice(BaseCascadeDevice):
    """Injection-prevention switch: turns a device ON to consume solar surplus."""
    _action_type: ClassVar[str] = "switch_on"
    supports_ack: ClassVar[bool] = True

    def applied(self, state, snapshot: DeviceSnapshot, cascade_type: str) -> bool:
        return state.state == "on"

    def restored(self, state, snapshot: DeviceSnapshot, cascade_type: str) -> bool:
        return state.state == "off"

    async def apply(self, excess: float, snapshots: dict, ctx: CascadeContext) -> float:
        state = ctx.hass.states.get(self.entity_id)
        if state is None:
            ctx.warn(
                "Peak Guard: entity '%s' ('%s') niet gevonden in HA — apparaat overgeslagen",
                self.entity_id, self.name,
            )
            return excess
        gate = self._open_action_gate(snapshots, ctx, float(self.power_watts or 0))
        if gate is not None:
            return excess - gate
        if state.state == "off":
            # Pas inschakelen als het overschot het apparaat volledig dekt.
            # Anders trekt het meteen stroom van het net, schakelt het herstel
            # het de volgende cyclus weer uit (verbruik ≥ 0) en klappert het
            # elke cyclus aan/uit.
            if self.power_watts and excess < self.power_watts:
                ctx.last_skip_reason = (
                    f"overschot {excess:.0f} W < vermogen {self.power_watts:.0f} W"
                )
                ctx.ev_guard.set_hold(
                    self.id, "surplus_too_small",
                    f"Blijft uit: het zonne-overschot ({excess:.0f} W) dekt het vermogen van "
                    f"dit apparaat ({self.power_watts:.0f} W) nog niet. Inschakelen zou "
                    f"stroom van het net halen en het apparaat na één cyclus weer uitschakelen.",
                )
                return excess
            created = self.entity_id not in snapshots
            if created:
                snapshots[self.entity_id] = DeviceSnapshot(
                    entity_id=self.entity_id, original_state=state.state)
            snapshot = snapshots[self.entity_id]
            try:
                await ctx.hass.services.async_call(
                    "switch", "turn_on", {"entity_id": self.entity_id}, blocking=True)
            except HomeAssistantError as err:
                ctx.warn(
                    "Peak Guard: '%s' niet bereikbaar voor turn_on — "
                    "volgende cyclus opnieuw (%s)", self.name, err,
                )
                if created:
                    snapshots.pop(self.entity_id, None)
                return excess
            ctx.track_action(self.entity_id, "switch.turn_on")
            _LOGGER.info(
                "Peak Guard: → '%s' AANgeschakeld (injectiepreventie, +%d W, overschot was %.0f W)",
                self.name, self.power_watts, excess,
            )
            await self._confirm_apply(snapshot, ctx, float(self.power_watts or 0))
            return excess - self.power_watts
        _LOGGER.info(
            "Peak Guard: → '%s' al AAN — overgeslagen (injectiepreventie, staat=%s)",
            self.name, state.state,
        )
        return excess

    async def restore(self, snapshot: DeviceSnapshot, ctx: CascadeContext) -> bool:
        state = ctx.hass.states.get(self.entity_id)
        if state is None:
            ctx.warn("Peak Guard: kan '%s' niet herstellen — entity niet gevonden", self.name)
            return False
        if snapshot.original_state != "off":
            return True
        if state.state == "off":
            # Uitgevoerd (bevestiging van een eerder commando), of manueel
            # uitgezet: de meting stopt nu.
            self.on_restore_ack(snapshot, ctx, ctx.now)
            return True
        try:
            await ctx.hass.services.async_call(
                "switch", "turn_off", {"entity_id": self.entity_id}, blocking=True)
        except HomeAssistantError as err:
            ctx.warn(
                "Peak Guard: '%s' niet bereikbaar bij herstel — "
                "volgende cyclus opnieuw proberen (%s)", self.name, err)
            return False
        ctx.track_action(self.entity_id, "switch.turn_off")
        _LOGGER.info("Peak Guard: '%s' terug uitgeschakeld na injectiepreventie", self.name)
        return await self._confirm_restore(snapshot, ctx)


@dataclass
class ThrottleDevice(BaseCascadeDevice):
    """Legacy throttle: reduces a number entity to shed power."""
    _action_type: ClassVar[str] = "throttle"

    min_value:      Optional[float] = None
    max_value:      Optional[float] = None
    power_per_unit: Optional[float] = None

    @classmethod
    def _from_dict(cls, base: dict, d: dict) -> "ThrottleDevice":
        return cls(
            **base,
            min_value=d.get("min_value"),
            max_value=d.get("max_value"),
            power_per_unit=d.get("power_per_unit"),
        )

    async def apply(self, excess: float, snapshots: dict, ctx: CascadeContext) -> float:
        state = ctx.hass.states.get(self.entity_id)
        if state is None:
            ctx.warn(
                "Peak Guard: entity '%s' ('%s') niet gevonden in HA — apparaat overgeslagen",
                self.entity_id, self.name,
            )
            return excess
        try:
            current = float(state.state)
            ppu = self.power_per_unit or 690.0
            new_value = max(self.min_value or 0, current - (excess / ppu))
            new_value = round(new_value, 1)
            reduction = (current - new_value) * ppu
            if new_value < current:
                if self.entity_id not in snapshots:
                    snapshots[self.entity_id] = DeviceSnapshot(
                        entity_id=self.entity_id, original_state=str(current))
                await ctx.hass.services.async_call(
                    "number", "set_value",
                    {"entity_id": self.entity_id, "value": new_value},
                    blocking=True,
                )
                ctx.track_action(self.entity_id, "number.set_value", new_value)
                _LOGGER.info(
                    "Peak Guard: '%s' teruggeschroefd %.1f → %.1f (-%d W)",
                    self.name, current, new_value, reduction,
                )
                return excess - reduction
        except HomeAssistantError as err:
            ctx.warn("Peak Guard: '%s' niet bereikbaar voor verlagen (%s)", self.name, err)
        except (ValueError, TypeError) as err:
            _LOGGER.error("Peak Guard throttle '%s': %s", self.name, err)
        return excess

    async def restore(self, snapshot: DeviceSnapshot, ctx: CascadeContext) -> bool:
        state = ctx.hass.states.get(self.entity_id)
        if state is None:
            ctx.warn("Peak Guard: kan '%s' niet herstellen — entity niet gevonden", self.name)
            return False
        try:
            original = float(snapshot.original_state)
            current  = float(state.state)
            new_value = round(original, 1)
            if new_value != round(current, 1):
                await ctx.hass.services.async_call(
                    "number", "set_value",
                    {"entity_id": self.entity_id, "value": new_value},
                    blocking=True,
                )
                _LOGGER.info(
                    "Peak Guard: '%s' hersteld %s → %s", self.name, current, new_value)
            return True
        except HomeAssistantError as err:
            ctx.warn(
                "Peak Guard: '%s' niet bereikbaar bij herstel — "
                "volgende cyclus opnieuw proberen (%s)", self.name, err)
        except (ValueError, TypeError) as err:
            _LOGGER.error("Peak Guard: fout bij herstellen '%s': %s", self.name, err)
        return False


@dataclass
class EVChargerDevice(BaseCascadeDevice):
    """EV charger: variable-current injection-prevention and peak-limiting device."""
    _action_type: ClassVar[str] = "ev_charger"

    min_value:       Optional[float] = None
    max_value:       Optional[float] = None
    # EV-specific fields (absorbed from the former EVChargerConfig)
    switch_entity:     Optional[str]   = None
    current_entity:    Optional[str]   = None
    soc_entity:        Optional[str]   = None
    battery_entity:    Optional[str]   = None
    max_soc:           Optional[int]   = None
    phases:            int             = 1
    min_current:       Optional[float] = None
    start_threshold_w: Optional[float] = None
    cable_entity:      Optional[str]   = None
    wake_button:       Optional[str]   = None
    status_sensor:     Optional[str]   = None
    location_tracker:  Optional[str]   = None
    # Laadstatus (bv. Tesla sensor.*_opladen: starting/charging/stopped/complete/…).
    # Gebruikt door het laadschema; status_sensor is vaak enkel online/slaap.
    charge_state_sensor: Optional[str] = None

    @classmethod
    def _from_dict(cls, base: dict, d: dict) -> "EVChargerDevice":
        return cls(
            **base,
            min_value=d.get("min_value"),
            max_value=d.get("max_value"),
            switch_entity=d.get("switch_entity"),
            current_entity=d.get("current_entity"),
            soc_entity=d.get("soc_entity"),
            battery_entity=d.get("battery_entity"),
            max_soc=d.get("max_soc"),
            phases=d.get("phases", 1),
            min_current=d.get("min_current"),
            start_threshold_w=d.get("start_threshold_w"),
            cable_entity=d.get("cable_entity"),
            wake_button=d.get("wake_button"),
            status_sensor=d.get("status_sensor"),
            location_tracker=d.get("location_tracker"),
            charge_state_sensor=d.get("charge_state_sensor"),
        )

    async def apply(self, excess: float, snapshots: dict, ctx: CascadeContext) -> float:
        result = await ctx.ev_guard.apply_action(
            device=self,
            excess=excess,
            snapshots=snapshots,
            cascade_type=ctx.cascade_type,
            peak_tracker=ctx.peak_tracker,
            solar_tracker=ctx.solar_tracker,
            now=ctx.now,
        )
        ctx.last_skip_reason = ctx.ev_guard.last_skip_reason
        return result

    async def restore(self, snapshot: DeviceSnapshot, ctx: CascadeContext) -> bool:
        return await ctx.ev_guard.restore(
            device=self,
            snapshot=snapshot,
            peak_tracker=ctx.peak_tracker,
            solar_tracker=ctx.solar_tracker,
            cascade_type=ctx.cascade_type,
            now=ctx.now,
        )


# ──────────────────────────────────────────────────────────────────────────── #
#  Laadschema (Planning-tab)                                                    #
# ──────────────────────────────────────────────────────────────────────────── #

SCHEDULE_KIND_TIME   = "time"
SCHEDULE_KIND_SENSOR = "sensor"


@dataclass
class ScheduleWindow:
    """Eén tijdsblok van een laadschema.

    kind="time":   actief op de startdagen `days` (0=ma … 6=zo) van `start` tot
                   `end` (lokale tijd). end <= start loopt door tot de volgende
                   dag; start == end is een volledige dag van 24 u.
    kind="sensor": actief zolang `entity_id` de staat `active_state` heeft
                   (bv. sensor.p1_meter_tarief = 2 → daltarief).
    """
    kind:         str = SCHEDULE_KIND_TIME
    days:         list = field(default_factory=list)
    start:        str = "00:00"
    end:          str = "00:00"
    entity_id:    Optional[str] = None
    active_state: Optional[str] = None
    target_soc:   Optional[int] = None   # enkel EV: laden tot dit batterijpercentage
    enabled:      bool = True


@dataclass
class ScheduleEntry:
    """Een apparaat met zijn laadschema.

    `device` is een kopie van een cascade-apparaat (zelfde id), zodat de
    EV-guard gedeeld blijft met de piek- en inject-cascade.
    """
    id:              str
    device:          BaseCascadeDevice
    enabled:         bool = True
    windows:         list = field(default_factory=list)
    max_current:     Optional[float] = None   # EV: max laadstroom tijdens het venster
    rest_soc:        Optional[int] = None     # EV: laadlimiet buiten de vensters
    block_unplanned: bool = True              # EV: ongeplande lading buiten vensters stoppen

    @property
    def entity_id(self) -> str:
        return self.device.entity_id

    @property
    def is_ev(self) -> bool:
        return isinstance(self.device, EVChargerDevice)


# ──────────────────────────────────────────────────────────────────────────── #
#  Factory + migration                                                          #
# ──────────────────────────────────────────────────────────────────────────── #

def _migrate_flat_format(d: dict) -> dict:
    """Convert old flat EV format (ev_switch_entity, ev_phases, …) to new format.

    Also handles intermediate nested format {"ev": {...}} from Step 1.
    Returns a new dict; never mutates the input.
    """
    has_old_keys = any(k.startswith("ev_") for k in d)
    if not has_old_keys and "ev" not in d:
        return d
    d = dict(d)
    # Intermediate nested format → flatten
    if "ev" in d and isinstance(d.get("ev"), dict):
        ev = d.pop("ev")
        d.update(ev)
        return d
    # Old flat format with ev_ prefix → strip prefix
    mapping = {
        "ev_switch_entity":    "switch_entity",
        "ev_current_entity":   "current_entity",
        "ev_soc_entity":       "soc_entity",
        "ev_battery_entity":   "battery_entity",
        "ev_max_soc":          "max_soc",
        "ev_phases":           "phases",
        "ev_min_current":      "min_current",
        "ev_cable_entity":     "cable_entity",
        "ev_wake_button":      "wake_button",
        "ev_status_sensor":    "status_sensor",
        "ev_location_tracker": "location_tracker",
        # start_threshold_w has no prefix — keep as-is
    }
    for old_key, new_key in mapping.items():
        if old_key in d:
            d[new_key] = d.pop(old_key)
    return d


_CLIMATE_OFF_STATES = ("off", "unavailable", "unknown", "")
# Tolerantie voor 'staat op de bijgestelde doeltemperatuur': thermostaten
# ronden af (0,5 °C, soms 1 °C).
_SETBACK_TOL = 0.5


def _float_attr(state, name: str) -> Optional[float]:
    try:
        value = state.attributes.get(name)
        return None if value is None else float(value)
    except (TypeError, ValueError, AttributeError):
        return None


@dataclass
class ClimateDevice(BaseCascadeDevice):
    """
    Thermostaat (climate.*), bv. een elektrische vloerverwarming.

    Piek-cascade: uitschakelen (hvac-modus 'off'); daarna terug naar de vorige
    modus en doeltemperatuur.
    Solar-cascade: verwarmen op inject_temp zolang er overschot is; daarna
    terug naar de vorige modus en doeltemperatuur.

    power_watts is het vermogen tijdens het verwarmen. Een thermostaat die op
    temperatuur is, trekt niets: dan telt het afschakelen niet als verlaging
    (hvac_action 'idle'), en bij overschot wordt hij niet aangestuurd als de
    kamer al op inject_temp is.
    """
    _action_type: ClassVar[str] = "climate"
    supports_ack: ClassVar[bool] = True

    inject_temp: Optional[float] = None
    # Leeg: de verwarmingsmodus die de thermostaat aanbiedt (heat, heat_cool, auto …).
    heat_mode:   str = ""
    # Piek-cascade: de doeltemperatuur zoveel °C bijstellen (lager bij
    # verwarmen, hoger bij koelen) in plaats van uitschakelen. None/0 = uit.
    peak_setback: Optional[float] = None

    @classmethod
    def _from_dict(cls, base: dict, d: dict) -> "ClimateDevice":
        def _opt_float(value):
            try:
                return float(value) if value not in (None, "") else None
            except (TypeError, ValueError):
                return None
        setback = _opt_float(d.get("peak_setback"))
        return cls(
            **base, inject_temp=_opt_float(d.get("inject_temp")),
            heat_mode=d.get("heat_mode") or "",
            peak_setback=abs(setback) if setback else None,
        )

    @staticmethod
    def _setback_sign(state) -> Optional[int]:
        """
        Richting van de bijstelling bij piekdreiging: -1 = doeltemperatuur
        omlaag (verwarmen), +1 = omhoog (koelen), None = niet bij te stellen
        (ventileren, ontvochtigen, auto zonder gekende richting).
        """
        mode = state.state
        if mode == "heat":
            return -1
        if mode == "cool":
            return 1
        if mode in ("heat_cool", "auto"):
            action = state.attributes.get("hvac_action")
            if action == "heating":
                return -1
            if action == "cooling":
                return 1
            room = _float_attr(state, "current_temperature")
            target = _float_attr(state, "temperature")
            if room is not None and target is not None and abs(room - target) >= 0.5:
                return -1 if room < target else 1
        return None

    def _setback_target(self, state, sign: int, target: Optional[float]) -> Optional[float]:
        """Bijgestelde doeltemperatuur (binnen min/max, op de stapgrootte), of None."""
        if target is None or not self.peak_setback:
            return None
        step = _float_attr(state, "target_temp_step") or 0.5
        new = target + sign * float(self.peak_setback)
        new = round(round(new / step) * step, 2)
        lo = _float_attr(state, "min_temp")
        hi = _float_attr(state, "max_temp")
        if lo is not None:
            new = max(new, lo)
        if hi is not None:
            new = min(new, hi)
        return new if abs(new - target) >= step - 0.01 else None

    def _heat_mode_for(self, state) -> str:
        """Ingestelde modus, anders de eerste niet-'off'-modus van de thermostaat."""
        modes = [m for m in (state.attributes.get("hvac_modes") or []) if m != "off"]
        if self.heat_mode and (not modes or self.heat_mode in modes):
            return self.heat_mode
        for preferred in ("heat", "heat_cool", "auto"):
            if preferred in modes:
                return preferred
        return modes[0] if modes else "heat"

    @staticmethod
    def _is_heating(state) -> bool:
        """
        hvac_action als de thermostaat het meldt; anders: gemeten temperatuur
        onder het doel. Onbekend → aannemen dat hij verwarmt.
        """
        action = state.attributes.get("hvac_action")
        if action is not None:
            return action == "heating"
        room = _float_attr(state, "current_temperature")
        target = _float_attr(state, "temperature")
        if room is None or target is None:
            return True
        return room < target

    def _setpoint_still_ours(self, state, snapshot: DeviceSnapshot) -> bool:
        """
        Staat de doeltemperatuur nog op wat Peak Guard zette (inject_temp)?
        Enkel dan wordt de oude temperatuur teruggezet. De piek-cascade wijzigt
        alleen de modus; en heeft het eigen tijdschema van de thermostaat (of
        de gebruiker) de temperatuur intussen veranderd, dan blijft die staan.
        Een temperatuur sturen zet sommige thermostaten (Tuya) ook van hun
        programma in manuele modus.
        """
        original = snapshot.original_temp
        target = _float_attr(state, "temperature")
        if original is None or target is None:
            return False
        if snapshot.set_temp is not None:
            return (
                abs(target - snapshot.set_temp) <= _SETBACK_TOL
                and abs(target - original) > _SETBACK_TOL / 2
            )
        if self.inject_temp is None:
            return False
        return abs(target - self.inject_temp) < 0.01 and abs(target - original) >= 0.01

    @staticmethod
    def _original_mode(snapshot: DeviceSnapshot) -> str:
        mode = snapshot.original_state or "off"
        return "off" if mode in ("unavailable", "unknown") else mode

    def _counts_avoid(self, snapshot: DeviceSnapshot) -> bool:
        return self._original_mode(snapshot) != "off"

    def applied(self, state, snapshot: DeviceSnapshot, cascade_type: str) -> bool:
        if cascade_type == "peak":
            if snapshot.set_temp is not None:
                target = _float_attr(state, "temperature")
                return target is not None and abs(target - snapshot.set_temp) <= _SETBACK_TOL
            return state.state == "off"
        target = _float_attr(state, "temperature")
        return (
            self.inject_temp is not None and target is not None
            and abs(target - self.inject_temp) <= 0.5   # afronding door de thermostaat
            and state.state == self._heat_mode_for(state)
        )

    def restored(self, state, snapshot: DeviceSnapshot, cascade_type: str) -> bool:
        if snapshot.set_temp is not None and self._original_mode(snapshot) != "off":
            # Enkel de doeltemperatuur werd bijgesteld: terug = niet meer op
            # de bijgestelde waarde (de oude, of wat de gebruiker koos).
            if state.state in ("unavailable", "unknown"):
                return False
            target = _float_attr(state, "temperature")
            if target is None:
                return False
            if snapshot.original_temp is not None and abs(target - snapshot.original_temp) <= 0.25:
                return True
            return abs(target - snapshot.set_temp) > _SETBACK_TOL
        return state.state == self._original_mode(snapshot)

    async def _call(self, ctx: CascadeContext, service: str, data: dict) -> None:
        await ctx.hass.services.async_call(
            "climate", service, {"entity_id": self.entity_id, **data}, blocking=True)
        ctx.track_action(self.entity_id, f"climate.{service}", data or None)

    async def apply(self, excess: float, snapshots: dict, ctx: CascadeContext) -> float:
        state = ctx.hass.states.get(self.entity_id)
        if state is None:
            ctx.warn(
                "Peak Guard: entity '%s' ('%s') niet gevonden in HA — apparaat overgeslagen",
                self.entity_id, self.name,
            )
            return excess
        if ctx.cascade_type == "peak":
            return await self._apply_peak(excess, snapshots, ctx, state)
        return await self._apply_solar(excess, snapshots, ctx, state)

    async def _apply_peak(self, excess, snapshots, ctx, state) -> float:
        gate = self._open_action_gate(snapshots, ctx, self.shed_power_w(ctx.hass))
        if gate is not None:
            return excess - gate
        if state.state in _CLIMATE_OFF_STATES:
            ctx.last_skip_reason = f"thermostaat staat al uit ({state.state or '?'})"
            return excess
        if self.peak_setback:
            return await self._apply_setback(excess, snapshots, ctx, state)
        stale = snapshots.get(self.entity_id)
        if stale is not None and stale.set_temp is not None:
            # peak_setback werd tijdens de piek uitgezet: nu uitschakelen.
            stale.set_temp = None
        if not self._is_heating(state):
            # Op temperatuur: niets af te schakelen. Uitzetten zou het eigen
            # tijdschema van de thermostaat onderbreken (een Tuya-thermostaat
            # met enkel off/heat_cool is dan echt uit). Begint hij tijdens de
            # piek te verwarmen, dan schakelt de volgende cyclus hem af.
            ctx.last_skip_reason = "verwarmt niet (op temperatuur) — eigen schema blijft lopen"
            return excess
        if self.draws_nothing(ctx.hass):
            ctx.last_skip_reason = (
                f"verbruikt nu niets ({self.live_power_w(ctx.hass):.0f} W volgens "
                f"{self.power_sensor})"
            )
            return excess
        power_w = self.shed_power_w(ctx.hass)
        created = self.entity_id not in snapshots
        if created:
            snapshots[self.entity_id] = DeviceSnapshot(
                entity_id=self.entity_id, original_state=state.state,
                original_temp=_float_attr(state, "temperature"),
            )
        snapshot = snapshots[self.entity_id]
        try:
            await self._call(ctx, "set_hvac_mode", {"hvac_mode": "off"})
        except HomeAssistantError as err:
            ctx.warn(
                "Peak Guard: '%s' niet bereikbaar om uit te schakelen — "
                "volgende cyclus opnieuw (%s)", self.name, err,
            )
            if created:
                snapshots.pop(self.entity_id, None)
            return excess
        _LOGGER.info(
            "Peak Guard: → thermostaat '%s' UITgeschakeld (piekbeperking, -%.0f W, overschot was %.0f W)",
            self.name, power_w, excess,
        )
        await self._confirm_apply(snapshot, ctx, power_w)
        return excess - power_w

    async def _apply_setback(self, excess, snapshots, ctx, state) -> float:
        """Piek: doeltemperatuur bijstellen (airco, warmtepomp) i.p.v. uitschakelen."""
        existing = snapshots.get(self.entity_id)
        target = _float_attr(state, "temperature")
        if existing is not None and existing.set_temp is not None:
            if target is not None and abs(target - existing.set_temp) <= _SETBACK_TOL:
                # Al bijgesteld: nooit een tweede keer (anders telkens verder).
                ctx.last_skip_reason = f"al bijgesteld naar {existing.set_temp:.1f} °C"
                return excess
            if existing.pending is None:
                # Bevestigd, en daarna zette de gebruiker (of het eigen schema)
                # een andere temperatuur: die blijft staan, ook na de piek.
                ctx.last_skip_reason = (
                    f"doeltemperatuur gewijzigd naar {target if target is not None else '?'} °C "
                    "tijdens de piek — blijft staan"
                )
                return excess
        if state.state in ("unavailable", "unknown") or target is None:
            ctx.last_skip_reason = "doeltemperatuur onbekend"
            return excess
        # Vertrekpunt: de instelling van vóór de ingreep — bij een herhaling
        # (geen bevestiging) dezelfde waarde, en bij een thermostaat die de
        # solar-cascade opwarmde de temperatuur van vóór het overschot.
        base_temp = target
        if existing is not None and existing.original_temp is not None:
            base_temp = existing.original_temp
        else:
            inject_snap = (ctx.inject_snapshots or {}).get(self.entity_id)
            if (
                inject_snap is not None and inject_snap.original_temp is not None
                and self._original_mode(inject_snap) != "off"
            ):
                base_temp = inject_snap.original_temp
        action = state.attributes.get("hvac_action")
        if action in ("idle", "off", "fan", "drying"):
            ctx.last_skip_reason = f"draait niet ({action}) — niets af te schakelen"
            return excess
        sign = self._setback_sign(state)
        if sign is None:
            ctx.last_skip_reason = f"modus '{state.state}': richting van bijstellen onbekend"
            return excess
        # Zonder hvac_action (Daikin Onecta) telt hij als draaiend: de
        # kamertemperatuur komt van de sensor in het binnentoestel (bovenaan
        # de muur) en ligt tijdens het verwarmen 3–4 °C boven de kamer
        # (veldwaarneming 2026-11-16: 25 °C bij doel 21 °C, 4,4 kWh verbruikt).
        new_temp = self._setback_target(state, sign, base_temp)
        if new_temp is None or (sign < 0 and new_temp >= target) or (sign > 0 and new_temp <= target):
            ctx.last_skip_reason = "doeltemperatuur al aan de grens of al lager"
            return excess
        if self.draws_nothing(ctx.hass):
            ctx.last_skip_reason = (
                f"verbruikt nu niets ({self.live_power_w(ctx.hass):.0f} W volgens "
                f"{self.power_sensor})"
            )
            return excess
        power_w = self.shed_power_w(ctx.hass)
        created = existing is None
        if created:
            snapshots[self.entity_id] = DeviceSnapshot(
                entity_id=self.entity_id, original_state=state.state, original_temp=base_temp,
            )
        snapshot = snapshots[self.entity_id]
        previous_set = snapshot.set_temp
        snapshot.set_temp = new_temp
        try:
            await self._call(ctx, "set_temperature", {"temperature": new_temp})
        except HomeAssistantError as err:
            ctx.warn(
                "Peak Guard: '%s' niet bereikbaar om bij te stellen — "
                "volgende cyclus opnieuw (%s)", self.name, err,
            )
            if created:
                snapshots.pop(self.entity_id, None)
            else:
                snapshot.set_temp = previous_set
            return excess
        _LOGGER.info(
            "Peak Guard: → thermostaat '%s' %.1f → %.1f °C (piekbeperking, -%.0f W, overschot was %.0f W)",
            self.name, target, new_temp, power_w, excess,
        )
        await self._confirm_apply(snapshot, ctx, power_w)
        return excess - power_w

    async def _apply_solar(self, excess, snapshots, ctx, state) -> float:
        if self.inject_temp is None or state.state in ("unavailable", "unknown"):
            return excess
        gate = self._open_action_gate(snapshots, ctx, float(self.power_watts or 0))
        if gate is not None:
            return excess - gate
        target = _float_attr(state, "temperature")
        heat_mode = self._heat_mode_for(state)
        if state.state == heat_mode and target is not None and target >= self.inject_temp:
            ctx.last_skip_reason = f"verwarmt al op {target:.1f} °C"
            return excess
        room = _float_attr(state, "current_temperature")
        if room is not None and room >= self.inject_temp:
            ctx.last_skip_reason = f"al {room:.1f} °C (doel {self.inject_temp:.1f} °C)"
            return excess
        if self.power_watts and excess < self.power_watts:
            ctx.last_skip_reason = (
                f"overschot {excess:.0f} W < vermogen {self.power_watts:.0f} W"
            )
            ctx.ev_guard.set_hold(
                self.id, "surplus_too_small",
                f"Wacht: het zonne-overschot ({excess:.0f} W) dekt het vermogen van "
                f"deze verwarming ({self.power_watts:.0f} W) nog niet.",
            )
            return excess
        created = self.entity_id not in snapshots
        if created:
            snapshots[self.entity_id] = DeviceSnapshot(
                entity_id=self.entity_id, original_state=state.state, original_temp=target,
            )
        snapshot = snapshots[self.entity_id]
        try:
            data = {"temperature": self.inject_temp}
            if state.state != heat_mode:
                data["hvac_mode"] = heat_mode
            await self._call(ctx, "set_temperature", data)
        except HomeAssistantError as err:
            ctx.warn(
                "Peak Guard: '%s' niet bereikbaar om op %.1f °C te zetten — "
                "volgende cyclus opnieuw (%s)",
                self.name, self.inject_temp, err,
            )
            if created:
                snapshots.pop(self.entity_id, None)
            return excess
        _LOGGER.info(
            "Peak Guard: → thermostaat '%s' op %.1f °C (injectiepreventie, +%d W, overschot was %.0f W)",
            self.name, self.inject_temp, self.power_watts, excess,
        )
        await self._confirm_apply(snapshot, ctx, float(self.power_watts or 0))
        return excess - self.power_watts

    async def restore(self, snapshot: DeviceSnapshot, ctx: CascadeContext) -> bool:
        state = ctx.hass.states.get(self.entity_id)
        if state is None:
            ctx.warn("Peak Guard: kan '%s' niet herstellen — entity niet gevonden", self.name)
            return False
        original_mode = self._original_mode(snapshot)
        try:
            sent = False
            if (
                snapshot.set_temp is not None and original_mode != "off"
                and (state.state in ("unavailable", "unknown")
                     or _float_attr(state, "temperature") is None)
            ):
                # Cloud even weg: snapshot houden en volgende cyclus opnieuw,
                # anders blijft de bijstelling zonder eigenaar staan.
                ctx.last_skip_reason = "niet bereikbaar — herstel volgende cyclus"
                return False
            if snapshot.set_temp is not None and original_mode != "off":
                # Bijgesteld (peak_setback): enkel de temperatuur terug, en
                # alleen als ze nog op de bijgestelde waarde staat en de
                # airco aan staat. De modus heeft Peak Guard niet gewijzigd.
                if (
                    state.state not in _CLIMATE_OFF_STATES
                    and self._setpoint_still_ours(state, snapshot)
                ):
                    await self._call(ctx, "set_temperature", {"temperature": snapshot.original_temp})
                    sent = True
            elif original_mode != "off" and self._setpoint_still_ours(state, snapshot):
                data = {"temperature": snapshot.original_temp}
                if state.state != original_mode:
                    data["hvac_mode"] = original_mode
                await self._call(ctx, "set_temperature", data)
                sent = True
            elif state.state != original_mode:
                await self._call(ctx, "set_hvac_mode", {"hvac_mode": original_mode})
                sent = True
        except HomeAssistantError as err:
            ctx.warn(
                "Peak Guard: '%s' niet bereikbaar bij herstel — "
                "volgende cyclus opnieuw proberen (%s)", self.name, err)
            return False
        except (ValueError, TypeError) as err:
            _LOGGER.error("Peak Guard: fout bij herstellen '%s': %s", self.name, err)
            return False
        if not sent:
            # Al in de oorspronkelijke modus: een eerder herstel is uitgevoerd,
            # of de gebruiker zette hem zelf terug.
            if ctx.cascade_type == "peak" and snapshot.pending != "restore":
                ctx.peak_tracker.discard_pending(self.id)
                snapshot.pending = None
                return True
            self.on_restore_ack(snapshot, ctx, ctx.now)
            return True
        _LOGGER.info(
            "Peak Guard: thermostaat '%s' hersteld na %s (%s%s)",
            self.name, "piekbeperking" if ctx.cascade_type == "peak" else "injectiepreventie",
            original_mode,
            f", {snapshot.original_temp:.1f} °C" if snapshot.original_temp is not None else "",
        )
        return await self._confirm_restore(snapshot, ctx)


def _opt_seconds(value) -> Optional[int]:
    """Optionele duur in seconden; leeg, ≤ 0 of onzin → None."""
    if value in (None, ""):
        return None
    try:
        seconds = int(float(value))
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


# Registry populated after all subclasses are defined.
_DEVICE_REGISTRY: dict[str, type[BaseCascadeDevice]] = {}


def from_dict(d: dict) -> BaseCascadeDevice:
    """Factory: create the right device subclass from a serialised dict.

    Accepts both the current format and old flat/nested EV formats (migrates
    on the fly).  To register a new device type, add a subclass with a
    non-empty _action_type ClassVar — no changes to this function needed.
    """
    d = _migrate_flat_format(d)
    action_type = d["action_type"]
    base = {
        "id":              d["id"],
        "name":            d["name"],
        "entity_id":       d["entity_id"],
        "priority":        d["priority"],
        "action_type":     action_type,
        "power_watts":     d.get("power_watts", 0),
        "enabled":         d.get("enabled", True),
        "manual_override": d.get("manual_override", False),
        "power_sensor":    d.get("power_sensor") or "",
        "max_off_s":       _opt_seconds(d.get("max_off_s")),
        "min_on_s":        _opt_seconds(d.get("min_on_s")),
    }
    cls = _DEVICE_REGISTRY.get(action_type)
    if cls is None:
        _LOGGER.warning(
            "Peak Guard: onbekend action_type '%s' voor '%s' — SwitchOff fallback",
            action_type, d.get("name", "?"),
        )
        return SwitchOffDevice(**base)
    return cls._from_dict(base, d)


# Populate registry after all subclasses are defined.
_DEVICE_REGISTRY.update({
    cls._action_type: cls
    for cls in (SwitchOffDevice, SwitchOnDevice, ThrottleDevice, EVChargerDevice, ClimateDevice)
})
