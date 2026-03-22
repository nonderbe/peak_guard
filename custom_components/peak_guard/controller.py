import asyncio
import logging
from dataclasses import dataclass, asdict
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
    CONF_SOLAR_NETTO_EUR_PER_KWH,
    DEFAULT_BUFFER_WATTS,
    DEFAULT_UPDATE_INTERVAL,
    DEFAULT_POWER_DETECTION_TOLERANCE_PERCENT,
    DEFAULT_SOLAR_NETTO_EUR_PER_KWH,
    ACTION_SWITCH_OFF,
    ACTION_SWITCH_ON,
    ACTION_THROTTLE,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class CascadeDevice:
    id: str
    name: str
    entity_id: str
    priority: int
    action_type: str
    power_watts: int = 0
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    power_per_unit: Optional[float] = None
    enabled: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DeviceSnapshot:
    """Oorspronkelijke staat van een apparaat vóór een Peak Guard ingreep."""
    entity_id: str
    # Voor switches: "on" of "off"
    # Voor throttle: de numerieke waarde als string
    original_state: str


class PeakGuardController:

    def __init__(self, hass: HomeAssistant, config: dict):
        self.hass = hass
        self.config = config
        self.peak_cascade: List[CascadeDevice] = []
        self.inject_cascade: List[CascadeDevice] = []
        self._monitoring = False
        self._task: Optional[asyncio.Task] = None
        self._store = Store(hass, STORAGE_VERSION, STORAGE_KEY)

        # Snapshots van apparaten die door Peak Guard zijn aangepast.
        # Key = entity_id, value = DeviceSnapshot.
        # Aanwezig zodra Peak Guard een ingreep heeft gedaan;
        # verwijderd zodra het apparaat volledig hersteld is.
        self._peak_snapshots: Dict[str, DeviceSnapshot] = {}
        self._inject_snapshots: Dict[str, DeviceSnapshot] = {}

        # ---- Modus 1: piekbeperking --------------------------------- #
        self.peak_tracker = PeakAvoidTracker()

        # ---- Modus 2: injectiepreventie ----------------------------- #
        self.solar_tracker = SolarShiftTracker()

        # Power-drop detectie: vorig verbruik per cyclus (W)
        self._prev_consumption: Optional[float] = None

    # ------------------------------------------------------------------ #
    #  Opslaan en laden                                                    #
    # ------------------------------------------------------------------ #

    async def async_load(self):
        data = await self._store.async_load()
        if data:
            self.peak_cascade = [CascadeDevice(**d) for d in data.get("peak", [])]
            self.inject_cascade = [CascadeDevice(**d) for d in data.get("inject", [])]

    async def async_save(self):
        await self._store.async_save(
            {
                "peak": [d.to_dict() for d in self.peak_cascade],
                "inject": [d.to_dict() for d in self.inject_cascade],
            }
        )

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
                "peak_sensor": self.config.get(CONF_PEAK_SENSOR),
                "buffer_watts": self.config.get(CONF_BUFFER_WATTS, DEFAULT_BUFFER_WATTS),
                "update_interval": self.config.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL),
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
                    # ---- Power-drop detectie voor avoided tracking ---- #
                    await self._check_power_drop(consumption)
                    # ---- Bestaande cascade logica --------------------- #
                    if consumption > 0:
                        await self._check_peak(consumption)
                        await self._check_inject_restore(consumption)
                    elif consumption < 0:
                        await self._check_injection(consumption)
                        await self._check_peak_restore(consumption)
                    else:
                        # Verbruik = 0: herstel beide cascades volledig
                        await self._check_peak_restore(0.0)
                        await self._check_inject_restore(0.0)
                    self._prev_consumption = consumption
                else:
                    # Sensor onbeschikbaar: reset om valse power-drop te vermijden
                    self._prev_consumption = None
            except Exception:
                _LOGGER.exception("Peak Guard: fout in monitoring loop")
            await asyncio.sleep(interval)

    # ------------------------------------------------------------------ #
    #  Cascade logica — ingreep                                            #
    # ------------------------------------------------------------------ #

    async def _check_peak(self, consumption: float):
        """Schakel apparaten terug als verbruik de maandpiek dreigt te overschrijden."""
        peak = self._sensor_value(self.config.get(CONF_PEAK_SENSOR))
        if peak is None:
            return
        buffer = float(self.config.get(CONF_BUFFER_WATTS, DEFAULT_BUFFER_WATTS))
        excess = consumption - peak + buffer
        if excess > 0:
            _LOGGER.warning(
                f"Peak Guard: piekverbruik overschreden met {excess:.0f} W – cascade gestart"
            )
            await self._run_cascade(self.peak_cascade, excess, self._peak_snapshots)

    async def _check_injection(self, consumption: float):
        """Schakel apparaten in als er te veel stroom wordt teruggeleverd."""
        injection = abs(consumption)
        buffer = float(self.config.get(CONF_BUFFER_WATTS, DEFAULT_BUFFER_WATTS))
        if injection > buffer:
            _LOGGER.info(
                f"Peak Guard: stroominjectie van {injection:.0f} W gedetecteerd – cascade gestart"
            )
            await self._run_cascade(self.inject_cascade, injection, self._inject_snapshots)

    # ------------------------------------------------------------------ #
    #  Cascade logica — herstel                                            #
    # ------------------------------------------------------------------ #

    async def _check_peak_restore(self, consumption: float):
        """
        Herstel piek-cascade apparaten als het verbruik voldoende gedaald is.
        Herstelt in omgekeerde prioriteitsvolgorde (laagste prioriteit eerst),
        één apparaat per cyclus om het effect te kunnen meten.
        """
        if not self._peak_snapshots:
            return
        peak = self._sensor_value(self.config.get(CONF_PEAK_SENSOR))
        if peak is None:
            return
        buffer = float(self.config.get(CONF_BUFFER_WATTS, DEFAULT_BUFFER_WATTS))
        # Herstel als het verbruik ruim onder de drempel zit (buffer als marge)
        threshold = peak - buffer
        if consumption >= threshold:
            return

        # Zoek het apparaat met de hoogste prioriteit dat nog hersteld kan worden
        # (herstel in omgekeerde volgorde: prioriteit hoogst = als laatste hersteld)
        snapshots_to_restore = self._get_restore_candidates(
            self.peak_cascade, self._peak_snapshots, reverse=True
        )
        if not snapshots_to_restore:
            return

        device, snapshot = snapshots_to_restore[0]
        restored = await self._restore_device(device, snapshot)
        if restored:
            del self._peak_snapshots[device.entity_id]
            _LOGGER.info(
                f"Peak Guard: '{device.name}' hersteld naar oorspronkelijke staat"
            )

    async def _check_inject_restore(self, consumption: float):
        """
        Herstel injectie-cascade apparaten zodra het verbruik positief is
        (geen injectie meer). Herstelt één apparaat per cyclus.
        """
        if not self._inject_snapshots:
            return
        # Herstel als er geen injectie meer is (verbruik >= 0)
        if consumption < 0:
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
            _LOGGER.info(
                f"Peak Guard: '{device.name}' hersteld naar oorspronkelijke staat"
            )

    def _get_restore_candidates(
        self,
        cascade: List[CascadeDevice],
        snapshots: Dict[str, DeviceSnapshot],
        reverse: bool = True,
    ) -> List[tuple]:
        """
        Geeft een lijst van (device, snapshot) tuples voor apparaten die
        in de snapshot zitten, gesorteerd op prioriteit.
        reverse=True: hoogste prioriteit als eerste (= als laatste aangepast,
        dus als eerste hersteld).
        """
        candidates = []
        for device in cascade:
            if device.entity_id in snapshots:
                candidates.append((device, snapshots[device.entity_id]))
        candidates.sort(key=lambda x: x[0].priority, reverse=reverse)
        return candidates

    async def _restore_device(self, device: CascadeDevice, snapshot: DeviceSnapshot) -> bool:
        """Zet een apparaat terug naar de staat van voor de ingreep."""
        state = self.hass.states.get(device.entity_id)
        if state is None:
            _LOGGER.warning(
                f"Peak Guard: kan '{device.name}' niet herstellen — entity niet gevonden"
            )
            return False

        try:
            if device.action_type == ACTION_SWITCH_OFF:
                # Was uitgeschakeld door peak guard → zet terug aan
                if snapshot.original_state == "on" and state.state != "on":
                    await self.hass.services.async_call(
                        "switch", "turn_on", {"entity_id": device.entity_id}, blocking=True
                    )
                    _LOGGER.info(
                        f"Peak Guard: '{device.name}' terug ingeschakeld"
                    )
                    # Hook 2 (peak): start duurmeting voor kW-impact
                    self.peak_tracker.start_measurement_on_turnon(
                        device_id=device.id,
                        device_name=device.name,
                        ts=datetime.now(timezone.utc),
                    )
                return True

            if device.action_type == ACTION_SWITCH_ON:
                # Was ingeschakeld door inject guard → zet terug uit
                if snapshot.original_state == "off" and state.state != "off":
                    await self.hass.services.async_call(
                        "switch", "turn_off", {"entity_id": device.entity_id}, blocking=True
                    )
                    _LOGGER.info(
                        f"Peak Guard: '{device.name}' terug uitgeschakeld"
                    )
                    # Hook B (solar): bereken kWh-verschuiving
                    self.solar_tracker.complete_solar_calculation(
                        device_id=device.id,
                        now=datetime.now(timezone.utc),
                    )
                return True

            if device.action_type == ACTION_THROTTLE:
                original = float(snapshot.original_state)
                current = float(state.state)
                # Herstel volledig naar de originele waarde in één stap
                new_value = round(original, 1)
                if new_value != round(current, 1):
                    await self.hass.services.async_call(
                        "number",
                        "set_value",
                        {"entity_id": device.entity_id, "value": new_value},
                        blocking=True,
                    )
                    _LOGGER.info(
                        f"Peak Guard: '{device.name}' hersteld {current} → {new_value}"
                    )
                return True

        except (ValueError, TypeError) as err:
            _LOGGER.error(f"Peak Guard: fout bij herstellen '{device.name}': {err}")

        return False

    # ------------------------------------------------------------------ #
    #  Cascade uitvoering                                                  #
    # ------------------------------------------------------------------ #

    async def _run_cascade(
        self,
        cascade: List[CascadeDevice],
        excess: float,
        snapshots: Dict[str, DeviceSnapshot],
    ):
        sorted_devices = sorted(
            [d for d in cascade if d.enabled], key=lambda x: x.priority
        )
        for device in sorted_devices:
            if excess <= 0:
                break
            excess = await self._apply_action(device, excess, snapshots)

    async def _apply_action(
        self,
        device: CascadeDevice,
        excess: float,
        snapshots: Dict[str, DeviceSnapshot],
    ) -> float:
        state = self.hass.states.get(device.entity_id)
        if state is None:
            _LOGGER.warning(f"Peak Guard: entity '{device.entity_id}' niet gevonden")
            return excess

        if device.action_type == ACTION_SWITCH_OFF and state.state == "on":
            # Sla de oorspronkelijke staat op vóór de ingreep
            if device.entity_id not in snapshots:
                snapshots[device.entity_id] = DeviceSnapshot(
                    entity_id=device.entity_id,
                    original_state=state.state,
                )
            await self.hass.services.async_call(
                "switch", "turn_off", {"entity_id": device.entity_id}, blocking=True
            )
            _LOGGER.info(f"Peak Guard: '{device.name}' uitgeschakeld (–{device.power_watts} W)")
            # Hook 1 (peak): registreer pending avoid
            self.peak_tracker.record_pending_avoid(
                device_id=device.id,
                device_name=device.name,
                nominal_kw=device.power_watts / 1000.0,
                ts=datetime.now(timezone.utc),
            )
            return excess - device.power_watts

        if device.action_type == ACTION_SWITCH_ON and state.state == "off":
            if device.entity_id not in snapshots:
                snapshots[device.entity_id] = DeviceSnapshot(
                    entity_id=device.entity_id,
                    original_state=state.state,
                )
            await self.hass.services.async_call(
                "switch", "turn_on", {"entity_id": device.entity_id}, blocking=True
            )
            _LOGGER.info(f"Peak Guard: '{device.name}' ingeschakeld (–{device.power_watts} W)")
            # Hook A (solar): start duurmeting voor kWh-verschuiving
            self.solar_tracker.start_solar_measurement(
                device_id=device.id,
                device_name=device.name,
                nominal_kw=device.power_watts / 1000.0,
                ts=datetime.now(timezone.utc),
            )
            return excess - device.power_watts

        if device.action_type == ACTION_THROTTLE:
            try:
                current = float(state.state)
                ppu = device.power_per_unit or 690.0
                new_value = max(device.min_value or 0, current - (excess / ppu))
                new_value = round(new_value, 1)
                reduction = (current - new_value) * ppu
                if new_value < current:
                    # Sla de oorspronkelijke staat op vóór de EERSTE ingreep
                    if device.entity_id not in snapshots:
                        snapshots[device.entity_id] = DeviceSnapshot(
                            entity_id=device.entity_id,
                            original_state=str(current),
                        )
                    await self.hass.services.async_call(
                        "number",
                        "set_value",
                        {"entity_id": device.entity_id, "value": new_value},
                        blocking=True,
                    )
                    _LOGGER.info(
                        f"Peak Guard: '{device.name}' teruggeschroefd {current} → {new_value} (–{reduction:.0f} W)"
                    )
                    return excess - reduction
            except (ValueError, TypeError) as err:
                _LOGGER.error(f"Peak Guard: fout bij throttle '{device.name}': {err}")

        return excess

    # ------------------------------------------------------------------ #
    #  Power-drop detectie — Hook 3                                        #
    # ------------------------------------------------------------------ #

    async def _check_power_drop(self, consumption: float) -> None:
        """
        Detecteer of een apparaat NATUURLIJK gestopt is met verbruiken
        door te controleren of het totaalverbruik significant gedaald is
        t.o.v. de vorige cyclus.

        We controleren voor elk apparaat met een actieve meting of het
        verbruik gedaald is met ≈ nominal_power (± tolerance van 20%).
        Bij een match wordt complete_avoided_calculation() aangeroepen.
        Tolerantie is configureerbaar via CONF_POWER_DETECTION_TOLERANCE_PERCENT.
        """
        if self._prev_consumption is None:
            return

        # Power-drop detectie enkel voor piek-modus
        # (solar-modus gebruikt hook B in _restore_device)
        active_ids = self.peak_tracker.get_active_ids()
        if not active_ids:
            return

        drop = self._prev_consumption - consumption   # positief = verbruik daalde

        # Zoek device bij elk actief id (enkel peak_cascade)
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
                    "Peak Guard: power-drop %.0f W gedetecteerd — "
                    "natural stop '%s' (nominaal %.0f W)",
                    drop, device.name, nominal_w,
                )
                # Hook 3 (peak): complete_peak_calculation
                self.peak_tracker.complete_peak_calculation(
                    device_id=device_id,
                    now=now,
                )
                # Één apparaat per cyclus om dubbeltelling te vermijden
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