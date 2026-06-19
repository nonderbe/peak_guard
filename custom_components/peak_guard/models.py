"""
Peak Guard — models.py

Alle dataclasses, enums en waarde-objecten die door meerdere modules
worden gebruikt.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any, Callable, ClassVar, Deque, Optional

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

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
EV_MIN_ON_DURATION_S: float = 360.0
EV_MIN_OFF_DURATION_S: float = 300.0
EV_WAKE_TIMEOUT_S: float = 15.0
EV_CMD_MAX_RETRIES: int = 2
EV_CMD_RETRY_DELAY_S: float = 3.0
EV_RATE_LIMIT_MAX_CALLS: int = 12
EV_RATE_LIMIT_WINDOW_S: float = 600.0
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
    last_skip_reason: str = ""


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


@dataclass
class SwitchOffDevice(BaseCascadeDevice):
    """Peak-limiting switch: turns a device OFF to reduce peak demand."""
    _action_type: ClassVar[str] = "switch_off"

    async def apply(self, excess: float, snapshots: dict, ctx: CascadeContext) -> float:
        state = ctx.hass.states.get(self.entity_id)
        if state is None:
            ctx.warn(
                "Peak Guard: entity '%s' ('%s') niet gevonden in HA — apparaat overgeslagen",
                self.entity_id, self.name,
            )
            return excess
        if state.state == "on":
            if self.entity_id not in snapshots:
                snapshots[self.entity_id] = DeviceSnapshot(
                    entity_id=self.entity_id, original_state=state.state)
            try:
                await ctx.hass.services.async_call(
                    "switch", "turn_off", {"entity_id": self.entity_id}, blocking=True)
            except HomeAssistantError as err:
                ctx.warn(
                    "Peak Guard: '%s' niet bereikbaar voor turn_off — "
                    "snapshot teruggedraaid, volgende cyclus opnieuw (%s)",
                    self.name, err,
                )
                snapshots.pop(self.entity_id, None)
                return excess
            ctx.track_action(self.entity_id, "switch.turn_off")
            _LOGGER.info(
                "Peak Guard: → '%s' UITgeschakeld (piekbeperking, -%d W, overschot was %.0f W)",
                self.name, self.power_watts, excess,
            )
            ctx.peak_tracker.record_pending_avoid(
                device_id=self.id, device_name=self.name,
                nominal_kw=self.power_watts / 1000.0, ts=datetime.now(timezone.utc),
            )
            return excess - self.power_watts
        _LOGGER.info(
            "Peak Guard: → '%s' al UIT — overgeslagen (piekbeperking, staat=%s)",
            self.name, state.state,
        )
        return excess

    async def restore(self, snapshot: DeviceSnapshot, ctx: CascadeContext) -> bool:
        state = ctx.hass.states.get(self.entity_id)
        if state is None:
            ctx.warn("Peak Guard: kan '%s' niet herstellen — entity niet gevonden", self.name)
            return False
        try:
            if snapshot.original_state == "on" and state.state != "on":
                await ctx.hass.services.async_call(
                    "switch", "turn_on", {"entity_id": self.entity_id}, blocking=True)
                _LOGGER.info(
                    "Peak Guard: '%s' terug ingeschakeld na piekbeperking (originele staat: %s)",
                    self.name, snapshot.original_state,
                )
                ctx.peak_tracker.start_measurement_on_turnon(
                    device_id=self.id, device_name=self.name, ts=datetime.now(timezone.utc))
            elif snapshot.original_state == "on" and state.state == "on":
                event = ctx.peak_tracker.complete_peak_calculation(
                    device_id=self.id, now=datetime.now(timezone.utc))
                if event:
                    _LOGGER.info(
                        "Peak Guard: piek-event afgerond voor '%s' — "
                        "duur=%.1f min, vermeden=%.3f kW, besparing=€%.4f",
                        self.name, event.measured_duration_min,
                        event.avoided_peak_kw, event.savings_euro,
                    )
            return True
        except HomeAssistantError as err:
            ctx.warn(
                "Peak Guard: '%s' niet bereikbaar bij herstel — "
                "volgende cyclus opnieuw proberen (%s)", self.name, err)
        except (ValueError, TypeError) as err:
            _LOGGER.error("Peak Guard: fout bij herstellen '%s': %s", self.name, err)
        return False


@dataclass
class SwitchOnDevice(BaseCascadeDevice):
    """Injection-prevention switch: turns a device ON to consume solar surplus."""
    _action_type: ClassVar[str] = "switch_on"

    async def apply(self, excess: float, snapshots: dict, ctx: CascadeContext) -> float:
        state = ctx.hass.states.get(self.entity_id)
        if state is None:
            ctx.warn(
                "Peak Guard: entity '%s' ('%s') niet gevonden in HA — apparaat overgeslagen",
                self.entity_id, self.name,
            )
            return excess
        if state.state == "off":
            if self.entity_id not in snapshots:
                snapshots[self.entity_id] = DeviceSnapshot(
                    entity_id=self.entity_id, original_state=state.state)
            try:
                await ctx.hass.services.async_call(
                    "switch", "turn_on", {"entity_id": self.entity_id}, blocking=True)
            except HomeAssistantError as err:
                ctx.warn(
                    "Peak Guard: '%s' niet bereikbaar voor turn_on — "
                    "snapshot teruggedraaid, volgende cyclus opnieuw (%s)",
                    self.name, err,
                )
                snapshots.pop(self.entity_id, None)
                return excess
            ctx.track_action(self.entity_id, "switch.turn_on")
            _LOGGER.info(
                "Peak Guard: → '%s' AANgeschakeld (injectiepreventie, +%d W, overschot was %.0f W)",
                self.name, self.power_watts, excess,
            )
            ctx.solar_tracker.start_solar_measurement(
                device_id=self.id, device_name=self.name,
                nominal_kw=self.power_watts / 1000.0, ts=datetime.now(timezone.utc),
            )
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
        try:
            if snapshot.original_state == "off" and state.state != "off":
                await ctx.hass.services.async_call(
                    "switch", "turn_off", {"entity_id": self.entity_id}, blocking=True)
                _LOGGER.info(
                    "Peak Guard: '%s' terug uitgeschakeld na injectiepreventie", self.name)
                event = ctx.solar_tracker.complete_solar_calculation(
                    device_id=self.id, now=datetime.now(timezone.utc))
                if event:
                    _LOGGER.info(
                        "Peak Guard: solar-event afgerond voor '%s' — "
                        "duur=%.1f min, verschoven=%.4f kWh, besparing=€%.4f",
                        self.name, event.measured_duration_min,
                        event.shifted_kwh, event.savings_euro,
                    )
            return True
        except HomeAssistantError as err:
            ctx.warn(
                "Peak Guard: '%s' niet bereikbaar bij herstel — "
                "volgende cyclus opnieuw proberen (%s)", self.name, err)
        except (ValueError, TypeError) as err:
            _LOGGER.error("Peak Guard: fout bij herstellen '%s': %s", self.name, err)
        return False


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
        )

    async def apply(self, excess: float, snapshots: dict, ctx: CascadeContext) -> float:
        result = await ctx.ev_guard.apply_action(
            device=self,
            excess=excess,
            snapshots=snapshots,
            cascade_type=ctx.cascade_type,
            peak_tracker=ctx.peak_tracker,
            solar_tracker=ctx.solar_tracker,
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
        )


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
    for cls in (SwitchOffDevice, SwitchOnDevice, ThrottleDevice, EVChargerDevice)
})
