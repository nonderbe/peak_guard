"""
Testinfrastructuur voor Peak Guard EV-logica.

Strategie:
  Alle homeassistant.*-imports worden vóór enige echte import
  vervangen door stub-modules in sys.modules.  Zo worden de zware
  HA-afhankelijkheden omzeild zonder dat de productie-logica hoeft
  aan te worden aangepast.

  custom_components.peak_guard wordt als stub-pakket geladen (zodat
  __init__.py + controller.py niet uitgevoerd worden). De deciders
  worden los geïmporteerd via hun eigen __path__.

  HomeAssistantError is een echte Exception-subklasse zodat
  try/except-blokken in ev_guard.py correct werken.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from types import ModuleType
from typing import List
from unittest.mock import AsyncMock, MagicMock

import pytest

# ──────────────────────────────────────────────────────────────────────────── #
#  Stap 1: HA-module stubs VÓÓR enige echte import                             #
# ──────────────────────────────────────────────────────────────────────────── #

class HomeAssistantError(Exception):
    """Echte Exception-klasse zodat 'except HomeAssistantError' werkt."""


def _mod(name: str, **attrs) -> ModuleType:
    m = ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


sys.modules.update({
    "homeassistant":                         _mod("homeassistant"),
    "homeassistant.core":                    _mod("homeassistant.core",
                                                  HomeAssistant=object,
                                                  callback=lambda f: f),
    "homeassistant.exceptions":              _mod("homeassistant.exceptions",
                                                  HomeAssistantError=HomeAssistantError),
    "homeassistant.config_entries":          _mod("homeassistant.config_entries",
                                                  ConfigEntry=object),
    "homeassistant.const":                   _mod("homeassistant.const",
                                                  UnitOfPower=MagicMock(),
                                                  UnitOfEnergy=MagicMock()),
    "homeassistant.components":              _mod("homeassistant.components"),
    "homeassistant.components.http":         _mod("homeassistant.components.http",
                                                  HomeAssistantView=object,
                                                  StaticPathConfig=object),
    "homeassistant.components.panel_custom": _mod("homeassistant.components.panel_custom",
                                                  async_register_panel=AsyncMock()),
    "homeassistant.components.frontend":     _mod("homeassistant.components.frontend"),
    "homeassistant.components.sensor":       _mod("homeassistant.components.sensor",
                                                  SensorEntity=object,
                                                  SensorDeviceClass=MagicMock(),
                                                  SensorStateClass=MagicMock()),
    "homeassistant.components.button":       _mod("homeassistant.components.button",
                                                  ButtonEntity=object),
    "homeassistant.components.switch":       _mod("homeassistant.components.switch",
                                                  SwitchEntity=object),
    "homeassistant.components.number":       _mod("homeassistant.components.number",
                                                  NumberEntity=object,
                                                  NumberMode=MagicMock()),
    "homeassistant.helpers":                 _mod("homeassistant.helpers"),
    "homeassistant.helpers.event":           _mod("homeassistant.helpers.event",
                                                  async_track_state_change_event=MagicMock(),
                                                  async_track_time_interval=MagicMock()),
    "homeassistant.helpers.storage":         _mod("homeassistant.helpers.storage",
                                                  Store=MagicMock()),
    "homeassistant.helpers.entity":          _mod("homeassistant.helpers.entity",
                                                  EntityCategory=MagicMock()),
    "homeassistant.helpers.entity_platform": _mod("homeassistant.helpers.entity_platform",
                                                  AddEntitiesCallback=object),
    "homeassistant.helpers.restore_state":   _mod("homeassistant.helpers.restore_state",
                                                  RestoreEntity=object),
    "homeassistant.helpers.selector":        _mod("homeassistant.helpers.selector"),
})

# ──────────────────────────────────────────────────────────────────────────── #
#  Stap 2: custom_components-stub (omzeil __init__.py + controller.py)         #
# ──────────────────────────────────────────────────────────────────────────── #

_ROOT = "/Users/nielsonderbeke/Projects/peak_guard"
_PG   = f"{_ROOT}/custom_components/peak_guard"
_DEC  = f"{_PG}/deciders"

if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_cc = ModuleType("custom_components")
_cc.__path__ = [f"{_ROOT}/custom_components"]
_cc.__package__ = "custom_components"

_pg = ModuleType("custom_components.peak_guard")
_pg.__path__ = [_PG]
_pg.__package__ = "custom_components.peak_guard"

_dec = ModuleType("custom_components.peak_guard.deciders")
_dec.__path__ = [_DEC]
_dec.__package__ = "custom_components.peak_guard.deciders"

sys.modules["custom_components"] = _cc
sys.modules["custom_components.peak_guard"] = _pg
sys.modules["custom_components.peak_guard.deciders"] = _dec

# ──────────────────────────────────────────────────────────────────────────── #
#  Stap 3: importeer de echte productie-modules                                 #
# ──────────────────────────────────────────────────────────────────────────── #

from custom_components.peak_guard.models import (  # noqa: E402
    CascadeDevice,
    DeviceSnapshot,
    EVDeviceGuard,
    EVRateLimiter,
    EVState,
    EV_DEBOUNCE_STABLE_S,
    EV_MIN_OFF_DURATION_S,
    EV_MIN_UPDATE_INTERVAL_S,
    EV_HYSTERESIS_AMPS,
)
from custom_components.peak_guard.deciders.ev_guard import EVGuard  # noqa: E402


# ──────────────────────────────────────────────────────────────────────────── #
#  Mock-klassen                                                                  #
# ──────────────────────────────────────────────────────────────────────────── #

class MockState:
    """Nep-state object zoals hass.states.get() retourneert."""
    def __init__(self, state_str: str) -> None:
        self.state = state_str


class MockStateRegistry:
    def __init__(self) -> None:
        self._states: dict[str, str] = {}

    def set(self, entity_id: str, state_str: str) -> None:
        self._states[entity_id] = state_str

    def get(self, entity_id: str):
        val = self._states.get(entity_id)
        return MockState(val) if val is not None else None


class MockServiceRegistry:
    """
    Registreert service-aanroepen. Kan geconfigureerd worden om
    HomeAssistantError te gooien op specifieke services.
    """
    def __init__(self, raise_on: set[str] | None = None) -> None:
        self.calls: list[dict] = []
        self._raise_on: set[str] = raise_on or set()

    async def async_call(
        self,
        domain: str,
        service: str,
        data: dict,
        blocking: bool = False,
    ) -> None:
        self.calls.append({"domain": domain, "service": service, "data": data})
        if service in self._raise_on:
            raise HomeAssistantError(f"Gesimuleerde fout: {service}")

    def calls_for(self, service: str) -> list[dict]:
        return [c for c in self.calls if c["service"] == service]

    def has_call(self, service: str) -> bool:
        return any(c["service"] == service for c in self.calls)


class MockHass:
    def __init__(self, raise_on: set[str] | None = None) -> None:
        self.states = MockStateRegistry()
        self.services = MockServiceRegistry(raise_on=raise_on)


class MockPeakTracker:
    def __init__(self) -> None:
        self.pending_avoids: list = []
        self.turn_on_measurements: list = []

    def record_pending_avoid(self, device_id, device_name, nominal_kw, ts):
        self.pending_avoids.append({"id": device_id, "kw": nominal_kw})

    def start_measurement_on_turnon(self, device_id, device_name, ts):
        self.turn_on_measurements.append(device_id)


class MockSolarTracker:
    def __init__(self) -> None:
        self.started: list = []
        self.completed: list = []

    def start_solar_measurement(self, device_id, device_name, nominal_kw, ts):
        self.started.append({"id": device_id, "kw": nominal_kw})

    def complete_solar_calculation(self, device_id, now):
        self.completed.append(device_id)
        return None


# ──────────────────────────────────────────────────────────────────────────── #
#  Fixtures                                                                      #
# ──────────────────────────────────────────────────────────────────────────── #

@pytest.fixture
def hass() -> MockHass:
    h = MockHass()
    h.states.set("switch.tesla_charge", "off")
    h.states.set("number.tesla_charge_current", "16")
    return h


@pytest.fixture
def ev_device() -> CascadeDevice:
    """Standaard 1-fase Tesla-configuratie zonder optionele sensors."""
    return CascadeDevice(
        id="ev_tesla",
        name="Tesla",
        entity_id="switch.tesla_charge",
        priority=1,
        action_type="ev_charger",
        ev_switch_entity="switch.tesla_charge",
        ev_current_entity="number.tesla_charge_current",
        ev_phases=1,
        ev_min_current=6.0,
        min_value=6.0,
        max_value=32.0,
        start_threshold_w=230.0,
        ev_max_soc=None,   # geen SOC-override in basistests
        ev_soc_entity=None,
        ev_cable_entity=None,
        ev_wake_button=None,
        ev_status_sensor=None,
        ev_location_tracker=None,
    )


@pytest.fixture
def peak_tracker() -> MockPeakTracker:
    return MockPeakTracker()


@pytest.fixture
def solar_tracker() -> MockSolarTracker:
    return MockSolarTracker()


@pytest.fixture
def ev_guard(hass: MockHass) -> EVGuard:
    return EVGuard(hass=hass, config={}, iteration_actions=[])


def make_surplus_history(guard: EVDeviceGuard, seconds_span: float, value_w: float = 5000.0) -> None:
    """Vul guard.surplus_history met samples die de gegeven tijdspanne beslaan."""
    t0 = datetime.now(timezone.utc) - timedelta(seconds=seconds_span)
    n = 6
    for i in range(n):
        ts = t0 + timedelta(seconds=i * seconds_span / (n - 1))
        guard.surplus_history.append((ts, value_w))
