# ⚡ Peak Guard

**Peak Guard** is een Home Assistant integratie die automatisch je maandelijkse elektriciteitspiek bewaakt en stroominjectie beheert door apparaten slim te schakelen.

Belgische energiecontracten met capaciteitstarief rekenen af op je **hoogste kwartiergemiddelde** van de maand. Peak Guard houdt dit bij, grijpt in wanneer nodig, en meet achteraf exact hoeveel je hebt bespaard.

---

## Wat doet Peak Guard?

**Modus 1 — Piekbeperking**
Apparaten worden tijdelijk uitgeschakeld als je maandpiek dreigt overschreden te worden. Zodra het verbruik daalt, schakelt Peak Guard ze automatisch terug in. Na elke cyclus berekent Peak Guard exact hoeveel kW piek werd vermeden en wat de besparing op je capaciteitstarief is.

**Modus 2 — Injectiepreventie**
Bij overtollige zonne-energie schakelt Peak Guard verbruikers in (bijv. boiler, laadpaal) zodat je de stroom lokaal verbruikt in plaats van terug te leveren aan het net. Na elke sessie berekent Peak Guard de verschoven kWh en de financiële besparing.

---

## Vereisten

- Home Assistant 2024.1.0 of nieuwer
- Digitale meter met sensoren voor:
  - **Huidig verbruik** (W) — positief bij afname, negatief bij injectie
  - **Maandelijkse piek** (W)
  - **Cumulatieve energie** (kWh, stijgend totaal)

---

## Installatie via HACS (aanbevolen)

