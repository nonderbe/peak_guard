"""
Peak Guard — deciders/schedule_decider.py

ScheduleDecider: stuurt apparaten volgens het laadschema (Planning-tab).

Eigenaarschap per apparaat, per iteratie:
  piek (piek-snapshot)  >  schema (venster actief, doel niet bereikt)
                        >  solar (inject-snapshot)  >  niemand

- In een venster laadt een EV tot het doel-SoC van het venster, met een
  laadstroom die onder de maandpiek blijft. De inject-cascade laat het
  apparaat dan met rust; de piek-cascade mag het nog altijd afschakelen,
  maar het herstel daarvan doet het schema zelf (anders flappert de lader).
- Doel bereikt → het schema laat de EV los; zonne-overschot mag hem dan via
  de solar-cascade verder laden (tot max_soc).
- Einde venster → laden stoppen, of overdragen aan solar als er zonder de EV
  overschot zou zijn. Daarna gaat de laadlimiet naar de rust-laadlimiet.
- Buiten de vensters wordt een lading die PG niet startte en die niet op
  zonne-overschot draait, gestopt (block_unplanned).
- Een schakelaar staat AAN tijdens het venster en gaat daarna terug naar zijn
  vorige staat.
"""
from __future__ import annotations

import logging
import math
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Callable, Dict, List, Optional

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from ..const import (
    CONF_BUFFER_WATTS,
    CONF_PEAK_SENSOR,
    DEFAULT_BUFFER_WATTS,
    DEFAULT_EV_MAX_AMPERE,
    SCHEDULE_INCREASE_INTERVAL_S,
    SCHEDULE_INCREASE_MIN_A,
    SCHEDULE_SENSOR_STALE_S,
    SCHEDULE_SOC_RESEND_S,
    SCHEDULE_START_CONFIRM_S,
    SCHEDULE_UNPLANNED_GRACE_S,
)
from ..models import (
    BaseCascadeDevice,
    DeviceSnapshot,
    EVState,
    ScheduleEntry,
)
from ..schedule import ActiveResult, evaluate_entry, normalize_state, sensor_entities
from ..utils import effective_peak_w
from .base import BaseDecider, read_power_w, read_sensor

if TYPE_CHECKING:
    from ..avoided_peak_tracker import PeakAvoidTracker, SolarShiftTracker
    from .ev_guard import EVGuard

_LOGGER = logging.getLogger(__name__)

_UNAVAILABLE = ("unknown", "unavailable", "")


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt is not None else None


