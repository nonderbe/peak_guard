DOMAIN = "peak_guard"

# ------------------------------------------------------------------ #
#  Configuratiesleutels                                                #
# ------------------------------------------------------------------ #

CONF_CONSUMPTION_SENSOR = "consumption_sensor"
CONF_PEAK_SENSOR = "peak_sensor"
CONF_BUFFER_WATTS = "buffer_watts"
CONF_UPDATE_INTERVAL = "update_interval"
CONF_ENERGY_SENSOR = "energy_sensor"
CONF_REGIO = "regio"
CONF_POWER_DETECTION_TOLERANCE_PERCENT = "power_detection_tolerance_percent"
CONF_SOLAR_NETTO_EUR_PER_KWH = "netto_besparing_per_kwh_verschoven"
CONF_DEBUG_DECISION_LOGGING = "debug_decision_logging"

# ------------------------------------------------------------------ #
#  Fluvius netgebieden + tarieven 2026 (euro/kW/jaar, excl. BTW)      #
# ------------------------------------------------------------------ #

FLUVIUS_REGIO_TARIEVEN: dict[str, float] = {
    "Antwerpen":         49.4037,
    "Halle-Vilvoorde":   56.0429,
    "Imewo":             54.2010,
    "Kempen":            56.2070,
    "Limburg":           49.0469,
    "Midden-Vlaanderen": 50.1240,
    "West-Vlaanderen":   57.0996,
    "Zenne-Dijle":       56.2070,
}

DEFAULT_REGIO = "Antwerpen"

# ------------------------------------------------------------------ #
#  Regelgeving                                                         #
# ------------------------------------------------------------------ #

CAPACITY_MIN_KW = 2.5
QUARTER_SECONDS = 900
QUARTER_HISTORY_DAYS = 30

# ------------------------------------------------------------------ #
#  Standaardwaarden                                                    #
# ------------------------------------------------------------------ #

DEFAULT_BUFFER_WATTS = 100
DEFAULT_UPDATE_INTERVAL = 5
DEFAULT_POWER_DETECTION_TOLERANCE_PERCENT = 10
DEFAULT_SOLAR_NETTO_EUR_PER_KWH = 0.25

# EV Charger standaardwaarden
DEFAULT_EV_MIN_AMPERE = 6
DEFAULT_EV_MAX_AMPERE = 32
DEFAULT_EV_MAX_SOC = 100   # % - maximaal batterijpercentage bij zonne-overschot

# Standaard entity-id voor de kabeldetectiesensor van de EV-lader.
# De sensor moet "on" / "true" / "connected" zijn als de kabel aangesloten is.
# Laden kan pas starten als deze sensor een truthy-state rapporteert.
DEFAULT_EV_CABLE_ENTITY = "sensor.tesla_opladen"

# EV Solar-cascade drempelwaarden
# Start-drempel: minimale injectie (W) vooraleer de EV-lader mag starten.
# Dit is LOSGEKOPPELD van het hardware-minimum (min_value/min_current_ev).
# 230 W ≈ 1 A @ 230 V — laagste zinvolle drempel voor Tesla hardware.
DEFAULT_EV_SOLAR_START_THRESHOLD_W: float = 230.0

# Stop-drempel: de EV stopt alleen als surplus ná uitschakelen ≤ 0 W zou zijn.
# Hysteresis: voorkomt constant aan/uit schakelen bij borderline surplus.
DEFAULT_EV_SOLAR_STOP_THRESHOLD_W: float = 0.0

# ------------------------------------------------------------------ #
#  Panel / frontend                                                    #
# ------------------------------------------------------------------ #

PANEL_URL = "peak-guard"
PANEL_TITLE = "Peak Guard"
PANEL_ICON = "mdi:flash-alert"
PANEL_JS_URL = "/peak_guard/panel.js"

# ------------------------------------------------------------------ #
#  Storage                                                             #
# ------------------------------------------------------------------ #

STORAGE_KEY = f"{DOMAIN}.cascade"
STORAGE_VERSION = 1

STORAGE_KEY_QUARTERS = f"{DOMAIN}.quarters"
STORAGE_VERSION_QUARTERS = 1

STORAGE_KEY_SAVINGS = f"{DOMAIN}.savings"
STORAGE_VERSION_SAVINGS = 1

STORAGE_KEY_SOLAR_SAVINGS = f"{DOMAIN}.solar_savings"
STORAGE_VERSION_SOLAR_SAVINGS = 1

# ------------------------------------------------------------------ #
#  Cascade actietypes                                                  #
# ------------------------------------------------------------------ #

ACTION_SWITCH_OFF  = "switch_off"
ACTION_SWITCH_ON   = "switch_on"
ACTION_THROTTLE    = "throttle"      # behouden voor backwards-compat met bestaande data
ACTION_EV_CHARGER  = "ev_charger"   # nieuw: elektrisch voertuig

# ------------------------------------------------------------------ #
#  Device-identifiers voor HA device registry                         #
# ------------------------------------------------------------------ #

DEVICE_ID_CAPACITY    = "capaciteit"
DEVICE_ID_SAVINGS     = "besparingen"
DEVICE_ID_OVERVIEW    = "overzicht"