1. Zorg dat [HACS](https://hacs.xyz) is geïnstalleerd
2. Ga naar **HACS → Integraties → ⋮ → Aangepaste opslagplaatsen**
3. Voeg toe: `https://github.com/nonderbe/peak_guard` — type **Integratie**
4. Zoek **Peak Guard** → **Downloaden**
5. Herstart Home Assistant
6. Ga naar **Instellingen → Apparaten & Diensten → + Integratie toevoegen → Peak Guard**

## Handmatige installatie

1. Download de [nieuwste release](https://github.com/nonderbe/peak_guard/releases/latest)
2. Kopieer de map `custom_components/peak_guard/` naar je HA-configuratiemap
3. Herstart Home Assistant
4. Ga naar **Instellingen → Apparaten & Diensten → + Integratie toevoegen → Peak Guard**

---

## Configuratie

Tijdens de installatie stel je in:

| Instelling | Beschrijving | Standaard |
|---|---|---|
| Sensor huidig verbruik | Vermogenssensor (W), pos. = afname | — |
| Sensor maandelijkse piek | Maandpiek-sensor (W) | — |
| Energiesensor | Cumulatieve kWh-teller (stijgend) | — |
| Fluvius-netgebied | Jouw distributieregio voor tarief 2026 | Antwerpen |
| Buffer (W) | Marge boven de piek vóór ingreep | 100 W |
| Controle-interval (s) | Hoe vaak Peak Guard controleert | 5 s |
| Tolerantie vermogensdetectie (%) | Afwijking voor 'stop'-detectie | 10% |
| Netto besparing per kWh verschoven | Afnameprijs − injectievergoeding | € 0,25 |

---

## Gebruik — cascades instellen

Na installatie verschijnt **Peak Guard** in de zijbalk. Stel hier twee cascades in:

### Cascade Piekbeperking
Apparaten die worden **uitgeschakeld** als de piek dreigt overschreden te worden. Volgorde = prioriteit (1 = eerste ingreep).

### Cascade Injectiepreventie
Apparaten die worden **ingeschakeld** bij overtollige zonne-energie.

| Actietype | Gebruik voor | Herstelt naar |
|---|---|---|
| Uitschakelen | Schakelaar die mag worden uitgedaan | Originele staat (aan) |
| Inschakelen | Schakelaar die extra verbruik opneemt | Originele staat (uit) |
| Vermogen verminderen | Laadpaal, boiler met `number` entity | Origineel vermogen |

---

## Visueel overzicht — Dashboard card toevoegen

Peak Guard bevat een ingebouwde knop die je in 4 klikken naar een kant-en-klare dashboard card leidt. **Geen YAML-kennis vereist.**

### Stap 1 — Activeer de instructie-knop

Ga naar **Instellingen → Apparaten & Diensten → Peak Guard → apparaat "Peak Guard Capaciteitstarief"**.

Klik op de knop **"Toon dashboard-instructies"**.

Er verschijnt een melding rechtsonder (of via het bel-icoon) met de volledige card-YAML.

### Stap 2 — Kopieer de YAML

Open de melding en kopieer de volledige YAML-inhoud (gebruik de kopieer-knop of selecteer alles).

> Je kunt de YAML ook ophalen via **Ontwikkelaarstools → Services → `peak_guard.get_dashboard_yaml` → Aanroepen**.

### Stap 3 — Voeg toe aan je dashboard

1. Ga naar je gewenste dashboard (bijv. **Overzicht**)
2. Klik rechtsboven op het **potlood-icoon** (Bewerken)
3. Klik rechtsonder op **+ Kaart toevoegen**
4. Scroll helemaal naar beneden en klik op **Handmatig** (Manual)
5. Verwijder de vooraf ingevulde tekst, plak de gekopieerde YAML
6. Klik **Opslaan**

De card is direct zichtbaar en toont automatisch de data van de huidige maand.

### Optioneel: rijkere grafieken met apexcharts-card

De standaard card gebruikt alleen ingebouwde HA-componenten (`statistics-graph`, `glance`, `markdown`). Voor interactieve bar-grafieken en scatter-tijdlijnen kun je optioneel **apexcharts-card** installeren:

1. Ga naar **HACS → Frontend**
2. Zoek **apexcharts-card** → Installeren
3. Herstart of herlaad de pagina

Na installatie kun je de uitgebreide versie van de card gebruiken — zie `lovelace_examples.yaml` in de repository voor de volledige 5-views variant met apexcharts.

### Wat toont de card?

De card bevat 6 onderdelen, allemaal zonder HACS:

| Onderdeel | Type | Beschrijving |
|---|---|---|
| Maand-overzicht | Markdown-tabel | Piekbeperking en injectie naast elkaar, totaal per maand en jaar |
| Capaciteit metrics | Glance card | Maandpiek werkelijk, hypothetisch, aangerekend, kost |
| Piek per maand | Statistics-graph | Vermeden kW + besparing EUR per maand (laatste 12 maanden) |
| Solar per maand | Statistics-graph | Verschoven kWh + besparing EUR per maand (laatste 12 maanden) |
| Piek-events tabel | Markdown + Jinja | Laatste 8 piek-events met tijdstip, duur, vermeden kW, besparing |
| Solar-events tabel | Markdown + Jinja | Laatste 8 solar-events met tijdstip, duur, verschoven kWh, besparing |

---

## Sensoren

Peak Guard registreert de volgende sensoren (alle onder het `peak_guard`-domein):

### Capaciteitstarief

| Sensor | Beschrijving |
|---|---|
| `sensor.peak_guard_quarter_peak_kw` | Lopend kwartiergemiddelde (kW) |
| `sensor.peak_guard_monthly_peak_kw` | Hoogste kwartier deze maand (kW) |
| `sensor.peak_guard_billed_peak_kw` | Aangerekende piek door Fluvius (kW) |
| `sensor.peak_guard_monthly_capacity_cost_euro` | Geschatte maandkost capaciteitstarief (EUR) |
| `sensor.peak_guard_rolling_12_month_avg_kw` | Voortschrijdend 12-maands gemiddelde (kW) |

### Piekbeperking

| Sensor | Beschrijving |
|---|---|
| `sensor.peak_guard_peak_avoided_kw_this_month` | Vermeden piekbijdrage deze maand (kW) |
| `sensor.peak_guard_peak_savings_euro_this_month` | Besparing capaciteitstarief deze maand (EUR) |
| `sensor.peak_guard_peak_savings_euro_this_year` | Cumulatieve besparing dit jaar (EUR, persistent) |
| `sensor.peak_guard_hypothetical_monthly_peak_kw` | Maandpiek zonder Peak Guard (kW) |
| `sensor.peak_guard_peak_avoided_events` | Log van laatste 50 piek-events (attribuut) |

### Injectiepreventie

| Sensor | Beschrijving |
|---|---|
| `sensor.peak_guard_solar_verschoven_kwh_this_month` | Verschoven energie deze maand (kWh) |
| `sensor.peak_guard_solar_savings_euro_this_month` | Besparing injectiepreventie deze maand (EUR) |
| `sensor.peak_guard_solar_savings_euro_this_year` | Cumulatieve besparing dit jaar (EUR, persistent) |
| `sensor.peak_guard_solar_avoided_events` | Log van laatste 50 solar-events (attribuut) |

---

## Services

| Service | Beschrijving |
|---|---|
| `peak_guard.get_dashboard_yaml` | Stuurt de card-YAML als notificatie. Gebruik via Ontwikkelaarstools of de button-entity. |

---

## Bestandsstructuur

```
custom_components/peak_guard/
├── __init__.py              # Setup, REST API, service-registratie
├── button.py                # Button entity: toon dashboard-instructies
├── config_flow.py           # Configuratiewizard
├── const.py                 # Constanten en standaardwaarden
├── controller.py            # Cascade-logica en monitoring
├── avoided_peak_tracker.py  # PeakAvoidTracker + SolarShiftTracker
├── sensor.py                # Alle 16 sensor-entiteiten
├── dashboard_yaml.py        # Compacte card-YAML als Python-constante
├── services.yaml            # Service-definitie voor get_dashboard_yaml
├── quarter_calculator.py    # 15-min kwartierpiek berekening
├── quarter_store.py         # Persistente opslag kwartierpiek-data
├── manifest.json
├── strings.json
└── frontend/
    └── peak_guard_panel.js  # Cascade-beheer UI (zijbalk)
```

---

## Licentie

MIT License — vrij te gebruiken en aan te passen.