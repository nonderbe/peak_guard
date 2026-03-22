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

# ------------------------------------------------------------------ #
#  Fluvius netgebieden + tarieven 2026 (€/kW/jaar, excl. BTW)         #
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

ACTION_SWITCH_OFF = "switch_off"
ACTION_SWITCH_ON  = "switch_on"
ACTION_THROTTLE   = "throttle"

# ------------------------------------------------------------------ #
#  Device-identifiers voor HA device registry                         #
# ------------------------------------------------------------------ #

DEVICE_ID_CAPACITY    = "capaciteit"
DEVICE_ID_SAVINGS     = "besparingen"
DEVICE_ID_OVERVIEW    = "overzicht"