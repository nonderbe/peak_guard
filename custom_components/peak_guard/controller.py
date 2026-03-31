import asyncio
import logging
import math
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Dict, List, Optional

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .avoided_peak_tracker import PeakAvoidTracker, SolarShiftTracker

from .const import (
    DOMAIN,
    STORAGE_KEY,
    STORAGE_VERSION,
    CONF_CONSUMPTION_SENSOR,
    CONF_PEAK_SENSOR,
    CONF_BUFFER_WATTS,
    CONF_UPDATE_INTERVAL,
    CONF_POWER_DETECTION_TOLERANCE_PERCENT,
    DEFAULT_BUFFER_WATTS,
    DEFAULT_UPDATE_INTERVAL,
    DEFAULT_POWER_DETECTION_TOLERANCE_PERCENT,
    DEFAULT_EV_MIN_AMPERE,
    DEFAULT_EV_MAX_AMPERE,
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
    entity_id:          str       # primaire entity (switch voor ev_charger, idem voor switch_on/off)
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
    ev_soc_entity:      Optional[str] = None   # number-entity voor SOC-limiet
    ev_battery_entity:  Optional[str] = None   # sensor-entity voor huidig batterijniveau
    ev_max_soc:         Optional[int] = None
    ev_phases:          int = 1       # aantal fasen: 1 (default) of 3

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DeviceSnapshot:
    """Oorspronkelijke staat van een apparaat voor een Peak Guard ingreep."""
    entity_id: str
    original_state: str
    # Extra veld voor EV: de laadstroom voor de ingreep
    original_current: Optional[float] = None
    # Extra veld voor EV: de SOC-limiet voor de ingreep
    original_soc: Optional[float] = None


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

    # ------------------------------------------------------------------ #
    #  Opslaan en laden                                                    #
    # ------------------------------------------------------------------ #

    async def async_load(self):
        data = await self._store.async_load()
        if data:
            self.peak_cascade   = [CascadeDevice(**d) for d in data.get("peak", [])]
            self.inject_cascade = [CascadeDevice(**d) for d in data.get("inject", [])]

    async def async_save(self):
        await self._store.async_save({
            "peak":   [d.to_dict() for d in self.peak_cascade],
            "inject": [d.to_dict() for d in self.inject_cascade],
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
                "monitoring": self._monitoring,
            },
        }

    def update_cascade(self, cascade_type: str, devices: list):
        parsed = [CascadeDevice(**d) for d in devices]
        if cascade_type == "peak":
            self.peak_cascade = parsed
        elif cascade_type == "inject":
            self.inject_cascade = parsed

    # ------------------------------------------------------------------ #
    #  Monitoring loop                                                     #
    # ------------------------------------------------------------------ #

    async def start_monitoring(self):
        self._monitoring = True
        self._task = self.hass.loop.create_task(self._monitor_loop())
        _LOGGER.info("Peak Guard: monitoring gestart")

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
        interval = float(self.config.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL))
        while self._monitoring:
            try:
                consumption = self._sensor_value(self.config.get(CONF_CONSUMPTION_SENSOR))
                if consumption is not None:
                    await self._check_power_drop(consumption)
                    if consumption > 0:
                        await self._check_peak(consumption)
                        await self._check_peak_restore(consumption)   # ook bij positief verbruik!
                        await self._check_inject_restore(consumption)
                    elif consumption < 0:
                        await self._check_injection(consumption)
                        await self._check_peak_restore(consumption)
                        await self._check_inject_restore(consumption)
                    else:
                        await self._check_peak_restore(0.0)
                        await self._check_inject_restore(0.0)
                    self._prev_consumption = consumption
                else:
                    self._prev_consumption = None
            except Exception:
                _LOGGER.exception("Peak Guard: fout in monitoring loop")
            await asyncio.sleep(interval)

    # ------------------------------------------------------------------ #
    #  Cascade logica — ingreep                                            #
    # ------------------------------------------------------------------ #

    async def _check_peak(self, consumption: float):
        peak = self._sensor_value(self.config.get(CONF_PEAK_SENSOR))
        if peak is None:
            _LOGGER.debug("Peak Guard: piek-sensor niet beschikbaar, check overgeslagen")
            return
        buffer = float(self.config.get(CONF_BUFFER_WATTS, DEFAULT_BUFFER_WATTS))
        excess = consumption - peak + buffer
        _LOGGER.debug(
            "Peak Guard _check_peak: verbruik=%.0f W, piek=%.0f W, buffer=%.0f W, overschot=%.0f W",
            consumption, peak, buffer, excess,
        )
        if excess > 0:
            _LOGGER.warning(
                "Peak Guard: piekverbruik overschreden met %.0f W – cascade gestart", excess
            )
            await self._run_cascade(self.peak_cascade, excess, self._peak_snapshots, "peak")

    async def _check_injection(self, consumption: float):
        injection = abs(consumption)
        buffer = float(self.config.get(CONF_BUFFER_WATTS, DEFAULT_BUFFER_WATTS))
        _LOGGER.debug(
            "Peak Guard _check_injection: injectie=%.0f W, buffer=%.0f W, actief=%d snapshot(s)",
            injection, buffer, len(self._inject_snapshots),
        )
        if injection > buffer:
            _LOGGER.info(
                "Peak Guard: stroominjectie van %.0f W gedetecteerd – cascade gestart", injection
            )
            await self._run_cascade(self.inject_cascade, injection, self._inject_snapshots, "solar")

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
        # Herstel pas als het huidig verbruik NIET meer boven de piekgrens zit.
        # Ingreep-conditie was: consumption > peak - buffer
        # Herstel-conditie is:  consumption <= peak - buffer
        if consumption > peak - buffer:
            _LOGGER.debug(
                "Peak Guard herstel geblokkeerd: verbruik %.0f W > piekgrens %.0f W "
                "(piek=%.0f W, buffer=%.0f W)",
                consumption, peak - buffer, peak, buffer,
            )
            return
        snapshots_to_restore = self._get_restore_candidates(
            self.peak_cascade, self._peak_snapshots, reverse=True
        )
        if not snapshots_to_restore:
            return
        device, snapshot = snapshots_to_restore[0]
        restored = await self._restore_device(device, snapshot)
        if restored:
            del self._peak_snapshots[device.entity_id]
            _LOGGER.info("Peak Guard: '%s' hersteld", device.name)

    async def _check_inject_restore(self, consumption: float):
        if not self._inject_snapshots:
            return
        _LOGGER.debug(
            "Peak Guard _check_inject_restore: verbruik=%.0f W, %d snapshot(s) actief",
            consumption, len(self._inject_snapshots),
        )
        if consumption < 0:
            _LOGGER.debug("Peak Guard: inject-herstel geblokkeerd — verbruik nog negatief (%.0f W)", consumption)
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
            _LOGGER.warning("Peak Guard: kan '%s' niet herstellen — entity niet gevonden", device.name)
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
                    # Apparaat al terug aan (bv. handmatig hersteld of al in meting):
                    # sluit eventuele lopende meting af als event
                    now_ts = datetime.now(timezone.utc)
                    event = self.peak_tracker.complete_peak_calculation(
                        device_id=device.id, now=now_ts
                    )
                    if event:
                        _LOGGER.info(
                            "Peak Guard: piek-event afgerond voor '%s' — "
                            "duur=%.1f min, vermeden=%.3f kW, besparing=€%.4f",
                            device.name, event.measured_duration_min,
                            event.avoided_peak_kw, event.savings_euro,
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
                    _LOGGER.info("Peak Guard: '%s' hersteld %s → %s", device.name, current, new_value)
                return True

            if device.action_type == ACTION_EV_CHARGER:
                return await self._restore_ev(device, snapshot)

        except (ValueError, TypeError) as err:
            _LOGGER.error("Peak Guard: fout bij herstellen '%s': %s", device.name, err)

        return False

    async def _restore_ev(self, device: CascadeDevice, snapshot: DeviceSnapshot) -> bool:
        """
        Herstel EV Charger na een Peak Guard ingreep.

        Voor PIEKBEPERKING (orig. state = "on"):
          - Zet schakelaar terug aan (was uitgeschakeld door peak cascade)
          - Herstel laadstroom naar originele waarde
          - Start duurmeting in peak_tracker (voor vermeden-piek berekening)

        Voor INJECTIEPREVENTIE (orig. state = "off"):
          - Verwijder SOC-override → auto laadt weer tot normaal max-SoC
          - Zet laadstroom terug naar originele waarde (of min_a)
          - Zet schakelaar uit (was aan gezet door solar cascade)
          - Voltooi duurmeting in solar_tracker (voor kWh-berekening)
        """
        try:
            sw_entity  = device.ev_switch_entity or device.entity_id
            cur_entity = device.ev_current_entity

            sw_state = self.hass.states.get(sw_entity)
            if sw_state is None:
                _LOGGER.warning("Peak Guard EV: schakelaar '%s' niet gevonden bij herstel", sw_entity)
                return False

            # ---- PIEKBEPERKING: schakelaar was aan, nu uitgeschakeld ----
            if snapshot.original_state == "on":
                if sw_state.state != "on":
                    # Zet terug aan
                    await self.hass.services.async_call(
                        "switch", "turn_on", {"entity_id": sw_entity}, blocking=True
                    )
                    _LOGGER.info("Peak Guard EV peak: '%s' terug ingeschakeld", device.name)

                # Herstel laadstroom naar originele waarde
                if cur_entity and snapshot.original_current is not None:
                    orig_a = snapshot.original_current
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
                                "Peak Guard EV peak: '%s' laadstroom hersteld naar %.1f A",
                                device.name, orig_a,
                            )
                # Trigger peak_tracker: meting starten (de EV is nu weer aan)
                self.peak_tracker.start_measurement_on_turnon(
                    device_id=device.id,
                    device_name=device.name,
                    ts=datetime.now(timezone.utc),
                )
                return True

            # ---- INJECTIEPREVENTIE: schakelaar was uit, nu aangezet ----
            if snapshot.original_state == "off":
                if sw_state.state != "off":
                    # 1. Verwijder SOC-override EERST (vóór uitschakelen)
                    await self._set_ev_soc_override(
                        device, override=False, original_soc=snapshot.original_soc
                    )

                    # 2. Herstel laadstroom naar originele waarde (of min_a)
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
                            "Peak Guard EV solar: '%s' laadstroom hersteld naar %.1f A",
                            device.name, restore_a,
                        )

                    # 3. Schakelaar uit
                    await self.hass.services.async_call(
                        "switch", "turn_off", {"entity_id": sw_entity}, blocking=True
                    )
                    _LOGGER.info("Peak Guard EV solar: '%s' schakelaar uitgeschakeld", device.name)

                    # 4. Voltooi solar-meting (berekent verschoven kWh)
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
                return True

        except (ValueError, TypeError) as err:
            _LOGGER.error("Peak Guard EV: fout bij herstellen '%s': %s", device.name, err)
            return False

        return False

    async def _set_ev_soc_override(self, device: CascadeDevice, override: bool,
                                    original_soc: Optional[float] = None) -> None:
        """
        Zet of verwijder een tijdelijke SOC-limiet via de geconfigureerde
        ev_soc_entity (number entity).

        override=True  → stel ev_max_soc in als tijdelijk maximaal laadpercentage
        override=False → herstel naar original_soc (waarde vóór de ingreep),
                         of naar 100 als die onbekend is
        """
        if device.ev_max_soc is None:
            return  # Geen SOC-override geconfigureerd

        soc_entity = device.ev_soc_entity
        if not soc_entity:
            # Geen entity geconfigureerd — enkel loggen
            _LOGGER.info(
                "Peak Guard EV: '%s' SOC-override %s (geen soc_entity geconfigureerd, "
                "geen service-call gedaan)",
                device.name, "ACTIEF" if override else "VERWIJDERD",
            )
            return

        if override:
            target_soc = float(device.ev_max_soc)
            _LOGGER.info(
                "Peak Guard EV: '%s' SOC-limiet ingesteld op %.0f%% via '%s'",
                device.name, target_soc, soc_entity,
            )
        else:
            target_soc = float(original_soc) if original_soc is not None else 100.0
            _LOGGER.info(
                "Peak Guard EV: '%s' SOC-limiet hersteld naar %.0f%% via '%s'",
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
        for device in sorted_devices:
            if excess <= 0:
                break
            excess = await self._apply_action(device, excess, snapshots, cascade_type)

    async def _apply_action(
        self,
        device: CascadeDevice,
        excess: float,
        snapshots: Dict[str, DeviceSnapshot],
        cascade_type: str = "peak",
    ) -> float:
        state = self.hass.states.get(device.entity_id)
        if state is None:
            _LOGGER.warning("Peak Guard: entity '%s' niet gevonden", device.entity_id)
            return excess

        # ---- Switch OFF (piekbeperking) -------------------------------- #
        if device.action_type == ACTION_SWITCH_OFF and state.state == "on":
            if device.entity_id not in snapshots:
                snapshots[device.entity_id] = DeviceSnapshot(
                    entity_id=device.entity_id,
                    original_state=state.state,
                )
            await self.hass.services.async_call(
                "switch", "turn_off", {"entity_id": device.entity_id}, blocking=True
            )
            _LOGGER.info(
                "Peak Guard: '%s' UITGESCHAKELD wegens piekbeperking (-%d W, overschot was %.0f W)",
                device.name, device.power_watts, excess,
            )
            self.peak_tracker.record_pending_avoid(
                device_id=device.id,
                device_name=device.name,
                nominal_kw=device.power_watts / 1000.0,
                ts=datetime.now(timezone.utc),
            )
            return excess - device.power_watts

        # ---- Switch ON (injectiepreventie) ----------------------------- #
        if device.action_type == ACTION_SWITCH_ON and state.state == "off":
            if device.entity_id not in snapshots:
                snapshots[device.entity_id] = DeviceSnapshot(
                    entity_id=device.entity_id,
                    original_state=state.state,
                )
            await self.hass.services.async_call(
                "switch", "turn_on", {"entity_id": device.entity_id}, blocking=True
            )
            _LOGGER.info(
                "Peak Guard: '%s' INGESCHAKELD wegens injectiepreventie (+%d W, overschot was %.0f W)",
                device.name, device.power_watts, excess,
            )
            self.solar_tracker.start_solar_measurement(
                device_id=device.id,
                device_name=device.name,
                nominal_kw=device.power_watts / 1000.0,
                ts=datetime.now(timezone.utc),
            )
            return excess - device.power_watts

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

    async def _apply_ev_action(
        self,
        device: CascadeDevice,
        excess: float,
        snapshots: Dict[str, DeviceSnapshot],
        cascade_type: str = "peak",
    ) -> float:
        """
        EV Charger cascade-ingreep.

        Vermogenformule: P = A × U
          1-fase: U = 230 V  →  W = A × 230
          3-fasen: U = 400 V  →  W = A × 400

        Piekbeperking  (cascade_type == "peak"):
          Laadstroom naar BENEDEN afronden (floor) zodat het verbruik
          maximaal onder de piekgrens blijft.

          Formule:
            needed_reduction_W  = excess  (te veel vermogen)
            current_W           = current_a × U  (U = 230 of 400 V)
            target_W            = current_W - needed_reduction_W
            target_a_raw        = target_W / U
            new_a               = floor(target_a_raw)         ← altijd naar beneden
            new_a               = clamp(new_a, min_a, max_a)

          Als new_a < min_a → schakelaar uit (niet verder te verlagen).

        Injectiepreventie (cascade_type == "solar"):
          Laadstroom naar BOVEN afronden (ceil) zodat zoveel mogelijk
          zonne-energie lokaal verbruikt wordt.

          Formule:
            available_a_raw     = excess / U
            new_a               = ceil(available_a_raw)       ← altijd naar boven
            new_a               = clamp(new_a, min_a, max_a)

          Als schakelaar uit is én ceil(excess / U) >= min_a → schakelaar aan.
          SOC-override: zet ev_max_soc tijdelijk als batterijlimiet (indien entity aanwezig).
        """
        min_a = device.min_value if device.min_value is not None else DEFAULT_EV_MIN_AMPERE
        max_a = device.max_value if device.max_value is not None else DEFAULT_EV_MAX_AMPERE
        phases = int(device.ev_phases) if device.ev_phases else 1

        # Spanning afhankelijk van het aantal fasen:
        # 1 fase → 230 V,  3 fasen → 400 V
        # Vermogen = A × voltage (niet A × fasen × 230)
        voltage = EV_VOLTS_3PHASE if phases == 3 else EV_VOLTS_1PHASE

        sw_entity  = device.ev_switch_entity or device.entity_id
        cur_entity = device.ev_current_entity

        sw_state = self.hass.states.get(sw_entity)
        if sw_state is None:
            _LOGGER.warning("Peak Guard EV: schakelaar '%s' niet gevonden", sw_entity)
            return excess

        sw_on = sw_state.state == "on"

        # Lees huidige laadstroom
        current_a: Optional[float] = None
        if cur_entity:
            cur_state = self.hass.states.get(cur_entity)
            if cur_state is not None:
                try:
                    current_a = float(cur_state.state)
                except (ValueError, TypeError):
                    current_a = None

        # Lees huidige SOC-limiet (voor herstel na ingreep)
        current_soc: Optional[float] = None
        if device.ev_soc_entity:
            soc_state = self.hass.states.get(device.ev_soc_entity)
            if soc_state is not None:
                try:
                    current_soc = float(soc_state.state)
                except (ValueError, TypeError):
                    current_soc = None

        # Snapshot bij eerste ingreep (bewaar originele staat + laadstroom + SOC)
        snap_key = device.entity_id
        if snap_key not in snapshots:
            snapshots[snap_key] = DeviceSnapshot(
                entity_id=snap_key,
                original_state=sw_state.state,
                original_current=current_a,
                original_soc=current_soc,
            )

        # ================================================================ #
        #  PIEKBEPERKING — floor, laadstroom verlagen                      #
        # ================================================================ #
        if cascade_type == "peak":
            if not sw_on:
                # EV laadt niet → niets te verlagen, excess onveranderd
                return excess

            # Huidige verbruik van de EV (W)
            eff_current_a = current_a if current_a is not None else max_a
            current_w = eff_current_a * voltage

            # Hoeveel watt moet de EV inleveren?
            needed_reduction_w = min(excess, current_w)

            # Nieuwe laadstroom na verlaging (FLOOR → nooit meer dan nodig)
            target_a_raw = (current_w - needed_reduction_w) / voltage
            # floor: altijd naar beneden afronden op hele ampère
            new_a = math.floor(target_a_raw)
            new_a = max(0, min(int(max_a), new_a))   # clamp op [0, max_a]

            if new_a < min_a:
                # Onder minimale laadstroom → schakelaar volledig uit
                await self.hass.services.async_call(
                    "switch", "turn_off", {"entity_id": sw_entity}, blocking=True
                )
                if cur_entity and current_a is not None:
                    # Reset laadstroom naar min_a zodat volgende sessie
                    # niet met 0 A start
                    await self.hass.services.async_call(
                        "number", "set_value",
                        {"entity_id": cur_entity, "value": min_a},
                        blocking=True,
                    )
                _LOGGER.info(
                    "Peak Guard EV peak: '%s' uitgeschakeld (%.1f A < min %.1f A). "
                    "Vermogensverlaging: %.0f W (%d fase(n))",
                    device.name, target_a_raw, min_a, current_w, phases,
                )
                # Registreer vermeden piek in tracker
                self.peak_tracker.record_pending_avoid(
                    device_id=device.id,
                    device_name=device.name,
                    nominal_kw=current_w / 1000.0,
                    ts=datetime.now(timezone.utc),
                )
                return excess - current_w

            else:
                # Laadstroom verlagen naar new_a
                actual_reduction_w = (eff_current_a - new_a) * voltage
                if cur_entity and new_a != int(eff_current_a):
                    await self.hass.services.async_call(
                        "number", "set_value",
                        {"entity_id": cur_entity, "value": float(new_a)},
                        blocking=True,
                    )
                    _LOGGER.info(
                        "Peak Guard EV peak: '%s' laadstroom %d → %d A "
                        "(floor van %.2f A, verlaging %.0f W, %d fase(n))",
                        device.name, int(eff_current_a), new_a,
                        target_a_raw, actual_reduction_w, phases,
                    )
                return excess - actual_reduction_w

        # ================================================================ #
        #  INJECTIEPREVENTIE — laadstroom instellen op beschikbaar overschot #
        # ================================================================ #
        # cascade_type == "solar"

        # Laadstroom: I = P / U  (U = 230 V voor 1-fase, 400 V voor 3-fasen)
        # Afgerond naar boven (ceil) conform gebruikersvraag.
        available_a_raw = excess / voltage
        new_a = max(int(min_a), min(int(max_a), math.ceil(available_a_raw)))

        # Drempel: is er genoeg overschot voor de minimale laadstroom?
        if math.ceil(available_a_raw) < min_a:
            _LOGGER.debug(
                "Peak Guard EV solar: '%s' onvoldoende overschot voor minimale laadstroom "
                "(overschot=%.0f W, beschikbaar=%.2f A, min=%.0f A)",
                device.name, excess, available_a_raw, min_a,
            )
            return excess

        if not sw_on:
            # EV staat uit → aanzetten
            await self.hass.services.async_call(
                "switch", "turn_on", {"entity_id": sw_entity}, blocking=True
            )
            start_a = new_a
            if cur_entity:
                await self.hass.services.async_call(
                    "number", "set_value",
                    {"entity_id": cur_entity, "value": float(start_a)},
                    blocking=True,
                )
            await self._set_ev_soc_override(device, override=True)
            actual_consumption_w = start_a * voltage
            _LOGGER.info(
                "Peak Guard EV solar: '%s' ingeschakeld op %d A "
                "(ceil van %.2f A, verbruik %.0f W, %d fase(n), SOC-override: %s%%)",
                device.name, start_a, available_a_raw,
                actual_consumption_w, phases,
                device.ev_max_soc if device.ev_max_soc is not None else "n.v.t.",
            )
            self.solar_tracker.start_solar_measurement(
                device_id=device.id,
                device_name=device.name,
                nominal_kw=actual_consumption_w / 1000.0,
                ts=datetime.now(timezone.utc),
            )
            return excess - actual_consumption_w

        else:
            # EV staat al aan → laadstroom aanpassen
            actual_consumption_w = new_a * voltage
            if cur_entity and current_a is not None and new_a != math.ceil(current_a):
                await self.hass.services.async_call(
                    "number", "set_value",
                    {"entity_id": cur_entity, "value": float(new_a)},
                    blocking=True,
                )
                _LOGGER.info(
                    "Peak Guard EV solar: '%s' laadstroom %d → %d A "
                    "(ceil van %.2f A, verbruik %.0f W, %d fase(n))",
                    device.name, int(current_a), new_a,
                    available_a_raw, actual_consumption_w, phases,
                )
            return excess - actual_consumption_w

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
                continue

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
