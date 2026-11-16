"""
Peak Guard — deciders/base.py

Abstracte BaseDecider met gedeelde helpers:
  - _sensor_value            : float lezen uit een HA state
  - _track_action            : actie registreren voor de beslissingslog
  - _get_restore_candidates  : geef te herstellen apparaten gesorteerd op prioriteit
  - _run_cascade             : voer een cascade uit (piek of solar)
  - _restore_device          : herstel één apparaat naar originele staat
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable, Dict, List, Optional

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .. import const as _const
from ..const import (
    CONF_DEBUG_DECISION_LOGGING,
)
from ..models import (
    CascadeContext,
    BaseCascadeDevice,
    DeviceSnapshot,
    age_s,
)

if TYPE_CHECKING:
    from ..avoided_peak_tracker import PeakAvoidTracker, SolarShiftTracker
    from .ev_guard import EVGuard

_LOGGER = logging.getLogger(__name__)


def track_action(
    config: dict,
    iteration_actions: list,
    entity_id: str,
    action: str,
    value=None,
) -> None:
    if not config.get(CONF_DEBUG_DECISION_LOGGING, False):
        return
    entry: dict = {"entity_id": entity_id, "action": action}
    if value is not None:
        entry["value"] = value
    iteration_actions.append(entry)


def read_sensor(hass: HomeAssistant, entity_id: Optional[str]) -> Optional[float]:
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None or state.state in ("unknown", "unavailable", ""):
        return None
    try:
        value = float(state.state)
    except (ValueError, TypeError):
        return None
    # "nan"/"inf" zijn geldige floats maar geen meting.
    return value if math.isfinite(value) else None


def read_power_w(hass: HomeAssistant, entity_id: Optional[str]) -> Optional[float]:
    """
    Lees een vermogenssensor (verbruik of maandpiek) uit in W.

    Peak Guard rekent in W. Rapporteert de sensor in kW (bv. de maandpiek van
    de DSMR-integratie), dan wordt de waarde omgerekend; elke andere of
    ontbrekende eenheid wordt als W gelezen.
    """
    value = read_sensor(hass, entity_id)
    if value is None:
        return None
    attributes = getattr(hass.states.get(entity_id), "attributes", None) or {}
    unit = str(attributes.get("unit_of_measurement") or "").strip().lower()
    return value * 1000.0 if unit == "kw" else value


def read_energy_kwh(hass: HomeAssistant, entity_id: Optional[str]) -> Optional[float]:
    """
    Lees een cumulatieve energiesensor uit in kWh.

    De kwartierberekening rekent in kWh. Een sensor in Wh of MWh wordt
    omgerekend; elke andere of ontbrekende eenheid wordt als kWh gelezen.
    """
    value = read_sensor(hass, entity_id)
    if value is None:
        return None
    attributes = getattr(hass.states.get(entity_id), "attributes", None) or {}
    unit = str(attributes.get("unit_of_measurement") or "").strip()
    if unit.lower() == "wh":
        return value / 1000.0
    if unit == "MWh":
        return value * 1000.0
    return value


def restore_need_w(device: BaseCascadeDevice, snapshot: DeviceSnapshot) -> float:
    """Vermogen dat een herstel terugbrengt (het vermogen bij het afschakelen)."""
    fn = getattr(device, "restore_need_w", None)
    if callable(fn):
        return fn(snapshot)
    return float(getattr(device, "power_watts", 0) or 0)


class BaseDecider:
    def __init__(
        self,
        hass: HomeAssistant,
        config: dict,
        peak_tracker: "PeakAvoidTracker",
        solar_tracker: "SolarShiftTracker",
        ev_guard: "EVGuard",
        iteration_actions: list,
        save_fn: Callable,
    ) -> None:
        self.hass = hass
        self.config = config
        self.peak_tracker = peak_tracker
        self.solar_tracker = solar_tracker
        self.ev_guard = ev_guard
        self._iteration_actions = iteration_actions
        self._save_fn = save_fn
        # Bevestigingen worden één keer per cyclus verwerkt.
        self._acks_polled_at: Optional[datetime] = None
        # Herstel dat in deze sessie al opnieuw verstuurd werd (opgeven pas daarna).
        self._resent_restore: set = set()

    # ------------------------------------------------------------------ #
    #  Hulpfuncties                                                        #
    # ------------------------------------------------------------------ #

    def _sensor_value(self, entity_id: Optional[str]) -> Optional[float]:
        return read_sensor(self.hass, entity_id)

    def _track_action(self, entity_id: str, action: str, value=None) -> None:
        track_action(self.config, self._iteration_actions, entity_id, action, value)

    def _make_ctx(self, cascade_type: str, now: datetime) -> CascadeContext:
        return CascadeContext(
            hass=self.hass,
            cascade_type=cascade_type,
            peak_tracker=self.peak_tracker,
            solar_tracker=self.solar_tracker,
            ev_guard=self.ev_guard,
            track_action=self._track_action,
            warn=self.ev_guard._warn,
            now=now,
            inject_snapshots=(
                getattr(self, "_inject_snapshots", None) if cascade_type == "peak" else None
            ),
        )

    # ------------------------------------------------------------------ #
    #  Herstel-kandidaten                                                  #
    # ------------------------------------------------------------------ #

    def _get_restore_candidates(
        self,
        cascade: List[BaseCascadeDevice],
        snapshots: Dict[str, DeviceSnapshot],
        reverse: bool = True,
        skip_fn: Optional[Callable[[BaseCascadeDevice], bool]] = None,
        on_drop: Optional[Callable[[BaseCascadeDevice], None]] = None,
    ) -> List[tuple]:
        candidates = []
        for device in cascade:
            if skip_fn is not None and skip_fn(device):
                # Laadschema beheert dit apparaat nu; snapshot blijft liggen
                # zodat het schema hem kan overnemen of opruimen.
                continue
            if device.entity_id in snapshots:
                if device.manual_override:
                    del snapshots[device.entity_id]
                    if on_drop is not None:
                        on_drop(device)
                    _LOGGER.info(
                        "Peak Guard: snapshot voor '%s' verwijderd — manuele bediening actief",
                        device.name,
                    )
                else:
                    candidates.append((device, snapshots[device.entity_id]))
        candidates.sort(key=lambda x: x[0].priority, reverse=reverse)
        return candidates

    # ------------------------------------------------------------------ #
    #  Cascade uitvoering                                                  #
    # ------------------------------------------------------------------ #

    async def _run_cascade(
        self,
        cascade: List[BaseCascadeDevice],
        excess: float,
        snapshots: Dict[str, DeviceSnapshot],
        cascade_type: str = "peak",
        now: Optional[datetime] = None,
        skip_fn: Optional[Callable[[BaseCascadeDevice], bool]] = None,
    ) -> float:
        """Doorloop de cascade; geeft het onverwerkte overschot (W) terug."""
        if now is None:
            now = datetime.now(timezone.utc)
        sorted_devices = sorted(
            [
                d for d in cascade
                if d.enabled and not d.manual_override
                and not (skip_fn is not None and skip_fn(d))
            ],
            key=lambda x: x.priority,
        )
        label = "PIEK" if cascade_type == "peak" else "SOLAR"
        _LOGGER.info(
            "Peak Guard [%s cascade]: start — overschot=%.0f W, %d apparaat/apparaten "
            "(prioriteitsvolgorde: %s)",
            label, excess, len(sorted_devices),
            ", ".join(f"'{d.name}'[{d.action_type}]" for d in sorted_devices) or "–",
        )

        ctx = self._make_ctx(cascade_type, now)
        remaining = excess

        for device in sorted_devices:
            if remaining <= 0:
                _LOGGER.info(
                    "Peak Guard [%s cascade]: overschot opgelost (0 W resterend) — "
                    "verdere apparaten niet verwerkt",
                    label,
                )
                break
            before = remaining
            ctx.last_skip_reason = ""
            try:
                remaining = await device.apply(remaining, snapshots, ctx)
            except Exception:  # noqa: BLE001 — één apparaat mag de cascade niet stoppen
                self.ev_guard.report_device_error(
                    f"apply:{label}:{device.id}",
                    f"Peak Guard [{label} cascade]: onverwachte fout bij '{device.name}' "
                    "— overgeslagen, volgende apparaat",
                    f"Onverwachte fout bij '{device.name}' — overgeslagen (zie HA-log)",
                )
                remaining = before
            handled = before - remaining
            if handled > 0:
                _LOGGER.info(
                    "Peak Guard [%s cascade]:   ✓ '%s' — %.0f W verwerkt, resterend: %.0f W",
                    label, device.name, handled, remaining,
                )
            elif handled < 0:
                _LOGGER.info(
                    "Peak Guard [%s cascade]:   ✓ '%s' — gestart (%.0f W > surplus), "
                    "resterend: %.0f W",
                    label, device.name, abs(handled), remaining,
                )
            else:
                _LOGGER.info(
                    "Peak Guard [%s cascade]:   · '%s' — geen actie: %s",
                    label, device.name, ctx.last_skip_reason or "zie detail-logs",
                )

        if remaining > 0:
            self.ev_guard._warn(
                "Peak Guard [%s cascade]: klaar — nog %.0f W overschot onverwerkt "
                "(alle apparaten doorlopen)",
                label, remaining,
            )
        else:
            _LOGGER.info(
                "Peak Guard [%s cascade]: klaar — overschot volledig verwerkt ✓", label)

        await self._save_fn()
        return remaining

    # ------------------------------------------------------------------ #
    #  Bevestiging (ack)                                                   #
    # ------------------------------------------------------------------ #

    def _on_apply_acked(self, device: BaseCascadeDevice, now: datetime) -> None:
        """Hook: een eerder verstuurde ingreep is (laat) bevestigd."""

    def _on_restore_acked(self, device: BaseCascadeDevice, now: datetime) -> None:
        """Hook: een herstel is bevestigd en het snapshot verwijderd."""

    def _on_restore_given_up(self, device: BaseCascadeDevice) -> None:
        """Hook: een herstel bleef ACK_GIVE_UP_S onbevestigd; snapshot verwijderd."""

    def _on_snapshot_dropped(self, device: BaseCascadeDevice) -> None:
        """Hook: snapshot weg zonder bevestigd herstel (manuele bediening, opgegeven ingreep)."""

    async def _poll_acks(
        self,
        cascade: List[BaseCascadeDevice],
        snapshots: Dict[str, DeviceSnapshot],
        cascade_type: str,
        now: datetime,
    ) -> None:
        """
        Verwerk openstaande bevestigingen (één keer per cyclus):

        - ingreep ('apply'/'noack') nu uitgevoerd → boeken (on_apply_ack);
          'apply' na ACK_TIMEOUT_S nog niet → 'noack' (telt niet meer mee).
          Een 'noack' ouder dan ACK_GIVE_UP_S vervalt zonder boeking: een
          latere uit-toestand (wasbeurt gedaan, manueel uit) hoort er niet bij.
          Een 'apply' die zo oud is (bewaard over een herstart) en nu
          uitgevoerd blijkt, telt als afgeschakeld maar boekt niets.
        - herstel ('restore') uitgevoerd → snapshot weg; na ACK_RESEND_S
          opnieuw versturen; opgeven pas na ACK_GIVE_UP_S én minstens één
          herhaling in deze sessie (na een herstart dus eerst opnieuw proberen).
        - manuele bediening of uitgeschakeld in de cascade: snapshot weg.
        """
        if self._acks_polled_at == now:
            return
        self._acks_polled_at = now
        changed = False
        ctx = self._make_ctx(cascade_type, now)
        for device in cascade:
            snap = snapshots.get(device.entity_id)
            pending = getattr(snap, "pending", None) if snap is not None else None
            if pending is None or not getattr(device, "supports_ack", False):
                continue
            # Manuele bediening: alles vervalt. Uitgeschakeld in de cascade:
            # enkel een open ingreep; een lopend herstel wordt afgewerkt.
            if device.manual_override or (not device.enabled and pending != "restore"):
                del snapshots[device.entity_id]
                self._on_snapshot_dropped(device)
                _LOGGER.info(
                    "Peak Guard: '%s' — open actie vervalt (manuele bediening of uitgeschakeld)",
                    device.name,
                )
                changed = True
                continue
            state = self.hass.states.get(device.entity_id)
            too_old = age_s(snap.pending_since, now) >= _const.ACK_GIVE_UP_S
            if pending in ("apply", "noack"):
                applied = state is not None and device.applied(state, snap, cascade_type)
                if pending == "noack" and too_old:
                    del snapshots[device.entity_id]
                    self._on_snapshot_dropped(device)
                    self.ev_guard._warn(
                        "Peak Guard: '%s' voerde de ingreep na %.0f min nog altijd niet uit — "
                        "opgegeven, niets geboekt",
                        device.name, _const.ACK_GIVE_UP_S / 60,
                    )
                    changed = True
                elif applied:
                    _LOGGER.info(
                        "Peak Guard: '%s' — ingreep bevestigd door het apparaat (%.0f s na het commando)",
                        device.name, age_s(snap.cmd_at, now),
                    )
                    device.on_apply_ack(snap, ctx, now, book=not too_old)
                    self._on_apply_acked(device, now)
                    changed = True
                elif too_old:
                    # 'apply' over een herstart heen en niet uitgevoerd: vervalt.
                    del snapshots[device.entity_id]
                    self._on_snapshot_dropped(device)
                    changed = True
                elif pending == "apply" and age_s(snap.cmd_at, now) >= _const.ACK_TIMEOUT_S:
                    snap.pending = "noack"
                    self.ev_guard._warn(
                        "Peak Guard: '%s' bevestigde het commando niet binnen %.0f s — telt niet "
                        "meer mee, nieuwe poging na %.0f s",
                        device.name, _const.ACK_TIMEOUT_S, _const.ACK_RESEND_S,
                    )
                    changed = True
            elif pending == "restore":
                if state is not None and device.restored(state, snap, cascade_type):
                    _LOGGER.info(
                        "Peak Guard: '%s' — herstel bevestigd door het apparaat (%.0f s na het commando)",
                        device.name, age_s(snap.cmd_at, now),
                    )
                    device.on_restore_ack(snap, ctx, now)
                    del snapshots[device.entity_id]
                    self._resent_restore.discard(device.entity_id)
                    self._on_restore_acked(device, now)
                    changed = True
                elif age_s(snap.cmd_at, now) >= _const.ACK_RESEND_S:
                    if too_old and device.entity_id in self._resent_restore:
                        del snapshots[device.entity_id]
                        self._resent_restore.discard(device.entity_id)
                        self.ev_guard._warn(
                            "Peak Guard: '%s' — herstel na %.0f min nog altijd niet bevestigd; "
                            "Peak Guard geeft het op (controleer het apparaat)",
                            device.name, age_s(snap.pending_since, now) / 60,
                        )
                        self._on_restore_given_up(device)
                    else:
                        _LOGGER.info("Peak Guard: '%s' — herstel opnieuw versturen", device.name)
                        self._resent_restore.add(device.entity_id)
                        if await self._restore_device(device, snap, cascade_type, now):
                            snapshots.pop(device.entity_id, None)
                            self._resent_restore.discard(device.entity_id)
                            if snap.pending == "done":
                                self._on_restore_acked(device, now)
                    changed = True
                else:
                    self.ev_guard.set_hold(
                        device.id, "awaiting_ack",
                        f"Herstel verstuurd {age_s(snap.cmd_at, now):.0f} s geleden; wacht tot "
                        f"het apparaat de oorspronkelijke toestand meldt.",
                    )
        if changed:
            await self._save_fn()

    # ------------------------------------------------------------------ #
    #  Apparaat herstel                                                    #
    # ------------------------------------------------------------------ #

    async def _restore_device(
        self,
        device: BaseCascadeDevice,
        snapshot: DeviceSnapshot,
        cascade_type: str = "peak",
        now: Optional[datetime] = None,
    ) -> bool:
        if now is None:
            now = datetime.now(timezone.utc)
        ctx = self._make_ctx(cascade_type, now)
        try:
            return await device.restore(snapshot, ctx)
        except Exception:  # noqa: BLE001 — één apparaat mag het herstel niet stoppen
            self.ev_guard.report_device_error(
                f"restore:{device.id}",
                f"Peak Guard: onverwachte fout bij herstel van '{device.name}' "
                "— volgende cyclus opnieuw",
                f"Onverwachte fout bij herstel van '{device.name}' (zie HA-log)",
            )
            return False
