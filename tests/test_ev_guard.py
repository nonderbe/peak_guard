"""
Peak Guard — tests/test_ev_guard.py

26 scenario-tests voor de EV-lader state machine, rate-limiting en debounce.
Alle tests werken zonder echte Home Assistant-installatie via stub-modules
die in conftest.py geladen worden.

Categorieën:
  1. EVRateLimiter          (3 tests)
  2. _surplus_floor         (4 tests)
  3. Kabel/status/locatie   (5 tests)
  4. apply_action — piek    (4 tests)
  5. apply_action — solar   (7 tests)
  6. throttle_down_solar    (3 tests)
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.peak_guard.models import (
    CascadeDevice,
    EVDeviceGuard,
    EVRateLimiter,
    EVState,
    EV_DEBOUNCE_STABLE_S,
    EV_MIN_OFF_DURATION_S,
    EV_MIN_UPDATE_INTERVAL_S,
    EV_HYSTERESIS_AMPS,
    EV_VOLTS_1PHASE,
)
from custom_components.peak_guard.deciders.ev_guard import EVGuard
from tests.conftest import (
    HomeAssistantError,
    MockHass,
    MockPeakTracker,
    MockSolarTracker,
    make_surplus_history,
)


# ═══════════════════════════════════════════════════════════════════════════ #
#  1. EVRateLimiter                                                           #
# ═══════════════════════════════════════════════════════════════════════════ #

class TestEVRateLimiter:
    def test_allows_when_under_limit(self):
        rl = EVRateLimiter(max_calls=3, window_s=60)
        for _ in range(3):
            assert rl.is_allowed()
            rl.record()
        assert not rl.is_allowed()

    def test_blocks_at_limit(self):
        rl = EVRateLimiter(max_calls=2, window_s=600)
        rl.record()
        rl.record()
        assert rl.calls_in_window == 2
        assert not rl.is_allowed()

    def test_old_calls_expire_from_window(self):
        rl = EVRateLimiter(max_calls=1, window_s=60)
        old_ts = datetime.now(timezone.utc) - timedelta(seconds=61)
        rl.record(now=old_ts)
        # Oude call zit buiten het venster → limiet is vrij
        assert rl.is_allowed()
        assert rl.calls_in_window == 0


# ═══════════════════════════════════════════════════════════════════════════ #
#  2. _surplus_floor                                                          #
# ═══════════════════════════════════════════════════════════════════════════ #

class TestSurplusFloor:
    def setup_method(self):
        self.hass = MockHass()
        self.guard_obj = EVGuard(hass=self.hass, config={}, iteration_actions=[])
        self.dg = EVDeviceGuard()

    def test_single_sample_not_ready(self):
        now = datetime.now(timezone.utc)
        ready, floor = self.guard_obj._surplus_floor(self.dg, 5000.0, now)
        assert not ready
        assert floor == 0.0

    def test_short_span_not_ready(self):
        """Tijdspanne < EV_DEBOUNCE_STABLE_S → nog niet stabiel."""
        t0 = datetime.now(timezone.utc) - timedelta(seconds=5)
        self.guard_obj._surplus_floor(self.dg, 4000.0, t0)
        now = datetime.now(timezone.utc)
        ready, floor = self.guard_obj._surplus_floor(self.dg, 4500.0, now)
        assert not ready, "Tijdspanne < 20 s moet nog niet stabiel zijn"

    def test_ready_after_sufficient_span(self):
        """Na tijdspanne ≥ EV_DEBOUNCE_STABLE_S met alle positieve waarden → klaar."""
        t0 = datetime.now(timezone.utc) - timedelta(seconds=25)
        values = [4000.0, 4200.0, 4400.0, 4600.0, 4800.0]
        for i, v in enumerate(values):
            ts = t0 + timedelta(seconds=i * 5)
            self.guard_obj._surplus_floor(self.dg, v, ts)
        now = t0 + timedelta(seconds=25)
        ready, floor = self.guard_obj._surplus_floor(self.dg, 5000.0, now)
        assert ready
        assert floor > 0
        # 10e-percentiel van gesorteerde waarden (incl. toegevoegde 5000W)
        all_vals = sorted(values + [5000.0])
        expected_idx = max(0, int(len(all_vals) * 10 / 100))
        assert floor == pytest.approx(all_vals[expected_idx])

    def test_negative_value_in_history_blocks_start(self):
        """Als surplus ooit ≤ 0 W was, mag EV nog niet starten."""
        t0 = datetime.now(timezone.utc) - timedelta(seconds=25)
        for i in range(5):
            ts = t0 + timedelta(seconds=i * 5)
            surplus = -200.0 if i == 2 else 4000.0   # één negatief sample
            self.guard_obj._surplus_floor(self.dg, surplus, ts)
        now = t0 + timedelta(seconds=25)
        ready, floor = self.guard_obj._surplus_floor(self.dg, 4000.0, now)
        assert not ready, "Negatief sample in history moet start blokkeren"


# ═══════════════════════════════════════════════════════════════════════════ #
#  3. Kabel / verbindingsstatus / locatie                                     #
# ═══════════════════════════════════════════════════════════════════════════ #

class TestHelpers:
    def setup_method(self):
        self.hass = MockHass()
        self.guard_obj = EVGuard(hass=self.hass, config={}, iteration_actions=[])

    def _device(self, **kwargs) -> CascadeDevice:
        return CascadeDevice(
            id="ev1", name="Tesla", entity_id="switch.ev",
            priority=1, action_type="ev_charger", **kwargs
        )

    def test_cable_connected_no_entity_assumes_connected(self):
        """Zonder geconfigureerde kabelentity → aannemen dat kabel aangesloten is."""
        dev = self._device(ev_cable_entity=None)
        assert self.guard_obj.cable_connected(dev) is True

    def test_cable_connected_truthy_tesla_states(self):
        """Tesla-specifieke kabelstates (charging, stopped, nopower, …) → verbonden."""
        truthy = ["connected", "charging", "stopped", "nopower", "starting",
                  "complete", "fully_charged", "pending", "on", "1"]
        dev = self._device(ev_cable_entity="sensor.cable")
        for state in truthy:
            self.hass.states.set("sensor.cable", state)
            assert self.guard_obj.cable_connected(dev), f"State '{state}' zou verbonden moeten zijn"

    def test_cable_not_connected_for_false_states(self):
        """Niet-truthy states → kabel niet aangesloten."""
        false_states = ["disconnected", "not_connected", "off", "0", "false"]
        dev = self._device(ev_cable_entity="sensor.cable")
        for state in false_states:
            self.hass.states.set("sensor.cable", state)
            assert not self.guard_obj.cable_connected(dev), f"State '{state}' zou niet verbonden moeten zijn"

    def test_is_connected_unavailable_with_wake_button_means_sleeping(self):
        """Status 'unavailable' + wake_button geconfigureerd → auto slaapt (False)."""
        dev = self._device(
            ev_status_sensor="binary_sensor.tesla_status",
            ev_wake_button="button.tesla_wake",
        )
        self.hass.states.set("binary_sensor.tesla_status", "unavailable")
        assert self.guard_obj.is_connected(dev) is False

    def test_is_home_returns_false_for_not_home(self):
        """device_tracker-state 'not_home' → EV is niet thuis."""
        dev = self._device(ev_location_tracker="device_tracker.tesla")
        self.hass.states.set("device_tracker.tesla", "not_home")
        assert self.guard_obj.is_home(dev) is False


# ═══════════════════════════════════════════════════════════════════════════ #
#  4. apply_action — piekbeperking                                             #
# ═══════════════════════════════════════════════════════════════════════════ #

class TestApplyActionPeak:
    @pytest.fixture(autouse=True)
    def setup(self, hass, ev_device, peak_tracker, solar_tracker):
        self.hass = hass
        self.device = ev_device
        self.pt = peak_tracker
        self.st = solar_tracker
        self.ev_guard = EVGuard(hass=hass, config={}, iteration_actions=[])

    async def _apply_peak(self, excess: float, snapshots: dict | None = None):
        return await self.ev_guard.apply_action(
            self.device, excess, snapshots or {}, "peak", self.pt, self.st
        )

    async def test_ev_off_is_skipped(self):
        """EV staat al uit → geen service-aanroep, surplus ongewijzigd."""
        self.hass.states.set("switch.tesla_charge", "off")
        result = await self._apply_peak(2000.0)
        assert result == 2000.0
        assert not self.hass.services.calls

    async def test_ev_turned_off_when_reduction_exceeds_minimum(self):
        """
        16 A @ 230 V = 3 680 W, excess 5 000 W → new_a = floor(0) = 0 < min 6 A
        → EV uitschakelen.
        """
        self.hass.states.set("switch.tesla_charge", "on")
        self.hass.states.set("number.tesla_charge_current", "16")
        await self._apply_peak(5000.0)
        assert self.hass.services.has_call("turn_off"), "EV moet uitgeschakeld worden"
        assert len(self.pt.pending_avoids) == 1, "Piek-event moet geregistreerd worden"

    async def test_ev_current_reduced_not_turned_off(self):
        """
        16 A @ 230 V = 3 680 W, excess 1 000 W
        → nieuwe stroom = floor((3680 - 1000) / 230) = floor(11.65) = 11 A
        → niet uitschakelen, set_value aanroepen.
        """
        self.hass.states.set("switch.tesla_charge", "on")
        self.hass.states.set("number.tesla_charge_current", "16")
        result = await self._apply_peak(1000.0)
        set_calls = self.hass.services.calls_for("set_value")
        assert set_calls, "Laadstroom moet verlaagd worden via set_value"
        assert set_calls[0]["data"]["value"] == 11.0
        assert not self.hass.services.has_call("turn_off")

    async def test_hysteresis_skips_small_current_change(self):
        """
        Δ < 1 A → set_value OVERGESLAGEN wegens hysteresis.
        16 A @ 230 V, excess 100 W → new_a = floor((3680 - 100) / 230) = floor(15.56) = 15 A
        last_sent_amps = 15.6 → |15 - 15.6| = 0.6 < 1 A hysteresis
        """
        self.hass.states.set("switch.tesla_charge", "on")
        self.hass.states.set("number.tesla_charge_current", "16")
        guard = self.ev_guard.get_guard(self.device.id)
        guard.last_sent_amps = 15.6  # dicht bij nieuwe waarde van 15 A
        await self._apply_peak(100.0)
        assert not self.hass.services.has_call("set_value"), (
            "set_value mag niet worden aangeroepen bij delta < 1 A"
        )


# ═══════════════════════════════════════════════════════════════════════════ #
#  5. apply_action — injectiepreventie (solar)                                #
# ═══════════════════════════════════════════════════════════════════════════ #

class TestApplyActionSolar:
    @pytest.fixture(autouse=True)
    def setup(self, hass, ev_device, peak_tracker, solar_tracker):
        self.hass = hass
        self.device = ev_device
        self.pt = peak_tracker
        self.st = solar_tracker
        self.ev_guard = EVGuard(hass=hass, config={}, iteration_actions=[])

    async def _apply_solar(self, excess: float, snapshots: dict | None = None):
        return await self.ev_guard.apply_action(
            self.device, excess, snapshots or {}, "solar", self.pt, self.st
        )

    async def test_surplus_below_threshold_ev_off_no_action(self):
        """Surplus < start-drempel (230 W) én EV uit → geen service-aanroep."""
        self.hass.states.set("switch.tesla_charge", "off")
        result = await self._apply_solar(100.0)
        assert result == 100.0
        assert not self.hass.services.calls

    async def test_cable_disconnected_mid_charge_turns_off_ev(self):
        """
        Kabel ontkoppeld terwijl EV aan het laden was
        → EV uitschakelen, staat = CABLE_DISCONNECTED.
        """
        self.device.ev_cable_entity = "sensor.cable"
        self.hass.states.set("switch.tesla_charge", "on")
        self.hass.states.set("sensor.cable", "disconnected")
        await self._apply_solar(5000.0)
        guard = self.ev_guard.get_guard(self.device.id)
        assert guard.state == EVState.CABLE_DISCONNECTED
        assert self.hass.services.has_call("turn_off"), "EV moet uitgeschakeld worden na kabelontkoppeling"

    async def test_debounce_builds_waiting_for_stable_state(self):
        """
        Eerste aanroep met voldoende surplus → surplus-geschiedenis opbouwen,
        staat = WAITING_FOR_STABLE, EV NIET starten.
        Wallclock-debounce: debounce_remaining_s en debounce_start_at worden gezet.
        """
        self.hass.states.set("switch.tesla_charge", "off")
        result = await self._apply_solar(5000.0)
        guard = self.ev_guard.get_guard(self.device.id)
        assert result == 5000.0, "Surplus mag niet verbruikt worden tijdens debounce"
        assert guard.state == EVState.WAITING_FOR_STABLE
        assert guard.debounce_start_at is not None, "Wallclock-timer moet gestart zijn"
        assert guard.debounce_remaining_s > 0, "Resterende debounce-tijd moet zichtbaar zijn"
        assert not self.hass.services.calls

    @patch("custom_components.peak_guard.deciders.ev_guard.asyncio.sleep", new_callable=AsyncMock)
    async def test_ev_starts_after_surplus_history_complete(self, _mock_sleep):
        """
        Wallclock-timer 25 s geleden gestart, history gevuld met positieve waarden
        → EV starten via switch.turn_on + number.set_value.
        De timer (debounce_start_at) moet expliciet gezet worden; make_surplus_history
        vult alleen de ringbuffer maar start de wallclock-timer niet.
        """
        self.hass.states.set("switch.tesla_charge", "off")
        guard = self.ev_guard.get_guard(self.device.id)
        guard.debounce_start_at = datetime.now(timezone.utc) - timedelta(seconds=25)
        make_surplus_history(guard, seconds_span=25.0, value_w=5000.0)
        await self._apply_solar(5000.0)
        assert self.hass.services.has_call("turn_on"), "EV moet gestart worden"
        assert self.hass.services.has_call("set_value"), "Laadstroom moet worden ingesteld"
        assert guard.state == EVState.CHARGING
        assert len(self.st.started) == 1, "Solar meting moet geregistreerd worden"

    async def test_min_off_cooldown_blocks_turn_on(self):
        """
        EV 10 s geleden door PG uitgeschakeld (min OFF = 300 s)
        → aanzetten geblokkeerd.
        """
        self.hass.states.set("switch.tesla_charge", "off")
        guard = self.ev_guard.get_guard(self.device.id)
        guard.turned_off_at = datetime.now(timezone.utc) - timedelta(seconds=10)
        guard.turned_off_by_pg = True
        make_surplus_history(guard, seconds_span=25.0, value_w=5000.0)
        result = await self._apply_solar(5000.0)
        assert result == 5000.0
        assert not self.hass.services.has_call("turn_on"), (
            "EV mag niet starten binnen min-OFF cooldown"
        )

    async def test_current_adjusted_when_ev_already_charging(self):
        """
        EV laadt al op 16 A; surplus neemt toe met 2 300 W (= 10 A)
        → laadstroom verhogen naar ceil((16 × 230 + 2300) / 230) = ceil(26) = 26 A.
        """
        self.hass.states.set("switch.tesla_charge", "on")
        self.hass.states.set("number.tesla_charge_current", "16")
        guard = self.ev_guard.get_guard(self.device.id)
        guard.state = EVState.CHARGING
        guard.last_switch_state = True
        guard.last_sent_amps = 16.0
        guard.last_current_update = datetime.now(timezone.utc) - timedelta(seconds=30)
        result = await self._apply_solar(2300.0)
        set_calls = self.hass.services.calls_for("set_value")
        assert set_calls, "Laadstroom moet aangepast worden"
        import math
        expected_a = max(6, min(32, math.ceil((16 * 230 + 2300) / 230)))
        assert set_calls[0]["data"]["value"] == float(expected_a)

    async def test_wallclock_timer_independent_of_sample_span(self):
        """
        Wallclock-timer staat al op 25 s geleden, slechts 2 samples in history.
        → debounce klaar want 25 s ≥ EV_DEBOUNCE_STABLE_S (20 s),
          ook al liggen de samples dicht bij elkaar.
        """
        self.hass.states.set("switch.tesla_charge", "off")
        guard = self.ev_guard.get_guard(self.device.id)
        # Timer op 25 s geleden simuleren (zoals bij een 60 s loop-interval)
        guard.debounce_start_at = datetime.now(timezone.utc) - timedelta(seconds=25)
        make_surplus_history(guard, seconds_span=1.0, value_w=5000.0)  # korte span, 6 samples
        result = await self._apply_solar(5000.0)
        assert self.hass.services.has_call("turn_on"), (
            "EV moet starten: wallclock 25 s ≥ debounce 20 s, ook bij korte sample-span"
        )

    async def test_debounce_resets_when_surplus_drops_below_threshold(self):
        """
        Surplus daalt onder start-drempel terwijl debounce bezig was
        → timer en history worden gewist, zodat de 20 s opnieuw telt.
        """
        self.hass.states.set("switch.tesla_charge", "off")
        guard = self.ev_guard.get_guard(self.device.id)
        guard.debounce_start_at = datetime.now(timezone.utc) - timedelta(seconds=10)
        make_surplus_history(guard, seconds_span=10.0, value_w=5000.0)

        await self._apply_solar(100.0)   # < 230 W start-drempel

        assert guard.debounce_start_at is None, "Wallclock-timer moet gereset zijn"
        assert len(guard.surplus_history) == 0, "History moet leeg zijn"
        assert guard.debounce_remaining_s == 0.0

    async def test_skip_reason_shows_floor_preview_in_amps(self):
        """
        Na ≥ 2 samples toont skip_reason al de verwachte floor in ampère,
        ook als de debounce nog loopt. Transparant voor de gebruiker.
        """
        self.hass.states.set("switch.tesla_charge", "off")
        guard = self.ev_guard.get_guard(self.device.id)
        # Timer 5 s geleden → debounce loopt nog
        guard.debounce_start_at = datetime.now(timezone.utc) - timedelta(seconds=5)
        make_surplus_history(guard, seconds_span=5.0, value_w=4600.0)  # ≈ 20 A @ 230 V

        await self._apply_solar(4600.0)

        assert "floor" in guard.skip_reason, (
            f"skip_reason moet floor-preview bevatten: '{guard.skip_reason}'"
        )
        assert " A" in guard.skip_reason, (
            f"Floor moet in ampère worden getoond: '{guard.skip_reason}'"
        )

    async def test_manual_start_sets_charging_state(self):
        """
        EV handmatig aangezet terwijl PG niet de schakelaar had bediend
        → staat = CHARGING, turned_off_by_pg = False.
        """
        self.hass.states.set("switch.tesla_charge", "on")
        self.hass.states.set("number.tesla_charge_current", "16")
        guard = self.ev_guard.get_guard(self.device.id)
        guard.last_switch_state = None   # PG heeft niet ingeschakeld
        guard.state = EVState.IDLE
        await self._apply_solar(5000.0)
        assert guard.state == EVState.CHARGING
        assert guard.turned_off_by_pg is False


# ═══════════════════════════════════════════════════════════════════════════ #
#  6. throttle_down_solar                                                     #
# ═══════════════════════════════════════════════════════════════════════════ #

class TestThrottleDownSolar:
    @pytest.fixture(autouse=True)
    def setup(self, hass, ev_device):
        self.hass = hass
        self.device = ev_device
        self.ev_guard = EVGuard(hass=hass, config={}, iteration_actions=[])

    async def test_reduces_current_for_grid_import(self):
        """
        Netimport 500 W → reductie ceil(500/230) = 3 A → 16 − 3 = 13 A.
        Returns True (EV blijft laden).
        """
        self.hass.states.set("number.tesla_charge_current", "16")
        result = await self.ev_guard.throttle_down_solar(self.device, consumption=500.0)
        assert result is True
        set_calls = self.hass.services.calls_for("set_value")
        assert set_calls
        assert set_calls[0]["data"]["value"] == 13.0

    async def test_at_hw_min_returns_false(self):
        """
        Al op hardware-minimum (6 A) → kan niet verder verlagen → Returns False.
        """
        self.hass.states.set("number.tesla_charge_current", "6")
        result = await self.ev_guard.throttle_down_solar(self.device, consumption=500.0)
        assert result is False
        assert not self.hass.services.has_call("set_value")

    async def test_within_min_update_interval_returns_true_without_call(self):
        """
        Vorige aanpassing < 20 s geleden → update-interval actief
        → returns True (wacht, stop EV niet), geen service-aanroep.
        """
        self.hass.states.set("number.tesla_charge_current", "16")
        guard = self.ev_guard.get_guard(self.device.id)
        guard.last_current_update = datetime.now(timezone.utc) - timedelta(seconds=5)
        result = await self.ev_guard.throttle_down_solar(self.device, consumption=500.0)
        assert result is True
        assert not self.hass.services.has_call("set_value"), (
            "Geen set_value tijdens update-interval"
        )
