"""
sensor.py
---------
Definieert alle Peak Guard sensoren:

  1. sensor.peak_guard_quarter_peak_kw
  2. sensor.peak_guard_monthly_peak_kw
  3. sensor.peak_guard_historical_monthly_peaks
  4. sensor.peak_guard_rolling_12_month_avg_kw
  5. sensor.peak_guard_billed_peak_kw
  6. sensor.peak_guard_monthly_capacity_cost_euro
  7. sensor.peak_guard_hypothetical_monthly_peak_kw   (nieuw)
  8. sensor.peak_guard_total_avoided_kw_this_month    (nieuw)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

from homeassistant.components.sensor import (
    SensorEntity,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfPower, UnitOfEnergy
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    DOMAIN,
    CONF_ENERGY_SENSOR,
    CONF_REGIO,
    FLUVIUS_REGIO_TARIEVEN,
    CAPACITY_MIN_KW,
    DEFAULT_REGIO,
    DEVICE_ID_CAPACITY,
    DEVICE_ID_SAVINGS,
    DEVICE_ID_OVERVIEW,
    STORAGE_KEY_SAVINGS,
    STORAGE_VERSION_SAVINGS,
    STORAGE_KEY_SOLAR_SAVINGS,
    STORAGE_VERSION_SOLAR_SAVINGS,
    CONF_SOLAR_NETTO_EUR_PER_KWH,
    DEFAULT_SOLAR_NETTO_EUR_PER_KWH,
)
from homeassistant.helpers.storage import Store
from .quarter_calculator import QuarterCalculator
from .quarter_store import QuarterStore
from .avoided_peak_tracker import PeakAvoidTracker, SolarShiftTracker

_LOGGER = logging.getLogger(__name__)

# Sensorupdate-interval: elke minuut
_UPDATE_INTERVAL = timedelta(minutes=1)

# Persistente opslag voor events en maandstatistieken
_STORAGE_KEY_PEAK_STATE   = f"peak_guard.peak_state"
_STORAGE_KEY_SOLAR_STATE  = f"peak_guard.solar_state"
_STORAGE_VERSION_STATE    = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Registreer alle Peak Guard sensoren."""
    energy_sensor_id = entry.data.get(CONF_ENERGY_SENSOR)
    regio = entry.data.get(CONF_REGIO, DEFAULT_REGIO)
    tarief = FLUVIUS_REGIO_TARIEVEN.get(regio, FLUVIUS_REGIO_TARIEVEN[DEFAULT_REGIO])

    store = QuarterStore(hass)
    await store.async_load()

    calculator = QuarterCalculator()

    # Gedeelde state-container zodat alle sensoren dezelfde berekeningen zien
    shared = SharedCapacityState(
        hass=hass,
        energy_sensor_id=energy_sensor_id,
        store=store,
        calculator=calculator,
        tarief=tarief,
        regio=regio,
    )

    # Haal controller + trackers op
    controller  = hass.data[DOMAIN]["controller"]
    peak_tracker  = controller.peak_tracker
    solar_tracker = controller.solar_tracker

    # Stel tarieven in
    peak_tracker.set_tarief(tarief)
    netto_eur = float(entry.data.get(
        CONF_SOLAR_NETTO_EUR_PER_KWH, DEFAULT_SOLAR_NETTO_EUR_PER_KWH
    ))
    solar_tracker.set_netto_eur_per_kwh(netto_eur)

    # Laad persistente jaarbesparingen — piek
    savings_store = Store(hass, STORAGE_VERSION_SAVINGS, STORAGE_KEY_SAVINGS)
    saved_data = await savings_store.async_load()
    if saved_data:
        current_year = datetime.now(timezone.utc).year
        if saved_data.get("year") == current_year:
            peak_tracker.savings_euro_this_year = float(
                saved_data.get("savings_euro_this_year", 0.0)
            )

    # Laad persistente jaarbesparingen — solar
    solar_savings_store = Store(hass, STORAGE_VERSION_SOLAR_SAVINGS, STORAGE_KEY_SOLAR_SAVINGS)
    solar_saved = await solar_savings_store.async_load()
    if solar_saved:
        current_year = datetime.now(timezone.utc).year
        if solar_saved.get("year") == current_year:
            solar_tracker.savings_euro_this_year = float(
                solar_saved.get("savings_euro_this_year", 0.0)
            )

    # Laad persistente events en maandstatistieken — piek
    peak_state_store = Store(hass, _STORAGE_VERSION_STATE, _STORAGE_KEY_PEAK_STATE)
    peak_state = await peak_state_store.async_load()
    if peak_state:
        current_month = datetime.now(timezone.utc).month
        current_year  = datetime.now(timezone.utc).year
        if (peak_state.get("year") == current_year
                and peak_state.get("month") == current_month):
            peak_tracker.avoided_kw_this_month   = float(peak_state.get("avoided_kw_this_month", 0.0))
            peak_tracker.savings_euro_this_month  = float(peak_state.get("savings_euro_this_month", 0.0))
            # Herstel lijst van hypothetische pieken — zorgt dat _recalc_hypo() na herstart
            # de hoogste bekende hypo als vloer gebruikt (in plaats van 0).
            peak_tracker.hypothetical_peaks_this_month = [
                float(v) for v in peak_state.get("hypothetical_peaks_this_month", [])
            ]
            # Herstel jaarbasis zodat _recalc_month_savings() de jaarbesparing correct herberekent.
            peak_tracker._savings_euro_year_base = round(
                max(0.0, peak_tracker.savings_euro_this_year - peak_tracker.savings_euro_this_month), 4
            )
            # Herstel events
            from .avoided_peak_tracker import PeakEvent
            from dataclasses import fields
            for ev_dict in peak_state.get("events", []):
                try:
                    ev = PeakEvent(
                        device_id=ev_dict["device_id"],
                        device_name=ev_dict["device_name"],
                        nominal_kw=float(ev_dict["nominal_kw"]),
                        avoid_ts=datetime.fromisoformat(ev_dict["avoid_ts"]),
                        turnon_ts=datetime.fromisoformat(ev_dict["turnon_ts"]),
                        natural_stop_ts=datetime.fromisoformat(ev_dict["natural_stop_ts"]),
                        measured_duration_min=float(ev_dict["measured_duration_min"]),
                        added_energy_kwh=float(ev_dict["added_energy_kwh"]),
                        avoided_peak_kw=float(ev_dict["avoided_peak_kw"]),
                        savings_euro=float(ev_dict["savings_euro"]),
                        hypothetical_peak_kw=float(ev_dict.get("hypothetical_peak_kw", 0.0)),
                    )
                    peak_tracker.events.append(ev)
                except (KeyError, ValueError):
                    pass
            _LOGGER.info(
                "Peak Guard: %d piek-events hersteld vanuit opslag (%d hypo-pieken)",
                len(peak_tracker.events),
                len(peak_tracker.hypothetical_peaks_this_month),
            )

    # Laad persistente events en maandstatistieken — solar
    solar_state_store = Store(hass, _STORAGE_VERSION_STATE, _STORAGE_KEY_SOLAR_STATE)
    solar_state = await solar_state_store.async_load()
    if solar_state:
        current_month = datetime.now(timezone.utc).month
        current_year  = datetime.now(timezone.utc).year
        if (solar_state.get("year") == current_year
                and solar_state.get("month") == current_month):
            solar_tracker.shifted_kwh_this_month  = float(solar_state.get("shifted_kwh_this_month", 0.0))
            solar_tracker.savings_euro_this_month = float(solar_state.get("savings_euro_this_month", 0.0))
            from .avoided_peak_tracker import SolarEvent
            for ev_dict in solar_state.get("events", []):
                try:
                    ev = SolarEvent(
                        device_id=ev_dict["device_id"],
                        device_name=ev_dict["device_name"],
                        nominal_kw=float(ev_dict["nominal_kw"]),
                        turnon_ts=datetime.fromisoformat(ev_dict["turnon_ts"]),
                        restore_ts=datetime.fromisoformat(ev_dict["restore_ts"]),
                        measured_duration_min=float(ev_dict["measured_duration_min"]),
                        shifted_kwh=float(ev_dict["shifted_kwh"]),
                        savings_euro=float(ev_dict["savings_euro"]),
                    )
                    solar_tracker.events.append(ev)
                except (KeyError, ValueError):
                    pass
            _LOGGER.info(
                "Peak Guard: %d solar-events hersteld vanuit opslag",
                len(solar_tracker.events),
            )

    entities = [
        # ---- Capaciteitstarief-sensoren (ongewijzigd) ----------------
        QuarterPeakSensor(shared),
        MonthlyPeakSensor(shared),
        HistoricalMonthlyPeaksSensor(shared),
        Rolling12MonthAvgSensor(shared),
        BilledPeakSensor(shared),
        MonthlyCapacityCostSensor(shared),
        # ---- Piek-modus sensoren ------------------------------------
        HypotheticalMonthlyPeakSensor(shared, peak_tracker),
        PeakAvoidedKwThisMonthSensor(shared, peak_tracker),
        PeakSavingsEuroThisMonthSensor(shared, peak_tracker),
        PeakSavingsEuroThisYearSensor(shared, peak_tracker),
        PeakAvoidedEventsSensor(shared, peak_tracker),
        # ---- Solar-modus sensoren -----------------------------------
        SolarShiftedKwhThisMonthSensor(shared, solar_tracker),
        SolarSavingsEuroThisMonthSensor(shared, solar_tracker),
        SolarSavingsEuroThisYearSensor(shared, solar_tracker),
        SolarAvoidedEventsSensor(shared, solar_tracker),
        # ---- Diagnostics --------------------------------------------
        DiagnosticsSensor(shared, peak_tracker, solar_tracker),
        # ---- Overzicht-device (zichtbaar op integratie-pagina) ------
        OverviewStatusSensor(shared, peak_tracker, solar_tracker),
        OverviewTotalSavingsMonthSensor(shared, peak_tracker, solar_tracker),
        OverviewTotalSavingsYearSensor(shared, peak_tracker, solar_tracker),
        OverviewPeakAvoidedKwMonthlySensor(shared, peak_tracker),
        OverviewPeakSavingsEuroMonthlySensor(shared, peak_tracker),
        OverviewSolarShiftedKwhMonthlySensor(shared, solar_tracker),
        OverviewSolarSavingsEuroMonthlySensor(shared, solar_tracker),
        OverviewRecentEventsSensor(shared, peak_tracker, solar_tracker),
    ]

    async_add_entities(entities)

    # Start minuut-timer
    shared.set_peak_tracker(peak_tracker)
    shared.set_solar_tracker(solar_tracker)
    shared.set_savings_store(savings_store)
    shared.set_solar_savings_store(solar_savings_store)
    shared.set_peak_state_store(peak_state_store)
    shared.set_solar_state_store(solar_state_store)
    await shared.async_start(entities)

    # Sla shared op zodat async_unload_entry de timer kan stoppen
    hass.data[DOMAIN]["shared"] = shared