def _parse_dt(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


_DT_FIELDS = ("started_at", "soc_sent_at", "unplanned_since", "retry_after")
# Na zoveel turn_on's zonder dat de EV echt begint te laden: pauze.
SCHEDULE_MAX_START_ATTEMPTS = 3
SCHEDULE_START_BACKOFF_S = 1800.0


@dataclass
class ScheduleRunState:
    """Runtime-toestand van één schema-item (wordt bewaard over herstarts)."""
    active:              bool = False
    started_at:          Optional[datetime] = None
    label:               str = ""
    target_soc:          Optional[int] = None
    fulfilled:           bool = False
    started_by_schedule: bool = False
    soc_target_sent:     Optional[int] = None
    soc_sent_at:         Optional[datetime] = None
    rest_pending:        bool = False
    stop_pending:        bool = False
    switch_prev_state:   Optional[str] = None
    unplanned_since:     Optional[datetime] = None
    start_attempts:      int = 0
    retry_after:         Optional[datetime] = None
    status:              str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        for f in _DT_FIELDS:
            d[f] = _iso(getattr(self, f))
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "ScheduleRunState":
        known = {f.name for f in fields(cls)}
        rs = cls(**{k: v for k, v in (d or {}).items() if k in known})
        for f in _DT_FIELDS:
            setattr(rs, f, _parse_dt(getattr(rs, f)))
        return rs


class ScheduleDecider(BaseDecider):

    def __init__(
        self,
        hass: HomeAssistant,
        config: dict,
        peak_tracker: "PeakAvoidTracker",
        solar_tracker: "SolarShiftTracker",
        ev_guard: "EVGuard",
        iteration_actions: list,
        save_fn: Callable,
        peak_cascade: List[BaseCascadeDevice],
        inject_cascade: List[BaseCascadeDevice],
        peak_snapshots: Dict[str, DeviceSnapshot],
        inject_snapshots: Dict[str, DeviceSnapshot],
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
        self._peak_cascade = peak_cascade
        self._inject_cascade = inject_cascade
        self._peak_snapshots = peak_snapshots
        self._inject_snapshots = inject_snapshots
        self.entries: List[ScheduleEntry] = []
        self._run: Dict[str, ScheduleRunState] = {}
        # Laatste geldige staat per tariefsensor: {"state": str, "seen_at": datetime}
        self._sensor_mem: Dict[str, dict] = {}
        self._sensor_states: Dict[str, Optional[str]] = {}
        self._sensor_warned: set = set()
        self._sensor_saved_at: Dict[str, datetime] = {}
        # Na een (her)start: een tariefsensor die nog niet beschikbaar is,
        # behoudt zijn bewaarde staat ongeacht de leeftijd ervan.
        self._startup_until: Optional[datetime] = None
        # Verwijderde items met een actief venster: volgende iteratie afsluiten.
        self._ending: List[tuple] = []

    # ------------------------------------------------------------------ #
    #  Configuratie en persistentie                                        #
    # ------------------------------------------------------------------ #

    def set_entries(self, entries: List[ScheduleEntry]) -> None:
        """Vervang alle schema-items (één item per entity)."""
        seen: set = set()
        unique: List[ScheduleEntry] = []
        for e in entries:
            if e.entity_id in seen:
                _LOGGER.warning(
                    "Peak Guard schema: tweede item voor '%s' genegeerd (één schema per apparaat)",
                    e.entity_id,
                )
                continue
            seen.add(e.entity_id)
            unique.append(e)
        new_ids = {e.id for e in unique}
        for old in self.entries:
            if old.id in new_ids:
                continue
            rs = self._run.get(old.id)
            if rs is not None and rs.active:
                # Venster netjes afsluiten (stoppen, limiet herstellen) zoals
                # bij uitschakelen; gebeurt in de volgende iteratie.
                self._ending.append((old, rs))
            elif old.is_ev:
                guard = self.ev_guard.guards.get(old.device.id)
                if guard is not None and guard.scheduled:
                    self.ev_guard.release_schedule(old.device)
        self.entries = unique
        self._run = {k: v for k, v in self._run.items() if k in new_ids}

    def entries_to_list(self) -> list:
        return [asdict(e) for e in self.entries]

    def state_to_dict(self) -> dict:
        return {
            "entries": {k: v.to_dict() for k, v in self._run.items()},
            "sensors": {
                k: {"state": v["state"], "seen_at": _iso(v["seen_at"])}
                for k, v in self._sensor_mem.items()
            },
        }

    def load_state(self, data: Optional[dict]) -> None:
        data = data or {}
        ids = {e.id for e in self.entries}
        self._run = {
            k: ScheduleRunState.from_dict(v)
            for k, v in (data.get("entries") or {}).items() if k in ids
        }
        self._sensor_mem = {}
        for k, v in (data.get("sensors") or {}).items():
            seen_at = _parse_dt((v or {}).get("seen_at"))
            if seen_at is not None and (v or {}).get("state") is not None:
                self._sensor_mem[k] = {"state": str(v["state"]), "seen_at": seen_at}

    def _rs(self, entry: ScheduleEntry) -> ScheduleRunState:
        if entry.id not in self._run:
            self._run[entry.id] = ScheduleRunState()
        return self._run[entry.id]

    # ------------------------------------------------------------------ #
    #  Opzoekingen voor de andere deciders                                 #
    # ------------------------------------------------------------------ #

    def entry_for_entity(self, entity_id: str) -> Optional[ScheduleEntry]:
        for e in self.entries:
            if e.enabled and e.entity_id == entity_id:
                return e
        return None

    def is_controlled(self, device: BaseCascadeDevice) -> bool:
        """True als het schema dit apparaat nu aanstuurt (inject-cascade slaat het over)."""
        entry = self.entry_for_entity(device.entity_id)
        if entry is None:
            return False
        rs = self._run.get(entry.id)
        if rs is None or not rs.active:
            return False
        return not entry.is_ev or not rs.fulfilled

    def skip_peak_restore(self, device: BaseCascadeDevice) -> bool:
        """True als het piekherstel van deze EV door het schema afgehandeld wordt.

        In een venster herstelt het schema zelf (met eigen hysteresis, zodat
        een Tesla-schakelaar die altijd 'unknown' meldt niet flappert). Buiten
        een venster met block_unplanned zou herstel een ongeplande lading
        herstarten, dus ruimt het schema de snapshot op.
        """
        entry = self.entry_for_entity(device.entity_id)
        if entry is None or not entry.is_ev:
            return False
        if self.is_controlled(device):
            return True
        rs = self._run.get(entry.id)
        return (
            (rs is None or not rs.active)
            and entry.block_unplanned
            and device.entity_id not in self._inject_snapshots
        )

    def soc_rest_for(self, entity_id: str) -> Optional[int]:
        """Laadlimiet die PG achterlaat: doel van het actieve venster, anders rust-laadlimiet."""
        entry = self.entry_for_entity(entity_id)
        if entry is None or not entry.is_ev:
            return None
        rs = self._run.get(entry.id)
        if rs is not None and rs.active and rs.target_soc is not None:
            return rs.target_soc
        return entry.rest_soc

    def watched_entities(self) -> set:
        """Tariefsensoren van sensorvensters (elke staatswissel → directe iteratie)."""
        return sensor_entities(self.entries)

    def _manual_override(self, entity_id: str) -> bool:
        for d in list(self._peak_cascade) + list(self._inject_cascade):
            if d.entity_id == entity_id and d.manual_override:
                return True
        entry = self.entry_for_entity(entity_id)
        return bool(entry and entry.device.manual_override)

    def _sync_guards(self, entry: ScheduleEntry) -> None:
        """Neem de schakeltoestand over naar de guards van andere kopieën.

        EVGuard houdt guards bij per device.id; staat dezelfde EV met een
        ander id in de piek-cascade, dan moet die guard weten dat het schema
        de lader aan- of uitzette (anders slaat _apply_peak een turn_off over
        als 'redundant').
        """
        src = self.ev_guard.get_guard(entry.device.id)
        for d in list(self._peak_cascade) + list(self._inject_cascade):
            if d.entity_id != entry.entity_id or d.id == entry.device.id:
                continue
            g = self.ev_guard.get_guard(d.id)
            g.state = src.state
            g.last_switch_state = src.last_switch_state
            g.turned_on_at = src.turned_on_at
            g.turned_off_at = src.turned_off_at
            g.turned_off_by_pg = src.turned_off_by_pg
            g.last_sent_amps = src.last_sent_amps
            g.last_current_update = src.last_current_update

    def _in_inject_cascade(self, entity_id: str) -> bool:
        return any(d.entity_id == entity_id for d in self._inject_cascade)

    # ------------------------------------------------------------------ #
    #  Status voor de REST API                                             #
    # ------------------------------------------------------------------ #

    def status_dict(self) -> dict:
        out = {}
        for e in self.entries:
            rs = self._run.get(e.id) or ScheduleRunState()
            battery = read_sensor(self.hass, getattr(e.device, "battery_entity", None)) if e.is_ev else None
            out[e.id] = {
                "active":     rs.active,
                "label":      rs.label,
                "target_soc": rs.target_soc,
                "fulfilled":  rs.fulfilled,
                "status":     rs.status or ("buiten venster" if not rs.active else ""),
                "battery":    battery,
                "started_at": _iso(rs.started_at),
                "controlled": self.is_controlled(e.device),
            }
        return out

    # ------------------------------------------------------------------ #
    #  Hoofdlus                                                            #
    # ------------------------------------------------------------------ #

    async def check(self, consumption: Optional[float], now: Optional[datetime] = None) -> None:
        """Eén iteratie. consumption=None: verbruiksensor onbeschikbaar — enkel
        stoppen en limieten herstellen, niets starten of overdragen."""
        if now is None:
            now = datetime.now(timezone.utc)
        changed = False
        while self._ending:
            old, rs = self._ending.pop(0)
            try:
                if old.is_ev:
                    await self._ev_window_end(old, rs, consumption, now)
                    self._sync_guards(old)
                else:
                    await self._tick_switch(old, ActiveResult(active=False), consumption, now, rs)
            except Exception:
                _LOGGER.exception("Peak Guard schema: afsluiten van '%s' mislukt", old.device.name)
            changed = True
        if not self.entries:
            if changed:
                await self._save_fn()
            return
        if self._startup_until is None:
            self._startup_until = now + timedelta(seconds=SCHEDULE_SENSOR_STALE_S)
        changed |= self._refresh_sensors(now)
        for entry in list(self.entries):
            res = (
                evaluate_entry(entry, now, self._sensor_states)
                if entry.enabled else ActiveResult(active=False)
            )
            try:
                if entry.is_ev:
                    ev_changed = await self._tick_ev(entry, res, consumption, now)
                    if ev_changed:
                        self._sync_guards(entry)
                    changed |= ev_changed
                else:
                    changed |= await self._tick_switch(entry, res, consumption, now)
            except Exception:
                _LOGGER.exception("Peak Guard schema: fout bij '%s'", entry.device.name)
        if changed:
            await self._save_fn()

    def _refresh_sensors(self, now: datetime) -> bool:
        """Bepaal de effectieve staat per tariefsensor.

        Geeft True als het sensorgeheugen bewaard moet worden (staat gewijzigd
        of de laatst bewaarde 'seen_at' ouder dan 5 min), zodat een herstart de
        laatste staat kent.
        """
        persist = False
        self._sensor_states = {}
        startup = self._startup_until is not None and now < self._startup_until
        for eid in sensor_entities(self.entries):
            st = self.hass.states.get(eid)
            if st is not None and st.state not in _UNAVAILABLE:
                state = normalize_state(st.state)
                prev = self._sensor_mem.get(eid)
                saved_at = self._sensor_saved_at.get(eid)
                if (
                    prev is None or prev["state"] != state or saved_at is None
                    or (now - saved_at).total_seconds() >= 300
                ):
                    persist = True
                    self._sensor_saved_at[eid] = now
                self._sensor_mem[eid] = {"state": state, "seen_at": now}
                self._sensor_warned.discard(eid)
                self._sensor_states[eid] = state
                continue
            mem = self._sensor_mem.get(eid)
            if mem is not None and (
                startup or (now - mem["seen_at"]).total_seconds() <= SCHEDULE_SENSOR_STALE_S
            ):
                self._sensor_states[eid] = mem["state"]
                continue
            self._sensor_states[eid] = None
            if eid not in self._sensor_warned:
                self._sensor_warned.add(eid)
                self.ev_guard._warn(
                    "Peak Guard schema: tariefsensor '%s' al meer dan %.0f min niet beschikbaar "
                    "— sensorvensters gelden als inactief",
                    eid, SCHEDULE_SENSOR_STALE_S / 60,
                )
        return persist

    def _allowance_w(self, consumption: Optional[float], own_draw_w: float) -> Optional[float]:
        """Vermogen (W) dat dit apparaat mag trekken zonder de piekgrens − buffer te raken."""
        if consumption is None:
            return None
        raw_peak = read_power_w(self.hass, self.config.get(CONF_PEAK_SENSOR))
        if raw_peak is None:
            return None
        buffer = float(self.config.get(CONF_BUFFER_WATTS, DEFAULT_BUFFER_WATTS))
        return effective_peak_w(raw_peak) - buffer - (consumption - own_draw_w)

    def _drop_peak_snapshot(self, entry: ScheduleEntry, now: datetime) -> bool:
        if self._peak_snapshots.pop(entry.entity_id, None) is None:
            return False
        try:
            self.peak_tracker.complete_peak_calculation(device_id=entry.device.id, now=now)
        except Exception:
            _LOGGER.exception("Peak Guard schema: piek-event afronden mislukt voor '%s'", entry.device.name)
        return True

    def _takeover_inject_snapshot(self, entry: ScheduleEntry, now: datetime) -> Optional[DeviceSnapshot]:
        snap = self._inject_snapshots.pop(entry.entity_id, None)
        if snap is None:
            return None
        self.solar_tracker.complete_solar_calculation(device_id=entry.device.id, now=now)
        _LOGGER.info(
            "Peak Guard [SCHEMA]: '%s' overgenomen van de solar-cascade", entry.device.name,
        )
        return snap

    # ------------------------------------------------------------------ #
    #  EV                                                                  #
    # ------------------------------------------------------------------ #

    async def _tick_ev(
        self, entry: ScheduleEntry, res: ActiveResult,
        consumption: Optional[float], now: datetime,
    ) -> bool:
        dev = entry.device
        rs = self._rs(entry)
        changed = False

        if res.active and not rs.active:
            rs.active = True
            rs.started_at = now
            rs.fulfilled = False
            rs.started_by_schedule = False
            rs.stop_pending = False
            rs.rest_pending = False
            rs.unplanned_since = None
            rs.soc_target_sent = None
            rs.soc_sent_at = None
            rs.target_soc = None
            rs.start_attempts = 0
            rs.retry_after = None
            _LOGGER.info("Peak Guard [SCHEMA]: '%s' laadvenster gestart (%s)", dev.name, res.label)
            changed = True
        elif not res.active and rs.active:
            await self._ev_window_end(entry, rs, consumption, now)
            return True

        if not rs.active:
            return await self._ev_outside(entry, rs, consumption, now) or changed

        target = res.target_soc or dev.max_soc or 100
        if rs.target_soc is not None and target > rs.target_soc and rs.fulfilled:
            rs.fulfilled = False   # aansluitend venster met hoger doel
        if rs.target_soc != target or rs.label != res.label:
            changed = True
        rs.target_soc = target
        rs.label = res.label
        return await self._ev_in_window(entry, rs, target, consumption, now) or changed

    def _reached(self, entry: ScheduleEntry, rs: ScheduleRunState, target: int, now: datetime) -> bool:
        dev = entry.device
        battery = read_sensor(self.hass, dev.battery_entity)
        if battery is not None and battery >= target:
            return True
        # Tesla meldt 'complete' aan de limiet; enkel geloven als die limiet het
        # doel is en een eventuele nieuwe limiet al even verstuurd is.
        if not self.ev_guard.charge_complete(dev):
            return False
        if rs.soc_sent_at is not None and rs.soc_target_sent == target:
            return (now - rs.soc_sent_at).total_seconds() >= SCHEDULE_START_CONFIRM_S
        limit = read_sensor(self.hass, dev.soc_entity)
        return limit is not None and round(limit) == round(target)

    async def _ensure_soc(
        self, entry: ScheduleEntry, rs: ScheduleRunState, value: int, now: datetime, reason: str,
    ) -> bool:
        """Zet de laadlimiet op value als hij afwijkt; True als hij nu klopt of verstuurd is."""
        dev = entry.device
        if not dev.soc_entity:
            return True
        current = read_sensor(self.hass, dev.soc_entity)
        if current is None:
            return False   # auto slaapt; later opnieuw
        if round(current) == round(value):
            if rs.soc_target_sent != value:
                rs.soc_target_sent = value
                rs.soc_sent_at = None
            return True
        if (
            rs.soc_target_sent == value and rs.soc_sent_at is not None
            and (now - rs.soc_sent_at).total_seconds() < SCHEDULE_SOC_RESEND_S
        ):
            return True
        if await self.ev_guard.set_soc_limit(dev, value, reason):
            rs.soc_target_sent = value
            rs.soc_sent_at = now
            return True
        return False

    async def _ev_in_window(
        self, entry: ScheduleEntry, rs: ScheduleRunState, target: int,
        consumption: Optional[float], now: datetime,
    ) -> bool:
        dev = entry.device
        ev = self.ev_guard
        guard = ev.get_guard(dev.id)
        changed = False

        if not rs.fulfilled and self._reached(entry, rs, target, now):
            rs.fulfilled = True
            changed = True
            if guard.scheduled:
                ev.release_schedule(dev)
            _LOGGER.info(
                "Peak Guard [SCHEMA]: '%s' doel %d%% bereikt — vrijgegeven voor zonne-overschot",
                dev.name, target,
            )
        if rs.fulfilled:
            rs.status = f"doel {target}% bereikt"
            return changed

        # Vanaf hier beheert het schema de EV.
        if self._takeover_inject_snapshot(entry, now) is not None:
            guard.soc_override_active = False
            changed = True

        if not ev.cable_connected(dev):
            rs.status = "laadkabel niet aangesloten"
            return changed
        if not ev.is_home(dev, guard):
            rs.status = "EV niet thuis"
            return changed

        charging = ev.is_charging(dev)
        voltage = ev.voltage(dev)
        hw_min = ev.hw_min_amps(dev)
        max_a = float(entry.max_current or dev.max_value or DEFAULT_EV_MAX_AMPERE)
        allowance = self._allowance_w(consumption, ev.charging_draw_w(dev))
        amps_allowed = (
            None if allowance is None
            else int(min(max_a, math.floor(allowance / voltage)))
        )

        snap = self._peak_snapshots.get(dev.entity_id)
        if snap is not None:
            if snap.original_state != "on":
                # Snapshot van een EV die niet laadde: niets te herstellen.
                self._peak_snapshots.pop(dev.entity_id, None)
                changed = True
            elif amps_allowed is not None and amps_allowed >= hw_min:
                self._peak_snapshots.pop(dev.entity_id, None)
                self.peak_tracker.start_measurement_on_turnon(
                    device_id=dev.id, device_name=dev.name, ts=now,
                )
                _LOGGER.info(
                    "Peak Guard [SCHEMA]: '%s' piekbeperking opgeheven — weer ruimte voor %d A",
                    dev.name, amps_allowed,
                )
                changed = True
            else:
                rs.status = "uitgesteld door piekbeperking"
                return changed

        if charging:
            await self._ensure_soc(entry, rs, target, now, "doel laadvenster")
            rs.start_attempts = 0
            rs.retry_after = None
            if not rs.started_by_schedule:
                rs.started_by_schedule = True
                changed = True
            guard.scheduled = True
            guard.state = EVState.CHARGING
            guard.last_switch_state = True
            current = ev.read_current_a(dev)
            if current is None:
                current = guard.last_sent_amps
            if amps_allowed is not None and current is not None:
                if current > max_a + 0.5:
                    await ev.schedule_set_current(dev, int(max_a), now)
                elif amps_allowed >= current + SCHEDULE_INCREASE_MIN_A and (
                    guard.last_current_update is None
                    or (now - guard.last_current_update).total_seconds() >= SCHEDULE_INCREASE_INTERVAL_S
                ):
                    await ev.schedule_set_current(dev, amps_allowed, now)
            shown = guard.last_sent_amps if guard.last_sent_amps is not None else current
            rs.status = f"laden{f' {shown:.0f} A' if shown is not None else ''} tot {target}%"
            return changed

        if amps_allowed is None:
            rs.status = "wacht op verbruik- of piek-sensor"
            return changed
        if amps_allowed < hw_min:
            rs.status = "wacht op piekruimte"
            return changed

        if rs.retry_after is not None and now < rs.retry_after:
            rs.status = "laden kwam niet op gang — nieuwe poging later"
            return changed
        await self._ensure_soc(entry, rs, target, now, "doel laadvenster")
        skip = await ev.schedule_start(dev, max(int(hw_min), amps_allowed), now)
        if skip is None:
            rs.started_by_schedule = True
            rs.start_attempts += 1
            rs.status = f"laden gestart tot {target}%"
            if rs.start_attempts >= SCHEDULE_MAX_START_ATTEMPTS:
                # De EV aanvaardt turn_on maar laadt niet (bv. laadpoort-
                # probleem): niet elke paar minuten API-calls blijven sturen.
                rs.start_attempts = 0
                rs.retry_after = now + timedelta(seconds=SCHEDULE_START_BACKOFF_S)
                ev._warn(
                    "Peak Guard [SCHEMA]: '%s' begint niet te laden na %d pogingen — "
                    "volgende poging over %.0f min",
                    dev.name, SCHEDULE_MAX_START_ATTEMPTS, SCHEDULE_START_BACKOFF_S / 60,
                )
            changed = True
        else:
            rs.status = skip
        return changed

    def _handover_to_solar(self, entry: ScheduleEntry, now: datetime, draw_w: float) -> None:
        """Lopende lading overdragen aan de solar-cascade (die stopt hem zodra het overschot weg is)."""
        dev = entry.device
        guard = self.ev_guard.get_guard(dev.id)
        self._inject_snapshots[dev.entity_id] = DeviceSnapshot(
            entity_id=dev.entity_id,
            original_state="off",
            original_current=None,
            original_soc=read_sensor(self.hass, dev.soc_entity),
        )
        self.solar_tracker.start_solar_measurement(
            device_id=dev.id, device_name=dev.name, nominal_kw=draw_w / 1000.0, ts=now,
        )
        guard.scheduled = False
        guard.state = EVState.CHARGING
        guard.last_switch_state = True
        guard.turned_on_at = now
        guard.soc_override_active = False
        self.ev_guard._reset_debounce(guard)
        _LOGGER.info(
            "Peak Guard [SCHEMA]: '%s' lading overgedragen aan zonne-overschot (%.0f W)",
            dev.name, draw_w,
        )

    def _surplus_without(self, consumption: Optional[float], draw_w: float) -> bool:
        return consumption is not None and draw_w > 0 and consumption - draw_w < 0

    async def _ev_window_end(
        self, entry: ScheduleEntry, rs: ScheduleRunState,
        consumption: Optional[float], now: datetime,
    ) -> None:
        dev = entry.device
        ev = self.ev_guard
        rs.active = False
        rs.fulfilled = False
        rs.label = ""
        rs.target_soc = None
        rs.unplanned_since = None
        self._drop_peak_snapshot(entry, now)

        if dev.entity_id in self._inject_snapshots:
            # Solar laadt hem al; die herstelt bij het einde naar de rust-laadlimiet.
            guard = ev.get_guard(dev.id)
            guard.scheduled = False
            rs.status = "laden op zonne-overschot"
        elif ev.is_charging(dev):
            draw = ev.charging_draw_w(dev)
            if self._surplus_without(consumption, draw) and self._in_inject_cascade(dev.entity_id):
                self._handover_to_solar(entry, now, draw)
                rs.status = "overgedragen aan zonne-overschot"
            elif await ev.schedule_stop(dev, now, "einde laadvenster"):
                rs.status = "gestopt bij einde venster"
            else:
                rs.stop_pending = True
                rs.status = "stoppen mislukt — volgende iteratie opnieuw"
        else:
            ev.release_schedule(dev)
            rs.status = "buiten venster"
        rs.rest_pending = entry.rest_soc is not None
        rs.started_by_schedule = False
        _LOGGER.info("Peak Guard [SCHEMA]: '%s' laadvenster beëindigd — %s", dev.name, rs.status)
        if rs.rest_pending and dev.entity_id not in self._inject_snapshots:
            if await self._ensure_soc(entry, rs, entry.rest_soc, now, "rust-laadlimiet"):
                rs.rest_pending = False

    async def _ev_outside(
        self, entry: ScheduleEntry, rs: ScheduleRunState,
        consumption: Optional[float], now: datetime,
    ) -> bool:
        dev = entry.device
        ev = self.ev_guard
        eid = dev.entity_id
        changed = False

        if rs.rest_pending:
            if entry.rest_soc is None or eid in self._inject_snapshots:
                rs.rest_pending = False
                changed = True
            elif await self._ensure_soc(entry, rs, entry.rest_soc, now, "rust-laadlimiet"):
                rs.rest_pending = False
                changed = True

        if not entry.enabled:
            rs.status = "uitgeschakeld"
            return changed

        charging = ev.is_charging(dev)
        solar_owned = eid in self._inject_snapshots
        blocks = (entry.block_unplanned or rs.stop_pending) and not solar_owned

        if not charging:
            rs.unplanned_since = None
            rs.stop_pending = False
            if blocks and eid in self._peak_snapshots:
                # Herstel zou een ongeplande lading herstarten.
                changed |= self._drop_peak_snapshot(entry, now)
            rs.status = "buiten venster"
            return changed
        if solar_owned:
            rs.unplanned_since = None
            rs.status = "laden op zonne-overschot"
            return changed
        if not blocks:
            rs.status = "laden (niet gepland)"
            return changed
        if self._manual_override(eid):
            rs.unplanned_since = None
            rs.status = "handmatige bediening — niet gestopt"
            return changed

        draw = ev.charging_draw_w(dev)
        if self._surplus_without(consumption, draw) and self._in_inject_cascade(eid):
            self._handover_to_solar(entry, now, draw)
            rs.unplanned_since = None
            rs.stop_pending = False
            rs.status = "laden op zonne-overschot"
            return True
        if not rs.stop_pending:
            if consumption is None:
                return changed
            if rs.unplanned_since is None:
                rs.unplanned_since = now
                rs.status = "ongeplande lading — wordt gestopt"
                return True
            if (now - rs.unplanned_since).total_seconds() < SCHEDULE_UNPLANNED_GRACE_S:
                return changed

        self._drop_peak_snapshot(entry, now)
        reason = "einde laadvenster" if rs.stop_pending else "ongeplande lading buiten laadvenster"
        if await ev.schedule_stop(dev, now, reason):
            if not rs.stop_pending:
                ev._warn(
                    "Peak Guard [SCHEMA]: '%s' ongeplande lading gestopt (buiten laadvenster, "
                    "geen zonne-overschot). Zet handmatige bediening aan om toch te laden.",
                    dev.name,
                )
            rs.stop_pending = False
            rs.unplanned_since = None
            rs.status = "ongeplande lading gestopt"
        return True

    # ------------------------------------------------------------------ #
    #  Schakelaar                                                          #
    # ------------------------------------------------------------------ #

    async def _switch_service(self, entry: ScheduleEntry, service: str) -> bool:
        dev = entry.device
        try:
            await self.hass.services.async_call(
                "switch", service, {"entity_id": dev.entity_id}, blocking=True,
            )
        except HomeAssistantError as err:
            self.ev_guard._warn(
                "Peak Guard [SCHEMA]: '%s' %s mislukt: %s", dev.name, service, err,
            )
            return False
        self._track_action(dev.entity_id, f"switch.{service}")
        _LOGGER.info("Peak Guard [SCHEMA]: '%s' %s", dev.name, service)
        return True

    async def _tick_switch(
        self, entry: ScheduleEntry, res: ActiveResult,
        consumption: Optional[float], now: datetime,
        rs: Optional[ScheduleRunState] = None,
    ) -> bool:
        dev = entry.device
        rs = rs if rs is not None else self._rs(entry)
        changed = False
        state = self.hass.states.get(dev.entity_id)

        if res.active and not rs.active:
            snap = self._takeover_inject_snapshot(entry, now)
            rs.active = True
            rs.started_at = now
            rs.switch_prev_state = (
                snap.original_state if snap is not None
                else (state.state if state is not None else None)
            )
            _LOGGER.info("Peak Guard [SCHEMA]: '%s' venster gestart (%s)", dev.name, res.label)
            changed = True
        elif not res.active and rs.active:
            rs.active = False
            rs.label = ""
            if rs.switch_prev_state != "on":
                # Stond hij vóór het venster aan, dan laat de piek-cascade hem
                # straks gewoon terug aan; anders niets te herstellen.
                self._drop_peak_snapshot(entry, now)
            if (
                rs.switch_prev_state == "off" and state is not None and state.state == "on"
                and not self._manual_override(dev.entity_id)
            ):
                await self._switch_service(entry, "turn_off")
            rs.switch_prev_state = None
            rs.status = "buiten venster"
            _LOGGER.info("Peak Guard [SCHEMA]: '%s' venster beëindigd", dev.name)
            return True

        if not rs.active:
            rs.status = "buiten venster"
            return changed

        rs.label = res.label
        if self._takeover_inject_snapshot(entry, now) is not None:
            changed = True
        if dev.entity_id in self._peak_snapshots:
            rs.status = "uitgesteld door piekbeperking"
            return changed
        if state is None:
            rs.status = "entity niet gevonden"
            return changed
        if state.state == "on":
            rs.status = "aan"
            return changed
        if self._manual_override(dev.entity_id):
            rs.status = "handmatige bediening"
            return changed
        allowance = self._allowance_w(consumption, 0.0)
        if allowance is None:
            rs.status = "wacht op verbruik- of piek-sensor"
            return changed
        if dev.power_watts and allowance < dev.power_watts:
            rs.status = "wacht op piekruimte"
            return changed
        if await self._switch_service(entry, "turn_on"):
            rs.status = "aan"
            changed = True
        return changed
