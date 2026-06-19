import asyncio
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Dict, List, Optional

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.storage import Store

from .avoided_peak_tracker import PeakAvoidTracker, SolarShiftTracker
from .decision_logger import DecisionLogger
from .deciders import EVGuard, InjectionDecider, PeakDecider
from .deciders.base import read_sensor
from .models import (
    BaseCascadeDevice,
    DeviceSnapshot,
    EVChargerDevice,
    from_dict as cascade_from_dict,
)
from .const import (
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
    ACTION_EV_CHARGER,
)

_LOGGER = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────── #
#  Controller                                                                  #
# ──────────────────────────────────────────────────────────────────────────── #

class PeakGuardController:

    def __init__(self, hass: HomeAssistant, config: dict):
        self.hass = hass
        self.config = config
        self.peak_cascade:   List[BaseCascadeDevice] = []
        self.inject_cascade: List[BaseCascadeDevice] = []
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

        # ── Entity listeners: callbacks die worden aangeroepen na elke
        # update_cascade() aanroep, zodat switch/number platforms
        # dynamisch nieuwe entities kunnen aanmaken.
        self._entity_listeners: list = []

        # Tijdstip van de laatste loop-iteratie (UTC ISO-string, voor de GUI).
        self._last_loop_at: Optional[str] = None

        # Simulatiemodus: als ingesteld, gebruikt de loop deze waarde i.p.v. de echte sensor.
        self._simulation_consumption: Optional[float] = None

        # Wakeup-event: als gezet wordt de volgende loop-iteratie onmiddellijk
        # uitgevoerd zonder te wachten op het interval. Vervangt de vroegere
        # boolean _force_check — Event is race-condition-vrij: een .set() die
        # binnenkomt terwijl de loop actief is gaat niet verloren.
        self._wakeup: asyncio.Event = asyncio.Event()

        # Unsubscribe-callbacks voor state-change listeners op EV-entiteiten.
        self._state_unsubs: list = []

        # Acties uitgevoerd in de huidige iteratie (reset elk begin loop).
        self._iteration_actions: list = []

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
        self._decision_logger = DecisionLogger(
            hass=hass,
            config=config,
            peak_cascade=self.peak_cascade,
            inject_cascade=self.inject_cascade,
            peak_snapshots=self._peak_snapshots,
            inject_snapshots=self._inject_snapshots,
            ev_guard=self._ev_guard_decider,
            iteration_actions=self._iteration_actions,
        )

    # ------------------------------------------------------------------ #
    #  Opslaan en laden                                                    #
    # ------------------------------------------------------------------ #

    async def async_load(self):
        data = await self._store.async_load()
        if data:
            self.peak_cascade.clear()
            self.peak_cascade.extend(cascade_from_dict(d) for d in data.get("peak", []))
            self.inject_cascade.clear()
            self.inject_cascade.extend(cascade_from_dict(d) for d in data.get("inject", []))
            peak_entity_ids   = {d.entity_id for d in self.peak_cascade}
            inject_entity_ids = {d.entity_id for d in self.inject_cascade}
            for k, v in data.get("peak_snapshots", {}).items():
                if k in peak_entity_ids:
                    self._peak_snapshots[k] = DeviceSnapshot(**v)
                else:
                    _LOGGER.info("Peak Guard: verouderd piek-snapshot verwijderd voor '%s' (niet meer in cascade)", k)
            for k, v in data.get("inject_snapshots", {}).items():
                if k in inject_entity_ids:
                    self._inject_snapshots[k] = DeviceSnapshot(**v)
                else:
                    _LOGGER.info("Peak Guard: verouderd inject-snapshot verwijderd voor '%s' (niet meer in cascade)", k)
            if self._peak_snapshots:
                _LOGGER.warning(
                    "Peak Guard: %d apparaat/apparaten nog uitgeschakeld uit vorige sessie — "
                    "worden hersteld zodra piekmarges het toelaten: %s",
                    len(self._peak_snapshots),
                    list(self._peak_snapshots.keys()),
                )

    async def async_save(self):
        await self._store.async_save({
            "peak":   [asdict(d) for d in self.peak_cascade],
            "inject": [asdict(d) for d in self.inject_cascade],
            "peak_snapshots":   {k: asdict(v) for k, v in self._peak_snapshots.items()},
            "inject_snapshots": {k: asdict(v) for k, v in self._inject_snapshots.items()},
        })

    # ------------------------------------------------------------------ #
    #  API data                                                            #
    # ------------------------------------------------------------------ #

    def to_dict(self) -> dict:
        return {
            "peak": [
                asdict(d)
                for d in sorted(self.peak_cascade, key=lambda x: x.priority)
            ],
            "inject": [
                asdict(d)
                for d in sorted(self.inject_cascade, key=lambda x: x.priority)
            ],
            "config": {
                "consumption_sensor": self.config.get(CONF_CONSUMPTION_SENSOR),
                "peak_sensor":        self.config.get(CONF_PEAK_SENSOR),
                "buffer_watts":       self.config.get(CONF_BUFFER_WATTS, DEFAULT_BUFFER_WATTS),
                "update_interval":    self.config.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL),
            },
            "status": {
                "monitoring":   self._monitoring,
                "last_loop_at": self._last_loop_at,
                "interval_s":   max(float(self.config.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)), 60.0),
                **self._ev_guard_decider.status_dict(),
            },
            "simulation": {
                "active":        self._simulation_consumption is not None,
                "consumption_w": self._simulation_consumption,
            },
        }

    def update_cascade(self, cascade_type: str, devices: list):
        parsed = [cascade_from_dict(d) for d in devices]
        new_entity_ids = {d.entity_id for d in parsed}
        if cascade_type == "peak":
            self.peak_cascade.clear()
            self.peak_cascade.extend(parsed)
            for eid in list(self._peak_snapshots):
                if eid not in new_entity_ids:
                    del self._peak_snapshots[eid]
                    _LOGGER.info("Peak Guard: snapshot verwijderd voor '%s' (niet meer in piek-cascade)", eid)
        elif cascade_type == "inject":
            self.inject_cascade.clear()
            self.inject_cascade.extend(parsed)
            for eid in list(self._inject_snapshots):
                if eid not in new_entity_ids:
                    del self._inject_snapshots[eid]
                    _LOGGER.info("Peak Guard: snapshot verwijderd voor '%s' (niet meer in inject-cascade)", eid)
        # Herregistreer EV listeners zodat nieuwe/gewijzigde apparaten meegenomen worden
        if self._monitoring:
            self._setup_ev_listeners()
        # Notificeer entity-platforms zodat nieuwe apparaten een entity krijgen
        for cb in self._entity_listeners:
            try:
                cb()
            except Exception:
                _LOGGER.exception("Peak Guard: entity listener raised an exception")

    def register_entity_listener(self, cb) -> None:
        """Registreer een callback die wordt aangeroepen na elke cascade-update.

        Gebruikt door switch.py en number.py om dynamisch nieuwe entities
        aan te maken wanneer apparaten worden toegevoegd via de UI.
        """
        self._entity_listeners.append(cb)

    # ------------------------------------------------------------------ #
    #  EV state-change listeners                                           #
    # ------------------------------------------------------------------ #

    def trigger_wakeup(self) -> None:
        """Wek de monitoring loop onmiddellijk op, zonder te wachten op het interval."""
        self._wakeup.set()

    def set_simulation(self, consumption_w: Optional[float]) -> None:
        """Activeer of deactiveer de simulatiemodus.

        consumption_w=float → simuleer dat waarde (W) als verbruik; echte sensor wordt genegeerd.
        consumption_w=None  → terug naar echte sensor.
        """
        self._simulation_consumption = consumption_w
        if consumption_w is None:
            _LOGGER.info("Peak Guard: simulatiemodus uitgeschakeld — echte sensor actief")
        else:
            _LOGGER.warning(
                "Peak Guard: simulatiemodus ACTIEF — verbruik vastgezet op %.0f W", consumption_w
            )
        self._wakeup.set()

    @property
    def simulation_active(self) -> bool:
        return self._simulation_consumption is not None

    @property
    def simulation_consumption(self) -> Optional[float]:
        return self._simulation_consumption

    def _ev_watched_entities(self) -> tuple:
        """Verzamel EV-entiteiten om op te luisteren.

        Geeft een tuple (alle_entities, schakelaar_entities) terug.
        Schakelaar-entities worden apart bijgehouden zodat de callback alleen
        op '→ off'-transities reageert: turn_on wordt door PG zelf gestuurd en
        vereist geen onmiddellijke hercheck; turn_off (handmatig of kabel) wel.
        """
        all_entities: set = set()
        switch_entities: set = set()
        for device in self.inject_cascade:
            if not isinstance(device, EVChargerDevice):
                continue
            if device.cable_entity:
                all_entities.add(device.cable_entity)
            sw = device.switch_entity or device.entity_id
            if sw:
                all_entities.add(sw)
                switch_entities.add(sw)
            if device.status_sensor:
                all_entities.add(device.status_sensor)
            if device.location_tracker:
                all_entities.add(device.location_tracker)
        return all_entities, switch_entities

    def _setup_ev_listeners(self) -> None:
        """Registreer state-change listeners op alle EV-entiteiten in de inject-cascade.

        Wanneer een relevante entiteit van staat wisselt (bijv. kabel ontkoppeld),
        wordt de monitoring loop direct gewekt in plaats van te wachten op het
        volgende interval.
        """
        self._teardown_ev_listeners()
        all_entities, switch_entities = self._ev_watched_entities()
        if not all_entities:
            return

        @callback
        def _on_ev_state_changed(event) -> None:
            old = event.data.get("old_state")
            new = event.data.get("new_state")
            if old is None or new is None or old.state == new.state:
                return
            entity_id = event.data.get("entity_id")
            # Schakelaar-entiteiten: enkel reageren op '→ off'. Een '→ on'-transitie
            # is door PG zelf geïnitieerd; die hoeft geen extra iteratie te triggeren.
            if entity_id in switch_entities and new.state != "off":
                return
            _LOGGER.debug(
                "Peak Guard: EV-entity '%s' gewijzigd (%s → %s) — directe check",
                entity_id, old.state, new.state,
            )
            self.trigger_wakeup()

        unsub = async_track_state_change_event(self.hass, list(all_entities), _on_ev_state_changed)
        self._state_unsubs.append(unsub)
        _LOGGER.debug(
            "Peak Guard: state-change listeners geregistreerd op %d EV-entiteit(en): %s",
            len(all_entities), sorted(all_entities),
        )

    def _teardown_ev_listeners(self) -> None:
        """Verwijder alle eerder geregistreerde EV state-change listeners."""
        for unsub in self._state_unsubs:
            try:
                unsub()
            except Exception:
                _LOGGER.exception("Peak Guard: fout bij verwijderen EV listener")
        self._state_unsubs.clear()

    # ------------------------------------------------------------------ #
    #  Monitoring loop                                                     #
    # ------------------------------------------------------------------ #

    async def start_monitoring(self):
        self._monitoring = True
        self._setup_ev_listeners()
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
        self._teardown_ev_listeners()
        self._wakeup.set()   # deblokkeert de loop zodat hij netjes kan stoppen
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        _LOGGER.info("Peak Guard: monitoring gestopt")

    def _resolve_interval(self) -> float:
        """Return the effective loop interval (minimum 60 s), warning if adjusted."""
        raw = float(self.config.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL))
        if raw < 60.0:
            _LOGGER.warning(
                "Peak Guard: geconfigureerd update_interval (%.0f s) is te laag voor EV-beveiliging. "
                "Verhoogd naar 60 s.",
                raw,
            )
        return max(raw, 60.0)

    def _read_consumption(self) -> Optional[float]:
        """Return the current consumption in W (simulation takes priority)."""
        if self._simulation_consumption is not None:
            _LOGGER.warning(
                "Peak Guard [SIMULATIE] verbruik=%.0f W (echte sensor genegeerd)",
                self._simulation_consumption,
            )
            return self._simulation_consumption
        return self._sensor_value(self.config.get(CONF_CONSUMPTION_SENSOR))

    async def _dispatch(self, consumption: float, now: datetime) -> None:
        """Run peak/inject cascade and restore logic for one loop tick."""
        _LOGGER.debug(
            "Peak Guard loop: verbruik=%.0f W — %s",
            consumption,
            "EXPORT (solar cascade actief)" if consumption < 0
            else "import (piek-cascade actief)" if consumption > 0
            else "nul",
        )
        await self._check_power_drop(consumption, now)
        if consumption > 0:
            await self._peak_decider.check(consumption, now)
            await self._peak_decider.check_restore(consumption, now)
            await self._injection_decider.check_restore(consumption, now)
        elif consumption < 0:
            _LOGGER.debug(
                "Peak Guard: zonne-overschot gedetecteerd — sensor=%.0f W "
                "(export %.0f W) — solar cascade wordt gecontroleerd",
                consumption, abs(consumption),
            )
            await self._injection_decider.check(consumption, now)
            await self._peak_decider.check_restore(consumption, now)
            await self._injection_decider.check_restore(consumption, now)
        else:
            await self._peak_decider.check_restore(0.0, now)
            await self._injection_decider.check_restore(0.0, now)

    async def _monitor_loop(self):
        interval = self._resolve_interval()

        # Opstart-vertraging: HA-entities zijn bij het laden van de integratie
        # soms nog in staat unknown/unavailable.
        _LOGGER.debug("Peak Guard: wacht 10 s op HA-opstart vóór eerste loop-iteratie")
        await asyncio.sleep(10.0)

        _sensor_unavailable_count = 0

        while self._monitoring:
            # Clear vóór het werk: een state-change die binnenkomt tijdens een await
            # in de werklus zet het event; door hier te clearen en pas ná het werk
            # te wachten mist de loop dat signaal niet.
            self._wakeup.clear()
            try:
                now = datetime.now(timezone.utc)
                self._last_loop_at = now.isoformat()
                self._iteration_actions.clear()

                consumption = self._read_consumption()
                if consumption is not None:
                    _sensor_unavailable_count = 0

                    _debug_logging = self.config.get(CONF_DEBUG_DECISION_LOGGING, False)
                    _pre_states: dict = {}
                    if _debug_logging:
                        for _d in self.peak_cascade + self.inject_cascade:
                            _s = self.hass.states.get(_d.entity_id)
                            _pre_states[_d.entity_id] = _s.state if _s else "?"

                    await self._dispatch(consumption, now)

                    if _debug_logging:
                        await self._decision_logger.log(consumption, _pre_states)

                    self._prev_consumption = consumption
                else:
                    sensor_id = self.config.get(CONF_CONSUMPTION_SENSOR)
                    _sensor_unavailable_count += 1
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
            try:
                await asyncio.wait_for(self._wakeup.wait(), timeout=interval)
                _LOGGER.debug("Peak Guard: vroegtijdige wakeup door EV-event of force_check")
            except asyncio.TimeoutError:
                pass


    # ------------------------------------------------------------------ #
    #  Power-drop detectie — Hook 3                                        #
    # ------------------------------------------------------------------ #

    async def _check_power_drop(self, consumption: float, now: datetime) -> None:
        if self._prev_consumption is None:
            return

        active_ids = self.peak_tracker.get_active_ids()
        if not active_ids:
            return

        drop = self._prev_consumption - consumption
        all_peak_devices = {d.id: d for d in self.peak_cascade}

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
        return read_sensor(self.hass, entity_id)