class SharedCapacityState:
    """
    Centrale coordinator die elke minuut de energiesensor leest,
    de kwartierberekening uitvoert en alle sensoren triggert.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        energy_sensor_id: Optional[str],
        store: QuarterStore,
        calculator: QuarterCalculator,
        tarief: float,
        regio: str,
    ) -> None:
        self.hass = hass
        self.energy_sensor_id = energy_sensor_id
        self.store = store
        self.calculator = calculator
        self.tarief = tarief
        self.regio = regio

        # Actuele berekende waarden
        self.current_quarter_kw: float = 0.0
        self.monthly_peak_kw: Optional[float] = None
        self.historical_peaks: list[dict] = []
        self.rolling_avg_kw: Optional[float] = None
        self.billed_peak_kw: float = CAPACITY_MIN_KW
        self.monthly_cost_euro: Optional[float] = None

        self._listeners: list = []
        self._unsub = None
        self._peak_tracker:  Optional[PeakAvoidTracker]  = None
        self._solar_tracker: Optional[SolarShiftTracker] = None
        self._current_month: Optional[int] = None
        self._current_year:  Optional[int] = None
        self._savings_store = None
        self._solar_savings_store = None
        self._peak_state_store = None
        self._solar_state_store = None
        self._last_persisted_peak_savings:       float = 0.0
        self._last_persisted_solar_savings:      float = 0.0
        self._last_peak_events_count:            int   = -1
        self._last_solar_events_count:           int   = -1
        self._last_persisted_peak_month_savings: float = -1.0
        self._last_persisted_solar_month_savings:float = -1.0

    async def async_start(self, entities: list) -> None:
        """Registreer listeners en start de minuut-timer."""
        self._listeners = entities
        # Voer meteen een eerste berekening uit
        await self._async_update(datetime.now(timezone.utc))
        # Plan daarna elke minuut een update
        self._unsub = async_track_time_interval(
            self.hass, self._async_update, _UPDATE_INTERVAL
        )

    def set_peak_tracker(self, tracker: PeakAvoidTracker) -> None:
        self._peak_tracker = tracker

    def set_solar_tracker(self, tracker: SolarShiftTracker) -> None:
        self._solar_tracker = tracker

    def set_savings_store(self, store) -> None:
        self._savings_store = store

    def set_solar_savings_store(self, store) -> None:
        self._solar_savings_store = store

    def set_peak_state_store(self, store) -> None:
        self._peak_state_store = store

    def set_solar_state_store(self, store) -> None:
        self._solar_state_store = store

    def stop(self) -> None:
        """Stop de timer (bij unload)."""
        if self._unsub:
            self._unsub()
            self._unsub = None

    async def _async_update(self, now: datetime) -> None:
        """Lees energiesensor, bereken kwartierpiek, update alle sensoren."""
        energy_kwh = self._read_energy()
        if energy_kwh is None:
            return

        # Kwartierberekening
        self.current_quarter_kw = self.calculator.update(energy_kwh, now)

        # Kwartier afgesloten? → opslaan
        if self.calculator.quarter_just_finished:
            finished_kw = self.calculator.last_finished_value
            finished_ts = self.calculator.last_finished_ts
            if finished_kw is not None and finished_ts is not None:
                await self.store.add_quarter(finished_ts, finished_kw)

        # Afgeleide waarden herberekenen
        self.monthly_peak_kw = self.store.get_current_month_peak()
        # Vergelijk ook met de lopende waarde (nog niet afgesloten kwartier)
        if self.monthly_peak_kw is None:
            self.monthly_peak_kw = self.current_quarter_kw
        else:
            self.monthly_peak_kw = max(self.monthly_peak_kw, self.current_quarter_kw)

        self.historical_peaks = self.store.get_monthly_peaks_last_12()
        self.rolling_avg_kw = self.store.get_rolling_12_month_avg()

        rolling = self.rolling_avg_kw if self.rolling_avg_kw is not None else 0.0
        self.billed_peak_kw = round(max(rolling, CAPACITY_MIN_KW), 4)

        if self.billed_peak_kw is not None:
            self.monthly_cost_euro = round(
                self.billed_peak_kw * self.tarief / 12, 2
            )

        # Jaar/maand-reset + context voor BEIDE trackers
        current_month = now.month
        current_year  = now.year

        if self._current_year is not None and current_year != self._current_year:
            if self._peak_tracker:
                self._peak_tracker.reset_year()
            if self._solar_tracker:
                self._solar_tracker.reset_year()
            _LOGGER.info("Peak Guard: nieuw jaar — jaarbesparingen gereset")
        self._current_year = current_year

        if self._current_month is not None and current_month != self._current_month:
            if self._peak_tracker:
                self._peak_tracker.reset_month()
            if self._solar_tracker:
                self._solar_tracker.reset_month()
            # Forceer persist van de lege staat voor de nieuwe maand
            self._last_peak_events_count            = -1
            self._last_solar_events_count           = -1
            self._last_persisted_peak_month_savings = -1.0
            self._last_persisted_solar_month_savings= -1.0
            _LOGGER.info("Peak Guard: nieuwe maand — trackers gereset")
        self._current_month = current_month

        # Injecteer kwartierdata in piek-tracker
        if self._peak_tracker is not None:
            actual_quarters = {
                datetime.fromisoformat(e["ts"]): e["kw"]
                for e in self.store.get_all_entries()
                if self._entry_in_current_month(e, now)
            }
            # Voeg ook het lopende open kwartier toe. Zonder deze stap berekent
            # _recalc_hypo() de hypothetische piek als enkel extra_dict (de
            # vermeden energie) zonder de basisbelasting van dat kwartier —
            # waardoor hypo < actual_peak en besparing altijd 0 blijft.
            q_start = self.calculator._current_quarter_start
            if q_start is not None:
                actual_quarters.setdefault(q_start, self.current_quarter_kw)
            self._peak_tracker.set_context(
                actual_quarters=actual_quarters,
                actual_monthly_peak=self.monthly_peak_kw or 0.0,
            )

        # Persist piek-jaarbesparingen bij wijziging
        if self._peak_tracker and self._savings_store:
            val = self._peak_tracker.savings_euro_this_year
            if val != self._last_persisted_peak_savings:
                await self._savings_store.async_save({
                    "year": now.year, "savings_euro_this_year": val,
                })
                self._last_persisted_peak_savings = val

        # Persist solar-jaarbesparingen bij wijziging
        if self._solar_tracker and self._solar_savings_store:
            val = self._solar_tracker.savings_euro_this_year
            if val != self._last_persisted_solar_savings:
                await self._solar_savings_store.async_save({
                    "year": now.year, "savings_euro_this_year": val,
                })
                self._last_persisted_solar_savings = val

        # Persist piek-events en maandstatistieken bij wijziging
        if self._peak_tracker and self._peak_state_store:
            n   = len(self._peak_tracker.events)
            msv = self._peak_tracker.savings_euro_this_month
            if n != self._last_peak_events_count or msv != self._last_persisted_peak_month_savings:
                events_data = [
                    {
                        "device_id":            e.device_id,
                        "device_name":          e.device_name,
                        "nominal_kw":           e.nominal_kw,
                        "avoid_ts":             e.avoid_ts.isoformat(),
                        "turnon_ts":            e.turnon_ts.isoformat(),
                        "natural_stop_ts":      e.natural_stop_ts.isoformat(),
                        "measured_duration_min": e.measured_duration_min,
                        "added_energy_kwh":     e.added_energy_kwh,
                        "avoided_peak_kw":      e.avoided_peak_kw,
                        "savings_euro":         e.savings_euro,
                        "hypothetical_peak_kw": e.hypothetical_peak_kw,
                    }
                    for e in self._peak_tracker.events
                ]
                await self._peak_state_store.async_save({
                    "year":                  now.year,
                    "month":                 now.month,
                    "avoided_kw_this_month": self._peak_tracker.avoided_kw_this_month,
                    "savings_euro_this_month": msv,
                    "hypothetical_peaks_this_month": list(self._peak_tracker.hypothetical_peaks_this_month),
                    "events":                events_data,
                })
                self._last_peak_events_count            = n
                self._last_persisted_peak_month_savings = msv

        # Persist solar-events en maandstatistieken bij wijziging
        if self._solar_tracker and self._solar_state_store:
            n   = len(self._solar_tracker.events)
            msv = self._solar_tracker.savings_euro_this_month
            if n != self._last_solar_events_count or msv != self._last_persisted_solar_month_savings:
                events_data = [
                    {
                        "device_id":            e.device_id,
                        "device_name":          e.device_name,
                        "nominal_kw":           e.nominal_kw,
                        "turnon_ts":            e.turnon_ts.isoformat(),
                        "restore_ts":           e.restore_ts.isoformat(),
                        "measured_duration_min": e.measured_duration_min,
                        "shifted_kwh":          e.shifted_kwh,
                        "savings_euro":         e.savings_euro,
                    }
                    for e in self._solar_tracker.events
                ]
                await self._solar_state_store.async_save({
                    "year":                   now.year,
                    "month":                  now.month,
                    "shifted_kwh_this_month": self._solar_tracker.shifted_kwh_this_month,
                    "savings_euro_this_month": msv,
                    "events":                 events_data,
                })
                self._last_solar_events_count            = n
                self._last_persisted_solar_month_savings = msv

        # Alle sensoren laten weten dat ze kunnen updaten
        for entity in self._listeners:
            entity.async_schedule_update_ha_state()

    @staticmethod
    def _entry_in_current_month(entry: dict, now: datetime) -> bool:
        """True als de entry tot de huidige maand behoort."""
        try:
            dt = datetime.fromisoformat(entry["ts"])
            return dt.year == now.year and dt.month == now.month
        except (KeyError, ValueError):
            return False

    def _read_energy(self) -> Optional[float]:
        """Lees de huidige kWh-waarde van de energiesensor."""
        if not self.energy_sensor_id:
            return None
        state = self.hass.states.get(self.energy_sensor_id)
        if state is None or state.state in ("unknown", "unavailable", ""):
            return None
        try:
            return float(state.state)
        except (ValueError, TypeError):
            return None


# ------------------------------------------------------------------ #
#  Basis-sensorklasse                                                  #
# ------------------------------------------------------------------ #

class PeakGuardSensorBase(SensorEntity):
    """Gemeenschappelijke basis voor alle Peak Guard capaciteitssensoren."""

    _attr_should_poll = False   # We pushen updates via async_schedule_update_ha_state

    def __init__(self, shared: SharedCapacityState, unique_suffix: str, name: str) -> None:
        self._shared = shared
        self._attr_unique_id = f"{DOMAIN}_{unique_suffix}"
        self._attr_name = name
        self._attr_device_info = {
            "identifiers": {(DOMAIN, DEVICE_ID_CAPACITY)},
            "name": "Peak Guard Capaciteitstarief",
            "manufacturer": "Peak Guard",
        }


# ------------------------------------------------------------------ #
#  Sensor 1 — Lopend kwartiergemiddelde                               #
# ------------------------------------------------------------------ #

class QuarterPeakSensor(PeakGuardSensorBase):
    """Lopend kwartiergemiddeld vermogen van het huidige 15-min blok (kW)."""

    def __init__(self, shared: SharedCapacityState) -> None:
        super().__init__(shared, "quarter_peak_kw", "Peak Guard Kwartierpiek")
        self._attr_native_unit_of_measurement = "kW"
        self._attr_device_class = SensorDeviceClass.POWER
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_icon = "mdi:timer-15-minutes"

    @property
    def native_value(self) -> float:
        return round(self._shared.current_quarter_kw, 3)

    @property
    def extra_state_attributes(self) -> dict:
        calc = self._shared.calculator
        return {
            "kwartier_start": (
                calc._current_quarter_start.isoformat()
                if calc._current_quarter_start else None
            ),
            "energie_sensor": self._shared.energy_sensor_id,
        }


# ------------------------------------------------------------------ #
#  Sensor 2 — Maandpiek                                               #
# ------------------------------------------------------------------ #

class MonthlyPeakSensor(PeakGuardSensorBase):
    """Hoogste 15-min kwartierpiek van de huidige maand (kW)."""

    def __init__(self, shared: SharedCapacityState) -> None:
        super().__init__(shared, "monthly_peak_kw", "Peak Guard Maandpiek")
        self._attr_native_unit_of_measurement = "kW"
        self._attr_device_class = SensorDeviceClass.POWER
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_icon = "mdi:chart-line"

    @property
    def native_value(self) -> Optional[float]:
        v = self._shared.monthly_peak_kw
        return round(v, 3) if v is not None else None


# ------------------------------------------------------------------ #
#  Sensor 3 — Historische maandpieken                                 #
# ------------------------------------------------------------------ #

class HistoricalMonthlyPeaksSensor(PeakGuardSensorBase):
    """
    Laatste 12 maandpieken als attribuut.
    State = aantal maanden met data.
    """

    def __init__(self, shared: SharedCapacityState) -> None:
        super().__init__(
            shared,
            "historical_monthly_peaks",
            "Peak Guard Historische Maandpieken",
        )
        self._attr_native_unit_of_measurement = "maanden"
        self._attr_icon = "mdi:history"
        self._attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> int:
        return len(self._shared.historical_peaks)

    @property
    def extra_state_attributes(self) -> dict:
        return {"maandpieken": self._shared.historical_peaks}


# ------------------------------------------------------------------ #
#  Sensor 4 — Voortschrijdend 12-maands gemiddelde                    #
# ------------------------------------------------------------------ #

class Rolling12MonthAvgSensor(PeakGuardSensorBase):
    """Voortschrijdend gemiddelde van de laatste 12 maandpieken (kW)."""

    def __init__(self, shared: SharedCapacityState) -> None:
        super().__init__(
            shared,
            "rolling_12_month_avg_kw",
            "Peak Guard Voortschrijdend Gemiddelde 12 Maanden",
        )
        self._attr_native_unit_of_measurement = "kW"
        self._attr_device_class = SensorDeviceClass.POWER
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_icon = "mdi:chart-bell-curve-cumulative"

    @property
    def native_value(self) -> Optional[float]:
        v = self._shared.rolling_avg_kw
        return round(v, 3) if v is not None else None


# ------------------------------------------------------------------ #
#  Sensor 5 — Aangerekende piek                                       #
# ------------------------------------------------------------------ #

class BilledPeakSensor(PeakGuardSensorBase):
    """
    Aangerekende piek = max(voortschrijdend gemiddelde, 2,5 kW).
    Dit is de waarde die Fluvius gebruikt voor de facturatie.
    """

    def __init__(self, shared: SharedCapacityState) -> None:
        super().__init__(shared, "billed_peak_kw", "Peak Guard Aangerekende Piek")
        self._attr_native_unit_of_measurement = "kW"
        self._attr_device_class = SensorDeviceClass.POWER
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_icon = "mdi:cash-clock"

    @property
    def native_value(self) -> float:
        return round(self._shared.billed_peak_kw, 3)

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "minimum_bijdrage_kw": CAPACITY_MIN_KW,
            "rolling_avg_kw": self._shared.rolling_avg_kw,
        }


# ------------------------------------------------------------------ #
#  Sensor 6 — Maandelijkse capaciteitskost                            #
# ------------------------------------------------------------------ #

class MonthlyCapacityCostSensor(PeakGuardSensorBase):
    """
    Geschatte maandelijkse capaciteitskost (€, excl. BTW).
    = aangerekende_piek × regio_tarief / 12
    """

    def __init__(self, shared: SharedCapacityState) -> None:
        super().__init__(
            shared,
            "monthly_capacity_cost_euro",
            "Peak Guard Maandelijkse Capaciteitskost",
        )
        self._attr_native_unit_of_measurement = "EUR"
        self._attr_device_class = SensorDeviceClass.MONETARY
        self._attr_state_class = SensorStateClass.TOTAL
        self._attr_icon = "mdi:currency-eur"

    @property
    def native_value(self) -> Optional[float]:
        return self._shared.monthly_cost_euro

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "regio": self._shared.regio,
            "tarief_eur_kw_jaar": self._shared.tarief,
            "aangerekende_piek_kw": self._shared.billed_peak_kw,
            "excl_btw": True,
        }
# ================================================================== #
#  PIEK-MODUS SENSOREN                                                #
# ================================================================== #

DEVICE_INFO_PEAK_SAVINGS = {
    "identifiers": {(DOMAIN, "piek_besparingen")},
    "name": "Peak Guard — Piekbeperking Besparingen",
    "manufacturer": "Peak Guard",
    "model": "Piek-module",
}

DEVICE_INFO_SOLAR_SAVINGS = {
    "identifiers": {(DOMAIN, "solar_besparingen")},
    "name": "Peak Guard — Injectiepreventie Besparingen",
    "manufacturer": "Peak Guard",
    "model": "Solar-module",
}


class HypotheticalMonthlyPeakSensor(SensorEntity):
    """Hypothetische maandpiek zonder Peak Guard (kW)."""
    _attr_should_poll = False

    def __init__(self, shared: SharedCapacityState, tracker: PeakAvoidTracker) -> None:
        self._shared  = shared
        self._tracker = tracker
        self._attr_unique_id = f"{DOMAIN}_hypothetical_monthly_peak_kw"
        self._attr_name = "Peak Guard Hypothetische Maandpiek"
        self._attr_native_unit_of_measurement = "kW"
        self._attr_device_class = SensorDeviceClass.POWER
        self._attr_state_class  = SensorStateClass.MEASUREMENT
        self._attr_icon = "mdi:chart-line-variant"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, DEVICE_ID_CAPACITY)},
            "name": "Peak Guard Capaciteitstarief",
            "manufacturer": "Peak Guard",
        }

    @property
    def native_value(self) -> Optional[float]:
        v = self._tracker.hypothetical_monthly_peak_kw
        return round(v, 3) if v is not None else self._shared.monthly_peak_kw

    @property
    def extra_state_attributes(self) -> dict:
        actual = self._shared.monthly_peak_kw or 0.0
        hypo   = self._tracker.hypothetical_monthly_peak_kw
        return {
            "actual_monthly_peak_kw": round(actual, 3),
            "verschil_kw": round((hypo or actual) - actual, 3),
        }


class PeakAvoidedKwThisMonthSensor(SensorEntity):
    """Vermeden piekbijdrage (kW) deze maand — piek-modus."""
    _attr_should_poll = False

    def __init__(self, shared: SharedCapacityState, tracker: PeakAvoidTracker) -> None:
        self._shared = shared; self._tracker = tracker
        self._attr_unique_id = f"{DOMAIN}_peak_avoided_kw_this_month"
        self._attr_name = "Peak Guard Vermeden Piek Deze Maand"
        self.entity_id = f"sensor.{DOMAIN}_peak_avoided_kw_this_month"
        self._attr_native_unit_of_measurement = "kW"
        self._attr_device_class = SensorDeviceClass.POWER
        self._attr_state_class  = SensorStateClass.MEASUREMENT
        self._attr_icon = "mdi:shield-check-outline"
        self._attr_device_info = DEVICE_INFO_PEAK_SAVINGS

    @property
    def native_value(self) -> float:
        return round(self._tracker.avoided_kw_this_month, 3)


class PeakSavingsEuroThisMonthSensor(SensorEntity):
    """Besparing op capaciteitstarief deze maand (€)."""
    _attr_should_poll = False

    def __init__(self, shared: SharedCapacityState, tracker: PeakAvoidTracker) -> None:
        self._shared = shared; self._tracker = tracker
        self._attr_unique_id = f"{DOMAIN}_peak_savings_euro_this_month"
        self._attr_name = "Peak Guard Piek Besparing Deze Maand"
        self.entity_id = f"sensor.{DOMAIN}_peak_savings_euro_this_month"
        self._attr_native_unit_of_measurement = "EUR"
        self._attr_device_class = SensorDeviceClass.MONETARY
        self._attr_state_class  = SensorStateClass.TOTAL
        self._attr_icon = "mdi:piggy-bank-outline"
        self._attr_device_info = DEVICE_INFO_PEAK_SAVINGS

    @property
    def native_value(self) -> float:
        return round(self._tracker.savings_euro_this_month, 2)

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "tarief_eur_kw_jaar": self._shared.tarief,
            "excl_btw": True,
        }


class PeakSavingsEuroThisYearSensor(SensorEntity):
    """Cumulatieve piek-besparing dit jaar (€, persistent)."""
    _attr_should_poll = False

    def __init__(self, shared: SharedCapacityState, tracker: PeakAvoidTracker) -> None:
        self._shared = shared; self._tracker = tracker
        self._attr_unique_id = f"{DOMAIN}_peak_savings_euro_this_year"
        self._attr_name = "Peak Guard Piek Besparing Dit Jaar"
        self.entity_id = f"sensor.{DOMAIN}_peak_savings_euro_this_year"
        self._attr_native_unit_of_measurement = "EUR"
        self._attr_device_class = SensorDeviceClass.MONETARY
        self._attr_state_class  = SensorStateClass.TOTAL
        self._attr_icon = "mdi:cash-check"
        self._attr_device_info = DEVICE_INFO_PEAK_SAVINGS

    @property
    def native_value(self) -> float:
        return round(self._tracker.savings_euro_this_year, 2)


class PeakAvoidedEventsSensor(SensorEntity):
    """Log van de laatste 100 piek-vermijdings-events."""
    _attr_should_poll = False

    def __init__(self, shared: SharedCapacityState, tracker: PeakAvoidTracker) -> None:
        self._shared = shared; self._tracker = tracker
        self._attr_unique_id = f"{DOMAIN}_peak_avoided_events"
        self._attr_name = "Peak Guard Piek Events Log"
        self.entity_id = f"sensor.{DOMAIN}_peak_avoided_events"
        self._attr_native_unit_of_measurement = "events"
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_icon = "mdi:format-list-bulleted-type"
        self._attr_device_info = DEVICE_INFO_PEAK_SAVINGS

    @property
    def native_value(self) -> int:
        return len(self._tracker.events)

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "events": [
                {
                    "timestamp_start_uitstel": e.avoid_ts.isoformat(),
                    "apparaat":                e.device_name,
                    "gemeten_duur_min":        e.measured_duration_min,
                    "vermeden_piek_kw":        e.avoided_peak_kw,
                    "hypothetische_piek_kw":   e.hypothetical_peak_kw,
                    "besparing_eur":           e.savings_euro,
                }
                for e in reversed(list(self._tracker.events))
            ],
        }


# ================================================================== #
#  SOLAR-MODUS SENSOREN                                               #
# ================================================================== #

class SolarShiftedKwhThisMonthSensor(SensorEntity):
    """Totaal verschoven energie via injectiepreventie deze maand (kWh)."""
    _attr_should_poll = False

    def __init__(self, shared: SharedCapacityState, tracker: SolarShiftTracker) -> None:
        self._shared = shared; self._tracker = tracker
        self._attr_unique_id = f"{DOMAIN}_solar_verschoven_kwh_this_month"
        self._attr_name = "Peak Guard Solar Verschoven kWh Deze Maand"
        self.entity_id = f"sensor.{DOMAIN}_solar_verschoven_kwh_this_month"
        self._attr_native_unit_of_measurement = "kWh"
        self._attr_device_class = SensorDeviceClass.ENERGY
        self._attr_state_class  = SensorStateClass.TOTAL_INCREASING
        self._attr_icon = "mdi:solar-power"
        self._attr_device_info = DEVICE_INFO_SOLAR_SAVINGS

    @property
    def native_value(self) -> float:
        return round(self._tracker.shifted_kwh_this_month, 3)


class SolarSavingsEuroThisMonthSensor(SensorEntity):
    """Besparing via verschoven zonne-energie deze maand (€)."""
    _attr_should_poll = False

    def __init__(self, shared: SharedCapacityState, tracker: SolarShiftTracker) -> None:
        self._shared = shared; self._tracker = tracker
        self._attr_unique_id = f"{DOMAIN}_solar_savings_euro_this_month"
        self._attr_name = "Peak Guard Solar Besparing Deze Maand"
        self.entity_id = f"sensor.{DOMAIN}_solar_savings_euro_this_month"
        self._attr_native_unit_of_measurement = "EUR"
        self._attr_device_class = SensorDeviceClass.MONETARY
        self._attr_state_class  = SensorStateClass.TOTAL
        self._attr_icon = "mdi:solar-panel"
        self._attr_device_info = DEVICE_INFO_SOLAR_SAVINGS

    @property
    def native_value(self) -> float:
        return round(self._tracker.savings_euro_this_month, 2)

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "netto_eur_per_kwh": self._tracker._netto_eur_per_kwh,
            "excl_btw": True,
        }


class SolarSavingsEuroThisYearSensor(SensorEntity):
    """Cumulatieve solar-besparing dit jaar (€, persistent)."""
    _attr_should_poll = False

    def __init__(self, shared: SharedCapacityState, tracker: SolarShiftTracker) -> None:
        self._shared = shared; self._tracker = tracker
        self._attr_unique_id = f"{DOMAIN}_solar_savings_euro_this_year"
        self._attr_name = "Peak Guard Solar Besparing Dit Jaar"
        self.entity_id = f"sensor.{DOMAIN}_solar_savings_euro_this_year"
        self._attr_native_unit_of_measurement = "EUR"
        self._attr_device_class = SensorDeviceClass.MONETARY
        self._attr_state_class  = SensorStateClass.TOTAL
        self._attr_icon = "mdi:cash-check"
        self._attr_device_info = DEVICE_INFO_SOLAR_SAVINGS

    @property
    def native_value(self) -> float:
        return round(self._tracker.savings_euro_this_year, 2)


class SolarAvoidedEventsSensor(SensorEntity):
    """Log van de laatste 100 solar-shift-events."""
    _attr_should_poll = False

    def __init__(self, shared: SharedCapacityState, tracker: SolarShiftTracker) -> None:
        self._shared = shared; self._tracker = tracker
        self._attr_unique_id = f"{DOMAIN}_solar_avoided_events"
        self._attr_name = "Peak Guard Solar Events Log"
        self.entity_id = f"sensor.{DOMAIN}_solar_avoided_events"
        self._attr_native_unit_of_measurement = "events"
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_icon = "mdi:format-list-bulleted-type"
        self._attr_device_info = DEVICE_INFO_SOLAR_SAVINGS

    @property
    def native_value(self) -> int:
        return len(self._tracker.events)

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "events": [
                {
                    "timestamp_start_inschakeling": e.turnon_ts.isoformat(),
                    "apparaat":                     e.device_name,
                    "gemeten_duur_min":              e.measured_duration_min,
                    "verschoven_kwh":                e.shifted_kwh,
                    "besparing_eur":                 e.savings_euro,
                }
                for e in reversed(list(self._tracker.events))
            ],
        }


# ================================================================== #
#  DIAGNOSTICS                                                        #
# ================================================================== #

class DiagnosticsSensor(SensorEntity):
    """Diagnostics: interne staat van beide trackers."""
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, shared: SharedCapacityState,
                 peak_tracker: PeakAvoidTracker,
                 solar_tracker: SolarShiftTracker) -> None:
        self._shared        = shared
        self._peak_tracker  = peak_tracker
        self._solar_tracker = solar_tracker
        self._attr_unique_id = f"{DOMAIN}_diagnostics"
        self._attr_name = "Peak Guard Diagnostics"
        self._attr_icon = "mdi:bug-outline"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, DEVICE_ID_SAVINGS)},
            "name": "Peak Guard Capaciteitstarief Besparingen",
            "manufacturer": "Peak Guard",
            "model": "Besparingsmodule",
        }

    @property
    def native_value(self) -> str:
        pp = len(self._peak_tracker.get_pending_ids())
        pa = len(self._peak_tracker.get_active_ids())
        sa = len(self._solar_tracker.get_active_ids())
        return f"peak(pending={pp} active={pa}) solar(active={sa})"

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "peak_pending": {
                did: {"apparaat": p.device_name, "avoid_ts": p.avoid_ts.isoformat()}
                for did, p in self._peak_tracker._pending.items()
            },
            "peak_active": {
                did: {"apparaat": m.device_name, "turnon_ts": m.turnon_ts.isoformat()}
                for did, m in self._peak_tracker._active.items()
            },
            "solar_active": {
                did: {"apparaat": m.device_name, "turnon_ts": m.turnon_ts.isoformat()}
                for did, m in self._solar_tracker._active.items()
            },
            "peak_extra_dict": {
                ts.isoformat(): round(kw, 3)
                for ts, kw in sorted(self._peak_tracker.extra_dict.items())
            },
            "peak_avoided_kw_month":    self._peak_tracker.avoided_kw_this_month,
            "peak_savings_eur_month":   self._peak_tracker.savings_euro_this_month,
            "peak_savings_eur_year":    self._peak_tracker.savings_euro_this_year,
            "solar_shifted_kwh_month":  self._solar_tracker.shifted_kwh_this_month,
            "solar_savings_eur_month":  self._solar_tracker.savings_euro_this_month,
            "solar_savings_eur_year":   self._solar_tracker.savings_euro_this_year,
        }



# ================================================================== #
#  DEVICE: "Peak Guard - Overzicht & Besparingen"                     #
#                                                                     #
#  Dit device verschijnt automatisch op de integratie-pagina onder    #
#  Instellingen -> Apparaten & Diensten -> Peak Guard.                #
#  Alle sensoren hieronder hebben long-term statistics (LTS) aan,     #
#  waardoor HA automatisch een maand-over-maand grafiek toont         #
#  wanneer de gebruiker op een sensor klikt (more-info dialog).       #
#                                                                     #
#  RestoreEntity zorgt dat waarden na HA-herstart bewaard blijven     #
#  zonder extra opslagbestanden.                                      #
# ================================================================== #

DEVICE_INFO_OVERVIEW = {
    "identifiers": {(DOMAIN, DEVICE_ID_OVERVIEW)},
    "name": "Peak Guard - Overzicht & Besparingen",
    "manufacturer": "Peak Guard",
    "model": "Overzicht-module",
    "entry_type": "service",
}


class _OverviewBase(SensorEntity, RestoreEntity):
    """
    Basisklasse voor alle overzicht-sensoren.
    - Erft van RestoreEntity zodat de state overleeft na HA-herstart.
    - device_info wijst naar DEVICE_INFO_OVERVIEW.
    - _shared zorgt dat async_schedule_update_ha_state() werkt vanuit
      SharedCapacityState._async_update().
    """

    _attr_should_poll    = False
    _attr_has_entity_name = True

    def __init__(self, shared: SharedCapacityState) -> None:
        self._shared = shared
        self._attr_device_info = DEVICE_INFO_OVERVIEW

    async def async_added_to_hass(self) -> None:
        """Herstel de laatste bekende state na herstart."""
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state not in ("unknown", "unavailable", ""):
            await self._restore_from_state(last)

    async def _restore_from_state(self, last_state) -> None:
        """Override in subklassen om specifieke restore-logica toe te voegen."""
        pass


# ------------------------------------------------------------------ #
#  1. Statussensor                                                    #
# ------------------------------------------------------------------ #

class OverviewStatusSensor(_OverviewBase):
    """
    Samenvattende statussensor.
    State: beschrijvende tekst, bijv. "3 vermijdingen | 2 solar-shifts deze maand"
    Zichtbaar bovenaan het device-overzicht.
    """

    _attr_icon = "mdi:flash-alert"

    def __init__(self, shared: SharedCapacityState,
                 peak_tracker: PeakAvoidTracker,
                 solar_tracker: SolarShiftTracker) -> None:
        super().__init__(shared)
        self._peak   = peak_tracker
        self._solar  = solar_tracker
        self._attr_unique_id = f"{DOMAIN}_overview_status"
        self._attr_name      = "Status"

    @property
    def native_value(self) -> str:
        p = len(self._peak.events)
        s = len(self._solar.events)
        if p == 0 and s == 0:
            return "Actief — nog geen events deze maand"
        parts = []
        if p > 0:
            parts.append(f"{p} piek-vermijding{'en' if p != 1 else ''}")
        if s > 0:
            parts.append(f"{s} solar-shift{'s' if s != 1 else ''}")
        return " | ".join(parts) + " deze maand"

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "piek_events_deze_maand":  len(self._peak.events),
            "solar_events_deze_maand": len(self._solar.events),
            "monitoring_actief":       True,
            "info": (
                "Klik op een sensor hieronder voor de volledige "
                "geschiedenis-grafiek per maand."
            ),
        }


# ------------------------------------------------------------------ #
#  2. Totaal bespaard deze maand (som van beide modi)                 #
# ------------------------------------------------------------------ #

class OverviewTotalSavingsMonthSensor(_OverviewBase):
    """
    Totaal bespaard deze maand = piek-besparing + solar-besparing (EUR).
    Wordt elke minuut herberekend vanuit de twee trackers.
    """

    _attr_icon         = "mdi:piggy-bank"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class  = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = "EUR"

    def __init__(self, shared: SharedCapacityState,
                 peak_tracker: PeakAvoidTracker,
                 solar_tracker: SolarShiftTracker) -> None:
        super().__init__(shared)
        self._peak  = peak_tracker
        self._solar = solar_tracker
        self._attr_unique_id = f"{DOMAIN}_overview_total_savings_month"
        self._attr_name      = "Totaal bespaard deze maand"

    @property
    def native_value(self) -> float:
        return round(
            self._peak.savings_euro_this_month +
            self._solar.savings_euro_this_month,
            2,
        )

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "piek_besparing_eur":  round(self._peak.savings_euro_this_month, 2),
            "solar_besparing_eur": round(self._solar.savings_euro_this_month, 2),
            "excl_btw": True,
        }


# ------------------------------------------------------------------ #
#  3. Totaal bespaard dit jaar (som van beide modi)                   #
# ------------------------------------------------------------------ #

class OverviewTotalSavingsYearSensor(_OverviewBase):
    """
    Totaal bespaard dit jaar (cumulatief, persistent via RestoreEntity).
    TOTAL state_class + LTS => HA toont automatisch jaaroverzicht in more-info.
    """

    _attr_icon         = "mdi:cash-multiple"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class  = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = "EUR"

    def __init__(self, shared: SharedCapacityState,
                 peak_tracker: PeakAvoidTracker,
                 solar_tracker: SolarShiftTracker) -> None:
        super().__init__(shared)
        self._peak  = peak_tracker
        self._solar = solar_tracker
        self._attr_unique_id = f"{DOMAIN}_overview_total_savings_year"
        self._attr_name      = "Totaal bespaard dit jaar"

    @property
    def native_value(self) -> float:
        return round(
            self._peak.savings_euro_this_year +
            self._solar.savings_euro_this_year,
            2,
        )


# ------------------------------------------------------------------ #
#  4+5. Piek-modus maandstatistieken (voor LTS-grafieken)            #
# ------------------------------------------------------------------ #

class OverviewPeakAvoidedKwMonthlySensor(_OverviewBase):
    """
    Vermeden piekbijdrage deze maand (kW).
    state_class=MEASUREMENT + LTS => more-info toont historische grafiek.
    Elke maand begint opnieuw bij 0 (via PeakAvoidTracker.reset_month).
    """

    _attr_icon         = "mdi:shield-check-outline"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class  = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "kW"

    def __init__(self, shared: SharedCapacityState,
                 tracker: PeakAvoidTracker) -> None:
        super().__init__(shared)
        self._tracker = tracker
        self._attr_unique_id = f"{DOMAIN}_overview_peak_avoided_kw_monthly"
        self._attr_name      = "Piekbeperking — vermeden kW (maand)"

    @property
    def native_value(self) -> float:
        return round(self._tracker.avoided_kw_this_month, 3)

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "hypothetische_piek_kw": self._tracker.hypothetical_monthly_peak_kw,
            "toelichting": (
                "Hoeveel kW de maandpiek lager is door Peak Guard. "
                "Klik op de grafiek-knop voor de geschiedenis per maand."
            ),
        }


class OverviewPeakSavingsEuroMonthlySensor(_OverviewBase):
    """
    Besparing capaciteitstarief deze maand (EUR).
    TOTAL + LTS => HA toont maand-over-maand grafiek in more-info.
    """

    _attr_icon         = "mdi:transmission-tower-export"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class  = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = "EUR"

    def __init__(self, shared: SharedCapacityState,
                 tracker: PeakAvoidTracker) -> None:
        super().__init__(shared)
        self._tracker = tracker
        self._attr_unique_id = f"{DOMAIN}_overview_peak_savings_euro_monthly"
        self._attr_name      = "Piekbeperking — besparing (maand)"

    @property
    def native_value(self) -> float:
        return round(self._tracker.savings_euro_this_month, 2)

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "tarief_eur_kw_jaar": self._shared.tarief,
            "excl_btw": True,
        }


# ------------------------------------------------------------------ #
#  6+7. Solar-modus maandstatistieken (voor LTS-grafieken)           #
# ------------------------------------------------------------------ #

class OverviewSolarShiftedKwhMonthlySensor(_OverviewBase):
    """
    Verschoven energie via injectiepreventie deze maand (kWh).
    TOTAL_INCREASING + LTS => HA toont automatisch statistieken.
    """

    _attr_icon         = "mdi:solar-power-variant"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class  = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = "kWh"

    def __init__(self, shared: SharedCapacityState,
                 tracker: SolarShiftTracker) -> None:
        super().__init__(shared)
        self._tracker = tracker
        self._attr_unique_id = f"{DOMAIN}_overview_solar_shifted_kwh_monthly"
        self._attr_name      = "Injectiepreventie — verschoven kWh (maand)"

    @property
    def native_value(self) -> float:
        return round(self._tracker.shifted_kwh_this_month, 3)


class OverviewSolarSavingsEuroMonthlySensor(_OverviewBase):
    """
    Besparing injectiepreventie deze maand (EUR).
    TOTAL + LTS => HA toont maand-over-maand grafiek in more-info.
    """

    _attr_icon         = "mdi:solar-panel"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class  = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = "EUR"

    def __init__(self, shared: SharedCapacityState,
                 tracker: SolarShiftTracker) -> None:
        super().__init__(shared)
        self._tracker = tracker
        self._attr_unique_id = f"{DOMAIN}_overview_solar_savings_euro_monthly"
        self._attr_name      = "Injectiepreventie — besparing (maand)"

    @property
    def native_value(self) -> float:
        return round(self._tracker.savings_euro_this_month, 2)

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "netto_eur_per_kwh": self._tracker._netto_eur_per_kwh,
            "excl_btw": True,
        }


# ------------------------------------------------------------------ #
#  8. Recente events sensor (tabel in attributen)                    #
# ------------------------------------------------------------------ #

class OverviewRecentEventsSensor(_OverviewBase):
    """
    Toont de laatste 10 events van beide modi als geformatteerde
    tekst in de state-attributen.

    State: totaal aantal events deze maand (int).

    Attributen:
      - piek_events_tabel:  tekst-tabel met laatste 10 piek-events
      - solar_events_tabel: tekst-tabel met laatste 10 solar-events
      - gecombineerd_tabel: alle events gesorteerd op tijdstip

    Wanneer de gebruiker op deze sensor klikt in de HA-UI, ziet hij
    de volledige tabel in het attribuut-paneel van de more-info dialog.
    """

    _attr_icon              = "mdi:history"
    _attr_state_class       = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "events"

    def __init__(self, shared: SharedCapacityState,
                 peak_tracker: PeakAvoidTracker,
                 solar_tracker: SolarShiftTracker) -> None:
        super().__init__(shared)
        self._peak  = peak_tracker
        self._solar = solar_tracker
        self._attr_unique_id = f"{DOMAIN}_overview_recent_events"
        self._attr_name      = "Recente gebeurtenissen"

    @property
    def native_value(self) -> int:
        return len(self._peak.events) + len(self._solar.events)

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "piek_events_tabel":       self._build_peak_table(),
            "solar_events_tabel":      self._build_solar_table(),
            "gecombineerd_tabel":      self._build_combined_table(),
            "piek_events_count":       len(self._peak.events),
            "solar_events_count":      len(self._solar.events),
        }

    # ---- tabel-helpers ------------------------------------------- #

    @staticmethod
    def _fmt_ts(iso: str) -> str:
        """ISO-timestamp naar leesbare notatie: '21 mrt 14:03'."""
        try:
            dt = datetime.fromisoformat(iso)
            maanden = ["jan","feb","mrt","apr","mei","jun",
                       "jul","aug","sep","okt","nov","dec"]
            return f"{dt.day} {maanden[dt.month - 1]} {dt.strftime('%H:%M')}"
        except (ValueError, IndexError):
            return iso[:16].replace("T", " ")

    def _build_peak_table(self) -> str:
        events = list(self._peak.events)[-100:]
        if not events:
            return "Nog geen piek-events deze maand."
        header = (
            "Datum         | Apparaat              | Duur  | Vermeden | Besparing\n"
            "------------- | --------------------- | ----- | -------- | ---------"
        )
        rows = "\n".join(
            f"{self._fmt_ts(e.avoid_ts.isoformat()):<13} | "
            f"{e.device_name[:21]:<21} | "
            f"{e.measured_duration_min:>4.1f}m | "
            f"{e.avoided_peak_kw:>6.3f} kW | "
            f"EUR {e.savings_euro:.2f}"
            for e in reversed(events)
        )
        return f"{header}\n{rows}"

    def _build_solar_table(self) -> str:
        events = list(self._solar.events)[-100:]
        if not events:
            return "Nog geen solar-events deze maand."
        header = (
            "Datum         | Apparaat              | Duur  | Verschoven | Besparing\n"
            "------------- | --------------------- | ----- | ---------- | ---------"
        )
        rows = "\n".join(
            f"{self._fmt_ts(e.turnon_ts.isoformat()):<13} | "
            f"{e.device_name[:21]:<21} | "
            f"{e.measured_duration_min:>4.1f}m | "
            f"{e.shifted_kwh:>7.3f} kWh | "
            f"EUR {e.savings_euro:.2f}"
            for e in reversed(events)
        )
        return f"{header}\n{rows}"

    def _build_combined_table(self) -> str:
        """Alle events van beide modi, gesorteerd op tijdstip (nieuwste eerst)."""
        combined = []
        for e in self._peak.events:
            combined.append({
                "ts":        e.avoid_ts,
                "modus":     "Piek",
                "apparaat":  e.device_name,
                "duur":      e.measured_duration_min,
                "waarde":    f"{e.avoided_peak_kw:.3f} kW vermeden",
                "besparing": e.savings_euro,
            })
        for e in self._solar.events:
            combined.append({
                "ts":        e.turnon_ts,
                "modus":     "Solar",
                "apparaat":  e.device_name,
                "duur":      e.measured_duration_min,
                "waarde":    f"{e.shifted_kwh:.3f} kWh verschoven",
                "besparing": e.savings_euro,
            })
        combined.sort(key=lambda x: x["ts"], reverse=True)
        recent = combined[:100]
        if not recent:
            return "Nog geen events deze maand."
        header = (
            "Datum         | Modus | Apparaat              | Duur  | Resultaat          | Besparing\n"
            "------------- | ----- | --------------------- | ----- | ------------------ | ---------"
        )
        rows = "\n".join(
            f"{self._fmt_ts(r['ts'].isoformat()):<13} | "
            f"{r['modus']:<5} | "
            f"{r['apparaat'][:21]:<21} | "
            f"{r['duur']:>4.1f}m | "
            f"{r['waarde']:<18} | "
            f"EUR {r['besparing']:.2f}"
            for r in recent
        )
        return f"{header}\n{rows}"
