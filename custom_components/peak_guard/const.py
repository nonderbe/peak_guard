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
# Kwartierhistoriek: 32 dagen, zodat ook een maand van 31 dagen volledig
# beschikbaar blijft voor de lopende maand.
QUARTER_HISTORY_DAYS = 32
# Aantal maanden waarvoor de maandpiek bewaard blijft (los van de kwartieren).
MONTHLY_PEAK_HISTORY_MONTHS = 36
# Hoogste kwartiergemiddelde dat nog als echte meting aanvaard wordt. Ruim
# boven een zware aansluiting (3×63 A ≈ 43 kW); alles daarboven is een
# meetfout (terugspringende meterstand, verkeerde eenheid) en wordt geweigerd.
MAX_PLAUSIBLE_QUARTER_KW = 100.0
# Controle van de eigen kwartierwaarden tegen de maandpiek van de P1-meter.
# Een afgesloten kwartier van de lopende maand kan niet hoger zijn dan die
# maandpiek; ligt het erboven, dan is het een meetfout en wordt het gewist.
#   grens = meterpiek × PEAK_VERIFY_TOLERANCE + PEAK_VERIFY_MARGIN_KW
# De marge vangt het verschil op tussen de meter (exact kwartiergemiddelde)
# en de eigen schatting uit minuutmetingen. Een kwartier wordt pas beoordeeld
# PEAK_VERIFY_GRACE_MINUTES na zijn einde, zodat de meter het kan melden.
PEAK_VERIFY_TOLERANCE = 1.15
PEAK_VERIFY_MARGIN_KW = 0.25
PEAK_VERIFY_GRACE_MINUTES = 10

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
DEFAULT_EV_CABLE_ENTITY = None

# EV Solar-cascade drempelwaarden
# Start-drempel: minimale injectie (W) vooraleer de EV-lader mag starten.
# Dit is LOSGEKOPPELD van het hardware-minimum (min_value/min_current_ev).
# Doel van de solar-cascade is injectie vermijden: zodra er ÉÉN watt naar het
# net wordt geïnjecteerd moet de EV starten, ook al trekt hij daarna stroom van
# het net (de auto draait op zijn hardware-minimum, bv. 5 A). Daarom is de
# default 0 W. De debounce (EV_DEBOUNCE_STABLE_S) zorgt nog steeds dat een
# kortstondige injectie-piek de EV niet doet thrashen.
DEFAULT_EV_SOLAR_START_THRESHOLD_W: float = 0.0

# Stop-drempel: de EV stopt alleen als surplus ná uitschakelen ≤ 0 W zou zijn.
# Hysteresis: voorkomt constant aan/uit schakelen bij borderline surplus.
DEFAULT_EV_SOLAR_STOP_THRESHOLD_W: float = 0.0

# Dagelijks hard plafond op echte EV-API-calls (bv. Tesla Fleet API), als
# lange-horizon vangnet bovenop EVRateLimiter (12 calls / 10 min). Die 10-min
# sliding window beschermt tegen thrashing binnen één cyclus, maar een
# structureel falend apparaat kan daar dag na dag tegenaan blijven botsen en
# zo het externe quotum van de API-provider opsouperen.
#
# Bewust een DAGELIJKS budget i.p.v. maandelijks: bereken het simpelweg als
# het maandquotum gedeeld door 30. Een storing op één dag put dan hooguit
# die ene dag uit — de volgende dag is het volledige dagbudget weer
# beschikbaar voor normaal gebruik, in plaats van dat de storing de rest
# van de maand blijft doorwerken op een cumulatief maandbudget.
#
# Houdt geen rekening met de achtergrond-polling van de Tesla-integratie
# zelf (die onafhankelijk van Peak Guard hetzelfde accountquotum verbruikt)
# — pas aan op basis van het werkelijke maandquotum en de waargenomen
# achtergrondbelasting.
EV_API_DAILY_BUDGET: int = 10_000 // 30  # = 333

# ------------------------------------------------------------------ #
#  Laadschema (Planning-tab)                                           #
# ------------------------------------------------------------------ #

# Laadstroom tijdens een venster enkel verhogen als de winst minstens zoveel
# ampère is én de vorige aanpassing lang genoeg geleden is (spaart API-calls).
SCHEDULE_INCREASE_MIN_A: float = 2.0
SCHEDULE_INCREASE_INTERVAL_S: float = 300.0
# Een ongeplande lading (buiten venster, zonder overschot) pas na deze tijd
# stoppen, zodat de solar-cascade een inplug tijdens injectie kan overnemen.
SCHEDULE_UNPLANNED_GRACE_S: float = 120.0
# Na een turn_on zo lang wachten op de start vóór een nieuwe poging.
SCHEDULE_START_CONFIRM_S: float = 180.0
# De laadlimiet pas opnieuw sturen als ze na deze tijd nog altijd afwijkt.
SCHEDULE_SOC_RESEND_S: float = 300.0
# Een tariefsensor die unavailable is, behoudt zo lang zijn laatste staat.
SCHEDULE_SENSOR_STALE_S: float = 900.0
# Standaard tariefsensor voor een daltarief-venster (1 = piek, 2 = dal).
DEFAULT_SCHEDULE_TARIFF_SENSOR = "sensor.p1_meter_tarief"
DEFAULT_SCHEDULE_TARIFF_STATE = "2"

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

STORAGE_KEY_DEVICE_SAVINGS = f"{DOMAIN}.monthly_device_savings"
STORAGE_VERSION_DEVICE_SAVINGS = 1

STORAGE_KEY_EV_CALL_BUDGET = f"{DOMAIN}.ev_call_budget"
STORAGE_VERSION_EV_CALL_BUDGET = 1

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
